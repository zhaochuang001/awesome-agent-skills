#!/usr/bin/env python3
"""fleet 服务生命周期管理：start / stop / restart / status（跨平台，无 systemd 依赖）。

- start：若健康检查已通过则幂等返回；否则以 detached 进程拉起 fleet_service.py
  （POSIX: 新会话脱离终端；Windows: 无窗口脱离），写 PID 文件，等待健康检查通过；
- stop：按 PID 文件终止进程（POSIX: SIGTERM；Windows: taskkill），清理 PID 文件；
- status：健康检查 + 进程存活检查。

日志落在 ~/.server-management/fleet-service.log。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    FLEET_PID_PATH,
    FLEET_PORT,
    FLEET_URL,
    STATE_DIR,
    emit,
    progress,
)

SERVICE_SCRIPT = Path(__file__).resolve().parent / "fleet_service.py"
LOG_PATH = STATE_DIR / "fleet-service.log"
HEALTH_TIMEOUT_SECONDS = 30.0


def is_windows() -> bool:
    return os.name == "nt"


def http_health(timeout: float = 3.0) -> dict[str, Any] | None:
    """查询服务健康端点；不可达返回 None。显式禁用系统代理（本地回环不走代理）。"""
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{FLEET_URL}/api/health", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def read_pid() -> int | None:
    try:
        return int(FLEET_PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if is_windows():
        # tasklist 输出跟随系统代码页（中文 Windows 为 GBK），必须容错解码，
        # 否则 stdout 为 None 且抛 UnicodeDecodeError
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
        return bool(result.stdout) and str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def do_start() -> dict[str, Any]:
    existing = http_health()
    if existing:
        return {"ok": True, "action": "start", "status": "ready", "note": "服务已在运行", "health": existing}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(SERVICE_SCRIPT)]
    flags = 0
    kwargs: dict[str, Any] = {}
    if is_windows():
        # DETACHED_PROCESS：脱离控制台；CREATE_NO_WINDOW：不弹新窗口
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # setsid，脱离终端会话
    progress("start", f"以 detached 进程拉起 fleet 服务：{argv[0]}")

    log_stream = open(LOG_PATH, "ab")  # noqa: SIM115 - 子进程持有句柄
    try:
        process = subprocess.Popen(
            argv,
            stdout=log_stream,
            stderr=log_stream,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            **kwargs,
        )
    finally:
        log_stream.close()

    FLEET_PID_PATH.write_text(f"{process.pid}\n", encoding="utf-8")
    progress("wait-health", f"等待健康检查（最长 {HEALTH_TIMEOUT_SECONDS:.0f}s），日志 {LOG_PATH}")
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        health = http_health()
        if health:
            return {
                "ok": True,
                "action": "start",
                "status": "ready",
                "pid": process.pid,
                "url": FLEET_URL,
                "health": health,
                "log": str(LOG_PATH),
            }
        if process.poll() is not None:
            break  # 进程已退出，不再等待
        time.sleep(0.5)

    return {
        "ok": False,
        "action": "start",
        "status": "blocked",
        "pid": process.pid,
        "url": FLEET_URL,
        "error": "服务未在超时内通过健康检查",
        "hint": f"查看日志 {LOG_PATH}；常见原因是缺少 fastapi/uvicorn 依赖",
    }


def do_stop() -> dict[str, Any]:
    pid = read_pid()
    if pid is None:
        # 兜底：PID 文件丢失但服务仍在（手动启动过）
        if http_health():
            return {
                "ok": False,
                "action": "stop",
                "status": "blocked",
                "error": "服务在运行但 PID 文件缺失，无法安全定位进程",
                "hint": f"手动停止：找到监听 {FLEET_PORT} 端口的 python 进程并结束",
            }
        return {"ok": True, "action": "stop", "status": "removed", "note": "服务未在运行（幂等）"}

    if not pid_alive(pid):
        FLEET_PID_PATH.unlink(missing_ok=True)
        return {"ok": True, "action": "stop", "status": "removed", "note": "进程已不存在（幂等）"}

    if is_windows():
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 10
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    FLEET_PID_PATH.unlink(missing_ok=True)
    return {"ok": not http_health(), "action": "stop", "status": "removed", "pid": pid}


def do_status() -> dict[str, Any]:
    pid = read_pid()
    health = http_health()
    return {
        "ok": True,
        "action": "status",
        "status": "ready" if health else "stopped",
        "pid": pid,
        "pid_alive": bool(pid and pid_alive(pid)),
        "health": health,
        "url": FLEET_URL,
        "log": str(LOG_PATH),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="fleet 服务生命周期管理")
    parser.add_argument("action", choices=("start", "stop", "restart", "status"))
    args = parser.parse_args()

    if args.action == "start":
        return emit(do_start())
    if args.action == "stop":
        return emit(do_stop())
    if args.action == "restart":
        do_stop()
        return emit(do_start())
    return emit(do_status())


if __name__ == "__main__":
    raise SystemExit(main())
