#!/usr/bin/env python3
"""无 pytest 依赖的简易测试运行器：python tests/run_tests.py"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_collectors  # noqa: E402
import test_npu_probe  # noqa: E402

MODULES = [test_npu_probe, test_collectors]


def main() -> int:
    failed = 0
    total = 0
    for module in MODULES:
        names = sorted(n for n in dir(module) if n.startswith("test_"))
        for name in names:
            total += 1
            func = getattr(module, name)
            try:
                func()
                print(f"PASS {module.__name__}.{name}")
            except AssertionError:
                failed += 1
                print(f"FAIL {module.__name__}.{name}")
                traceback.print_exc()
            except Exception as exc:  # noqa: BLE001 - 运行器要报告所有异常
                failed += 1
                print(f"ERROR {module.__name__}.{name}: {exc}")
                traceback.print_exc()
    print(f"\n{total - failed}/{total} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
