"""Run the engine test suite and report results."""

import sys
import os

# Add the source directory to the path
sys.path.insert(0, r"C:\Ronni\Projects\astra chatbot\src")

from tests.test_engine import *

def run_tests():
    """Run all test functions and report results."""
    # Collect test function names
    test_funcs = [name for name in dir() if name.startswith("test_") and name != "run_tests"]

    passed = 0
    failed = 0
    errors = 0

    print(f"Running {len(test_funcs)} tests...")
    print("=" * 60)

    for name in test_funcs:
        func = globals()[name]
        try:
            func()
            passed += 1
            print(f"  PASS: {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL: {name} — {e}")
            import traceback
            traceback.print_exc()

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed, {errors} errors")
    if failed > 0:
        sys.exit(1)
    else:
        print("All tests passed!")


if __name__ == "__main__":
    run_tests()