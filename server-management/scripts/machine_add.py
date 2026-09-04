#!/usr/bin/env python3
"""添加（attach）一台服务器：一次性密码 bootstrap 推公钥，之后永久密钥认证。

流程：
1. 若 inventory 已有该 host:port，幂等转 verify/repair 路径，不重复建记录；
2. 确保本地 ed25519 密钥对存在（没有则生成）；
3. 密码只在以下条件同时满足时使用一次：密钥登录尚不可用、用户提供了密码；
   - 密码通过 paramiko 一次性登录，把本地公钥幂等追加到宿主机 authorized_keys；
   - 密码不落盘、不回显、不写入命令行参数之外的地方；
4. 验证密钥登录；
5. 探测机器基本信息与 NPU 状态；
6. 写入 inventory，输出 ready。

密钥登录已可用时，即使提供了密码也不使用。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    _NO_WINDOW,
    find_machine,
    progress,
    emit,
    ssh_key_ok,
    upsert_machine,
)
from npu_probe import infer_machine_type, probe_remote_npu  # noqa: E402


def ensure_local_key() -> tuple[Path, str, str]:
    """确保本地 ed25519 密钥对存在。返回 (公钥路径, 公钥文本, 说明)。"""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)  # Windows 忽略 mode，POSIX 需要 700
    key_path = ssh_dir / "id_ed25519"
    pub_path = ssh_dir / "id_ed25519.pub"
    if pub_path.is_file() and key_path.is_file():
        return pub_path, pub_path.read_text(encoding="utf-8").strip(), "existing"
    progress("generate-key", "本地无 ed25519 密钥，生成新密钥对")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-q"],
        check=True,
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    return pub_path, pub_path.read_text(encoding="utf-8").strip(), "generated"


def bootstrap_authorized_key(
    host: str, port: int, user: str, password: str, public_key: str
) -> tuple[bool, str]:
    """用密码一次性登录（paramiko），幂等追加公钥到宿主机 authorized_keys。

    paramiko 延迟导入：密钥登录已可用时完全不依赖该库。
    """
    try:
        import paramiko  # type: ignore[import-not-found]
    except ImportError:
        return False, (
            "paramiko 未安装（密码 bootstrap 需要）。"
            "安装：python -m pip install --user -i https://repo.huaweicloud.com/repository/pypi/simple/ paramiko"
        )
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        # 幂等：先检查公钥是否已存在，避免 authorized_keys 膨胀
        check_cmd = (
            f"grep -qxF '{public_key}' ~/.ssh/authorized_keys 2>/dev/null && echo present || echo absent"
        )
        _stdin, stdout, _stderr = client.exec_command(check_cmd, timeout=15)
        state = stdout.read().decode("utf-8").strip()
        if state != "present":
            append_cmd = (
                "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys "
                f"&& chmod 600 ~/.ssh/authorized_keys && echo '{public_key}' >> ~/.ssh/authorized_keys"
            )
            _stdin, stdout, _stderr = client.exec_command(append_cmd, timeout=15)
            if stdout.channel.recv_exit_status() != 0:
                return False, "failed to append public key on remote host"
        client.close()
        return True, "public key installed"
    except Exception as exc:  # noqa: BLE001 - paramiko 异常类型不稳定，统一收敛
        return False, f"password bootstrap failed: {exc}"


def read_password(args: argparse.Namespace) -> str | None:
    """按优先级读取密码：stdin > 环境变量 > 命令行参数。密码绝不写入文件。"""
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n") or None
    if args.password_env:
        import os

        return os.environ.get(args.password_env)
    return args.password


def normalize_tags(raw: list[str]) -> tuple[list[str], str | None]:
    """归一化标签：strip、去重、保序。返回 (tags, 错误说明)；错误说明非空表示非法。

    规则与 fleet_service 的面板 API 一致：最多 20 个、单个 32 字符。
    """
    tags: list[str] = []
    for tag in raw:
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > 20:
        return [], "最多 20 个标签"
    for tag in tags:
        if len(tag) > 32:
            return [], f"标签超过 32 字符: {tag!r}"
    return tags, None


def probe_machine_meta(host: str, port: int, user: str) -> dict[str, Any]:
    """采集机器元数据：hostname、系统、CPU 核数，以及 NPU 概况。"""
    script = (
        "echo \"hostname=$(hostname 2>/dev/null)\"; "
        "echo \"kernel=$(uname -s -r 2>/dev/null)\"; "
        "echo \"cpu_cores=$(nproc 2>/dev/null)\"; "
        "echo \"memory_kb=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}')\""
    )
    from common import run_ssh

    try:
        code, out, _err = run_ssh(host, port, user, script, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        code, out = 1, ""
    meta: dict[str, Any] = {}
    if code == 0:
        for line in out.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                meta[key.strip()] = value.strip() or None
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description="添加服务器：一次性密码推公钥 + 探测 + 入册")
    parser.add_argument("--host", required=True, help="宿主机 IP")
    parser.add_argument("--port", type=int, default=22, help="SSH 端口，默认 22")
    parser.add_argument("--user", default="root", help="SSH 用户，默认 root")
    parser.add_argument("--alias", help="机器别名，默认使用 host")
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help="机器标签，可重复（如 --tag a3 --tag 910）；最多 20 个、单个 32 字符",
    )
    parser.add_argument("--password-stdin", action="store_true", help="从 stdin 读密码（推荐）")
    parser.add_argument("--password-env", help="从该环境变量读密码（推荐）")
    parser.add_argument("--password", help="明文密码（仅当密码已出现在当前对话中时使用）")
    args = parser.parse_args()

    tags, tag_error = normalize_tags(args.tag)
    if tag_error:
        return emit(
            {
                "ok": False,
                "action": "add",
                "status": "blocked",
                "machine": {"host": args.host, "port": args.port, "user": args.user},
                "error": f"invalid tags: {tag_error}",
            }
        )

    identifier = f"{args.host}:{args.port}"
    progress("start", f"add {identifier}")

    # 幂等：已登记的机器不重复 add；带 --tag 时合并补标签（不动其他字段）
    existing = find_machine(identifier) or find_machine(args.host)
    if existing:
        _index, machine = existing
        if tags:
            current = [t for t in (machine.get("tags") or []) if isinstance(t, str)]
            merged = current + [t for t in tags if t not in current]
            if merged != current:
                machine["tags"] = merged
                upsert_machine(machine)
        ok, detail = ssh_key_ok(machine["host"], int(machine.get("port", 22)), machine.get("user", "root"))
        return emit(
            {
                "ok": True,
                "action": "add",
                "status": "ready" if ok else "needs_repair",
                "machine": machine,
                "note": "machine already in inventory; idempotent attach. "
                + ("key auth verified" if ok else f"key auth failed: {detail}; run machine_repair path"),
            }
        )

    # 1. 确保本地密钥
    pub_path, public_key, key_note = ensure_local_key()
    progress("local-key", f"local public key: {pub_path} ({key_note})")

    # 2. 检查密钥登录是否已可用
    key_ok, key_err = ssh_key_ok(args.host, args.port, args.user)
    password_used = False
    if not key_ok:
        password = read_password(args)
        if password:
            progress("password-bootstrap", "使用密码一次性安装公钥（此后不再需要密码）")
            installed, install_note = bootstrap_authorized_key(
                args.host, args.port, args.user, password, public_key
            )
            password_used = installed
            if not installed:
                return emit(
                    {
                        "ok": False,
                        "action": "add",
                        "status": "blocked",
                        "machine": {"host": args.host, "port": args.port, "user": args.user},
                        "error": install_note,
                    }
                )
            key_ok, key_err = ssh_key_ok(args.host, args.port, args.user)
        else:
            return emit(
                {
                    "ok": False,
                    "action": "add",
                    "status": "needs_input",
                    "machine": {"host": args.host, "port": args.port, "user": args.user},
                    "error": f"key auth unavailable ({key_err}) and no password provided",
                    "hint": "提供 --password-stdin / --password-env / --password 之一后重试",
                }
            )

    if not key_ok:
        return emit(
            {
                "ok": False,
                "action": "add",
                "status": "needs_repair",
                "machine": {"host": args.host, "port": args.port, "user": args.user},
                "error": f"key auth still fails after bootstrap: {key_err}",
            }
        )

    # 3. 探测
    progress("probe", "采集机器元数据与 NPU 状态")
    record: dict[str, Any] = {
        "alias": args.alias or args.host,
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "auth": "key",
        "tags": tags,
        "enabled": True,
        "password_used_for_bootstrap": password_used,
    }
    meta = probe_machine_meta(args.host, args.port, args.user)
    record.update(
        remote_hostname=meta.get("hostname"),
        kernel=meta.get("kernel"),
        cpu_cores=int(meta["cpu_cores"]) if str(meta.get("cpu_cores", "")).isdigit() else None,
    )
    npu = probe_remote_npu(record)
    record["npu_count"] = npu.get("npu_count", 0)
    names = {n.get("name") for n in npu.get("npus", []) if n.get("name")}
    record["npu_name"] = sorted(names)[0] if len(names) == 1 else (sorted(names) if names else None)
    record["machine_type"] = infer_machine_type(sorted(names)[0]) if len(names) == 1 else "unknown"

    # 4. 入册
    progress("inventory", "写入机器清单")
    machines = upsert_machine(record)

    return emit(
        {
            "ok": True,
            "action": "add",
            "status": "ready",
            "machine": record,
            "fleet_size": len(machines),
            "note": "密钥认证已就绪；密码不会再次使用，也未持久化",
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
