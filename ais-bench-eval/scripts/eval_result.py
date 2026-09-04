#!/usr/bin/env python3
"""提取评测分数。

从任务 work_dir 下最新时间戳目录（--all 则全部）收集：
- results/<模型>/<数据集>.json    精度/Agent 分数（accuracy、resolved_instances 等）
- performances/<模型>/<数据集>.json 性能指标（吞吐、TTFT、TPOT 等）
- summary/summary_<时间戳>.txt    ais_bench 官方汇总文本

大块字段（details、completed_ids 等列表/字典）在远端就被丢弃，只带回标量指标，
避免几百 MB 的 per-instance 明细把本地结果撑爆。完整明细仍在评测机任务目录里。

用法：
  python eval_result.py <task-id>      # 提取最新一次运行的分数
  python eval_result.py <task-id> --all  # 带上该任务全部历史时间戳
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

# 在评测机上跑的收集脚本：对每个时间戳目录抓 标量指标 + summary 文本。
# 用 python3 而不是 cat/jq，保证 JSON 解析正确且自动过滤大字段。
# 性能 json 的结构是 {"指标名": {"total": 值}}，做一层展开取 total。
# argv[2]：'all' 或指定时间戳目录名（任务记录里 eval_status 固化的 output_ts，
# 评测容器时钟有偏差，靠"最新目录"猜会取错）
REMOTE_COLLECT = r"""
import glob, json, os, sys
work_dir = sys.argv[1]
ts_all = sorted(d for d in os.listdir(work_dir) if os.path.isdir(os.path.join(work_dir, d)))
if sys.argv[2] == 'all':
    targets = ts_all
elif sys.argv[2] in ts_all:
    targets = [sys.argv[2]]
else:
    targets = ts_all[-1:]
result = {}
for ts in targets:
    ts_dir = os.path.join(work_dir, ts)
    entry = {'metrics': {}, 'summary': ''}
    for pattern in ('results/*/*.json', 'performances/*/*.json'):
        for path in glob.glob(os.path.join(ts_dir, pattern)):
            try:
                with open(path) as fh:
                    data = json.load(fh)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            rel = os.path.relpath(path, ts_dir)
            metrics = {}
            for k, v in data.items():
                if isinstance(v, (int, float, str, bool)):
                    metrics[k] = v
                elif isinstance(v, dict) and isinstance(v.get('total'), (int, float, str, bool)):
                    metrics[k] = v['total']
            entry['metrics'][rel] = metrics
    for path in glob.glob(os.path.join(ts_dir, 'summary', 'summary_*.txt')):
        try:
            with open(path) as fh:
                entry['summary'] = fh.read()[:6000]
        except Exception:
            pass
    result[ts] = entry
print(json.dumps(result, ensure_ascii=False))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="提取 ais_bench 评测分数")
    parser.add_argument("task_id", help="任务 ID（支持唯一前缀）")
    parser.add_argument("--all", action="store_true", help="收集全部时间戳（默认只取最新）")
    args = parser.parse_args()

    task = common.find_task(args.task_id)
    if task is None:
        return common.emit({"ok": False, "action": "result", "status": "needs_input",
                            "error": f"任务 {args.task_id} 不存在或前缀不唯一，用 eval_status.py --list 查看"})
    machine = common.find_host(task.get("host", ""))
    if machine is None:
        return common.emit({"ok": False, "action": "result", "status": "blocked",
                            "error": f"任务所在评测机 {task.get('host')} 不在配置中"})

    common.progress("collect", f"收集 {task['work_dir']} 的结果")
    # 目标时间戳：--all 全部；任务记录里固化过 output_ts 就精确取它（容器时钟偏差
    # 会让"最新目录"猜错）；两者都没有才退化为最新目录
    if args.all:
        target = "all"
    elif task.get("output_ts"):
        target = str(task["output_ts"])
    else:
        target = "latest"
    command = (
        f"python3 -c {common.sh_quote(REMOTE_COLLECT)} "
        f"{common.sh_quote(task['work_dir'])} {common.sh_quote(target)}"
    )
    code, out, err = common.run_ssh(machine, command, timeout=120)
    if code != 0:
        return common.emit({"ok": False, "action": "result", "status": "blocked",
                            "error": f"远端收集失败：{(err or out).strip()[:300]}",
                            "hint": "任务可能还没跑出结果（先 eval_status.py 确认状态）"})
    try:
        collected = json.loads(out)
    except json.JSONDecodeError:
        return common.emit({"ok": False, "action": "result", "status": "blocked",
                            "error": f"远端返回无法解析：{out[:200]}"})

    if not collected:
        return common.emit({"ok": False, "action": "result", "status": "needs_input",
                            "error": "work_dir 下没有任何时间戳目录，任务尚未产出结果",
                            "hint": "用 eval_status.py 查看任务是否还在推理阶段"})

    # 汇总：给 agent 一个直接可读的分数视图。
    latest_ts = sorted(collected.keys())[-1]
    latest = collected[latest_ts]
    scores: dict[str, Any] = {}
    for rel, metrics in latest.get("metrics", {}).items():
        kind = "performance" if rel.startswith("performances/") else "accuracy"
        scores[rel] = {"kind": kind, **metrics}

    return common.emit({
        "ok": True, "action": "result", "status": "finished" if scores else "needs_input",
        "task_id": task["task_id"],
        "suite": task.get("suite"),
        "latest_timestamp": latest_ts,
        "scores": scores,
        "summary": latest.get("summary", ""),
        "all_timestamps": collected if args.all else None,
        "note": "完整明细（predictions/per-instance report）保留在评测机 "
                f"{task['work_dir']}/{latest_ts}/ 下" if scores else "",
    })


if __name__ == "__main__":
    sys.exit(main())
