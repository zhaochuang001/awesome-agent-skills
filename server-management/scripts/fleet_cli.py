#!/usr/bin/env python3
"""fleet 查询 CLI：servers / status HOST / capacity。

数据来源优先级：
- 默认：运行中的服务的缓存数据（不触发新探测）；
- --live：优先让服务即时探测（POST /api/refresh）；服务未运行时本地并行探测一次。

输出协议与其他脚本一致：stderr 进度、stdout 单个 JSON。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    FLEET_CACHE_PATH,
    FLEET_URL,
    emit,
    find_machine,
    load_inventory,
    progress,
)
from fleet_service import probe_one_machine  # noqa: E402
from npu_probe import summarize_fleet  # noqa: E402

LOCAL_PROBE_TIMEOUT_SECONDS = 60.0


def http_json(path: str, method: str = "GET", timeout: float = 90.0) -> dict[str, Any] | None:
    """访问服务 API；显式禁用系统代理（本地回环不走代理）。失败返回 None。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"{FLEET_URL}{path}", method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def service_alive() -> bool:
    return http_json("/api/health", timeout=3.0) is not None


def read_cache() -> dict[str, Any] | None:
    if not FLEET_CACHE_PATH.is_file():
        return None
    try:
        return json.loads(FLEET_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def probe_local() -> dict[str, Any]:
    """服务未运行时的兜底：本地并行探测全部机器一次。"""
    progress("local-probe", "fleet 服务未运行，本地并行探测一次")
    machines = load_inventory()
    result: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(probe_one_machine, m): m for m in machines}
        try:
            for future in as_completed(futures, timeout=LOCAL_PROBE_TIMEOUT_SECONDS + 15):
                try:
                    entry = future.result()
                except Exception as exc:  # noqa: BLE001 - 单机失败不拖垮整体
                    machine = futures[future]
                    entry = {"alias": machine.get("alias"), "host": machine["host"], "reachable": False, "error": str(exc)}
                result[entry["host"]] = entry
        except TimeoutError:
            pass  # 个别机器超时，返回已完成的
    return {"machines": result, "summary": summarize_fleet(result), "source": "local-probe"}


def collect_fleet(live: bool) -> dict[str, Any]:
    if live:
        if service_alive():
            progress("live-probe", "请求服务即时探测")
            data = http_json("/api/refresh", method="POST", timeout=120.0)
            if data:
                data["source"] = "service-refresh"
                return data
        return probe_local()
    if service_alive():
        data = http_json("/api/servers", timeout=10.0)
        if data:
            data["source"] = "service-cache"
            return data
    cached = read_cache()
    if cached:
        cached["source"] = "file-cache"
        return cached
    # 没有服务也没有缓存：退化为一次本地探测
    return probe_local()


def cmd_servers(live: bool) -> int:
    progress("start", "fleet servers")
    return emit({"ok": True, "action": "fleet-servers", "status": "ready", **collect_fleet(live)})


def cmd_status_host(host: str, live: bool) -> int:
    progress("start", f"fleet status {host}")
    found = find_machine(host)
    if not found:
        return emit(
            {
                "ok": True,
                "action": "fleet-status",
                "status": "unmanaged",
                "machine": host,
                "note": "不在 inventory 中",
            }
        )
    _index, machine = found
    if live or not service_alive():
        # 单机 live：直接本地探测比唤醒服务更直接
        progress("probe", f"直接探测 {machine['host']}")
        entry = probe_one_machine(machine)
    else:
        data = http_json("/api/servers", timeout=10.0)
        entry = (data or {}).get("machines", {}).get(machine["host"])
        if entry is None:
            entry = probe_one_machine(machine)
    return emit(
        {
            "ok": True,
            "action": "fleet-status",
            "status": "ready" if entry.get("reachable") else "needs_repair",
            "machine": machine,
            "observation": entry,
        }
    )


def cmd_capacity(min_idle: int, max_age: int, live: bool) -> int:
    progress("start", f"fleet capacity min_idle={min_idle} max_age={max_age}")
    note_capacity = "capacity 是观测到的空闲，不是预留；查询不锁定资源"
    if service_alive():
        query = f"/api/capacity?min_idle={min_idle}"
        if max_age:
            query += f"&max_age={max_age}"
        data = http_json(query, timeout=120.0 if live else 10.0)
        if data:
            return emit({"ok": True, "action": "fleet-capacity", "status": "ready", **data})
    # 服务未运行：基于本地探测结果自行计算
    fleet = collect_fleet(live)
    from fleet_service import _is_idle_npu  # noqa: E402

    candidates = []
    for host, entry in (fleet.get("machines") or {}).items():
        if not entry.get("reachable"):
            continue
        # 本地路径没有历史空闲轨迹，按当前快照判定；max_age 语义只在服务持续运行时有意义
        idle = [
            {"id": n.get("id")}
            for n in (entry.get("npu") or {}).get("npus", [])
            if _is_idle_npu(n)
        ]
        if len(idle) >= min_idle:
            candidates.append(
                {
                    "alias": entry.get("alias"),
                    "host": host,
                    "port": entry.get("port"),
                    "idle_npus": idle,
                    "idle_count": len(idle),
                }
            )
    return emit(
        {
            "ok": True,
            "action": "fleet-capacity",
            "status": "ready",
            "candidates": candidates,
            "note": note_capacity + ("；max_age 需要服务持续运行积累观测历史，本次按当前快照计算" if max_age else ""),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="NPU 集群查询 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_servers = sub.add_parser("servers", help="列出全部机器与 NPU 状态")
    p_servers.add_argument("--live", action="store_true", help="触发即时探测（默认读缓存）")

    p_status = sub.add_parser("status", help="查询单台机器")
    p_status.add_argument("machine", help="别名、IP 或 IP:端口")
    p_status.add_argument("--live", action="store_true")

    p_capacity = sub.add_parser("capacity", help="查询满足空闲卡数条件的机器")
    p_capacity.add_argument("--min-idle", type=int, default=1, help="最少空闲卡数，默认 1")
    p_capacity.add_argument("--max-age", type=int, default=0, help="空闲至少多少秒（需服务持续运行），默认 0 不限制")
    p_capacity.add_argument("--live", action="store_true")

    args = parser.parse_args()

    if args.command == "servers":
        return cmd_servers(args.live)
    if args.command == "status":
        return cmd_status_host(args.machine, args.live)
    return cmd_capacity(args.min_idle, args.max_age, args.live)


if __name__ == "__main__":
    raise SystemExit(main())
