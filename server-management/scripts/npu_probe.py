#!/usr/bin/env python3
"""npu-smi 输出解析与远程 NPU 状态采集。

支持两种真实格式（fixture 单测见 tests/）：
- 24.x：每卡两行（概要 + device），device 行首列是 NPU 编号、Name 列为 "NA"；
- 26.x：每卡四个物理行（每 chip 一组概要+device），device 行首列是 chip/Phy-ID，
  功耗在 chip1 概要行为 "-"，第三列含 DDR Memory-Usage 与 HBM-Usage 两组数字对，
  输出末尾附进程表。

解析策略（宽容优先，坏行跳过不中断）：
- 概要行与紧随的 device 行配对成一个 chip 记录，按概要行的 NPU 编号聚合到卡；
- bus-id 必须是严格 "0000:9D:00.0" 格式才认 device 行，避免把概要行/进程行误认；
- 显存取每组数字对的最后一组（24.x 是唯一一组，26.x 是 HBM）；
- 聚合语义：功耗取首个非空（chip0），温度/AICore 取各 chip 最大值（保守），
  健康状态任一 chip 异常即报异常。
"""

from __future__ import annotations

import re
from typing import Any

from common import run_ssh_machine

# 合并采集命令：一次 SSH 往返拿到负载、CPU、内存、磁盘挂载、Docker 容器状态。
# 用 __SECTION__ 分隔符切分，避免输出歧义；docker 不可用时输出 unavailable 不算错误。
# CPU 利用率取 /proc/stat 两次采样（间隔 1s），低频采集（5 分钟一次）下这点延迟可接受。
EXTRA_PROBE_COMMAND = (
    "echo \"__LOAD__$(cat /proc/loadavg 2>/dev/null)\"; "
    "echo \"__MEM__$(grep -E '^(MemTotal|MemAvailable):' /proc/meminfo 2>/dev/null | tr '\\n' ' ')\"; "
    "echo __CPU__; grep '^cpu ' /proc/stat; sleep 1; grep '^cpu ' /proc/stat; "
    "echo __DF__; df -h -P -x tmpfs -x devtmpfs 2>/dev/null; "
    "echo __DOCKER__; "
    "(docker ps --format '{{.Names}}\\t{{.Status}}\\t{{.Image}}' 2>/dev/null || echo __UNAVAILABLE__)"
)

# 概要行：| 0     Ascend910  | OK  | 204.0   43   0/0 |（power/temp 可能为 "-"）
# name 兼容数字开头（910B3）与字母开头（Ascend910）两种命名
_SUMMARY_RE = re.compile(
    r"^\|\s*(?P<id>\d+)\s+(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]*)\s*\|\s*(?P<health>\S+)\s*\|"
    r"\s*(?P<power>-|\d+\.?\d*)\s+(?P<temp>-|\d+\.?\d*)\s"
)
# device 行：col2 必须是严格 bus-id 格式（含冒号点号），这是与概要行/进程行的关键区分
_DEVICE_RE = re.compile(
    r"^\|\s*(?P<chip>\d+)\s+(?P<phy>\d+|NA)\s*\|"
    r"\s*(?P<bus>[\da-fA-F]{4}:[\da-fA-F]{2}:[\da-fA-F]{2}\.[\da-fA-F])\s*\|"
    r"\s*(?P<aicore>-|\d+)\s+(?P<rest>[\d\s/]+)\|"
)
# 进程行：| 0     0  | 1774110  | VLLMWorker_PP  | 56684  | NA |
_PROCESS_RE = re.compile(
    r"^\|\s*(?P<npu>\d+)\s+(?P<chip>\d+)\s+\|\s*(?P<pid>\d+)\s+\|\s*(?P<name>\S+)\s+\|"
    r"\s*(?P<mem>\d+)\s"
)
# 数字对，如 "59807/ 65536" 或 "0    / 0"
_PAIR_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

# 芯片名 -> 集群口径的机器类型（仅元数据用途，未识别时为 unknown）
_NAME_TYPE_MAP = (
    ("310P", "310P"),
    ("910B", "910B"),
    ("910C", "910C"),
    ("910A", "910A"),
    ("910", "910"),
)


def infer_machine_type(name: str) -> str:
    """从 NPU 名称推断机器类型，未识别返回 unknown。"""
    upper = name.upper()
    for token, machine_type in _NAME_TYPE_MAP:
        if token in upper:
            return machine_type
    return "unknown"


def _to_float(value: str | None) -> float | None:
    if value is None or value == "-":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    if value is None or value == "-":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _new_card(npu_id: int) -> dict[str, Any]:
    return {
        "id": npu_id,
        "name": None,
        "health": None,
        "power_w": None,
        "temp_c": None,
        "bus_id": None,
        "aicore_util": None,
        "mem_used_mb": None,
        "mem_total_mb": None,
        "chips": [],
        "processes": [],
    }


