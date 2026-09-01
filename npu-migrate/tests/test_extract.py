#!/usr/bin/env python3
"""migrate.py 纯函数单测：卡数提取、可见设备解析、docker run 命令构造。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from migrate import build_run_command, extract_npu_devices, parse_visible_devices, select_target, weight_status

# 真实 8 卡 910 容器的典型 docker inspect 设备列表
DEVICES_8CARD = [
    "/dev/davinci0", "/dev/davinci1", "/dev/davinci2", "/dev/davinci3",
    "/dev/davinci4", "/dev/davinci5", "/dev/davinci6", "/dev/davinci7",
    "/dev/davinci_manager", "/dev/devmm_svm", "/dev/hisi_hdc",
]
ENV_8CARD = [
    "ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7",
    "PATH=/usr/local/bin:/usr/bin",
    "LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64",
]
# 部分 NPU 的容器（只挂 2 张卡）
DEVICES_2CARD = [
    "/dev/davinci4", "/dev/davinci5",
    "/dev/davinci_manager", "/dev/devmm_svm", "/dev/hisi_hdc",
]
ENV_2CARD = ["ASCEND_RT_VISIBLE_DEVICES=4,5", "FOO=bar"]

INFO_SAMPLE = {
    "image": "migrate/demo:20260831",
    "devices": DEVICES_8CARD,
    "env": ["ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7", "PATH=/usr/bin"],
    "binds": ["/home/user/code:/workspace", "/data/weights:/weights"],
    "ports": {"8000/tcp": [{"HostPort": "8000"}]},
    "restart": "unless-stopped",
    "privileged": False,
    "network_mode": "host",
    "shared_devices": ["/dev/davinci_manager", "/dev/devmm_svm", "/dev/hisi_hdc"],
    "status": "running",
}


def test_parse_visible_devices():
    assert parse_visible_devices("0,1,2") == 3
    assert parse_visible_devices("0-3") == 4
    assert parse_visible_devices("0") == 1
    assert parse_visible_devices("") is None
    assert parse_visible_devices("bad") is None


def test_extract_8card():
    count, shared, cards = extract_npu_devices(DEVICES_8CARD, ENV_8CARD)
    assert count == 8
    assert set(shared) == {"/dev/davinci_manager", "/dev/devmm_svm", "/dev/hisi_hdc"}
    assert len(cards) == 8


def test_extract_2card():
    count, shared, _cards = extract_npu_devices(DEVICES_2CARD, ENV_2CARD)
    assert count == 2
    assert "/dev/davinci_manager" in shared


def test_extract_env_priority():
    # 设备数与环境变量计数不一致时，以环境变量为准
    count, _, _ = extract_npu_devices(DEVICES_8CARD, ["ASCEND_RT_VISIBLE_DEVICES=0-1"])
    assert count == 2


def test_extract_no_npu():
    count, shared, cards = extract_npu_devices(["/dev/null"], ["A=b"])
    assert count == 0
    assert shared == []
    assert cards == []


def test_build_run_command():
    cmd = build_run_command("migrate/demo:20260831", INFO_SAMPLE, [2, 5, 6], "demo")
    assert "--name demo" in cmd
    assert "--restart unless-stopped" in cmd
    assert "--network host" in cmd
    # 目标机物理卡映射：容器看到 0,1,2 -> 物理 2,5,6
    assert "--device /dev/davinci2" in cmd
    assert "--device /dev/davinci5" in cmd
    assert "--device /dev/davinci6" in cmd
    assert "/dev/davinci0" not in cmd
    assert "-e ASCEND_RT_VISIBLE_DEVICES=0,1,2" in cmd
    # 源的环境变量里旧的可见设备被替换掉
    assert "ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7" not in cmd
    # 共享设备保留
    assert "--device /dev/davinci_manager" in cmd
    # 挂载与端口原样复刻
    assert "-v /home/user/code:/workspace" in cmd
    assert "-p 8000:8000" in cmd


def test_select_target_auto():
    idle = {"a": [0, 1, 2, 3], "b": [0, 1], "c": [4, 5, 6]}
    host, cards, candidates, note = select_target("c", 2, idle, None)
    assert host == "a"  # 空闲最多
    assert cards == [0, 1]  # 升序取前 N
    assert any(c["host"] == "b" for c in candidates)


def test_select_target_excludes_source():
    idle = {"a": [0, 1]}
    host, _, _, _ = select_target("a", 1, idle, None)
    assert host is None  # 源机被排除


def test_select_target_prefer():
    idle = {"a": [0, 1, 2], "b": [3, 4]}
    host, cards, _, note = select_target("s", 2, idle, "b")
    assert host == "b"
    assert cards == [3, 4]
    assert "用户指定" in note


def test_select_target_prefer_insufficient():
    idle = {"a": [0, 1, 2], "b": [3]}
    host, _, _, note = select_target("s", 2, idle, "b")
    assert host is None
    assert "不足" in note


def test_weight_status():
    assert weight_status(100, None) == "missing"        # 目标不存在
    assert weight_status(100, 50) == "partial"          # 目标明显偏小
    assert weight_status(100, 98.9) == "partial"        # 差超 1%（98.9 < 99）
    assert weight_status(100, 99.5) == "ok"             # 差 0.5% 在容差内
    assert weight_status(100, 100) == "ok"
    assert weight_status(100, 120) == "ok"              # 目标更大（可能含额外文件）视为可用
    assert weight_status(None, 100) == "ok"             # 源上也读不到大小时只看目标存在性
    assert weight_status(None, None) == "missing"
