#!/usr/bin/env python3
"""npu_probe 纯函数解析的单元测试。运行：python -m pytest server-management/tests/ -v"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from npu_probe import infer_machine_type, parse_npu_smi_output, summarize_fleet

# 真实 npu-smi 25.6.rc1.b218 的输出格式（Ascend950DT：NPU ID 独立列、Bus-Id 位置为 NA）
SAMPLE_OUTPUT_950 = """
+--------+------------------+---------------+----------------------------------------------------------------------+
| npu-smi 25.6.rc1.b218                            Version: 25.6.rc1.b218                |
+--------+------------------+---------------+----------------------------------------------------------------------+
| NPU ID | Name             | Health        | Power(W)              Temp(C)                  Hugepages-Usage(page) |
|        |                  | Bus-Id        | NPU Util(%)           Memory-Usage(MB)         HBM-Usage(MB)         |
+========+==================+===============+======================================================================+
| 0      | Ascend950DT      | OK            | 398.6                 46                       0     / 0             |
|        |                  | NA            | 0                     0     / 0                4765  / 98304         |
+===========================+===============+======================================================================+
| 1      | Ascend950DT      | OK            | 401.0                 51                       0     / 0             |
|        |                  | NA            | 0                     0     / 0                4764  / 98304         |
+===========================+===============+======================================================================+
| 7      | Ascend950DT      | Warning       | 397.5                 61                       0     / 0             |
|        |                  | NA            | 42                    0     / 0                91000 / 98304         |
+===========================+===============+======================================================================+
"""

# 真实 npu-smi 24.x 的典型输出格式（910B3 双行卡格式）
SAMPLE_OUTPUT = """
+-------------------------------------------------------------------------------------------+
| npu-smi 24.1.0                        Version: 24.1.0                                     |
+==========================+===============+=================================================+
| NPU     Name             | Health        | Power(W)     Temp(C)           Hugepages-Usage(page) |
| Chip    Device           | Bus-Id        | AICore(%)   Memory-Usage(MB)                  |
|==========================+===============+=================================================|
| 0       910B3            | OK            | 95.4        42                0    / 0         |
| 0       NA               | 0000:C1:00.0  | 0           4428 / 311295                      |
+==========================+===============+=================================================|
| 1       910B3            | OK            | 97.1        43                0    / 0         |
| 1       NA               | 0000:C2:00.0  | 12          4431 / 311295                      |
+==========================+===============+=================================================|
| 7       910B3            | Alarm         | 310.5       88                0    / 0         |
| 7       NA               | 0000:C8:00.0  | 96          310000 / 311295                   |
+==========================+===============+=================================================|
"""

# 真实 npu-smi 26.0.rc1 的输出格式（Ascend910 双 chip：每卡两组概要+device，功耗 chip1 为 -，
# 第三列含 DDR Memory-Usage 与 HBM-Usage 两组数字对，末尾附进程表）
SAMPLE_OUTPUT_26 = """
+------------------------------------------------------------------------------------------------------------------+
| npu-smi 26.0.rc1                            Version: 26.0.rc1                                                    |
+---------------------------+---------------+----------------------------------------------------------------------+
| NPU   Name                | Health        | Power(W)             Temp(C)                 Hugepages-Usage(page)   |
| Chip  Phy-ID              | Bus-Id        | AICore(%)            Memory-Usage(MB)        HBM-Usage(MB)           |
+===========================+===============+======================================================================+
| 0     Ascend910           | OK            | 204.0                43                      0    / 0                |
| 0     0                   | 0000:9D:00.0  | 100                  0    / 0                59807/ 65536            |
+------------------------------------------------------------------------------------------------------------------+
| 0     Ascend910           | OK            | -                    40                      0    / 0                |
| 1     1                   | 0000:9F:00.0  | 60                   0    / 0                59536/ 65536            |
+===========================+===============+======================================================================+
| 5     Ascend910           | OK            | 159.6                39                      0    / 0                |
| 0     10                  | 0000:89:00.0  | 0                    0    / 0                3140 / 65536            |
+------------------------------------------------------------------------------------------------------------------+
| 5     Ascend910           | OK            | -                    40                      0    / 0                |
| 1     11                  | 0000:8B:00.0  | 0                    0    / 0                2882 / 65536            |
+===========================+===============+======================================================================+
+---------------------------+---------------+----------------------------------------------------------------------+
| NPU     Chip              | Process id    | Process name       | Process memory(MB)    | Process id in container |
+===========================+===============+======================================================================+
| 0       0                 | 1774110       | VLLMWorker_PP      | 56684                 | NA                      |
| 0       1                 | 1777209       | VLLMWorker_PP      | 56684                 | NA                      |
+===========================+===============+======================================================================+
| No running processes found in NPU 5                                                                              |
+===========================+===============+======================================================================+
"""


# 真实 npu-smi 25.2.0 输出格式（910B4 单 chip：chip 行只有 chip 一列，无 phy 列；
# 第三列 Memory-Usage 与 HBM-Usage 两组数字对，HBM 是最后一组）
SAMPLE_OUTPUT_25 = """
+------------------------------------------------------------------------------------------------+
| npu-smi 25.2.0                   Version: 25.2.0                                               |
+---------------------------+---------------+----------------------------------------------------+
| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|
| Chip                      | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |
+===========================+===============+====================================================+
| 0     910B4-1             | OK            | 91.8        36                0    / 0             |
| 0                         | 0000:C1:00.0  | 0           0    / 0          3451 / 65536         |
+===========================+===============+====================================================+
| 1     910B4-1             | OK            | 89.4        37                0    / 0             |
| 0                         | 0000:C2:00.0  | 0           0    / 0          3436 / 65536         |
+===========================+===============+====================================================+
| 7     910B4-1             | Alarm         | 310.5       88                0    / 0             |
| 0                         | 0000:42:00.0  | 96          0    / 0          65000 / 65536        |
+===========================+===============+====================================================+
+---------------------------+---------------+----------------------------------------------------+
| NPU     Chip              | Process id    | Process name             | Process memory(MB)      |
+===========================+===============+====================================================+
| No running processes found in NPU 0                                                    |
+===========================+===============+====================================================+
"""


def test_parse_basic_fields():
    result = parse_npu_smi_output(SAMPLE_OUTPUT)
    assert result["npu_count"] == 3
    first = result["npus"][0]
    assert first["id"] == 0
    assert first["name"] == "910B3"
    assert first["health"] == "OK"
    assert first["power_w"] == 95.4
    assert first["temp_c"] == 42.0
    assert first["bus_id"] == "0000:C1:00.0"
    assert first["aicore_util"] == 0
    assert first["mem_used_mb"] == 4428
    assert first["mem_total_mb"] == 311295


def test_parse_merges_two_lines_per_card():
    result = parse_npu_smi_output(SAMPLE_OUTPUT)
    # 概要行和 device 行通过 NPU 编号合并到同一条记录
    assert result["npu_count"] == 3
    card7 = [n for n in result["npus"] if n["id"] == 7][0]
    assert card7["health"] == "Alarm"
    assert card7["mem_used_mb"] == 310000


def test_parse_empty_and_garbage():
    assert parse_npu_smi_output("") == {"npu_count": 0, "npus": []}
    result = parse_npu_smi_output("random text\n| no match here |\n")
    assert result["npu_count"] == 0


def test_parse_partial_columns_do_not_crash():
    # 缺列的行不匹配任何正则，被静默跳过
    result = parse_npu_smi_output("| 3       910B3            | OK            |\n")
    assert result["npu_count"] == 0


def test_parse_26_dual_chip():
    """26.x 双 chip 格式：配对聚合、HBM 显存、功耗取 chip0、AICore 取 max。"""
    result = parse_npu_smi_output(SAMPLE_OUTPUT_26)
    assert result["npu_count"] == 2  # NPU 0 和 NPU 5
    card0 = result["npus"][0]
    # 双 chip 配对到同一张卡（device 行的 chip/Phy-ID 编号不参与 NPU 归属）
    assert len(card0["chips"]) == 2
    assert card0["chips"][0]["bus_id"] == "0000:9D:00.0"
    assert card0["chips"][1]["bus_id"] == "0000:9F:00.0"
    # 功耗取首个非空（chip1 概要行是 "-"）
    assert card0["power_w"] == 204.0
    # 温度取各 chip 最大值
    assert card0["temp_c"] == 43.0
    # AICore 取各 chip 最大值（保守：任一 die 忙则卡视为忙）
    assert card0["aicore_util"] == 100
    # 显存取 HBM（最后一组数字对），不是 DDR 的 0/0
    assert card0["mem_used_mb"] == 59807
    assert card0["mem_total_mb"] == 65536
    # bus 取 chip0 的
    assert card0["bus_id"] == "0000:9D:00.0"
    # 进程表解析到对应 NPU
    assert len(card0["processes"]) == 2
    assert card0["processes"][0]["name"] == "VLLMWorker_PP"
    assert card0["processes"][0]["pid"] == 1774110


def test_parse_26_idle_card():
    """26.x 空闲卡：无进程、HBM 低占用。"""
    result = parse_npu_smi_output(SAMPLE_OUTPUT_26)
    card5 = [n for n in result["npus"] if n["id"] == 5][0]
    assert card5["processes"] == []  # "No running processes" 行被忽略
    assert card5["mem_used_mb"] == 3140  # chip0 3140 vs chip1 2882，取 max
    assert card5["aicore_util"] == 0
    assert card5["power_w"] == 159.6


def test_parse_950_format():
    """950 系格式（NPU ID 独立列、Bus-Id 为 NA）：配对解析与聚合。"""
    result = parse_npu_smi_output(SAMPLE_OUTPUT_950)
    assert result["npu_count"] == 3
    card0 = result["npus"][0]
    assert card0["id"] == 0
    assert card0["name"] == "Ascend950DT"
    assert card0["health"] == "OK"
    assert card0["power_w"] == 398.6
    assert card0["temp_c"] == 46.0
    assert card0["bus_id"] is None  # 950 格式 bus 位置是 NA
    assert card0["aicore_util"] == 0
    assert card0["mem_used_mb"] == 4765
    assert card0["mem_total_mb"] == 98304
    card7 = [n for n in result["npus"] if n["id"] == 7][0]
    assert card7["health"] == "Warning"
    assert card7["aicore_util"] == 42
    assert card7["mem_used_mb"] == 91000


def test_parse_910b4_single_chip_column():
    # 25.2 格式：chip 行只有 chip 一列（无 phy 列），不能与 24/26 的双列格式混淆
    result = parse_npu_smi_output(SAMPLE_OUTPUT_25)
    assert result["npu_count"] == 3
    first = result["npus"][0]
    assert first["id"] == 0
    assert first["name"] == "910B4-1"
    assert first["health"] == "OK"
    assert first["power_w"] == 91.8
    assert first["temp_c"] == 36.0
    assert first["bus_id"] == "0000:C1:00.0"
    assert first["aicore_util"] == 0
    # 显存取最后一组数字对（HBM 3451/65536），不是 Memory-Usage（0/0）
    assert first["mem_used_mb"] == 3451
    assert first["mem_total_mb"] == 65536
    # 进程表区域（No running processes 行）不产生卡记录
    card7 = [n for n in result["npus"] if n["id"] == 7][0]
    assert card7["health"] == "Alarm"
    assert card7["aicore_util"] == 96


def test_infer_machine_type():
    assert infer_machine_type("910B3") == "910B"
    assert infer_machine_type("310P4") == "310P"
    assert infer_machine_type("Ascend910C1") == "910C"
    assert infer_machine_type("Ascend910") == "910"
    assert infer_machine_type("Ascend950DT") == "950"
    assert infer_machine_type("Unknown") == "unknown"


def test_summarize_fleet():
    probed = {
        "10.0.0.1": {"reachable": True, "npu": {"npu_count": 8, "npus": [{"health": "OK"}] * 8}},
        "10.0.0.2": {"reachable": False, "npu": {"npu_count": 0, "npus": []}},
    }
    summary = summarize_fleet(probed)
    assert summary["machines_total"] == 2
    assert summary["machines_reachable"] == 1
    assert summary["npu_total"] == 8
    assert summary["npu_healthy"] == 8