def _merge_chip(card: dict[str, Any], chip: dict[str, Any]) -> None:
    """把一个 chip 记录聚合进卡级视图（保守语义）。"""
    card["chips"].append(chip)
    if card["name"] is None:
        card["name"] = chip["name"]
    # 健康状态：任一 chip 异常即整体异常
    if card["health"] in (None, "OK") and chip["health"] not in (None, "OK"):
        card["health"] = chip["health"]
    elif card["health"] is None:
        card["health"] = chip["health"]
    # 功耗只有 chip0 上报（chip1 为 "-"），取首个非空
    if card["power_w"] is None:
        card["power_w"] = chip["power_w"]
    # 温度与 AICore 取各 chip 最大值（保守：任一 die 忙则卡视为忙）
    for field in ("temp_c", "aicore_util"):
        values = [c[field] for c in card["chips"] if c[field] is not None]
        card[field] = max(values) if values else None
    # bus 取第一个 chip 的；显存取各 chip 的最大占用（同一分母）
    if card["bus_id"] is None:
        card["bus_id"] = chip["bus_id"]
    if chip["mem_used_mb"] is not None:
        card["mem_used_mb"] = max(card["mem_used_mb"] or 0, chip["mem_used_mb"])
        card["mem_total_mb"] = chip["mem_total_mb"]


def parse_npu_smi_output(text: str) -> dict[str, Any]:
    """把 npu-smi info 的文本输出解析成结构化数据。"""
    npus: dict[int, dict[str, Any]] = {}
    pending_summary: dict[str, Any] | None = None
    in_process_section = False

    def card(npu_id: int) -> dict[str, Any]:
        if npu_id not in npus:
            npus[npu_id] = _new_card(npu_id)
        return npus[npu_id]

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue

        # 进程表区域切换（26.x 特有）
        if "Process id" in line:
            in_process_section = True
            pending_summary = None
            continue

        if in_process_section:
            matched = _PROCESS_RE.match(line)
            if matched:
                entry = card(int(matched.group("npu")))
                entry["processes"].append(
                    {
                        "pid": int(matched.group("pid")),
                        "name": matched.group("name"),
                        "memory_mb": int(matched.group("mem")),
                    }
                )
            continue

        # device 行：bus-id 严格格式 + 只在等待配对时接受
        matched = _DEVICE_RE.match(line)
        if matched and pending_summary is not None:
            pairs = _PAIR_RE.findall(matched.group("rest"))
            hbm_used, hbm_total = (int(pairs[-1][0]), int(pairs[-1][1])) if pairs else (None, None)
            chip = {
                "name": pending_summary["name"],
                "health": pending_summary["health"],
                "power_w": pending_summary["power_w"],
                "temp_c": pending_summary["temp_c"],
                "bus_id": matched.group("bus"),
                "aicore_util": _to_int(matched.group("aicore")),
                "mem_used_mb": hbm_used,
                "mem_total_mb": hbm_total,
            }
            _merge_chip(card(pending_summary["id"]), chip)
            pending_summary = None
            continue

        # 概要行：暂存等待下一行配对
        matched = _SUMMARY_RE.match(line)
        if matched:
            pending_summary = {
                "id": int(matched.group("id")),
                "name": matched.group("name"),
                "health": matched.group("health"),
                "power_w": _to_float(matched.group("power")),
                "temp_c": _to_float(matched.group("temp")),
            }
            continue
        # 其余行（表头、分隔线、未知格式）静默跳过

    return {
        "npu_count": len(npus),
        "npus": [npus[key] for key in sorted(npus)],
    }


