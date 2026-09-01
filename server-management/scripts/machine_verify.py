#!/usr/bin/env python3
"""验证一台受管服务器的健康状态（只读，不修复）。

判定：
- ready        密钥 SSH 可用且 npu-smi 可采集（无 NPU 的机器按 SSH 判定）；
- needs_repair 受管但检查失败（SSH 断、NPU 异常）；
- unmanaged    不在 inventory 中。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import emit, find_machine, progress, ssh_key_ok, upsert_machine  # noqa: E402
from npu_probe import probe_remote_npu  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="验证服务器健康状态（只读）")
    parser.add_argument("--machine", required=True, help="别名、IP 或 IP:端口")
    args = parser.parse_args()

    progress("start", f"verify {args.machine}")

    found = find_machine(args.machine)
    if not found:
        return emit(
            {
                "ok": True,
                "action": "verify",
                "status": "unmanaged",
                "machine": args.machine,
                "note": "不在 inventory 中；先运行 machine_add 登记该机器",
            }
        )

    _index, machine = found
    host = machine["host"]
    port = int(machine.get("port", 22))
    user = machine.get("user", "root")

    progress("ssh-check", f"密钥登录检查 {user}@{host}:{port}")
    ok, detail = ssh_key_ok(host, port, user)
    result: dict[str, Any] = {
        "ok": True,
        "action": "verify",
        "machine": machine,
        "ssh_ok": ok,
    }

    if not ok:
        result.update(
            status="needs_repair",
            error=f"key SSH failed: {detail}",
            hint="可运行 machine_add（幂等 attach）或检查网络/密钥后重试",
        )
        return emit(result)

    progress("npu-probe", "采集 NPU 状态")
    npu = probe_remote_npu(machine)
    result["npu"] = npu

    alarms = [n for n in npu.get("npus", []) if n.get("health") not in (None, "OK")]
    if alarms:
        result.update(
            status="needs_repair",
            npu_alarms=alarms,
            note=f"{len(alarms)} 张 NPU 健康状态异常（Alarm/Critical）",
        )
        return emit(result)

    # 只读指不动目标机器；本地记账时间戳正常回写
    machine["last_verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    upsert_machine(machine)
    result.update(status="ready")
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
