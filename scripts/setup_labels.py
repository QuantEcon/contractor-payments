"""Create the workflow labels required on a contractor repo.

GitHub Issue Forms silently drop `labels:` entries that don't exist on the
repo, which would break the workflow's label-based routing. This script
idempotently creates every label the contractor-payments workflows need.

Phase 3's `onboarding/new-contractor.py` will call this as part of repo
setup. Pre-Phase-3, the admin runs it manually against any new contractor
repo (or against `contractor-engine-test` to keep labels in sync).

Usage:
  python -m scripts.setup_labels                    # uses current dir's repo
  python -m scripts.setup_labels --repo OWNER/NAME  # explicit target

Requires the `gh` CLI authenticated for the target org.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Optional


# Each entry: (name, description, hex color without leading #).
# Colors borrowed from the existing repo conventions (timesheet, parse-error,
# pending-review, submission were already in the test repo at those values).
LABELS: list[tuple[str, str, str]] = [
    ("timesheet",        "Hourly timesheet submission",                "1d76db"),
    ("milestone-invoice", "Milestone invoice submission",              "1d76db"),
    ("reimbursement",    "Reimbursement claim submission (Phase 5+)",  "1d76db"),
    ("pending-review",   "Awaiting admin review",                      "fbca04"),
    ("parse-error",      "Submission has parse errors; see comment",   "d73a4a"),
    ("submission",       "Auto-generated submission PR",               "0e8a16"),
    ("processed",        "Submission merged and recorded in ledger",   "6f42c1"),
]


def create_label(name: str, description: str, color: str, repo: Optional[str]) -> str:
    """Idempotently create one label. Returns a one-line status string."""
    cmd = ["gh", "label", "create", name,
           "--description", description,
           "--color", color]
    if repo:
        cmd.extend(["--repo", repo])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return f"created  {name}"
    # gh exits non-zero when the label already exists. Detect that case and
    # treat it as a no-op so the script stays idempotent.
    stderr = (result.stderr or "").lower()
    if "already exists" in stderr:
        return f"exists   {name}"
    raise RuntimeError(
        f"Failed to create label `{name}`: "
        f"exit {result.returncode}; stderr: {result.stderr.strip()}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo",
        help="Target repo as OWNER/NAME (default: the repo the current "
             "working directory is in, per `gh`).",
    )
    args = parser.parse_args(argv)

    for name, description, color in LABELS:
        try:
            status = create_label(name, description, color, args.repo)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(status)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
