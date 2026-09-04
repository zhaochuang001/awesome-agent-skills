#!/usr/bin/env python3
"""NPU 集群常驻监控服务：FastAPI + 后台探测线程 + 静态前端 + SQLite 历史。

设计约束：
- 仅监听 127.0.0.1，不对局域网暴露；
- 后台线程周期并行探测 inventory 中全部机器（每机独立超时，坏机器不影响整体）；
- 探测分两层：NPU 高频采样（随探测轮），磁盘/挂载/Docker/负载低频（每 5 分钟）；
- 自适应频率：浏览器活跃（2 分钟内有 API 轮询）时 30s 一轮，无人看时降到 120s；
- 结果同时保存在内存与 fleet-cache.json，CLI 在服务未运行时可读缓存；
- 每轮采样写入 SQLite（fleet_history.py），保留 7 天，支持趋势查询；
- capacity 语义是"观测到的空闲"，不是预留：查询结果不锁定任何资源；
- 探测只用密钥 SSH，绝不使用密码。

启动：python fleet_service.py（通常经 fleet_manage.py start 以 detached 方式拉起）
依赖：fastapi、uvicorn
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    FLEET_CACHE_PATH,
    FLEET_PORT,
    INVENTORY_PATH,
    find_machine,
    load_inventory,
    progress,
    remove_known_host,
    remove_machine,
    save_inventory,
    ssh_key_ok,
    upsert_machine,
)
from fleet_history import FleetHistory  # noqa: E402
from npu_probe import probe_machine_extras, probe_remote_npu, summarize_fleet  # noqa: E402

PROBE_INTERVAL_SECONDS = 30.0          # 有客户端时的探测间隔
IDLE_PROBE_INTERVAL_SECONDS = 120.0    # 无客户端（2 分钟无轮询）时的降频间隔
CLIENT_FRESH_WINDOW_SECONDS = 120.0    # 客户端活跃判定窗口
MACHINE_EXTRA_INTERVAL_SECONDS = 300.0  # 磁盘/挂载/Docker/负载的低频采集间隔
PROBE_TIMEOUT_SECONDS = 45.0
MACHINE_PROBE_TIMEOUT_SECONDS = 30.0
PROBE_WORKERS = 16  # 探测线程池并发数（77+ 台集群规模下 8 并发一轮耗时过长）


def probe_window_seconds(machine_count: int) -> float:
    """as_completed 收割窗口：随机器数自适应，避免大规模集群下慢机器被截断漏采。

    估算：单机最坏 ~80s（连接 20 + NPU 30 + extras 30），PROBE_WORKERS 并发下的
    排队成本约每台 80/16=5s。窗口只是兜底——全部完成时 as_completed 立即返回，
    加大它不会拖慢正常轮次。
    """
    return max(PROBE_TIMEOUT_SECONDS + 10, machine_count * 5.0)

# 前端静态文件目录（web/ 与 scripts/ 同级）
WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# 空闲卡判定阈值：健康 + AICore 利用率低 + 显存占用低
IDLE_AICORE_UTIL_MAX = 5
IDLE_MEM_FRACTION_MAX = 0.05


class FleetState:
    """线程安全的集群状态：最近一次探测结果 + 每卡空闲起始时间 + 客户端活跃时间。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._machines: dict[str, dict[str, Any]] = {}
        self._idle_since: dict[tuple[str, int], float] = {}
        self._last_client_seen = time.time()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._machines))  # 深拷贝防篡改

    def forget_machine(self, host: str) -> None:
        """移除机器时清掉内存状态与空闲轨迹。"""
        with self._lock:
            self._machines.pop(host, None)
            for key in [k for k in self._idle_since if k[0] == host]:
                self._idle_since.pop(key, None)

    def update_machine(self, machine: dict[str, Any], entry: dict[str, Any]) -> None:
        with self._lock:
            old = self._machines.get(machine["host"]) or {}
            # 低频字段（磁盘/Docker/负载每 5 分钟采一次）在未采到的轮次继承上一轮，
            # 避免被 30s 的高频探测覆盖丢失；机器不可达时不继承旧数据以免误导。
            if entry.get("reachable"):
                for key in (
                    "load1", "cpu_percent", "memory_available_bytes", "memory_total_bytes",
                    "disks", "docker", "extras_probed_at",
                ):
                    if key not in entry and key in old:
                        entry[key] = old[key]
            self._machines[machine["host"]] = entry
            self._track_idle(machine["host"], entry)

    def _track_idle(self, host: str, entry: dict[str, Any]) -> None:
        now = time.time()
        for npu in (entry.get("npu") or {}).get("npus", []):
            key = (host, int(npu.get("id", -1)))
            if _is_idle_npu(npu):
                self._idle_since.setdefault(key, now)
            else:
                self._idle_since.pop(key, None)

    def idle_seconds(self, host: str, npu_id: int) -> float | None:
        with self._lock:
            since = self._idle_since.get((host, npu_id))
            return (time.time() - since) if since else None

    # 客户端活跃心跳：驱动自适应探测频率
    def note_client(self) -> None:
        with self._lock:
            self._last_client_seen = time.time()

    def probe_interval(self) -> float:
        with self._lock:
            idle = time.time() - self._last_client_seen
        return PROBE_INTERVAL_SECONDS if idle < CLIENT_FRESH_WINDOW_SECONDS else IDLE_PROBE_INTERVAL_SECONDS

    def persist_cache(self) -> None:
        snapshot = self.snapshot()
        payload = {
            "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "machines": snapshot,
            "summary": summarize_fleet(snapshot),
        }
        tmp = FLEET_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(FLEET_CACHE_PATH)

    def load_cache(self) -> None:
        if FLEET_CACHE_PATH.is_file():
            try:
                data = json.loads(FLEET_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(data.get("machines"), dict):
                    with self._lock:
                        self._machines = data["machines"]
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass


def _is_idle_npu(npu: dict[str, Any]) -> bool:
    if npu.get("health") != "OK":
        return False
    util = npu.get("aicore_util")
    if util is not None and util > IDLE_AICORE_UTIL_MAX:
        return False
    used, total = npu.get("mem_used_mb"), npu.get("mem_total_mb")
    if used is not None and total:
        if used / total > IDLE_MEM_FRACTION_MAX:
            return False
    return True


def probe_one_machine(machine: dict[str, Any], include_extras: bool = True) -> dict[str, Any]:
    """探测单台机器（在线程池里跑）：SSH 健康度 + NPU 状态，可选机器级扩展采集。"""
    host = machine["host"]
    ok, detail = ssh_key_ok(
        host, int(machine.get("port", 22)), machine.get("user", "root"), timeout=20
    )
    entry: dict[str, Any] = {
        "alias": machine.get("alias", host),
        "host": host,
        "port": machine.get("port", 22),
        "user": machine.get("user", "root"),
        "reachable": ok,
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tags": machine.get("tags", []),
        "enabled": machine.get("enabled", True),
    }
    if not ok:
        entry["error"] = detail
        return entry
    npu = probe_remote_npu(machine, timeout=MACHINE_PROBE_TIMEOUT_SECONDS)
    entry["npu"] = npu
    entry["npu_idle"] = sum(1 for n in npu.get("npus", []) if _is_idle_npu(n))
    if include_extras:
        extras = probe_machine_extras(machine, timeout=MACHINE_PROBE_TIMEOUT_SECONDS)
        entry.update(
            load1=extras.get("load1"),
            cpu_percent=extras.get("cpu_percent"),
            memory_available_bytes=extras.get("memory_available_bytes"),
            memory_total_bytes=extras.get("memory_total_bytes"),
            disks=extras.get("disks"),
            docker=extras.get("docker"),
            extras_probed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        if extras.get("error"):
            entry["extras_error"] = extras["error"]
    return entry


def probe_all(
    state: FleetState,
    executor: ThreadPoolExecutor,
    history: FleetHistory,
    last_extra_ts: dict[str, float],
) -> dict[str, dict[str, Any]]:
    """并行探测全部机器；单机失败或超时不影响其他机器的结果。

    extras（磁盘/Docker/负载）按机器独立节流到 MACHINE_EXTRA_INTERVAL_SECONDS。
    每轮结果写入 SQLite 历史；低频数据写 machine_samples。
    """
    machines = [m for m in load_inventory() if m.get("enabled", True)]
    now = time.time()
    want_extras = {
        m["host"]: (now - last_extra_ts.get(m["host"], 0)) >= MACHINE_EXTRA_INTERVAL_SECONDS
        for m in machines
    }
    futures = {
        executor.submit(probe_one_machine, m, want_extras.get(m["host"], True)): m
        for m in machines
    }
    snapshot_hosts: dict[str, dict[str, Any]] = {}
    fresh_extras: set[str] = set()
    try:
        for future in as_completed(futures, timeout=probe_window_seconds(len(machines))):
            machine = futures[future]
            try:
                entry = future.result()
            except Exception as exc:  # noqa: BLE001 - 单机异常收敛为错误字段
                entry = {
                    "alias": machine.get("alias", machine["host"]),
                    "host": machine["host"],
                    "reachable": False,
                    "error": f"probe error: {exc}",
                    "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            state.update_machine(machine, entry)
            snapshot_hosts[entry["host"]] = entry
            if entry.get("reachable") and want_extras.get(entry["host"]):
                last_extra_ts[entry["host"]] = now
                fresh_extras.add(entry["host"])
    except TimeoutError:
        # 个别机器探测超时：保留已完成的结果，未完成的机器保留上一次观测
        pass

    # 历史入库（NPU 高频每轮；机器级低频只在本轮真正采集了 extras 的机器）
    for host, entry in snapshot_hosts.items():
        try:
            memory_total = entry.get("memory_total_bytes")
            memory_used = (
                memory_total - entry["memory_available_bytes"]
                if memory_total is not None and entry.get("memory_available_bytes") is not None
                else None
            )
            history.record_probe(
                host,
                (entry.get("npu") or {}).get("npus", []),
                load1=entry.get("load1"),
                disks=entry.get("disks"),
                docker=entry.get("docker"),
                reachable=bool(entry.get("reachable")),
                machine_sample=host in fresh_extras,
                cpu_percent=entry.get("cpu_percent"),
                memory_used=memory_used,
                memory_total=memory_total,
            )
        except Exception as exc:  # noqa: BLE001 - 历史写入失败不影响探测
            progress("history", f"record failed for {host}: {exc}")

    state.persist_cache()
    return state.snapshot()


def build_app(state: FleetState, executor: ThreadPoolExecutor, history: FleetHistory):
    """构造 FastAPI 应用。独立函数便于测试注入。"""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse

    app = FastAPI(title="server-management fleet monitor", docs_url=None, redoc_url=None)
    probe_lock = threading.Lock()
    last_extra_ts: dict[str, float] = {}

    def probe_once() -> dict[str, dict[str, Any]]:
        # 防止并发 refresh 打爆线程池
        with probe_lock:
            return probe_all(state, executor, history, last_extra_ts)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        snapshot = state.snapshot()
        return {
            "status": "ok",
            "machines": len(snapshot),
            "inventory_path": str(INVENTORY_PATH),
            "probe_interval_seconds": state.probe_interval(),
            "history": history.coverage(),
        }

    @app.get("/api/servers")
    def servers() -> dict[str, Any]:
        state.note_client()  # 轮询即心跳：驱动探测频率保持在 30s
        snapshot = state.snapshot()
        return {"machines": snapshot, "summary": summarize_fleet(snapshot)}

    @app.post("/api/refresh")
    def refresh() -> dict[str, Any]:
        state.note_client()
        snapshot = probe_once()
        return {"machines": snapshot, "summary": summarize_fleet(snapshot)}

    @app.post("/api/collect")
    def collect(host: str) -> dict[str, Any]:
        """单机即时采集（面板"立即采集"按钮）：探测一次并更新状态、写入历史。"""
        state.note_client()
        machines = load_inventory()
        machine = next((m for m in machines if m["host"] == host), None)
        if machine is None:
            raise HTTPException(status_code=404, detail=f"machine {host} not in inventory")
        with probe_lock:
            entry = probe_one_machine(machine, include_extras=True)
        state.update_machine(machine, entry)
        try:
            memory_total = entry.get("memory_total_bytes")
            memory_used = (
                memory_total - entry["memory_available_bytes"]
                if memory_total is not None and entry.get("memory_available_bytes") is not None
                else None
            )
            history.record_probe(
                host,
                (entry.get("npu") or {}).get("npus", []),
                load1=entry.get("load1"),
                disks=entry.get("disks"),
                docker=entry.get("docker"),
                reachable=bool(entry.get("reachable")),
                machine_sample=True,
                cpu_percent=entry.get("cpu_percent"),
                memory_used=memory_used,
                memory_total=memory_total,
            )
        except Exception as exc:  # noqa: BLE001 - 历史写入失败不影响采集结果
            progress("history", f"collect record failed for {host}: {exc}")
        return {"ok": True, "machine": entry}

    @app.get("/api/history")
    def history_query(
        host: str,
        npu_id: int = -1,
        metric: str = "aicore",
        range: int = 3600,
    ) -> dict[str, Any]:
        """查询某卡指标趋势（range 秒）。metric: aicore | mem | temp | power。"""
        if npu_id < 0:
            raise HTTPException(status_code=400, detail="npu_id required")
        if range <= 0 or range > 7 * 24 * 3600:
            raise HTTPException(status_code=400, detail="range must be within 7 days")
        try:
            points = history.query_npu_series(host, npu_id, metric, range)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"host": host, "npu_id": npu_id, "metric": metric, "points": points}

    @app.get("/api/history/aggregate")
    def history_aggregate(
        host: str,
        range: int = 604800,
        bucket: int = 7200,
    ) -> dict[str, Any]:
        """按时间桶聚合整机的 NPU 与机器级历史（趋势线与热力图数据源）。"""
        if range <= 0 or range > 7 * 24 * 3600:
            raise HTTPException(status_code=400, detail="range must be within 7 days")
        if bucket < 60 or bucket > 86400:
            raise HTTPException(status_code=400, detail="bucket must be 60s..86400s")
        return {
            "host": host,
            "range": range,
            "bucket": bucket,
            "npu": history.query_aggregate_buckets(host, range, bucket),
            "machine": history.query_machine_buckets(host, range, bucket),
        }

    @app.get("/api/capacity")
    def capacity(min_idle: int = 1, max_age: int = 0) -> dict[str, Any]:
        snapshot = state.snapshot()
        candidates = []
        for host, entry in snapshot.items():
            if not entry.get("reachable"):
                continue
            idle_np_us = []
            for npu in (entry.get("npu") or {}).get("npus", []):
                if not _is_idle_npu(npu):
                    continue
                idle_seconds = state.idle_seconds(host, int(npu.get("id", -1)))
                if max_age and (idle_seconds is None or idle_seconds < max_age):
                    continue
                idle_np_us.append({"id": npu.get("id"), "idle_seconds": round(idle_seconds or 0, 1)})
            if len(idle_np_us) >= min_idle:
                candidates.append(
                    {
                        "alias": entry.get("alias"),
                        "host": host,
                        "port": entry.get("port"),
                        "idle_npus": idle_np_us,
                        "idle_count": len(idle_np_us),
                    }
                )
        return {
            "candidates": candidates,
            "note": "capacity 是观测到的空闲，不是预留；查询不锁定资源",
        }

    # ----------------------------------------------------------------------
    # 服务器管理：批量添加 / 更新（标签、暂停）/ 移除。
    # 复用 machine_add.py 的 bootstrap 与探测逻辑，CLI 与面板走同一套实现。
    # ----------------------------------------------------------------------

    @app.post("/api/servers/batch")
    def add_servers(payload: dict[str, Any]) -> dict[str, Any]:
        """批量添加：先试已有密钥，失败后按顺序尝试候选密码做一次性 bootstrap。

        密码只在本次请求内使用，不落盘、不返回。
        """
        from machine_add import bootstrap_authorized_key, ensure_local_key, probe_machine_meta
        from npu_probe import infer_machine_type, probe_remote_npu

        specs = payload.get("servers") or []
        passwords = payload.get("passwords") or []
        if not specs:
            raise HTTPException(status_code=400, detail="servers list is empty")
        _, public_key, _key_note = ensure_local_key()
        results: list[dict[str, Any]] = []
        for spec in specs:
            host = str(spec.get("host") or spec.get("name") or "").strip()
            port = int(spec.get("port") or 22)
            user = str(spec.get("username") or "root")
            alias = str(spec.get("name") or host)
            tags = [str(t) for t in (spec.get("tags") or []) if str(t).strip()]
            item: dict[str, Any] = {"alias": alias, "host": host, "port": port, "user": user}
            if not host:
                results.append({**item, "auth": {"ok": False, "error": "missing host"}})
                continue
            ok, detail = ssh_key_ok(host, port, user)
            method = "key"
            if not ok and passwords:
                for password in passwords:
                    installed, install_note = bootstrap_authorized_key(
                        host, port, user, password, public_key
                    )
                    if installed:
                        ok, detail = ssh_key_ok(host, port, user)
                        method = "password-once"
                        if ok:
                            break
                    else:
                        detail = install_note
            if not ok:
                results.append({**item, "auth": {"ok": False, "error": (detail or "no auth path")[:200]}})
                continue
            meta = probe_machine_meta(host, port, user)
            record: dict[str, Any] = {
                "alias": alias,
                "host": host,
                "port": port,
                "user": user,
                "added_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "auth": "key",
                "tags": tags,
                "enabled": True,
                "remote_hostname": meta.get("hostname"),
                "kernel": meta.get("kernel"),
                "cpu_cores": int(meta["cpu_cores"]) if str(meta.get("cpu_cores", "")).isdigit() else None,
            }
            npu = probe_remote_npu(record)
            record["npu_count"] = npu.get("npu_count", 0)
            names = {n.get("name") for n in npu.get("npus", []) if n.get("name")}
            record["npu_name"] = sorted(names)[0] if len(names) == 1 else (sorted(names) if names else None)
            record["machine_type"] = infer_machine_type(sorted(names)[0]) if len(names) == 1 else "unknown"
            upsert_machine(record)
            results.append({**item, "auth": {"ok": True, "method": method}, "npu_count": record["npu_count"]})
        return {"results": results}

    @app.put("/api/servers/{host}")
    def update_server(host: str, payload: dict[str, Any]) -> dict[str, Any]:
        """更新机器的标签或启用状态。"""
        found = find_machine(host)
        if not found:
            raise HTTPException(status_code=404, detail=f"machine {host} not in inventory")
        _index, machine = found
        if "tags" in payload and isinstance(payload["tags"], list):
            machine["tags"] = [str(t)[:32] for t in payload["tags"] if str(t).strip()][:20]
        if "enabled" in payload:
            machine["enabled"] = bool(payload["enabled"])
        machines = upsert_machine(machine)
        if not machine.get("enabled", True):
            state.forget_machine(machine["host"])
        else:
            # 同步 tags/enabled 到内存条目，面板立即生效（不必等下一轮探测）
            entry = state.snapshot().get(machine["host"])
            if entry is not None:
                entry["tags"] = machine.get("tags", [])
                entry["enabled"] = True
                state.update_machine(machine, entry)
        return {"ok": True, "machine": machine, "fleet_size": len(machines)}

    @app.delete("/api/servers/{host}")
    def delete_server(host: str) -> dict[str, Any]:
        """移除机器：仅删本地登记与 known_hosts 条目（与 machine_remove.py 同边界）。"""
        result = remove_machine(host)
        removed = result["removed"]
        if removed is None:
            raise HTTPException(status_code=404, detail=result["reason"])
        note = remove_known_host(removed["host"], int(removed.get("port", 22)))
        state.forget_machine(removed["host"])
        return {
            "ok": True,
            "removed": removed,
            "known_hosts": note,
            "boundary": "仅清理本地 inventory 与 known_hosts；宿主机侧公钥保留",
        }

    # ----------------------------------------------------------------------
    # 静态前端（web/ 目录，无构建链）
    # ----------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        state.note_client()
        index_path = WEB_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="web/index.html missing")
        return FileResponse(index_path)

    # 静态文件服务：/assets/*（vite 构建产物，位于 web/assets/）
    # 与 /static/*（web/ 根下的其他静态文件），均带路径穿越防护。
    def serve_static(relative: str) -> FileResponse:
        target = (WEB_DIR / relative).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())):
            raise HTTPException(status_code=404)
        if not target.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(target)

    @app.get("/assets/{file_path:path}", include_in_schema=False)
    def assets_files(file_path: str) -> FileResponse:
        return serve_static(f"assets/{file_path}")

    @app.get("/static/{file_path:path}", include_in_schema=False)
    def static_files(file_path: str) -> FileResponse:
        return serve_static(file_path)

    return app


def main() -> int:
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print(
            "缺少依赖 fastapi/uvicorn。安装："
            "python -m pip install --user -i https://repo.huaweicloud.com/repository/pypi/simple/ fastapi uvicorn",
            file=sys.stderr,
        )
        return 2

    state = FleetState()
    state.load_cache()
    history = FleetHistory()
    executor = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
    stop_event = threading.Event()
    last_extra_ts: dict[str, float] = {}

    def worker() -> None:
        while not stop_event.is_set():
            try:
                probe_all(state, executor, history, last_extra_ts)
            except Exception as exc:  # noqa: BLE001 - 探测循环永不退出
                progress("fleet-probe", f"probe cycle error: {exc}")
            stop_event.wait(state.probe_interval())  # 自适应：无人看时降频

    app = build_app(state, executor, history)
    thread = threading.Thread(target=worker, name="fleet-probe", daemon=True)
    thread.start()

    import uvicorn

    progress("fleet-service", f"listening on 127.0.0.1:{FLEET_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=FLEET_PORT, log_level="warning")
    stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
