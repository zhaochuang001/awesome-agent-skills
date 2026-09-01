#!/usr/bin/env python3
"""把源服务器上的容器 + 代码文件夹迁移到有空闲 NPU 卡的目标服务器并拉起服务。

依赖 server-management skill（inventory 与 SSH 工具）：
- 机器清单来自 ~/.server-management/inventory.json（唯一状态源）；
- 空闲卡数据：fleet 服务在跑走 HTTP API，否则本地并行探测。

安全边界（详见 references/behavior.md）：
- 源容器默认不停不删（迁移失败可回滚：回源机重启）；
- 只写目标机的 docker 镜像/容器与用户指定的代码路径；
- 挂载里代码路径之外的目录不自动同步，报告中提醒。

输出协议与 server-management 一致：stderr __SM_PROGRESS__ 进度，stdout 单个最终 JSON。
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 依赖注入：server-management skill 的 scripts 目录（安装态与仓库开发态两种布局）
# ---------------------------------------------------------------------------


def _inject_dependency() -> str:
    candidates = [
        Path.home() / ".claude" / "skills" / "server-management" / "scripts",
        Path(__file__).resolve().parents[2] / "server-management" / "scripts",
    ]
    for candidate in candidates:
        if (candidate / "common.py").is_file():
            sys.path.insert(0, str(candidate))
            return str(candidate)
    print(
        "缺少依赖 skill：server-management（提供机器清单与 SSH 工具）。"
        "请先安装 awesome-agent-skills 仓库中的 server-management。",
        file=sys.stderr,
    )
    raise SystemExit(2)


_DEPENDENCY_PATH = _inject_dependency()

from common import emit, find_machine, load_inventory, progress, run_ssh_machine  # noqa: E402
from npu_probe import probe_remote_npu  # noqa: E402

FLEET_URL = "http://127.0.0.1:8790"
TRANSFER_TIMEOUT_SECONDS = 3600  # 镜像直传上限（大镜像走 gzip -1 快速压缩）
STARTUP_GRACE_SECONDS = 60

# 空闲卡判定阈值（与 server-management/fleet_service 保持一致）
IDLE_AICORE_UTIL_MAX = 5
IDLE_MEM_FRACTION_MAX = 0.05


# ---------------------------------------------------------------------------
# 源容器解析（docker inspect）
# ---------------------------------------------------------------------------

def inspect_source_container(machine: dict[str, Any], container: str) -> dict[str, Any]:
    """提取迁移所需的容器配置。容器不存在/未运行返回 error。"""
    code, out, err = run_ssh_machine(
        machine, f"docker inspect {shlex.quote(container)}", timeout=30
    )
    if code != 0 or not out.strip():
        raise RuntimeError(f"docker inspect 失败：{(err or out or '容器不存在').strip()[:300]}")
    info = json.loads(out)[0]
    host_config = info.get("HostConfig") or {}
    return {
        "image": (info.get("Config") or {}).get("Image"),
        "devices": [d.get("PathOnHost") for d in host_config.get("Devices") or []],
        "env": (info.get("Config") or {}).get("Env") or [],
        "binds": host_config.get("Binds") or [],
        "ports": host_config.get("PortBindings") or {},
        "restart": (host_config.get("RestartPolicy") or {}).get("Name") or "no",
        "privileged": bool(host_config.get("Privileged")),
        "network_mode": host_config.get("NetworkMode") or "",
        "tty": bool((info.get("Config") or {}).get("Tty")),
        "status": (info.get("State") or {}).get("Status"),
    }


def parse_visible_devices(value: str) -> int | None:
    """解析 ASCEND_RT_VISIBLE_DEVICES 的值（"0,1,2" / "0-3" / "0"）为卡数。"""
    ids: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            start, _, end = part.partition("-")
            if start.strip().isdigit() and end.strip().isdigit():
                ids.update(range(int(start), int(end) + 1))
        elif part.isdigit():
            ids.add(int(part))
    return len(ids) if ids else None


def extract_npu_devices(devices: list[str], env: list[str]) -> tuple[int, list[str], list[str]]:
    """从设备列表与环境变量提取 NPU 信息。

    返回 (卡数, 共享设备列表, 源容器用的数字卡设备)。
    - 数字卡：/dev/davinci0-7 这类；
    - 共享设备：davinci_manager / hisi_hdc* / devmm_svm（每卡容器都要挂）；
    - 卡数优先取 ASCEND_RT_VISIBLE_DEVICES / ASCEND_VISIBLE_DEVICES 的计数，否则数数字设备。
    """
    cards: list[str] = []
    shared: list[str] = []
    for dev in devices or []:
        name = str(dev).rsplit("/", 1)[-1]
        if name.startswith("davinci") and name[len("davinci"):].isdigit():
            cards.append(dev)
        elif name == "davinci_manager" or name.startswith("hisi_hdc") or name == "devmm_svm":
            shared.append(dev)
    count = len(cards)
    for entry in env or []:
        if entry.startswith(("ASCEND_RT_VISIBLE_DEVICES=", "ASCEND_VISIBLE_DEVICES=")):
            parsed = parse_visible_devices(entry.split("=", 1)[1])
            if parsed:
                count = parsed
    return count, shared, cards


# ---------------------------------------------------------------------------
# 空闲卡获取与目标机选择
# ---------------------------------------------------------------------------

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


def fetch_idle_map() -> dict[str, list[int]]:
    """获取每台机器的空闲卡编号列表。fleet 服务在跑走 API，否则并行探测。"""
    data = _fetch_fleet_servers()
    if data is not None:
        return _idle_map_from_entries(data)
    progress("idle-probe", "fleet 服务未运行，本地并行探测全部机器")
    machines = load_inventory()
    machines = [m for m in machines if m.get("enabled", True)]
    result: dict[str, list[int]] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(probe_remote_npu, m, 45): m for m in machines}
        for future in as_completed(futures):
            machine = futures[future]
            try:
                npu = future.result()
            except Exception:  # noqa: BLE001 - 单机失败跳过
                continue
            ids = [n["id"] for n in npu.get("npus", []) if _is_idle_npu(n)]
            if ids:
                result[machine["host"]] = ids
    return result


def _fetch_fleet_servers() -> dict[str, Any] | None:
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{FLEET_URL}/api/servers", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def _idle_map_from_entries(data: dict[str, Any]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for host, entry in (data.get("machines") or {}).items():
        if not entry.get("reachable"):
            continue
        ids = [
            n["id"]
            for n in (entry.get("npu") or {}).get("npus", [])
            if _is_idle_npu(n)
        ]
        if ids:
            result[host] = ids
    return result


def select_target(
    source_host: str,
    need: int,
    idle_map: dict[str, list[int]],
    prefer: str | None,
) -> tuple[str | None, list[int], list[dict[str, Any]], str]:
    """选择目标机：优先用户指定，否则取空闲卡最多的机器。返回
    (目标host, 分配的物理卡, 备选列表, 说明)。目标不可得时 host 为 None。"""
    candidates = [
        {"host": host, "idle_count": len(ids), "idle_npus": sorted(ids)}
        for host, ids in idle_map.items()
        if host != source_host and len(ids) >= need
    ]
    candidates.sort(key=lambda c: -c["idle_count"])
    if prefer:
        if prefer in idle_map and prefer != source_host:
            if len(idle_map[prefer]) >= need:
                return prefer, sorted(idle_map[prefer])[:need], candidates, "用户指定且空闲充足"
            return (
                None,
                [],
                candidates,
                f"指定的目标机 {prefer} 空闲卡不足（需 {need}，空闲 {len(idle_map[prefer])}）",
            )
        return None, [], candidates, f"指定的目标机 {prefer} 不在清单或不可达"
    if not candidates:
        return None, [], [], f"没有满足条件的机器（需空闲卡 >= {need}，排除源机 {source_host}）"
    best = candidates[0]
    return best["host"], best["idle_npus"][:need], candidates, "自动选择空闲卡最多的机器"


# ---------------------------------------------------------------------------
# 迁移步骤
# ---------------------------------------------------------------------------

def commit_container(machine: dict[str, Any], container: str, image: str) -> None:
    progress("commit", f"提交源容器为镜像 {image}")
    code, out, err = run_ssh_machine(
        machine, f"docker commit {shlex.quote(container)} {shlex.quote(image)}", timeout=600
    )
    if code != 0:
        raise RuntimeError(f"docker commit 失败：{(err or out).strip()[:300]}")


def image_exists_on(machine: dict[str, Any], image: str) -> bool:
    """检查目标机上是否已有该镜像（多目标/重试场景跳过重复传输）。"""
    code, _out, _err = run_ssh_machine(
        machine, f"docker image inspect {shlex.quote(image)}", timeout=30
    )
    return code == 0


def transfer_image(
    source: dict[str, Any], target: dict[str, Any], image: str
) -> None:
    """源机直传目标机：docker save | gzip | ssh 目标 docker load（不经本地）。"""
    progress("transfer", f"镜像直传 {source['host']} -> {target['host']}（gzip -1，大镜像耗时较长）")
    command = (
        f"docker save {shlex.quote(image)} | gzip -1 | "
        f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        f"{target['user']}@{target['host']} 'gzip -d | docker load'"
    )
    code, out, err = run_ssh_machine(source, command, timeout=TRANSFER_TIMEOUT_SECONDS)
    if code != 0:
        raise RuntimeError(f"镜像传输失败：{(err or out).strip()[:300]}")


# ---------------------------------------------------------------------------
# 模型权重检查与同步
# ---------------------------------------------------------------------------

def path_bytes(machine: dict[str, Any], path: str) -> int | None:
    """远端路径的总字节数（du -sb）。路径不存在返回 None。"""
    code, out, _err = run_ssh_machine(
        machine,
        f"test -e {shlex.quote(path)} && du -sb {shlex.quote(path)}",
        timeout=180,
    )
    if code != 0:
        return None
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return None


def weight_status(source_bytes: int | None, target_bytes: int | None) -> str:
    """判定目标机权重状态。纯函数（单测覆盖）。

    - missing：目标不存在
    - partial：目标比源小超过 1%（可能传输中断或版本不一致）
    - ok：大小一致（du 粒度下的近似判断）
    """
    if target_bytes is None:
        return "missing"
    if source_bytes and target_bytes < source_bytes * 0.99:
        return "partial"
    return "ok"


def check_weights(
    source: dict[str, Any], target: dict[str, Any], paths: list[str]
) -> list[dict[str, Any]]:
    """检查目标机上的模型权重。返回每个路径的状态报告。"""
    report: list[dict[str, Any]] = []
    for path in paths:
        source_bytes = path_bytes(source, path)
        target_bytes = path_bytes(target, path)
        report.append(
            {
                "path": path,
                "source_bytes": source_bytes,
                "target_bytes": target_bytes,
                "status": weight_status(source_bytes, target_bytes),
            }
        )
    return report


def sync_weights(source: dict[str, Any], target: dict[str, Any], path: str) -> None:
    """把权重目录从源机 rsync 到目标机相同路径。权重巨大，优先 rsync 断点续传。"""
    progress("sync-weights", f"同步权重 {path} -> {target['host']}:{path}（大目录，耗时较长）")
    command = (
        f"rsync -a --partial --info=progress2 {shlex.quote(path + '/')} "
        f"{target['user']}@{target['host']}:{shlex.quote(path + '/')}"
    )
    code, out, err = run_ssh_machine(source, command, timeout=TRANSFER_TIMEOUT_SECONDS)
    if code != 0:
        # rsync 不可用降级 tar（无断点续传，失败需整体重来，报告中提示）
        progress("sync-weights", "rsync 不可用，降级 tar 管道（无断点续传）")
        normalized = path.rstrip("/")
        parent, _, name = normalized.rpartition("/")
        command = (
            f"tar -C {shlex.quote(parent)} -cf - {shlex.quote(name)} | "
            f"ssh -o BatchMode=yes {target['user']}@{target['host']} "
            f"'mkdir -p {shlex.quote(normalized)} && tar -C {shlex.quote(parent)} -xf -'"
        )
        code, out, err = run_ssh_machine(source, command, timeout=TRANSFER_TIMEOUT_SECONDS)
        if code != 0:
            raise RuntimeError(f"权重同步失败：{(err or out).strip()[:300]}")


def sync_code(source: dict[str, Any], target: dict[str, Any], code_path: str) -> None:
    """rsync 代码到目标机相同绝对路径（路径不变，容器挂载参数无需修改）。"""
    progress("sync-code", f"同步代码 {code_path} -> {target['host']}:{code_path}")
    command = (
        f"rsync -a {shlex.quote(code_path + '/')} "
        f"{target['user']}@{target['host']}:{shlex.quote(code_path + '/')}"
    )
    code, out, err = run_ssh_machine(source, command, timeout=1800)
    if code != 0:
        # rsync 不存在时降级为 tar 管道。注意：这里必须用纯字符串拆路径，
        # Windows 上 pathlib 会把 Linux 路径规范化成反斜杠形态发给远端 tar。
        normalized = code_path.rstrip("/")
        parent, _, name = normalized.rpartition("/")
        progress("sync-code", "rsync 不可用，降级 tar 管道")
        command = (
            f"tar -C {shlex.quote(parent)} -cf - {shlex.quote(name)} | "
            f"ssh -o BatchMode=yes {target['user']}@{target['host']} "
            f"'mkdir -p {shlex.quote(normalized)} && tar -C {shlex.quote(parent)} -xf -'"
        )
        code, out, err = run_ssh_machine(source, command, timeout=1800)
        if code != 0:
            raise RuntimeError(f"代码同步失败：{(err or out).strip()[:300]}")


def build_run_command(
    image: str,
    info: dict[str, Any],
    physical_cards: list[int],
    name: str,
) -> str:
    """构造目标机上的 docker run 命令：复刻源容器参数 + 新卡映射。纯函数（单测覆盖）。"""
    parts = ["docker run -d", f"--name {shlex.quote(name)}", f"--restart {info['restart']}"]
    if info["privileged"]:
        parts.append("--privileged")
    if info.get("tty"):
        # 交互式 bash 容器（如 VSCode 开发容器）需要 tty 保活，否则 -d 起的 bash 立即退出
        parts.append("-t")
    if info["network_mode"] and info["network_mode"] not in ("default", ""):
        parts.append(f"--network {info['network_mode']}")
    # 共享设备原样保留（davinci_manager / hisi_hdc / devmm_svm）
    for dev in info["shared_devices"]:
        parts.append(f"--device {shlex.quote(dev)}")
    # 数字卡映射到目标机的物理卡；容器内可见编号重排为 0..N-1。
    # privileged 且源容器未挂卡设备时不做任何卡映射（复刻"可见全部卡"的语义）。
    for card in physical_cards:
        parts.append(f"--device /dev/davinci{card}")
    if physical_cards:
        visible = ",".join(str(i) for i in range(len(physical_cards)))
        parts.append(f"-e ASCEND_RT_VISIBLE_DEVICES={visible}")
    for entry in info["env"]:
        if entry.startswith(("ASCEND_RT_VISIBLE_DEVICES=", "ASCEND_VISIBLE_DEVICES=")):
            continue  # 已用重排后的新值替换
        parts.append(f"-e {shlex.quote(entry)}")
    for bind in info["binds"]:
        parts.append(f"-v {shlex.quote(bind)}")
    for container_port, bindings in (info["ports"] or {}).items():
        port_number = container_port.split("/")[0]
        for binding in bindings or []:
            parts.append(f"-p {binding.get('HostPort', '')}:{port_number}")
    parts.append(shlex.quote(image))
    return " ".join(parts)


def run_target_container(
    target: dict[str, Any], command: str, name: str
) -> None:
    progress("run", f"在 {target['host']} 上启动容器 {name}")
    code, out, err = run_ssh_machine(target, command, timeout=120)
    if code != 0:
        # 清理半成品容器后报错（镜像保留，可重试）
        run_ssh_machine(target, f"docker rm -f {shlex.quote(name)} 2>/dev/null || true", timeout=30)
        raise RuntimeError(
            f"容器启动失败（常见原因：端口冲突/挂载源不存在）：{(err or out).strip()[:300]}"
        )


def start_service(target: dict[str, Any], container: str, script: str) -> None:
    """容器内后台执行启动脚本，日志重定向 /tmp/migrate-startup.log。"""
    progress("start", f"容器内执行启动脚本 {script}")
    command = (
        f"docker exec {shlex.quote(container)} bash -c "
        f"{shlex.quote(f'nohup bash {script} > /tmp/migrate-startup.log 2>&1 & echo started')}"
    )
    code, out, err = run_ssh_machine(target, command, timeout=60)
    if code != 0:
        raise RuntimeError(f"启动脚本执行失败：{(err or out).strip()[:300]}")


def verify_startup(target: dict[str, Any], container: str) -> dict[str, Any]:
    """验证：容器 running + 启动日志无立即崩溃。宽限等待由调用方控制。"""
    code, out, _ = run_ssh_machine(
        target,
        f"docker inspect -f '{{{{.State.Status}}}}' {shlex.quote(container)}",
        timeout=30,
    )
    status = out.strip() if code == 0 else "unknown"
    _code, log, _err = run_ssh_machine(
        target,
        f"docker exec {shlex.quote(container)} sh -c 'tail -c 2000 /tmp/migrate-startup.log 2>/dev/null || echo \"(no log)\"'",
        timeout=30,
    )
    return {"container_status": status, "startup_log_tail": (log or "").strip()[-1500:]}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="迁移容器 + 代码到有空闲 NPU 卡的服务器并拉起服务"
    )
    parser.add_argument("--source", required=True, help="源服务器（alias 或 IP，须在清单中）")
    parser.add_argument("--container", required=True, help="源容器名")
    parser.add_argument("--code-path", required=True, help="要同步的代码文件夹（源机上的绝对路径）")
    parser.add_argument("--script", help="服务启动脚本（容器内路径）；开发容器可不给，跳过启动")
    parser.add_argument("--target", help="目标服务器（不指定则自动选择空闲最多的机器）")
    parser.add_argument("--image", help="复用已 commit 的迁移镜像（多目标迁移时避免重复 commit）")
    parser.add_argument(
        "--weights-path",
        action="append",
        help="服务依赖的模型权重路径（源机绝对路径，可传多次）；迁移前检查目标机是否存在",
    )
    parser.add_argument(
        "--sync-weights",
        action="store_true",
        help="目标机权重缺失/不完整时自动从源机同步（大目录耗时较长）",
    )
    parser.add_argument("--npus", type=int, help="覆盖自动提取的卡数")
    parser.add_argument("--stop-source", action="store_true", help="迁移成功后停止源容器（默认保留）")
    parser.add_argument("--plan", action="store_true", help="干跑：输出迁移计划与将执行的命令，不实际迁移")
    args = parser.parse_args()

    progress("start", f"migrate {args.container} from {args.source}")

    # 1. 解析源机
    found = find_machine(args.source)
    if not found:
        return emit(
            {"ok": False, "action": "migrate", "status": "blocked",
             "error": f"源机器 {args.source} 不在清单中，先添加服务器"}
        )
    _index, source = found

    # 2. 检查源容器
    try:
        progress("inspect", f"检查源容器 {args.container}")
        info = inspect_source_container(source, args.container)
    except Exception as exc:  # noqa: BLE001
        return emit(
            {"ok": False, "action": "migrate", "status": "blocked", "error": str(exc)}
        )
    npu_count, shared_devices, source_cards = extract_npu_devices(info["devices"], info["env"])
    info["shared_devices"] = shared_devices
    if args.npus:
        npu_count = args.npus
    # privileged 且源容器没挂卡设备：完全复刻（可见全部卡），仅按"至少 1 张空闲卡"选机
    full_visibility = info["privileged"] and npu_count <= 0
    if full_visibility:
        npu_count = 1
    if npu_count <= 0:
        return emit(
            {"ok": False, "action": "migrate", "status": "needs_input",
             "error": "未能从容器提取到 NPU 卡数（设备与环境变量都没有），请用 --npus 指定"}
        )

    # 3. 选目标机
    idle_map = fetch_idle_map()
    target_host, cards, candidates, note = select_target(
        source["host"], npu_count, idle_map, args.target
    )
    if target_host is None:
        return emit(
            {"ok": False, "action": "migrate", "status": "needs_input",
             "error": f"无法选择目标机：{note}",
             "candidates": candidates,
             "hint": "查看备选列表，或稍后重试（空闲状态会变化）"}
        )
    target = find_machine(target_host)[1]
    if full_visibility:
        # privileged 全卡可见容器：不做卡重映射，完全复刻源容器
        cards = []

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    image = args.image or f"migrate/{args.container}:{timestamp}"
    card_map = [{"container_index": i, "physical_card": c} for i, c in enumerate(cards)]
    plan = {
        "source": {"host": source["host"], "container": args.container, "status": info["status"]},
        "target": {"host": target_host, "alias": target.get("alias")},
        "image": image,
        "npus_needed": npu_count,
        "card_map": card_map,
        "code_path": args.code_path,
        "script": args.script,
        "alternative_candidates": [c for c in candidates if c["host"] != target_host][:5],
        "selection_note": note,
    }

    run_command = build_run_command(image, info, cards, args.container)
    plan["commands"] = {
        "commit": f"docker commit {args.container} {image}",
        "transfer": f"docker save {image} | gzip -1 | ssh {target['user']}@{target_host} 'gzip -d | docker load'",
        "sync_code": f"rsync -a {args.code_path}/ {target['user']}@{target_host}:{args.code_path}/",
        "run": run_command,
        "start": (
            f"docker exec {args.container} bash -c 'nohup bash {args.script} > /tmp/migrate-startup.log 2>&1 &'"
            if args.script
            else "（未指定启动脚本，跳过服务启动）"
        ),
    }

    if args.plan:
        if args.weights_path:
            progress("weights", "检查目标机模型权重（只读）")
            plan["weights"] = check_weights(source, target, args.weights_path)
        return emit(
            {"ok": True, "action": "migrate", "status": "migrated", "plan": plan,
             "note": "--plan 干跑模式：未执行任何变更"}
        )

    # 4. 执行迁移
    try:
        if args.image:
            progress("commit", f"复用已有镜像 {image}（跳过 commit）")
        else:
            commit_container(source, args.container, image)
        if image_exists_on(target, image):
            progress("transfer", f"目标机已有镜像 {image}，跳过传输")
        else:
            transfer_image(source, target, image)
        sync_code(source, target, args.code_path)

        # 4.5 模型权重检查（起容器前止损：服务起不来最常见的坑就是目标机没权重）
        weights_report: list[dict[str, Any]] | None = None
        if args.weights_path:
            progress("weights", "检查目标机模型权重")
            weights_report = check_weights(source, target, args.weights_path)
            incomplete = [w for w in weights_report if w["status"] != "ok"]
            if incomplete:
                if not args.sync_weights:
                    return emit(
                        {"ok": False, "action": "migrate", "status": "needs_input",
                         "error": "目标机缺少或权重不完整：" + "; ".join(
                             f"{w['path']}({w['status']})" for w in incomplete),
                         "weights": weights_report,
                         "hint": "加 --sync-weights 自动从源机同步，或自行在目标机准备后重跑（幂等）"}
                    )
                for w in incomplete:
                    sync_weights(source, target, w["path"])
                weights_report = check_weights(source, target, args.weights_path)
                still = [w for w in weights_report if w["status"] != "ok"]
                if still:
                    return emit(
                        {"ok": False, "action": "migrate", "status": "failed",
                         "error": "权重同步后仍不完整：" + "; ".join(
                             f"{w['path']}({w['status']})" for w in still),
                         "weights": weights_report}
                    )

        run_target_container(target, run_command, args.container)
        if args.script:
            start_service(target, args.container, args.script)
    except Exception as exc:  # noqa: BLE001
        return emit(
            {"ok": False, "action": "migrate", "status": "failed",
             "plan": plan, "error": str(exc),
             "hint": "源容器未动，回滚方式：继续用源机服务，或修复问题后重跑迁移（幂等）"}
        )

    # 5. 验证（宽限等待启动）
    progress("verify", f"等待服务启动（宽限 {STARTUP_GRACE_SECONDS}s）")
    time.sleep(min(STARTUP_GRACE_SECONDS, 15))
    verification = verify_startup(target, args.container)

    # 6. 可选：停源容器
    stopped_source = False
    if args.stop_source and verification["container_status"] == "running":
        progress("stop-source", "停止源容器（用户指定 --stop-source）")
        run_ssh_machine(source, f"docker stop {shlex.quote(args.container)}", timeout=60)
        stopped_source = True

    # 挂载里代码路径之外的目录提醒
    external_mounts = [
        bind for bind in info["binds"]
        if not str(bind).startswith(str(args.code_path))
    ]

    return emit(
        {
            "ok": verification["container_status"] == "running",
            "action": "migrate",
            "status": "migrated" if verification["container_status"] == "running" else "failed",
            "plan": plan,
            "verification": verification,
            "weights": weights_report,
            "source_container_stopped": stopped_source,
            "external_mounts_warning": {
                "note": "以下挂载路径未自动同步（仅同步了 --code-path），服务读取失败时需手动处理",
                "mounts": external_mounts,
            }
            if external_mounts
            else None,
            "rollback": "源容器未修改，可直接回源机重启服务回滚",
            "cleanup_hint": f"确认稳定后可清理：源机 docker rmi {image}",
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
