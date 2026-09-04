#!/usr/bin/env python3
"""查询评测任务进度。

任务状态判定（优先级从高到低）：
1. 评测机任务目录下 exit_code 文件存在 → 已结束（0=finished，非0=failed）
2. 容器内 ais_bench 进程还活着 → running
3. 进程没了且无 exit_code → failed（异常终止，如容器被杀）

进度结构化：从日志尾解析 ais_bench 的 POST/RECV/FAIL/FIN 计数行，
输出 posted/finished/failed/total 与按平均速率估算的剩余时间。

服务崩溃感知：任务 failed 且日志里 FAIL 计数高时，从评测机探测被测模型
API 是否存活——服务挂了会明确提示修复后 --reuse 续跑。

每次探测还会发现 work_dir 下的实际输出时间戳目录并写入任务记录
（评测容器时钟与宿主机有偏差，eval_result/--reuse 都依赖这个精确值）。

用法：
  python eval_status.py <task-id>          # 查单个任务（task-id 支持唯一前缀）
  python eval_status.py --list             # 列出本地注册的全部任务
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

LOG_TAIL_LINES = 40

# ais_bench 推理阶段的实时计数行：POST=6 (0.0/s)  RECV=5 ... FAIL=0 ... FIN=5 ...
_PROGRESS_RE = re.compile(r"POST=(\d+).*?RECV=(\d+).*?FAIL=(\d+).*?FIN=(\d+)")


def parse_progress_stats(log_tail: str, task: dict[str, Any]) -> dict[str, Any] | None:
    """解析日志尾里的请求计数行，给出结构化进度。"""
    matches = _PROGRESS_RE.findall(log_tail)
    if not matches:
        return None
    posted, recv, failed, finished = (int(x) for x in matches[-1])
    stats: dict[str, Any] = {
        "posted": posted, "received": recv, "failed": failed, "finished": finished,
    }
    # 总数：显式 --num-prompts 时精确已知；全量时以已发数为准（发出后不再涨）
    total = task.get("num_prompts") or posted
    stats["total"] = total
    if total and finished <= total:
        stats["percent"] = round(finished / total * 100, 1)
        # 按平均速率估算剩余时间（粗略；长尾请求会使实际偏久）
        started_at = str(task.get("started_at", ""))
        try:
            elapsed = time.time() - time.mktime(time.strptime(started_at[:19], "%Y-%m-%dT%H:%M:%S"))
            if finished > 0 and finished < total and elapsed > 60:
                remaining = elapsed / finished * (total - finished)
                stats["eta_minutes"] = round(remaining / 60, 1)
        except ValueError:
            pass
    return stats


def probe_task(machine: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """在评测机上收集一个任务的现场：exit_code、进程、日志尾、进度文件、输出时间戳。"""
    task_dir = task["task_dir"]
    container = task["container"]
    info: dict[str, Any] = {"exit_code": None, "ais_bench_alive": False,
                            "log_tail": "", "progress_files": {}}

    code, out, _ = common.run_ssh(machine, f"cat {common.sh_quote(task_dir + '/exit_code')} 2>/dev/null")
    if code == 0 and out.strip().lstrip("-").isdigit():
        info["exit_code"] = int(out.strip())

    # bootstrap.pid 记录的是 setsid 出来的 run.sh 进程；容器里直接查 ais_bench 更准。
    code, out, _ = common.run_ssh(
        machine,
        f"docker exec {container} bash -c 'pgrep -af ais_bench 2>/dev/null | head -5' 2>/dev/null",
        timeout=30,
    )
    if code == 0 and "ais_bench" in out:
        info["ais_bench_alive"] = True
        info["ais_bench_proc"] = out.strip().splitlines()[0][:200]

    code, out, _ = common.run_ssh(
        machine,
        f"tail -n {LOG_TAIL_LINES} {common.sh_quote(task_dir + '/aisbench.log')} 2>/dev/null",
        timeout=30,
    )
    if code == 0 and out.strip():
        info["log_tail"] = out.strip()[-4000:]

    # SWE-bench 等长任务的实时进度文件：work_dir/<时间戳>/status_tmp/tmp_*.json
    code, out, _ = common.run_ssh(
        machine,
        f"for f in {common.sh_quote(task['work_dir'])}/*/status_tmp/tmp_*.json; do "
        f"  [ -s \"$f\" ] && echo \"== $f\" && cat \"$f\"; done 2>/dev/null",
        timeout=30,
    )
    if code == 0 and out.strip():
        info["progress_files"] = out.strip()[:3000]

    # 发现实际输出时间戳目录（容器时钟与宿主机有偏差，不能靠本地推算）
    code, out, _ = common.run_ssh(
        machine,
        f"ls -1d {common.sh_quote(task['work_dir'])}/*/ 2>/dev/null | xargs -n1 basename 2>/dev/null",
        timeout=30,
    )
    if code == 0 and out.strip():
        info["output_timestamps"] = sorted(line.strip() for line in out.splitlines() if line.strip())

    return info


def probe_model_api(machine: dict[str, Any], api_base: str) -> bool:
    """从评测机探测被测模型服务是否存活（诊断 failed 用）。"""
    url = api_base.rstrip("/") + "/v1/models" if not api_base.rstrip("/").endswith("/v1") else api_base.rstrip("/") + "/models"
    code, out, _ = common.run_ssh(machine, f"curl -sS -m 8 -o /dev/null -w '%{{http_code}}' {common.sh_quote(url)}", timeout=30)
    return code == 0 and out.strip() in ("200", "401")


def classify(task: dict[str, Any], info: dict[str, Any]) -> str:
    """状态机：exit_code 优先，其次进程存活。"""
    if info.get("exit_code") is not None:
        return "finished" if info["exit_code"] == 0 else "failed"
    if info.get("ais_bench_alive"):
        return "running"
    # 没有 exit_code 也没进程：刚发起时 ais_bench 要几秒才出现在进程表里，
    # 用本地任务表的启动时间兜底，30 秒内仍视为启动中，之后才算异常终止。
    started_at = str(task.get("started_at", ""))
    try:
        started_epoch = time.mktime(time.strptime(started_at[:19], "%Y-%m-%dT%H:%M:%S"))
        if time.time() - started_epoch < 30:
            return "running"
    except ValueError:
        pass
    return "failed"


def main() -> int:
    parser = argparse.ArgumentParser(description="查询 ais_bench 评测任务进度")
    parser.add_argument("task_id", nargs="?", help="任务 ID（支持唯一前缀）")
    parser.add_argument("--list", action="store_true", help="列出全部本地任务")
    args = parser.parse_args()

    tasks = common.load_tasks()
    if args.list or not args.task_id:
        summary = [{k: t.get(k) for k in ("task_id", "suite", "status", "model", "host", "started_at")}
                   for t in reversed(tasks)]
        return common.emit({"ok": True, "action": "status", "status": "ready",
                            "count": len(summary), "tasks": summary})

    task = common.find_task(args.task_id)
    if task is None:
        return common.emit({"ok": False, "action": "status", "status": "needs_input",
                            "error": f"任务 {args.task_id} 不存在或前缀不唯一，用 --list 查看"})

    machine = common.find_host(task.get("host", ""))
    if machine is None:
        return common.emit({"ok": False, "action": "status", "status": "blocked",
                            "error": f"任务所在评测机 {task.get('host')} 不在配置中"})

    common.progress("probe", f"探测任务 {task['task_id']}")
    info = probe_task(machine, task)
    state = classify(task, info)
    progress_stats = parse_progress_stats(info.get("log_tail", ""), task)

    # 把发现的输出时间戳固化进任务记录（eval_result 与 --reuse 的精确锚点）
    timestamps = info.get("output_timestamps") or []
    if timestamps and task.get("output_ts") != timestamps[-1]:
        task["output_ts"] = timestamps[-1]
        task["output_ts_all"] = timestamps

    task["status"] = state
    if progress_stats:
        task["last_progress"] = progress_stats
    task["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    common.upsert_task(task)

    # ---- 服务崩溃感知：failed 且失败请求多时，探测模型服务并给出续跑指引 ----
    hints: list[str] = []
    model_api_alive = None
    if state == "failed":
        failed_count = (progress_stats or {}).get("failed", 0)
        if task.get("api_base"):
            model_api_alive = probe_model_api(machine, task["api_base"])
        if model_api_alive is False:
            hints.append(f"被测模型服务 {task['api_base']} 已不可达——评测失败的根因大概率是服务崩溃。"
                         f"服务恢复后运行 eval_run.py --reuse {task['task_id']} 续跑，已完成的推理不会重做")
        elif failed_count and failed_count >= 5:
            hints.append(f"有 {failed_count} 条请求失败（服务仍可达，可能是偶发超时/限流）。"
                         f"可运行 eval_run.py --reuse {task['task_id']} 断点续跑失败部分")

    return common.emit({
        "ok": True, "action": "status", "status": state,
        "task": {k: task.get(k) for k in ("task_id", "suite", "category", "model", "api_base",
                                          "container", "task_dir", "started_at", "status", "output_ts")},
        "exit_code": info.get("exit_code"),
        "ais_bench_alive": info.get("ais_bench_alive"),
        "progress_stats": progress_stats,
        "log_tail": info.get("log_tail"),
        "progress": info.get("progress_files"),
        "model_api_alive": model_api_alive,
        "hints": hints,
        "next": "评测结束后用 eval_result.py 提取分数" if state == "finished" else None,
    })


if __name__ == "__main__":
    sys.exit(main())
