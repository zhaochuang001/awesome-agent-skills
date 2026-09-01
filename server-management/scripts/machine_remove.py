#!/usr/bin/env python3
"""移除一台受管服务器：有边界的本地清理。

只删除本 skill 登记的资源：
- inventory 中的机器记录；
- 本地 known_hosts 中该端点的条目。

不触碰：宿主机 authorized_keys、防火墙规则、宿主机上任何文件。
宿主机不可达时移除仍然成功（本地清理），并注明宿主机侧公钥保留。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import emit, progress, remove_known_host, remove_machine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="移除服务器：仅删除本地登记与 known_hosts 条目")
    parser.add_argument("--machine", required=True, help="别名、IP 或 IP:端口")
    args = parser.parse_args()

    progress("start", f"remove {args.machine}")

    result: dict[str, Any] = remove_machine(args.machine)
    removed = result["removed"]

    if removed is None:
        # 不在清单里：幂等成功，无需任何动作
        return emit(
            {
                "ok": True,
                "action": "remove",
                "status": "unmanaged",
                "machine": args.machine,
                "note": result["reason"],
            }
        )

    host = removed["host"]
    port = int(removed.get("port", 22))
    note = remove_known_host(host, port)

    return emit(
        {
            "ok": True,
            "action": "remove",
            "status": "removed",
            "machine": removed,
            "known_hosts": note,
            "boundary": (
                "仅清理了本地 inventory 记录与 known_hosts 条目；"
                "宿主机上的公钥与配置保持原样，如需彻底清理请手动删除对应 authorized_keys 行"
            ),
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
