#!/usr/bin/env python3
"""无 pytest 依赖的简易测试运行器：python tests/run_tests.py"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_filters  # noqa: E402


def main() -> int:
    failed = 0
    names = sorted(n for n in dir(test_filters) if n.startswith("test_"))
    for name in names:
        func = getattr(test_filters, name)
        try:
            func()
            print(f"PASS {name}")
        except AssertionError:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {exc}")
            traceback.print_exc()
    print(f"\n{len(names) - failed}/{len(names)} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
