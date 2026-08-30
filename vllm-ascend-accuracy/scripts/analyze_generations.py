#!/usr/bin/env python3
"""对 JSONL 推理结果做轻量异常筛查。"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def nested_get(record: dict[str, Any], field: str) -> Any:
    """按点分路径读取字段，例如 choices.0.text。"""
    value: Any = record
    for part in field.split("."):
        if isinstance(value, list):
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise KeyError(field)
    return value


def repeated_ngram_fraction(text: str, n: int) -> float:
    """返回重复字符 n-gram 占全部 n-gram 的比例。"""
    compact = "".join(text.split())
    if len(compact) < n:
        return 0.0
    grams = [compact[i : i + n] for i in range(len(compact) - n + 1)]
    counts = Counter(grams)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    return repeated / len(grams)


def load_jsonl(path: Path, text_field: str, id_field: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                text = nested_get(record, text_field)
                if not isinstance(text, str):
                    raise TypeError(f"字段 {text_field} 不是字符串")
                record_id = nested_get(record, id_field) if id_field else len(rows)
            except (json.JSONDecodeError, KeyError, IndexError, ValueError, TypeError) as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc
            rows.append({"id": str(record_id), "text": text})
    return rows


def analyze(rows: list[dict[str, Any]], ngram: int, threshold: float) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    lengths: list[int] = []
    counters = Counter()
    for row in rows:
        text = row["text"]
        fraction = repeated_ngram_fraction(text, ngram)
        flags: list[str] = []
        if not text.strip():
            flags.append("empty")
        if "\ufffd" in text:
            flags.append("replacement_character")
        if CONTROL_RE.search(text):
            flags.append("control_character")
        if fraction >= threshold and len("".join(text.split())) >= ngram * 4:
            flags.append("high_ngram_repetition")
        counters.update(flags)
        lengths.append(len(text))
        if flags:
            details.append(
                {
                    "id": row["id"],
                    "length": len(text),
                    "repeated_ngram_fraction": round(fraction, 6),
                    "flags": flags,
                }
            )
    return {
        "records": len(rows),
        "flag_counts": dict(counters),
        "length": {
            "min": min(lengths, default=0),
            "max": max(lengths, default=0),
            "mean": round(sum(lengths) / len(lengths), 3) if lengths else 0,
        },
        "flagged_records": details,
    }


def compare(rows: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> dict[str, Any]:
    current = {row["id"]: row["text"] for row in rows}
    reference = {row["id"]: row["text"] for row in baseline}
    shared = sorted(current.keys() & reference.keys())
    exact = sum(current[key] == reference[key] for key in shared)
    ratios = [
        len(current[key]) / len(reference[key])
        for key in shared
        if len(reference[key]) > 0
    ]
    return {
        "shared_ids": len(shared),
        "exact_matches": exact,
        "exact_match_rate": round(exact / len(shared), 6) if shared else None,
        "missing_from_current": sorted(reference.keys() - current.keys()),
        "missing_from_baseline": sorted(current.keys() - reference.keys()),
        "mean_length_ratio": round(math.fsum(ratios) / len(ratios), 6) if ratios else None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="筛查 JSONL 生成结果中的空输出、异常字符和 n-gram 复读。"
    )
    parser.add_argument("input", type=Path, help="待分析 JSONL")
    parser.add_argument("--text-field", default="text", help="文本字段的点分路径")
    parser.add_argument("--id-field", help="用于基线对齐的 ID 字段点分路径")
    parser.add_argument("--baseline", type=Path, help="可选的参考 JSONL")
    parser.add_argument("--ngram", type=int, default=4, help="字符 n-gram 大小")
    parser.add_argument(
        "--repetition-threshold",
        type=float,
        default=0.35,
        help="标记高重复的 n-gram 比例阈值",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.ngram < 1:
        print("--ngram 必须大于 0", file=sys.stderr)
        return 2
    if not 0 <= args.repetition_threshold <= 1:
        print("--repetition-threshold 必须在 0 到 1 之间", file=sys.stderr)
        return 2
    try:
        rows = load_jsonl(args.input, args.text_field, args.id_field)
        report = analyze(rows, args.ngram, args.repetition_threshold)
        if args.baseline:
            baseline = load_jsonl(args.baseline, args.text_field, args.id_field)
            report["baseline_comparison"] = compare(rows, baseline)
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
