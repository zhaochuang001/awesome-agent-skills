#!/usr/bin/env python3
"""共享工具：SSH 执行、任务注册表、评测机配置、输出协议。

所有对外脚本遵守同一契约（与 server-management / npu-migrate 一致）：
- stderr 输出 __SM_PROGRESS__=<json> 阶段进度事件；
- stdout 只输出一个最终结果 JSON；
- 退出码由结果 JSON 的 ok 字段决定（ok=true -> 0，否则 -> 1）。

状态目录 ~/.ais-bench-eval/ 独立于任何 git 工作区，升级 skill 不丢数据：
- tasks.json  任务注册表（本地唯一任务状态源）
- hosts.json  评测机配置（可选；不存在时用内置默认）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".ais-bench-eval"
TASKS_PATH = STATE_DIR / "tasks.json"
HOSTS_PATH = STATE_DIR / "hosts.json"

EXIT_OK = 0
EXIT_FAILED = 1

# 统一终态词汇。定义见 references/behavior.md，脚本只输出这些值。
# running 是中间态（评测任务动辄数小时，发起成功即返回 running）。
STATUSES = ("running", "finished", "failed", "needs_input", "blocked", "removed")

SSH_CONNECT_TIMEOUT = 10

# Windows 上无控制台后台进程调用 ssh 会闪黑框，CREATE_NO_WINDOW 抑制；POSIX 置 0。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ---------------------------------------------------------------------------
# 内置评测机配置：hosts.json 里的同名条目覆盖这里的默认值。
# 布局字段（image/benchmark_src/task_root）是"这台机器的环境事实"，
# 机器上没装这些路径时脚本会在预检阶段报 blocked 并说明缺什么。
# ---------------------------------------------------------------------------

DEFAULT_HOSTS: dict[str, dict[str, Any]] = {
    # 极光平台的模型评测服务器（共享）：上面的 aisbench-session-* 容器与
    # /home/jiguang 由平台管理，本 skill 一律不动它们，只用自建容器隔离运行。
    "90.90.122.21": {
        "host": "90.90.122.21",
        "port": 22,
        "user": "root",
        "alias": "eval-main",
        # 评测容器镜像（含 python3.11 + ais_bench_benchmark editable 安装，
        # 依赖宿主机 /home 下的源码，所以容器必须挂载 /home）
        "image": "aisbench-swe:20260630",
        # AISBench 源码根（容器内可读，cd 到这里跑 ais_bench）
        "benchmark_src": "/home/jiguang/inference/benchmark_20260630/benchmark",
        # 评测任务目录根：config / 日志 / pid / exit_code / outputs 都在
        # <task_root>/<task-id>/ 下。/tmp 在机器重启后可能被清空，
        # 长跑任务建议在 hosts.json 里把 task_root 改到持久盘。
        "task_root": "/tmp/ais-bench-eval",
        # 性能场景必填的本地 tokenizer（default_perf 汇总靠它算 token 数）；
        # 用被测模型自己的 tokenizer 统计口径才准，此默认值是兜底
        "default_tokenizer": "/home/weight/qwen3-32B-w8a8-no-w_axes-1118-full",
    }
}


def progress(phase: str, detail: str = "", **extra: Any) -> None:
    """向 stderr 输出一条阶段进度事件；stdout 留给最终 JSON。"""
    event: dict[str, Any] = {"phase": phase, "ts": round(time.time(), 3)}
    if detail:
        event["detail"] = detail
    event.update(extra)
    print(f"__SM_PROGRESS__={json.dumps(event, ensure_ascii=False)}", file=sys.stderr, flush=True)


def emit(payload: dict[str, Any]) -> int:
    """打印最终结果 JSON 并返回退出码。"""
    payload.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK if payload.get("ok") else EXIT_FAILED


# ---------------------------------------------------------------------------
# 评测机配置
# ---------------------------------------------------------------------------

def load_hosts() -> dict[str, dict[str, Any]]:
    """合并内置默认与 hosts.json（同名 host:port 以用户配置为准）。"""
    hosts: dict[str, dict[str, Any]] = {}
    for key, value in DEFAULT_HOSTS.items():
        hosts[key] = dict(value)
    if HOSTS_PATH.is_file():
        try:
            data = json.loads(HOSTS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return hosts
        custom = data.get("hosts", data) if isinstance(data, dict) else {}
        if isinstance(custom, dict):
            for key, value in custom.items():
                if not isinstance(value, dict):
                    continue
                base = None
                for candidate in hosts.values():
                    if value.get("host") == candidate.get("host") and int(value.get("port", 22)) == int(candidate.get("port", 22)):
                        base = candidate
                        break
                merged = dict(base) if base else {"host": key, "port": 22, "user": "root"}
                merged.update(value)
                hosts[key] = merged
    return hosts


def find_host(identifier: str) -> dict[str, Any] | None:
    """按 host / alias / host:port 查找评测机配置。"""
    normalized = identifier.strip().lower()
    for config in load_hosts().values():
        host = str(config.get("host", "")).lower()
        alias = str(config.get("alias", "")).lower()
        port = int(config.get("port", 22))
        if normalized in (host, alias, f"{host}:{port}"):
            return config
    return None


# ---------------------------------------------------------------------------
# SSH：全部走系统 ssh（密钥认证），BatchMode 确保永不弹交互提示。
# ---------------------------------------------------------------------------

def run_ssh(machine: dict[str, Any], command: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """在评测机上执行命令，返回 (退出码, stdout, stderr)。超时抛 TimeoutExpired。"""
    argv = [
        "ssh",
        "-p", str(int(machine.get("port", 22))),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        f"{machine.get('user', 'root')}@{machine['host']}",
        command,
    ]
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        check=False, creationflags=_NO_WINDOW,
    )
    return result.returncode, result.stdout, result.stderr


def ssh_ok(machine: dict[str, Any], timeout: float = 15.0) -> tuple[bool, str]:
    """验证密钥登录可用（只跑 echo）。"""
    try:
        code, out, err = run_ssh(machine, "echo ok", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "ssh timeout"
    except OSError as exc:
        return False, str(exc)
    if code == 0 and out.strip() == "ok":
        return True, ""
    return False, (err or out or f"exit code {code}").strip()[:500]


def sh_quote(value: str) -> str:
    """单引号包裹，用于把字面量安全嵌入远端 bash 命令。"""
    return "'" + str(value).replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# 任务注册表：本地唯一任务状态源。
# 记录里不含 API key（key 只出现在评测机任务目录的 config 文件里）。
# ---------------------------------------------------------------------------

def new_task_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]


def load_tasks() -> list[dict[str, Any]]:
    if not TASKS_PATH.is_file():
        return []
    try:
        data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        return [t for t in data["tasks"] if isinstance(t, dict)]
    return []


def save_tasks(tasks: list[dict[str, Any]]) -> None:
    """原子化写入（临时文件 + rename，避免并发写坏）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tasks": tasks,
    }
    fd, tmp_name = tempfile.mkstemp(dir=str(STATE_DIR), prefix="tasks-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(tmp_path, TASKS_PATH)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def upsert_task(record: dict[str, Any]) -> None:
    tasks = load_tasks()
    for index, task in enumerate(tasks):
        if task.get("task_id") == record["task_id"]:
            tasks[index] = record
            break
    else:
        tasks.append(record)
    save_tasks(tasks)


def find_task(identifier: str) -> dict[str, Any] | None:
    """按 task_id（或其唯一前缀）查找任务。"""
    tasks = load_tasks()
    normalized = identifier.strip()
    for task in tasks:
        if task.get("task_id") == normalized:
            return task
    # 前缀匹配要求唯一（task_id 含时间戳，前 15 位即可定位一次发起）
    matches = [t for t in tasks if str(t.get("task_id", "")).startswith(normalized)]
    return matches[0] if len(matches) == 1 else None
