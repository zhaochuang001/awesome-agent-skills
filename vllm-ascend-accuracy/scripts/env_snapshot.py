#!/usr/bin/env python3
"""环境快照：一键冻结精度诊断所需的完整环境信息，输出可存档、可对比的 JSON。

自包含脚本，无外部 skill 依赖——SSH 直连（--host/--user/--port），
npu-smi 双格式解析器内置（24.x 单 chip / 26.x 双 chip + HBM + 进程表）。

用途（对应 SKILL.md「解决闭环」第 2 步"冻结环境"）：
- 版本配套矩阵一次采全（vLLM/vllm-ascend/CANN/torch_npu/driver/卡状态）；
- 诊断前后各拍一次，diff 能发现"环境被谁动过"（环境漂移是复现失效的头号原因）；
- 抓运行中 vllm 进程的真实环境变量与库加载计数（CANN 版本切换是否生效的铁证）。

前置条件：本机能以密钥 SSH 到目标机器（BatchMode 非交互，不弹密码）。

输出协议：stderr __SM_PROGRESS__ 进度，stdout 单个最终 JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Windows 上从无控制台进程调 ssh.exe 会闪黑框，CREATE_NO_WINDOW 抑制；POSIX 置 0
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def progress(phase: str, detail: str = "") -> None:
    """stderr 输出阶段进度；stdout 留给最终 JSON。"""
    event = {"phase": phase, "ts": round(time.time(), 3)}
    if detail:
        event["detail"] = detail
    print(f"__SM_PROGRESS__={json.dumps(event, ensure_ascii=False)}", file=sys.stderr, flush=True)


def emit(payload: dict[str, Any]) -> int:
    """打印最终结果 JSON 并返回退出码。"""
    payload.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


def run_ssh(host: str, port: int, user: str, command: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """非交互 SSH 执行（BatchMode 杜绝密码提示；accept-new 首连自动记录指纹）。"""
    argv = [
        "ssh", "-p", str(port),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}", command,
    ]
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
        check=False, creationflags=_NO_WINDOW,
    )
    return result.returncode, result.stdout, result.stderr


def _run(host: str, port: int, user: str, cmd: str, timeout: float = 60.0) -> str:
    code, out, _err = run_ssh(host, port, user, cmd, timeout=timeout)
    return out.strip() if code == 0 else ""


# ---------------------------------------------------------------------------
# npu-smi 解析器（内置，与 server-management 同源；支持 24.x / 26.x 双格式）
# ---------------------------------------------------------------------------

# 概要行：| 0     Ascend910  | OK  | 204.0   43   0/0 |（power/temp 可能为 "-"）
_SUMMARY_RE = re.compile(
    r"^\|\s*(?P<id>\d+)\s+(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-]*)\s*\|\s*(?P<health>\S+)\s*\|"
    r"\s*(?P<power>-|\d+\.?\d*)\s+(?P<temp>-|\d+\.?\d*)\s"
)
# device 行：col2 必须是严格 bus-id 格式（与概要行/进程行的关键区分）
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
_PAIR_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def parse_npu_smi_output(text: str) -> dict[str, Any]:
    """把 npu-smi info 输出解析成结构化数据（卡级聚合，26.x 附进程表）。"""
    npus: dict[int, dict[str, Any]] = {}
    pending_summary: dict[str, Any] | None = None
    in_process_section = False

    def card(npu_id: int) -> dict[str, Any]:
        if npu_id not in npus:
            npus[npu_id] = {
                "id": npu_id, "name": None, "health": None, "power_w": None,
                "temp_c": None, "bus_id": None, "aicore_util": None,
                "mem_used_mb": None, "mem_total_mb": None, "processes": [],
            }
        return npus[npu_id]

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        if "Process id" in line:  # 26.x 进程表区域切换
            in_process_section = True
            pending_summary = None
            continue
        if in_process_section:
            matched = _PROCESS_RE.match(line)
            if matched:
                entry = card(int(matched.group("npu")))
                entry["processes"].append(
                    {"pid": int(matched.group("pid")), "name": matched.group("name"),
                     "memory_mb": int(matched.group("mem"))}
                )
            continue
        matched = _DEVICE_RE.match(line)
        if matched and pending_summary is not None:
            pairs = _PAIR_RE.findall(matched.group("rest"))
            hbm_used, hbm_total = (int(pairs[-1][0]), int(pairs[-1][1])) if pairs else (None, None)
            chip = {
                "name": pending_summary["name"], "health": pending_summary["health"],
                "power_w": pending_summary["power_w"], "temp_c": pending_summary["temp_c"],
                "bus_id": matched.group("bus"),
                "aicore_util": None if matched.group("aicore") == "-" else int(matched.group("aicore")),
                "mem_used_mb": hbm_used, "mem_total_mb": hbm_total,
            }
            entry = card(pending_summary["id"])
            # 卡级聚合：功耗取首个非空、温度/AICore 取各 chip 最大（保守）、显存取最大占用
            if entry["name"] is None:
                entry["name"] = chip["name"]
            if entry["health"] in (None, "OK") and chip["health"] not in (None, "OK"):
                entry["health"] = chip["health"]
            elif entry["health"] is None:
                entry["health"] = chip["health"]
            if entry["power_w"] is None:
                entry["power_w"] = chip["power_w"]
            for field in ("temp_c", "aicore_util"):
                values = [c.get(field) for c in [chip] if c.get(field) is not None]
                if values and (entry[field] is None or entry[field] < max(values)):
                    entry[field] = max(values) if values else entry[field]
            if chip["mem_used_mb"] is not None:
                entry["mem_used_mb"] = max(entry["mem_used_mb"] or 0, chip["mem_used_mb"])
                entry["mem_total_mb"] = chip["mem_total_mb"]
            if entry["bus_id"] is None:
                entry["bus_id"] = chip["bus_id"]
            pending_summary = None
            continue
        matched = _SUMMARY_RE.match(line)
        if matched:
            pending_summary = {
                "id": int(matched.group("id")),
                "name": matched.group("name"),
                "health": matched.group("health"),
                "power_w": None if matched.group("power") == "-" else float(matched.group("power")),
                "temp_c": None if matched.group("temp") == "-" else float(matched.group("temp")),
            }
    return {"npu_count": len(npus), "npus": [npus[k] for k in sorted(npus)]}


# ---------------------------------------------------------------------------
# 快照采集
# ---------------------------------------------------------------------------

# 容器内要采集的 pip 包（存在才记录）
PACKAGES = [
    "vllm", "vllm-ascend", "torch", "torch-npu", "torch_npu",
    "npu-accelerate", "transformers", "accelerate", "msprobe",
]
# 容器环境变量中与昇腾运行时相关的关键项
ENV_KEYS = [
    "ASCEND_TOOLKIT_HOME", "ATB_HOME_PATH", "ASCEND_RT_VISIBLE_DEVICES",
    "ASCEND_VISIBLE_DEVICES", "LD_LIBRARY_PATH", "CANN_HOME",
]


def snapshot_host(host: str, port: int, user: str) -> dict[str, Any]:
    """宿主机层：系统、driver、NPU 状态。"""
    progress("host", f"采集宿主机 {host} 系统与 driver 信息")
    info: dict[str, Any] = {
        "host": host, "user": user,
        "kernel": _run(host, port, user, "uname -r"),
        "hostname": _run(host, port, user, "hostname"),
    }
    progress("host", "采集 npu-smi 卡状态")
    code, raw, _err = run_ssh(host, port, user, "npu-smi info 2>/dev/null || true", timeout=60)
    info["npu"] = parse_npu_smi_output(raw) if code == 0 and raw.strip() else {"error": "npu-smi unavailable"}
    info["npu_raw_output"] = raw[:8000]  # 原始输出截断存档（解析器未覆盖的版本时人工可读）
    # driver/firmware：npu-smi 的 board 信息最可靠（version.info 路径各版本不一）
    board = _run(host, port, user, "npu-smi info -t board -i 0 2>/dev/null | grep -iE 'software version|firmware version'")
    info["board_versions"] = [line.strip() for line in board.splitlines() if line.strip()]
    for line in info["board_versions"]:
        if "software" in line.lower():
            info["driver_version"] = line.split(":", 1)[-1].strip()
            break
    return info


def snapshot_container(host: str, port: int, user: str, container: str) -> dict[str, Any]:
    """容器层：docker 配置、包版本、运行中 vllm 进程的真实环境。

    实现注意（真机验证过的坑）：
    1. docker inspect 多字段 format 输出按空格 split 解析时，含空格的 JSON 数组
       （Binds）必须放最后一位；
    2. docker exec sh -c "cmd $(...)" 双引号里的 $() 会被宿主机 shell 在 exec 之前
       展开（拿到宿主机结果），变量展开一律不用，复杂逻辑写脚本文件 cp 进容器执行；
    3. 多行命令（heredoc）不能用 json.dumps 传（换行变字面 \\n 两字符），用 shlex.quote；
    4. vllm 常装在 /usr/local/pythonX.Y/bin（非登录 shell 的 PATH 不含它），
       先探测绝对路径再用。
    """
    progress("container", f"采集容器 {container} 配置")
    result: dict[str, Any] = {"name": container}
    # 注意字段顺序：Binds 是唯一内部含空格的 JSON 数组，必须放最后（maxsplit 兜住）
    code, out, _err = run_ssh(
        host, port, user,
        f"docker inspect {container} --format "
        "'{{json .Config.Image}} {{json .Config.Cmd}} {{json .HostConfig.ShmSize}} "
        "{{json .HostConfig.IpcMode}} {{json .HostConfig.Privileged}} "
        "{{json .HostConfig.NetworkMode}} {{json .HostConfig.Binds}}'",
        timeout=30,
    )
    if code == 0 and out.strip():
        parts = out.strip().split(" ", 6)
        result["image"] = json.loads(parts[0]) if len(parts) > 0 else None
        result["cmd"] = json.loads(parts[1]) if len(parts) > 1 else None
        result["shm_size"] = json.loads(parts[2]) if len(parts) > 2 else None
        result["ipc_mode"] = json.loads(parts[3]) if len(parts) > 3 else None
        result["privileged"] = json.loads(parts[4]) if len(parts) > 4 else None
        result["network_mode"] = json.loads(parts[5]) if len(parts) > 5 else None
        result["binds"] = json.loads(parts[6]) if len(parts) > 6 else None
    else:
        result["error"] = "docker inspect 失败（容器不存在？）"
        return result

    # 容器内的昇腾运行时环境变量 + pip 版本矩阵
    progress("container", "采集容器内包版本与环境变量")
    env_cmd = "env | grep -E '^(" + "|".join(ENV_KEYS) + ")=' | sort"
    _code, env_out, _e = run_ssh(host, port, user, f"docker exec {container} sh -c {shlex.quote(env_cmd)}", timeout=30)
    result["runtime_env"] = dict(line.split("=", 1) for line in env_out.splitlines() if "=" in line)

    # 探测容器内最高版本的 python 绝对路径（无变量展开，安全）
    _code, py_out, _e = run_ssh(
        host, port, user,
        f"docker exec {container} sh -c \"ls /usr/local/python*/bin/python3 2>/dev/null | sort -V | tail -1\"",
        timeout=30,
    )
    python_bin = (py_out or "").strip().splitlines()[-1].strip() if (py_out or "").strip() else "python3"
    pkg_cmd = (
        f"{python_bin} - << 'PYEOF'\n"
        "import importlib.metadata as m\n"
        "for name in " + repr(PACKAGES) + ":\n"
        "    try:\n"
        "        print(name, m.version(name))\n"
        "    except Exception:\n"
        "        pass\n"
        "PYEOF"
    )
    _code, pkg_out, _e = run_ssh(host, port, user, f"docker exec {container} sh -c {shlex.quote(pkg_cmd)}", timeout=120)
    result["packages"] = dict(line.split(" ", 1) for line in pkg_out.splitlines() if " " in line)

    # 运行中 vllm 进程的真实环境与库加载（CANN 切换验证的铁证）。
    # 必须在容器 PID namespace 里找进程——走脚本文件，避免宿主机 shell 抢先展开 $()
    progress("container", "抓取运行中 vllm 进程的实际环境")
    probe_script = (
        "#!/bin/sh\n"
        "for p in $(ls /proc | grep -E '^[0-9]+$'); do\n"
        "  if grep -qa vllm /proc/$p/cmdline 2>/dev/null && grep -qa serve /proc/$p/cmdline 2>/dev/null; then\n"
        "    echo PID=$p\n"
        "    tr '\\0' '\\n' < /proc/$p/environ | grep -E '^(ASCEND_TOOLKIT_HOME|ATB_HOME_PATH)='\n"
        "    echo custom_cann_libs=$(grep -c 'z00893295/cann' /proc/$p/maps 2>/dev/null)\n"
        "    break\n"
        "  fi\ndone\n"
    )
    remote_tmp = f"/tmp/env_snapshot_probe_{int(time.time())}.sh"
    transfer = "printf '%s\\n' " + " ".join(
        "'" + line.replace("'", "'\\''") + "'" for line in probe_script.splitlines()
    ) + f" > {remote_tmp}"
    run_ssh(host, port, user, transfer, timeout=30)
    run_ssh(host, port, user, f"docker cp {remote_tmp} {container}:{remote_tmp}", timeout=30)
    _code, proc_out, _e = run_ssh(host, port, user, f"docker exec {container} sh {remote_tmp}", timeout=60)
    run_ssh(host, port, user, f"rm -f {remote_tmp} && docker exec {container} rm -f {remote_tmp}", timeout=30)
    vllm_proc: dict[str, Any] = {}
    for line in proc_out.splitlines():
        if line.startswith("PID="):
            vllm_proc["pid"] = line[4:]
        elif line.startswith("custom_cann_libs="):
            vllm_proc["custom_cann_libs_loaded"] = line.split("=", 1)[1]
        elif "=" in line:
            key, value = line.split("=", 1)
            vllm_proc[key] = value
    result["vllm_process"] = vllm_proc if vllm_proc else "not running"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="精度诊断环境快照采集（自包含，SSH 直连）")
    parser.add_argument("--host", required=True, help="目标服务器 IP")
    parser.add_argument("--user", default="root", help="SSH 用户，默认 root")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口，默认 22")
    parser.add_argument("--container", help="目标容器名（省略则只采宿主机层）")
    parser.add_argument("--out", help="快照 JSON 输出路径（默认当前目录）")
    args = parser.parse_args()

    # 前置：密钥 SSH 可达
    progress("connect", f"验证 {args.user}@{args.host}:{args.port} 密钥登录")
    code, _out, err = run_ssh(args.host, args.port, args.user, "echo ok", timeout=20)
    if code != 0:
        return emit(
            {"ok": False, "action": "env-snapshot", "status": "blocked",
             "error": f"SSH 不可达（需密钥认证，不弹密码）：{(err or '').strip()[:200]}"}
        )

    snapshot: dict[str, Any] = {"action": "env-snapshot", "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    snapshot["host"] = snapshot_host(args.host, args.port, args.user)
    if args.container:
        snapshot["container"] = snapshot_container(args.host, args.port, args.user, args.container)

    # 存档（快照的价值在于可回溯、可 diff）
    default_name = f"env-snapshot-{args.host}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path = Path(args.out) if args.out else Path.cwd() / default_name
    try:
        out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return emit(
            {"ok": False, "action": "env-snapshot", "status": "failed",
             "error": f"快照写入失败：{exc}", "snapshot": snapshot}
        )

    # 摘要：版本矩阵一览
    container = snapshot.get("container") or {}
    packages = container.get("packages") or {}
    vllm_proc = container.get("vllm_process")
    summary = {
        "driver": snapshot["host"].get("driver_version"),
        "npu_count": (snapshot["host"].get("npu") or {}).get("npu_count"),
        "npu_name": next(
            (n.get("name") for n in (snapshot["host"].get("npu") or {}).get("npus", []) if n.get("name")), None
        ),
        "vllm": packages.get("vllm"),
        "vllm_ascend": packages.get("vllm-ascend") or packages.get("vllm_ascend"),
        "torch": packages.get("torch"),
        "torch_npu": packages.get("torch-npu") or packages.get("torch_npu"),
        "runtime_ascend_toolkit_home": (container.get("runtime_env") or {}).get("ASCEND_TOOLKIT_HOME"),
    }
    if isinstance(vllm_proc, dict):
        summary["vllm_process_env"] = {
            k: vllm_proc.get(k) for k in ("ASCEND_TOOLKIT_HOME", "ATB_HOME_PATH", "custom_cann_libs_loaded")
        }

    return emit(
        {"ok": True, "action": "env-snapshot", "status": "ready",
         "snapshot_path": str(out_path), "version_matrix": summary, "snapshot": snapshot}
    )


if __name__ == "__main__":
    raise SystemExit(main())
