"""Prove every hardening fix by running its tests against the unfixed code.

A claim that a defect is fixed is only worth as much as the proof. This script
checks out the upstream commit this fork started from, copies the current test
suite over it, and runs each fix's tests twice: once against the original code
(where they must fail) and once against this fork (where they must pass).

The result is written to evidence/hardening-evidence.md, with the raw pytest
output kept alongside it so any claim can be traced back to a real test run.

Usage:
    python scripts/verify_hardening.py
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# The upstream commit this fork branched from.
BASELINE_COMMIT = "f427768"

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / "evidence"
RAW_DIR = EVIDENCE_DIR / "raw"
WORKTREE_DIR = REPO_ROOT.parent / "u2-mcp-baseline"


@dataclass
class Fix:
    """One defect, its tests, and why it matters.

    Attributes:
        key: Short identifier used in filenames and the report
        title: What was wrong, in plain language
        impact: What it costs an operator in production
        tests: pytest node ids that fail before the fix and pass after it
    """

    key: str
    title: str
    impact: str
    tests: list[str] = field(default_factory=list)


FIXES: list[Fix] = [
    Fix(
        key="H1",
        title="Non-ASCII business data was silently deleted from query output",
        impact=(
            "Accented customer names and currency symbols vanished from results with no "
            "error, so an operator could not tell a wrong answer from a right one."
        ),
        tests=[
            "tests/test_connection.py::TestOutputSanitization::test_accented_customer_names_survive",
            "tests/test_connection.py::TestOutputSanitization::test_currency_symbols_survive",
        ],
    ),
    Fix(
        key="H2",
        title="Auto-reconnect never reconnected",
        impact=(
            "After the database session dropped, every later request received a null "
            "session. In stdio mode -- the default -- no watchdog exists to clear the "
            "stale record, so the server stayed broken until someone restarted it."
        ),
        tests=[
            "tests/test_connection.py::TestConnectionLifecycle"
            "::test_get_session_reconnects_when_session_went_inactive",
        ],
    ),
]


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output as text."""
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def prepare_baseline_worktree() -> Path:
    """Create a worktree at the upstream commit with the current tests copied in.

    Returns:
        Path to a prepared baseline checkout with its own virtual environment
    """
    if WORKTREE_DIR.exists():
        run(["git", "worktree", "remove", "--force", str(WORKTREE_DIR)], cwd=REPO_ROOT)
        shutil.rmtree(WORKTREE_DIR, ignore_errors=True)

    print(f"Creating baseline worktree at {BASELINE_COMMIT} ...")
    result = run(
        ["git", "worktree", "add", "--detach", str(WORKTREE_DIR), BASELINE_COMMIT], cwd=REPO_ROOT
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not create worktree: {result.stderr}")

    # The tests are the measuring instrument, so they must be identical on both sides.
    shutil.rmtree(WORKTREE_DIR / "tests", ignore_errors=True)
    shutil.copytree(
        REPO_ROOT / "tests",
        WORKTREE_DIR / "tests",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    print("Installing baseline dependencies ...")
    run(["uv", "venv", "--python", "3.12", ".venv"], cwd=WORKTREE_DIR)
    install = run(
        ["uv", "pip", "install", "-e", ".[dev]", "--python", ".venv/Scripts/python.exe"],
        cwd=WORKTREE_DIR,
    )
    if install.returncode != 0:
        raise RuntimeError(f"Could not install baseline: {install.stderr}")

    return WORKTREE_DIR


def python_for(root: Path) -> str:
    """Return the interpreter path for a checkout's virtual environment."""
    windows_python = root / ".venv" / "Scripts" / "python.exe"
    return str(windows_python if windows_python.exists() else root / ".venv" / "bin" / "python")


def run_tests(root: Path, node_ids: list[str]) -> tuple[bool, str]:
    """Run the given tests in a checkout.

    Args:
        root: Checkout to run in
        node_ids: pytest node ids to select

    Returns:
        Tuple of (every test passed, captured output)
    """
    result = run([python_for(root), "-m", "pytest", *node_ids, "-p", "no:cacheprovider"], cwd=root)
    output = result.stdout + result.stderr
    return result.returncode == 0, output


def write_raw_log(name: str, content: str) -> str:
    """Write raw pytest output and return its path relative to the report."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / name
    path.write_text(content, encoding="utf-8")
    return path.relative_to(EVIDENCE_DIR).as_posix()


def build_report(rows: list[dict[str, str]]) -> str:
    """Render the evidence report as markdown."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Hardening evidence",
        "",
        f"Generated {stamp} by `scripts/verify_hardening.py`.",
        "",
        f"Each fix below is measured with the same tests against two checkouts: the upstream "
        f"code at `{BASELINE_COMMIT}`, and this fork. A fix counts as proven only when its "
        "tests fail on the original and pass here.",
        "",
        "| Fix | Defect | Upstream | This fork | Raw output |",
        "|-----|--------|----------|-----------|------------|",
    ]
    for row in rows:
        lines.append(
            f"| {row['key']} | {row['title']} | {row['before']} | {row['after']} | "
            f"[before]({row['before_log']}) / [after]({row['after_log']}) |"
        )

    lines.extend(["", "## What each defect cost", ""])
    for row in rows:
        lines.extend([f"### {row['key']} — {row['title']}", "", row["impact"], ""])

    return "\n".join(lines) + "\n"


def main() -> int:
    """Verify every registered fix and write the evidence report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-worktree",
        action="store_true",
        help="Leave the baseline checkout in place for inspection",
    )
    args = parser.parse_args()

    baseline = prepare_baseline_worktree()
    rows: list[dict[str, str]] = []
    all_proven = True

    for fix in FIXES:
        print(f"\n{fix.key}: {fix.title}")

        before_passed, before_output = run_tests(baseline, fix.tests)
        after_passed, after_output = run_tests(REPO_ROOT, fix.tests)

        proven = (not before_passed) and after_passed
        all_proven = all_proven and proven
        print(f"  upstream: {'PASS' if before_passed else 'FAIL'} (expected FAIL)")
        print(f"  fork:     {'PASS' if after_passed else 'FAIL'} (expected PASS)")
        print(f"  proven:   {'yes' if proven else 'NO'}")

        rows.append(
            {
                "key": fix.key,
                "title": fix.title,
                "impact": fix.impact,
                "before": "fails" if not before_passed else "unexpectedly passes",
                "after": "passes" if after_passed else "STILL FAILS",
                "before_log": write_raw_log(f"{fix.key}-upstream.txt", before_output),
                "after_log": write_raw_log(f"{fix.key}-fork.txt", after_output),
            }
        )

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    report_path = EVIDENCE_DIR / "hardening-evidence.md"
    report_path.write_text(build_report(rows), encoding="utf-8")
    print(f"\nWrote {report_path.relative_to(REPO_ROOT)}")

    if not args.keep_worktree:
        run(["git", "worktree", "remove", "--force", str(baseline)], cwd=REPO_ROOT)

    return 0 if all_proven else 1


if __name__ == "__main__":
    sys.exit(main())
