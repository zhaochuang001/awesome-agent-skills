#!/usr/bin/env python3
"""cleanup.py 纯函数单测：docker 状态字符串的年龄解析与筛选。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cleanup import is_stale_enough, parse_status_age


def test_parse_days():
    assert parse_status_age("Exited (255) 9 days ago") == 9
    assert parse_status_age("Exited (137) 13 days ago") == 13
    assert parse_status_age("Exited (0) 31 days ago") == 31


def test_parse_weeks_months():
    assert parse_status_age("Exited (255) 2 weeks ago") == 14
    assert parse_status_age("Exited (255) 3 weeks ago") == 21
    assert parse_status_age("Exited (255) 2 months ago") == 60


def test_parse_sub_day():
    # 不足一天按 0（不算陈旧）
    assert parse_status_age("Exited (1) 5 hours ago") == 0
    assert parse_status_age("Exited (1) 30 minutes ago") == 0
    assert parse_status_age("Exited (1) 8 hours ago") == 0


def test_parse_running():
    # 运行中的容器返回 -1，永远不算陈旧
    assert parse_status_age("Up 3 days") == -1
    assert parse_status_age("Up About an hour") == -1
    assert parse_status_age("Restarting (1) 2 minutes ago") == -1


def test_parse_created():
    # Created 状态（从未启动）无 ago 字样 -> 0
    assert parse_status_age("Created") == 0


def test_is_stale_enough():
    assert is_stale_enough("Exited (255) 9 days ago", 9) is True
    assert is_stale_enough("Exited (255) 8 days ago", 9) is False
    assert is_stale_enough("Exited (255) 2 weeks ago", 9) is True
    assert is_stale_enough("Exited (255) 2 hours ago", 9) is False
    assert is_stale_enough("Up 30 days", 9) is False
