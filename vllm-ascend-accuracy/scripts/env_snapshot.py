#!/usr/bin/env python3
"""环境快照：一键冻结精度诊断所需的完整环境信息，输出可存档、可对比的 JSON。

用途（对应 SKILL.md「解决闭环」第 2 步"冻结环境"）：
- 版本配套矩阵一次采全（vLLM/vllm-ascend/CANN/torch_npu/driver/卡状态），替代手工十几条命令；
- 诊断前后各拍一次，diff 能发现"环境被谁动过"（环境漂移是复现失效的头号原因）；
- 抓运行中 vllm 进程的真实环境变量与库加载计数（CANN 版本切换是否生效的铁证）。

依赖 server-management skill（机器清单、SSH 工具、npu-smi 双格式解析器）；未安装时
本脚本会提示，v1 的其余流程不受影响（该依赖仅此脚本需要）。

输出协议与 server-management 一致：stderr __SM_PROGRESS__ 进度，stdout 单个最终 JSON。
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 依赖注入：server-management skill 的 scripts 目录
# ---------------------------------------------------------------------------


def _inject_dependency() -> str:
    candidates = [
        Path.home() / ".claude" / "skills" / "server-management" / "scripts",  # Claude Code
        Path.home() / ".agents" / "skills" / "server-management" / "scripts",  # Codex
        Path(__file__).resolve().parents[2] / "server-management" / "scripts",  # 仓库开发布局
    ]
    for candidate in candidates:
        if (candidate / "common.py").is_file():
            sys.path.insert(0, str(candidate))
            return str(candidate)
    print(
        "缺少依赖 skill：server-management（提供机器清单、SSH 与 npu-smi 解析）。"
        "请先安装 awesome-agent-skills 仓库中的 server-management，"
        "或按 remote-container-workflow.md 手工采集环境信息。",
        file=sys.stderr,
    )
    raise SystemExit(2)


_inject_dependency()

from common import emit, find_machine, progress, run_ssh_machine  # noqa: E402
from npu_probe import parse_npu_smi_output  # noqa: E402

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


def _run(machine: dict[str, Any], cmd: str, timeout: float = 60.0) -> str:
    code, out, _err = run_ssh_machine(machine, cmd, timeout=timeout)
    return out.strip() if code == 0 else ""


def snapshot_host(machine: dict[str, Any]) -> dict[str, Any]:
    """宿主机层：系统、driver、NPU 状态。"""
    progress("host", "采集宿主机系统与 driver 信息")
    host: dict[str, Any] = {
        "host": machine["host"],
        "alias": machine.get("alias"),
        "kernel": _run(machine, "uname -r"),
        "hostname": _run(machine, "hostname"),
    }
    # npu-smi：结构化解析（复用 server-management 的 24.x/26.x 双格式解析器）
    progress("host", "采集 npu-smi 卡状态")
    code, raw, _err = run_ssh_machine(machine, "npu-smi info 2>/dev/null || true", timeout=60)
    host["npu"] = parse_npu_smi_output(raw) if code == 0 and raw.strip() else {"error": "npu-smi unavailable"}
    host["npu_raw_output"] = raw[:8000]  # 原始输出截断存档（解析器未覆盖的版本时人工可读）
    # driver/firmware：npu-smi 的 board 信息最可靠（version.info 路径各版本不一）
    board = _run(machine, "npu-smi info -t board -i 0 2>/dev/null | grep -iE 'software version|firmware version'")
    host["board_versions"] = [line.strip() for line in board.splitlines() if line.strip()]
    for line in host["board_versions"]:
        if "software" in line.lower():
            host["driver_version"] = line.split(":", 1)[-1].strip()
            break
    return host


def snapshot_container(machine: dict[str, Any], container: str) -> dict[str, Any]:
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
    code, out, _err = run_ssh_machine(
        machine,
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
    _code, env_out, _e = run_ssh_machine(
        machine, f"docker exec {container} sh -c {shlex.quote(env_cmd)}", timeout=30
    )
    result["runtime_env"] = dict(
        line.split("=", 1) for line in env_out.splitlines() if "=" in line
    )

    # 探测容器内最高版本的 python 绝对路径（无变量展开，安全）
    _code, py_out, _e = run_ssh_machine(
        machine,
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
    _code, pkg_out, _e = run_ssh_machine(
        machine, f"docker exec {container} sh -c {shlex.quote(pkg_cmd)}", timeout=120
    )
    result["packages"] = dict(
        line.split(" ", 1) for line in pkg_out.splitlines() if " " in line
    )

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
    run_ssh_machine(machine, transfer, timeout=30)
    run_ssh_machine(machine, f"docker cp {remote_tmp} {container}:{remote_tmp}", timeout=30)
    _code, proc_out, _e = run_ssh_machine(
        machine, f"docker exec {container} sh {remote_tmp}", timeout=60
    )
    run_ssh_machine(machine, f"rm -f {remote_tmp} && docker exec {container} rm -f {remote_tmp}", timeout=30)
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
    parser = argparse.ArgumentParser(description="精度诊断环境快照采集（冻结环境用）")
    parser.add_argument("--host", required=True, help="目标服务器（alias 或 IP，须在 server-management 清单中）")
    parser.add_argument("--container", help="目标容器名（省略则只采宿主机层）")
    parser.add_argument("--out", help="快照 JSON 输出路径（默认当前目录）")
    args = parser.parse_args()

    found = find_machine(args.host)
    if not found:
        return emit(
            {"ok": False, "action": "env-snapshot", "status": "blocked",
             "error": f"机器 {args.host} 不在清单中，先用 server-management 添加"}
        )
    _index, machine = found

    snapshot: dict[str, Any] = {
        "action": "env-snapshot",
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    snapshot["host"] = snapshot_host(machine)
    if args.container:
        snapshot["container"] = snapshot_container(machine, args.container)

    # 存档（快照的价值在于可回溯、可 diff）
    default_name = f"env-snapshot-{machine['host']}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path = Path(args.out) if args.out else Path.cwd() / default_name
    try:
        out_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
