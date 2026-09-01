#!/usr/bin/env python3
"""机器级采集解析器（负载/磁盘/Docker）的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from npu_probe import parse_df, parse_docker, parse_loadavg

SAMPLE_LOADAVG = "3.42 2.10 1.87 5/1024 12345\n"

SAMPLE_DF = """Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1       986G  412G  523G  45% /
/dev/sda2       3.5T  1.2T  2.1T  37% /data
tmpfs           189G     0  189G   0% /dev/shm
overlay          98G   30G   64G  32% /var/lib/docker/overlay2/abc/def
"""

# docker ps --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 的输出
SAMPLE_DOCKER = """vllm-serve\tUp 3 days\tquay.io/ascend/vllm-ascend:v0.9.1rc1
nginx\tUp 2 hours (healthy)\tnginx:alpine
"""

SAMPLE_EXTRA_PROBE_OUTPUT = """__LOAD__3.42 2.10 1.87 5/1024 12345
__DF__
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1       986G  412G  523G  45% /
__DOCKER__
vllm-serve\tUp 3 days\tquay.io/ascend/vllm-ascend:v0.9.1rc1
"""


def test_parse_loadavg():
    assert parse_loadavg(SAMPLE_LOADAVG) == 3.42
    assert parse_loadavg("") is None
    assert parse_loadavg("garbage") is None


def test_parse_df_basic():
    disks = parse_df(SAMPLE_DF)
    # tmpfs 行已被采集命令的 -x tmpfs 过滤；这里 fixture 含 tmpfs 只是验证解析器容错
    mounts = [d["mount"] for d in disks]
    assert "/" in mounts and "/data" in mounts
    root = [d for d in disks if d["mount"] == "/"][0]
    assert root["total_gb"] == 986.0
    assert root["used_gb"] == 412.0
    assert root["use_pct"] == 45
    data = [d for d in disks if d["mount"] == "/data"][0]
    assert data["total_gb"] == round(3.5 * 1024, 2)  # 3.5T -> GB


def test_parse_df_garbage():
    assert parse_df("") == []
    assert parse_df("random\nno columns") == []


def test_parse_docker():
    containers = parse_docker(SAMPLE_DOCKER)
    assert len(containers) == 2
    assert containers[0] == {
        "name": "vllm-serve",
        "status": "Up 3 days",
        "image": "quay.io/ascend/vllm-ascend:v0.9.1rc1",
    }


def test_parse_docker_unavailable():
    assert parse_docker("") is None
    assert parse_docker("__UNAVAILABLE__") is None
    assert parse_docker("unavailable") is None
