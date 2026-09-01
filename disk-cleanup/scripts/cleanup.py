#!/usr/bin/env python3
"""服务器磁盘空间分析与清理（docker 镜像/容器为主）。

设计原则（从共享 NPU 节点的实战清理提炼）：
- **分析只读**：默认输出磁盘全景与分级清理候选（safe / needs-confirm），不动任何东西；
- **删除必须显式授权**：--execute-safe 只做无损操作（悬空镜像、构建缓存）；
  --execute-confirm 才删未引用镜像与陈旧容器，且必须带 --days；
- **绝不触碰**：运行中的容器、被引用的镜像、/home /mnt /data /root 下的任何用户数据
  （这些只出现在报告里，由数据主人自己处理）；
- 共享节点意识：所有删除项都先打印完整清单（名字/大小/状态）再动手。

依赖 server-management skill（机器清单与 SSH 工具）。
输出协议与其一致：stderr __SM_PROGRESS__ 进度，stdout 单个最终 JSON。
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 依赖注入：server-management skill 的 scripts 目录
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


_inject_dependency()

from common import emit, find_machine, progress, run_ssh_machine  # noqa: E402


# ---------------------------------------------------------------------------
# 纯函数：状态年龄解析（单测覆盖）
# ---------------------------------------------------------------------------

def parse_status_age(status: str) -> int:
    """把 docker 状态字符串解析为退出至今的天数（向下取整，不足一天算 0）。

    例："Exited (255) 9 days ago" -> 9；"Exited (137) 2 weeks ago" -> 14；
    "Exited (0) 5 hours ago" -> 0；"Up 3 days" -> -1（仍在运行）。
    取整方向是安全考量：8 天 23 小时算 8 天（宁可晚删不可早删）。
    """
    if status.startswith(("Up", "Restarting", "Paused", "running")):
        return -1
    match = re.search(r"(\d+) (second|minute|hour|day|week|month)s? ago", status)
    if not match:
        return 0
    value, unit = int(match.group(1)), match.group(2)
    factor = {"second": 1 / 86400, "minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}
    return int(value * factor[unit])


def is_stale_enough(status: str, min_days: int) -> bool:
    """容器退出时间是否超过 min_days 天（运行中的永远 False）。"""
    age = parse_status_age(status)
    return age >= min_days


# ---------------------------------------------------------------------------
# 远端信息采集
# ---------------------------------------------------------------------------

def disk_overview(machine: dict[str, Any]) -> list[dict[str, str]]:
    """df 全景（排除虚拟文件系统）。"""
    _code, out, _err = run_ssh_machine(
        machine, "df -h | grep -vE 'tmpfs|devtmpfs|overlay|shm|efivarfs'", timeout=60
    )
    lines = [line.split() for line in out.splitlines() if line.strip()]
    keys = ["filesystem", "size", "used", "avail", "use_pct", "mount"]
    return [dict(zip(keys, parts)) for parts in lines if len(parts) >= 6]


def top_dirs(machine: dict[str, Any], path: str = "/") -> list[dict[str, str]]:
    """指定路径下一级目录占用（-x 不跨挂载点）。"""
    progress("scan", f"扫描 {path} 一级目录大小（大目录耗时）")
    _code, out, _err = run_ssh_machine(
        machine, f"du -x -d1 -h {shlex.quote(path)} 2>/dev/null | sort -rh | head -15", timeout=600
    )
    result = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            result.append({"size": parts[0], "dir": parts[1]})
    return result


def docker_images(machine: dict[str, Any]) -> list[dict[str, str]]:
    """镜像清单。--no-trunc 输出完整 ID（sha256:xxx），与 inspect 的 {{.Image}} 精确可比——
    短 ID（12 位）与完整 ID 精确匹配永远失败，会把在用镜像全部误判为未引用。"""
    _code, out, _err = run_ssh_machine(
        machine,
        "docker images --no-trunc --format '{{.ID}}\\t{{.Repository}}:{{.Tag}}\\t{{.Size}}\\t{{.CreatedSince}}'",
        timeout=60,
    )
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            rows.append({"id": parts[0], "repo_tag": parts[1], "size": parts[2], "created": parts[3]})
    return rows


def docker_containers(machine: dict[str, Any]) -> list[dict[str, str]]:
    """容器全量清单。注意：docker ps --format 不支持 .State 字段（docker inspect 才有），
    用了会整体报错返回空表，导致所有镜像被误判为未引用——状态从 .Status 推导。"""
    _code, out, _err = run_ssh_machine(
        machine,
        "docker ps -a --format '{{.ID}}\\t{{.Names}}\\t{{.Status}}\\t{{.RunningFor}}'",
        timeout=60,
    )
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            status = parts[2]
            state = "running" if status.startswith(("Up", "Restarting", "Paused")) else "stopped"
            rows.append(
                {"id": parts[0], "name": parts[1], "state": state, "status": status, "running_for": parts[3]}
            )
    return rows


def referenced_image_ids(machine: dict[str, Any]) -> set[str]:
    """被任何容器（含停止的）引用的镜像 ID 集合。

    注意：必须用 `docker inspect` 从容器侧取完整 image ID。
    `docker ps --format {{.Image}}` 对无 tag 镜像输出的是 ID 简写且对有 tag 的
    输出名字，直接比对会漏检（实战中曾因此把在用镜像列进可删清单）。
    """
    _code, out, _err = run_ssh_machine(
        machine,
        "docker ps -a -q | xargs -r docker inspect --format '{{.Image}}' | sort -u",
        timeout=120,
    )
    return {line.strip() for line in out.splitlines() if line.strip()}


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------

def analyze(machine: dict[str, Any], scan_path: str | None) -> dict[str, Any]:
    """只读分析：磁盘全景 + docker 占用 + 分级清理候选。"""
    progress("analyze", f"分析 {machine['host']}")
    disks = disk_overview(machine)
    images = docker_images(machine)
    containers = docker_containers(machine)
    referenced = referenced_image_ids(machine)

    dangling = [img for img in images if img["repo_tag"].split(":")[-1] == "<none>" or "<none>" in img["repo_tag"]]
    unreferenced = [img for img in images if img["id"] not in referenced and img not in dangling]
    running = [c for c in containers if c["state"] == "running"]
    stopped = [c for c in containers if c["state"] != "running"]

    def approx_gb(rows: list[dict[str, str]], key: str = "size") -> float:
        total = 0.0
        for row in rows:
            size = row.get(key, "")
            match = re.match(r"([\d.]+)([KMGT]?)B?", size)
            if match:
                total += float(match.group(1)) * {"K": 1 / 1024**2, "M": 1 / 1024, "G": 1, "T": 1024, "": 1 / 1024**3}.get(
                    match.group(2), 1 / 1024**3
                )
        return round(total, 1)

    return {
        "disk": disks,
        "docker": {
            "images_total": len(images),
            "containers_running": len(running),
            "containers_stopped": len(stopped),
        },
        "cleanup_candidates": {
            "safe": {
                "note": "无损可删：无 tag 且无容器引用的悬空层",
                "dangling_images": dangling,
                "estimated_gb": approx_gb(dangling),
            },
            "needs_confirm": {
                "note": "删除需要用户确认：共享节点上可能是他人备用资源",
                "unreferenced_images": unreferenced,
                "unreferenced_images_estimated_gb": approx_gb(unreferenced),
                "stopped_containers": stopped,
                "stopped_containers_note": "删除退出超过 --days 天的容器；容器可写层数据会一并丢失",
            },
        },
        "disk_hogs": top_dirs(machine, scan_path) if scan_path else None,
        "size_disclaimer": "docker 镜像 SIZE 是逻辑值：层共享导致实际回收量通常小于清单求和",
    }


# ---------------------------------------------------------------------------
# 执行清理
# ---------------------------------------------------------------------------

def execute_safe(machine: dict[str, Any]) -> dict[str, Any]:
    """无损清理：悬空镜像 + 构建缓存。"""
    progress("execute-safe", "清理悬空镜像与构建缓存")
    _code, out, _err = run_ssh_machine(machine, "docker image prune -f 2>&1 | tail -1", timeout=600)
    image_reclaimed = out.strip()
    _code, out2, _err = run_ssh_machine(machine, "docker builder prune -f 2>&1 | tail -1", timeout=600)
    return {"images": image_reclaimed, "build_cache": out2.strip()}


def execute_confirm(machine: dict[str, Any], days: int, include_images: bool) -> dict[str, Any]:
    """确认级清理：退出超 days 天的容器（必做）+ 未引用镜像（--include-images 时）。"""
    progress("execute-confirm", f"删除退出超过 {days} 天的容器")
    containers = docker_containers(machine)
    stale = [c for c in containers if c["state"] != "running" and is_stale_enough(c["status"], days)]
    removed_containers = []
    for container in stale:
        code, out, _err = run_ssh_machine(machine, f"docker rm {shlex.quote(container['id'])}", timeout=120)
        if code == 0:
            removed_containers.append(container["name"])

    removed_images: list[str] = []
    if include_images:
        progress("execute-confirm", "删除未被任何容器引用的镜像")
        images = docker_images(machine)
        referenced = referenced_image_ids(machine)
        for img in images:
            if img["id"] in referenced or "<none>" in img["repo_tag"]:
                continue
            code, _out, _err = run_ssh_machine(
                machine, f"docker rmi {shlex.quote(img['id'])} 2>&1", timeout=600
            )
            if code == 0:
                removed_images.append(img["repo_tag"])
        # 删完镜像再清一轮悬空层
        run_ssh_machine(machine, "docker image prune -f", timeout=600)

    return {
        "removed_containers": removed_containers,
        "removed_containers_count": len(removed_containers),
        "removed_images": removed_images,
        "removed_images_count": len(removed_images),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="服务器磁盘空间分析与清理（docker 为主）")
    parser.add_argument("--host", required=True, help="目标服务器（alias 或 IP，须在清单中）")
    parser.add_argument("--path", help="额外扫描某路径下一级目录占用（如 / 或 /var）")
    parser.add_argument("--execute-safe", action="store_true", help="执行无损清理（悬空镜像/构建缓存）")
    parser.add_argument(
        "--execute-confirm",
        action="store_true",
        help="执行确认级清理：删除退出超过 --days 天的容器（先看分析报告再决定）",
    )
    parser.add_argument("--days", type=int, default=9, help="容器退出多少天以上才删（默认 9）")
    parser.add_argument(
        "--include-images",
        action="store_true",
        help="--execute-confirm 时同时删除未被任何容器引用的镜像（共享节点慎用）",
    )
    args = parser.parse_args()

    found = find_machine(args.host)
    if not found:
        return emit(
            {"ok": False, "action": "disk-cleanup", "status": "blocked",
             "error": f"机器 {args.host} 不在清单中，先用 server-management 添加"}
        )
    _index, machine = found

    if not (args.execute_safe or args.execute_confirm):
        # 默认：只读分析报告
        return emit(
            {"ok": True, "action": "disk-cleanup", "status": "ready", **analyze(machine, args.path)}
        )

    before = disk_overview(machine)
    results: dict[str, Any] = {}
    if args.execute_safe:
        results["safe"] = execute_safe(machine)
    if args.execute_confirm:
        if args.days < 1:
            return emit(
                {"ok": False, "action": "disk-cleanup", "status": "needs_input",
                 "error": "--days 必须 >= 1（防止误删刚退出的容器）"}
            )
        results["confirm"] = execute_confirm(machine, args.days, args.include_images)

    after = disk_overview(machine)
    return emit(
        {
            "ok": True,
            "action": "disk-cleanup",
            "status": "removed",
            "results": results,
            "disk_before": [d for d in before if d["mount"] == "/"],
            "disk_after": [d for d in after if d["mount"] == "/"],
            "note": "实际回收量以 df 对比为准（镜像层共享导致小于清单求和）",
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
