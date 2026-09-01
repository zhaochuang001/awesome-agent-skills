#!/usr/bin/env python3
"""fleet 历史 SQLite 封装：建表、写入、查询、保留期清理。

- npu_samples 随每轮探测写入（30s/120s 自适应频率）；
- machine_samples 低频（磁盘挂载、Docker、负载），约每 5 分钟一轮；
- 保留 7 天，每次写入后顺手清理，无需独立任务；
- 线程安全：所有操作走同一个连接 + 锁（探测线程与 API 查询线程并发）。

依赖：仅 Python 标准库 sqlite3。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from common import STATE_DIR

HISTORY_DB_PATH = STATE_DIR / "fleet-history.db"
RETENTION_SECONDS = 7 * 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS npu_samples (
    ts        REAL NOT NULL,
    host      TEXT NOT NULL,
    npu_id    INTEGER NOT NULL,
    aicore    INTEGER,
    mem_used  INTEGER,
    mem_total INTEGER,
    temp_c    REAL,
    power_w   REAL,
    health    TEXT
);
CREATE INDEX IF NOT EXISTS idx_npu_samples_query
    ON npu_samples (host, npu_id, ts);

CREATE TABLE IF NOT EXISTS machine_samples (
    ts        REAL NOT NULL,
    host      TEXT NOT NULL,
    reachable INTEGER NOT NULL,
    load1     REAL,
    disks     TEXT,
    docker    TEXT,
    cpu_percent REAL,
    memory_used INTEGER,
    memory_total INTEGER
);
CREATE INDEX IF NOT EXISTS idx_machine_samples_query
    ON machine_samples (host, ts);
"""

# 旧库迁移：machine_samples 补 CPU/内存列（列已存在时忽略）
_MIGRATIONS = (
    "ALTER TABLE machine_samples ADD COLUMN cpu_percent REAL",
    "ALTER TABLE machine_samples ADD COLUMN memory_used INTEGER",
    "ALTER TABLE machine_samples ADD COLUMN memory_total INTEGER",
)

# metric -> (SQL 列, 是否为比率分母配合列)
_METRIC_COLUMNS = {
    "aicore": "aicore",
    "mem": "mem_used",
    "temp": "temp_c",
    "power": "power_w",
}