def probe_remote_npu(machine: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """在远程机器上采集 npu-smi 输出并解析。机器没有 npu-smi 时返回 npu_count=0。"""
    try:
        code, out, _err = run_ssh_machine(machine, "npu-smi info 2>/dev/null || true", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 网络错误统一收敛为字段
        return {"npu_count": 0, "npus": [], "error": f"probe failed: {exc}"}
    if code != 0 or not out.strip():
        return {"npu_count": 0, "npus": [], "error": "npu-smi unavailable"}
    return parse_npu_smi_output(out)


# ---------------------------------------------------------------------------
# 机器级采集：负载、磁盘挂载、Docker 容器。解析器均为纯函数（见 tests）。
# ---------------------------------------------------------------------------

def _parse_size_gb(size: str) -> float | None:
    """把 df 的容量字符串（如 3.5T、986G、512M）折算成 GB。"""
    match = re.match(r"^([\d.]+)([KMGTPE]?)", size.strip())
    if not match:
        return None
    value = float(match.group(1))
    factor = {"": 1.0 / 1024 / 1024 / 1024, "K": 1.0 / 1024 / 1024, "M": 1.0 / 1024, "G": 1.0, "T": 1024.0, "P": 1024.0 * 1024.0, "E": 1024.0 ** 3}
    return round(value * factor[match.group(2)], 2)


def parse_loadavg(text: str) -> float | None:
    """从 /proc/loadavg 内容取 1 分钟负载。"""
    parts = text.split()
    if not parts:
        return None
    try:
        return round(float(parts[0]), 2)
    except ValueError:
        return None


def parse_meminfo(text: str) -> dict[str, int]:
    """解析 MemTotal/MemAvailable 行（kB），返回字节数。"""
    result = {"memory_total_bytes": None, "memory_available_bytes": None}
    for key, field in (("MemTotal", "memory_total_bytes"), ("MemAvailable", "memory_available_bytes")):
        match = re.search(rf"{key}:\s+(\d+)\s*kB", text)
        if match:
            result[field] = int(match.group(1)) * 1024
    return result


def parse_cpu_percent(text: str) -> float | None:
    """解析 /proc/stat 两次 cpu 行采样，计算采样间隔内的 CPU 利用率百分比。"""
    lines = [line for line in text.splitlines() if line.startswith("cpu ")]
    if len(lines) < 2:
        return None

    def fields(line: str) -> list[int]:
        return [int(v) for v in line.split()[1:] if v.lstrip("-").isdigit()]

    first, second = fields(lines[0]), fields(lines[1])
    if len(first) < 5 or len(second) < 5 or len(first) != len(second):
        return None
    delta_total = sum(second) - sum(first)
    delta_idle = second[3] + second[4] - first[3] - first[4]  # idle + iowait
    if delta_total <= 0:
        return None
    return round(max(0.0, min(100.0, (1 - delta_idle / delta_total) * 100)), 1)


def parse_df(text: str) -> list[dict[str, Any]]:
    """解析 df -h -P 输出为磁盘挂载列表。坏行跳过。"""
    disks: list[dict[str, Any]] = []
    for line in text.splitlines():
        fields = line.split()
        # Filesystem Size Used Avail Use% Mounted-on（-P 保证一行 6 列）
        if len(fields) != 6 or not fields[4].endswith("%"):
            continue
        if fields[0] in ("Filesystem", "map:"):
            continue
        try:
            disks.append(
                {
                    "filesystem": fields[0],
                    "mount": fields[5],
                    "total_gb": _parse_size_gb(fields[1]),
                    "used_gb": _parse_size_gb(fields[2]),
                    "use_pct": int(fields[4].rstrip("%")),
                }
            )
        except ValueError:
            continue
    return disks


def parse_docker(text: str) -> list[dict[str, str]] | None:
    """解析 docker ps 的 tab 分隔输出。__UNAVAILABLE__ 或空输出返回 None（不算错误）。"""
    text = text.strip()
    if not text or "__UNAVAILABLE__" in text or text == "unavailable":
        return None
    containers: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        containers.append({"name": parts[0], "status": parts[1], "image": parts[2]})
    return containers


def probe_machine_extras(machine: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """一次 SSH 采集负载、CPU、内存、磁盘挂载、Docker 容器。失败返回空字段而非抛异常。"""
    try:
        code, out, _err = run_ssh_machine(machine, EXTRA_PROBE_COMMAND, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"load1": None, "disks": [], "docker": None, "error": str(exc)}
    if code != 0:
        return {"load1": None, "disks": [], "docker": None, "error": f"exit {code}"}

    sections: dict[str, str] = {}
    current = "load"
    for line in out.splitlines():
        if line.startswith("__LOAD__"):
            current = "load"
            sections[current] = line[len("__LOAD__"):]
        elif line.startswith("__MEM__"):
            current = "mem"
            sections[current] = line[len("__MEM__"):]
        elif line.strip() == "__CPU__":
            current = "cpu"
            sections[current] = ""
        elif line.strip() == "__DF__":
            current = "df"
            sections[current] = ""
        elif line.strip() == "__DOCKER__":
            current = "docker"
            sections[current] = ""
        else:
            sections[current] = sections.get(current, "") + line + "\n"

    result: dict[str, Any] = {
        "load1": parse_loadavg(sections.get("load", "")),
        "disks": parse_df(sections.get("df", "")),
        "docker": parse_docker(sections.get("docker", "")),
    }
    result.update(parse_meminfo(sections.get("mem", "")))
    result["cpu_percent"] = parse_cpu_percent(sections.get("cpu", ""))
    return result


def summarize_fleet(probed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把按机器组织的探测结果汇总成集群口径统计。"""
    total_npus = 0
    healthy_npus = 0
    reachable = 0
    for entry in probed.values():
        if entry.get("reachable"):
            reachable += 1
        npu_info = entry.get("npu") or {}
        total_npus += int(npu_info.get("npu_count", 0))
        healthy_npus += sum(1 for n in npu_info.get("npus", []) if n.get("health") == "OK")
    return {
        "machines_total": len(probed),
        "machines_reachable": reachable,
        "npu_total": total_npus,
        "npu_healthy": healthy_npus,
    }
