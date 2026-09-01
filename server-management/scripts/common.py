#!/usr/bin/env python3
"""共享工具：SSH 执行、inventory 读写、输出协议。

所有对外脚本遵守同一契约：
- stderr 输出 __SM_PROGRESS__=<json> 阶段进度事件；
- stdout 只输出一个最终结果 JSON；
- 退出码由结果 JSON 的 ok 字段决定（ok=true -> 0，否则 -> 1）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# 状态目录放在用户主目录下，独立于任何 git 工作区，升级 skill 不丢数据。
STATE_DIR = Path.home() / ".server-management"
INVENTORY_PATH = STATE_DIR / "inventory.json"
FLEET_CACHE_PATH = STATE_DIR / "fleet-cache.json"
FLEET_PID_PATH = STATE_DIR / "fleet-service.pid"
FLEET_PORT = 8790
FLEET_URL = f"http://127.0.0.1:{FLEET_PORT}"

EXIT_OK = 0
EXIT_FAILED = 1

# 统一终态词汇。定义见 references/behavior.md，脚本只输出这些值。
STATUSES = ("ready", "needs_input", "needs_repair", "blocked", "removed", "unmanaged")

SSH_CONNECT_TIMEOUT = 10

# Windows 上，无控制台的后台进程（如 detached 的 fleet 服务）调用控制台程序（ssh、
# ssh-keygen 等）时会弹出新的控制台窗口——每轮探测闪黑框即源于此。
# CREATE_NO_WINDOW 抑制它；POSIX 上无此标志，置 0 即可。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


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
# SSH：bootstrap 之后全部走系统 ssh（密钥认证），禁止交互式密码提示。
# ---------------------------------------------------------------------------

def build_ssh_argv(host: str, port: int, user: str, command: str) -> list[str]:
    """构造一次非交互 SSH 命令行。BatchMode 确保永远不弹密码提示。"""
    return [
        "ssh",
        "-p", str(port),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        f"{user}@{host}",
        command,
    ]


def run_ssh(
    host: str, port: int, user: str, command: str, timeout: float = 60.0
) -> tuple[int, str, str]:
    """在远程机器上执行命令，返回 (退出码, stdout, stderr)。超时抛 TimeoutExpired。"""
    result = subprocess.run(
        build_ssh_argv(host, port, user, command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=_NO_WINDOW,
    )
    return result.returncode, result.stdout, result.stderr


def run_ssh_machine(machine: dict[str, Any], command: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """按 inventory 记录执行远程命令。"""
    return run_ssh(machine["host"], int(machine.get("port", 22)), machine.get("user", "root"), command, timeout)


def ssh_key_ok(host: str, port: int, user: str, timeout: float = 15.0) -> tuple[bool, str]:
    """验证密钥登录是否可用（只跑 echo，不产生副作用）。"""
    try:
        code, out, err = run_ssh(host, port, user, "echo ok", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "ssh timeout"
    except OSError as exc:
        return False, str(exc)
    if code == 0 and out.strip() == "ok":
        return True, ""
    return False, (err or out or f"exit code {code}").strip()[:500]


def remove_known_host(host: str, port: int) -> str:
    """从本地 known_hosts 移除该端点条目（幂等），返回说明文本。"""
    target = f"[{host}]:{port}" if port != 22 else host
    result = subprocess.run(
        ["ssh-keygen", "-R", target],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_NO_WINDOW,
    )
    note = (result.stderr or "").strip().splitlines()
    return note[-1] if note else f"known_hosts entry for {target} removed"


# ---------------------------------------------------------------------------
# Inventory：机器清单是唯一状态源，读写都经过这里。
# ---------------------------------------------------------------------------

def load_inventory() -> list[dict[str, Any]]:
    """读取机器清单；文件不存在或损坏时返回空表（不抛异常）。"""
    if not INVENTORY_PATH.is_file():
        return []
    try:
        data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("machines"), list):
        return [m for m in data["machines"] if isinstance(m, dict)]
    return []


def save_inventory(machines: list[dict[str, Any]]) -> None:
    """原子化写入机器清单（临时文件 + rename，避免并发写坏）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machines": machines,
    }
    fd, tmp_name = tempfile.mkstemp(dir=str(STATE_DIR), prefix="inventory-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(tmp_path, INVENTORY_PATH)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def find_machine(identifier: str) -> tuple[int, dict[str, Any]] | None:
    """按 alias / host / host:port 查找机器，返回 (下标, 记录)。"""
    machines = load_inventory()
    normalized = identifier.strip().lower()
    for index, machine in enumerate(machines):
        alias = str(machine.get("alias", "")).lower()
        host = str(machine.get("host", "")).lower()
        port = int(machine.get("port", 22))
        if normalized in (alias, host, f"{host}:{port}"):
            return index, machine
    return None


def upsert_machine(record: dict[str, Any]) -> list[dict[str, Any]]:
    """新增或按 host:port 更新机器记录，返回新清单。"""
    machines = load_inventory()
    key = (record["host"], int(record.get("port", 22)))
    for index, machine in enumerate(machines):
        if (machine.get("host"), int(machine.get("port", 22))) == key:
            machines[index] = record
            break
    else:
        machines.append(record)
    save_inventory(machines)
    return machines


def remove_machine(identifier: str) -> dict[str, Any]:
    """删除机器记录。返回 {"removed": 记录或 None, "reason": ...}。"""
    machines = load_inventory()
    normalized = identifier.strip().lower()
    for index, machine in enumerate(machines):
        alias = str(machine.get("alias", "")).lower()
        host = str(machine.get("host", "")).lower()
        port = int(machine.get("port", 22))
        if normalized in (alias, host, f"{host}:{port}"):
            removed = machines.pop(index)
            save_inventory(machines)
            return {"removed": removed, "reason": ""}
    return {"removed": None, "reason": "machine not found in inventory"}