class FleetHistory:
    """SQLite 历史库。一个实例内部串行化所有访问。"""

    def __init__(self, db_path: Path = HISTORY_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        for statement in _MIGRATIONS:
            try:
                self._conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # 列已存在
        self._conn.commit()
        self._last_cleanup = 0.0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def record_probe(
        self,
        host: str,
        npus: list[dict[str, Any]],
        *,
        load1: float | None = None,
        disks: list[dict[str, Any]] | None = None,
        docker: list[dict[str, str]] | None = None,
        reachable: bool = True,
        machine_sample: bool = False,
        cpu_percent: float | None = None,
        memory_used: int | None = None,
        memory_total: int | None = None,
    ) -> None:
        """记录一轮探测结果。machine_sample=True 时同时落 machine_samples 行。"""
        now = time.time()
        with self._lock:
            rows = [
                (
                    now,
                    host,
                    int(npu.get("id", -1)),
                    npu.get("aicore_util"),
                    npu.get("mem_used_mb"),
                    npu.get("mem_total_mb"),
                    npu.get("temp_c"),
                    npu.get("power_w"),
                    npu.get("health"),
                )
                for npu in npus
            ]
            if rows:
                self._conn.executemany(
                    "INSERT INTO npu_samples (ts, host, npu_id, aicore, mem_used, mem_total, temp_c, power_w, health)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            if machine_sample:
                self._conn.execute(
                    "INSERT INTO machine_samples"
                    " (ts, host, reachable, load1, disks, docker, cpu_percent, memory_used, memory_total)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        host,
                        1 if reachable else 0,
                        load1,
                        json.dumps(disks, ensure_ascii=False) if disks is not None else None,
                        json.dumps(docker, ensure_ascii=False) if docker is not None else None,
                        cpu_percent,
                        memory_used,
                        memory_total,
                    ),
                )
            self._conn.commit()
            self._maybe_cleanup(now)

    def _maybe_cleanup(self, now: float) -> None:
        """每小时最多清理一次，删掉保留期之外的数据。"""
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        cutoff = now - RETENTION_SECONDS
        self._conn.execute("DELETE FROM npu_samples WHERE ts < ?", (cutoff,))
        self._conn.execute("DELETE FROM machine_samples WHERE ts < ?", (cutoff,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query_npu_series(
        self, host: str, npu_id: int, metric: str, range_seconds: int
    ) -> list[tuple[float, Any]]:
        """查询某卡某指标在时间范围内的 (ts, value) 序列，按 ts 升序。"""
        column = _METRIC_COLUMNS.get(metric)
        if column is None:
            raise ValueError(f"unknown metric: {metric}（可选：{sorted(_METRIC_COLUMNS)}）")
        cutoff = time.time() - range_seconds
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT ts, {column} FROM npu_samples"
                " WHERE host = ? AND npu_id = ? AND ts >= ? ORDER BY ts",
                (host, npu_id, cutoff),
            )
            return [(ts, value) for ts, value in cursor.fetchall() if value is not None]

    def query_machine_series(
        self, host: str, range_seconds: int, field: str
    ) -> list[tuple[float, Any]]:
        """查询机器级低频序列。field 取 load1 / disks / docker。"""
        if field not in ("load1", "disks", "docker"):
            raise ValueError(f"unknown machine field: {field}")
        cutoff = time.time() - range_seconds
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT ts, {field} FROM machine_samples"
                " WHERE host = ? AND ts >= ? ORDER BY ts",
                (host, cutoff),
            )
            return cursor.fetchall()

    def query_aggregate_buckets(
        self, host: str, range_seconds: int, bucket_seconds: int
    ) -> list[dict[str, Any]]:
        """按时间桶聚合该机器全部卡的采样，返回每桶的平均利用率/显存比/温度/功耗。

        面板趋势线与热力图的数据源：每桶输出
        {bucket, npu_util_percent, hbm_percent, temp_c, power_w, sample_count}。
        """
        cutoff = time.time() - range_seconds
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT (CAST(ts / ? AS INTEGER) * ?) AS bucket,
                       AVG(aicore)  AS util,
                       AVG(CASE WHEN mem_total > 0
                            THEN mem_used * 100.0 / mem_total END) AS hbm_pct,
                       AVG(temp_c)  AS temp_c,
                       AVG(power_w) AS power_w,
                       COUNT(*)     AS samples
                FROM npu_samples
                WHERE host = ? AND ts >= ? AND aicore IS NOT NULL
                GROUP BY bucket ORDER BY bucket
                """,
                (bucket_seconds, bucket_seconds, host, cutoff),
            )
            rows = cursor.fetchall()
        return [
            {
                "bucket": row[0],
                "npu_util_percent": round(row[1], 1) if row[1] is not None else None,
                "hbm_percent": round(row[2], 1) if row[2] is not None else None,
                "temp_c": round(row[3], 1) if row[3] is not None else None,
                "power_w": round(row[4], 1) if row[4] is not None else None,
                "sample_count": row[5],
            }
            for row in rows
        ]

    def query_machine_buckets(
        self, host: str, range_seconds: int, bucket_seconds: int
    ) -> list[dict[str, Any]]:
        """按时间桶聚合机器级低频采样：CPU% / 内存% / 磁盘最高水位 / load1。"""
        cutoff = time.time() - range_seconds
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT (CAST(ts / ? AS INTEGER) * ?) AS bucket,
                       AVG(cpu_percent) AS cpu,
                       AVG(CASE WHEN memory_total > 0
                            THEN memory_used * 100.0 / memory_total END) AS mem_pct,
                       AVG(load1)     AS load1,
                       disks          AS disks_json,
                       COUNT(*)       AS samples
                FROM machine_samples
                WHERE host = ? AND ts >= ?
                GROUP BY bucket ORDER BY bucket
                """,
                (bucket_seconds, bucket_seconds, host, cutoff),
            )
            rows = cursor.fetchall()
        result = []
        for row in rows:
            disk_max = None
            if row[4]:
                try:
                    disks = json.loads(row[4])
                    pcts = [d.get("use_pct") for d in disks if isinstance(d.get("use_pct"), (int, float))]
                    disk_max = max(pcts) if pcts else None
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(
                {
                    "bucket": row[0],
                    "cpu_percent": round(row[1], 1) if row[1] is not None else None,
                    "memory_percent": round(row[2], 1) if row[2] is not None else None,
                    "load1": round(row[3], 2) if row[3] is not None else None,
                    "disk_max_percent": round(disk_max, 1) if disk_max is not None else None,
                    "sample_count": row[5],
                }
            )
        return result

    def coverage(self) -> dict[str, Any]:
        """历史库概况（面板可显示数据从何时开始有）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(ts), MAX(ts), COUNT(*) FROM npu_samples"
            ).fetchone()
        if not row or row[2] == 0:
            return {"samples": 0}
        return {
            "earliest": row[0],
            "latest": row[1],
            "samples": row[2],
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
