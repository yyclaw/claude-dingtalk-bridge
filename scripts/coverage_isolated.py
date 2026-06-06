#!/usr/bin/env python3
"""Per-module isolated branch-coverage gate.

The aggregate `pytest --cov` run reports a module as covered even when only
*other* test files exercise it incidentally — so a module whose own test is
weak can still show 100%. This gate re-runs each `tests/test_<mod>.py` alone,
measuring only `claude_dingtalk_bridge.<mod>`, so a line counts only when the
module's *own* test reaches it. Anything below 100% (branches included) fails.

Run:  .venv/bin/python scripts/coverage_isolated.py
Exits non-zero if any module is below 100% or its test run fails.

Coverage's own dynamic-context attribution can't replace this: in a single
combined run it drops some (line, test) records, so it misreports which test
covered a line. Re-importing the module under its own test process is the only
reliable signal.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "claude_dingtalk_bridge"
TESTS = ROOT / "tests"

# Module stem -> owning test stem, when they don't share a name.
TEST_OVERRIDES = {"__main__": "test_main"}

# Modules with no executable logic, exempt from the gate (keep the reason).
SKIP = {"__init__"}  # package docstring only


def owning_test(module: str) -> Path:
    stem = TEST_OVERRIDES.get(module, f"test_{module}")
    return TESTS / f"{stem}.py"


def measure(module: str, test: Path) -> dict:
    """Run one test file in isolation; return that module's coverage facts."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        report = Path(tf.name)
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "pytest", str(test), "-q",
                f"--cov=claude_dingtalk_bridge.{module}", "--cov-branch",
                f"--cov-report=json:{report}", "-o", "addopts=",
                "-p", "no:cacheprovider",
            ],
            cwd=ROOT, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return {"ok": False, "error": "test run failed", "log": proc.stdout + proc.stderr}
        data = json.loads(report.read_text())
    finally:
        report.unlink(missing_ok=True)

    key = next((k for k in data["files"] if k.endswith(f"{module}.py")), None)
    if key is None:
        # The module never imported under its own test — that itself is a gap.
        return {"ok": False, "error": "module not exercised by its own test"}
    fd = data["files"][key]
    return {
        "ok": True,
        "percent": fd["summary"]["percent_covered"],
        "missing_lines": fd["missing_lines"],
        "missing_branches": fd["missing_branches"],
    }


def main() -> int:
    failures = []
    for src in sorted(SRC.glob("*.py")):
        module = src.stem
        if module in SKIP:
            continue
        test = owning_test(module)
        if not test.exists():
            print(f"FAIL  {module}: no owning test ({test.relative_to(ROOT)})")
            failures.append(module)
            continue

        result = measure(module, test)
        if not result["ok"]:
            print(f"FAIL  {module}: {result['error']}")
            if result.get("log"):
                print("\n".join("      " + ln for ln in result["log"].splitlines()[-15:]))
            failures.append(module)
            continue

        if result["percent"] >= 100:
            print(f"OK    {module}: 100%")
            continue

        failures.append(module)
        lines = ", ".join(str(n) for n in result["missing_lines"])
        branches = ", ".join(f"{a}->{b}" for a, b in result["missing_branches"])
        print(f"FAIL  {module}: {result['percent']:.0f}%")
        if lines:
            print(f"      missing lines:    {lines}")
        if branches:
            print(f"      missing branches: {branches}")

    print("-" * 60)
    if failures:
        print(f"{len(failures)} module(s) below 100% self-coverage: {', '.join(failures)}")
        return 1
    print("All modules at 100% self-coverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
