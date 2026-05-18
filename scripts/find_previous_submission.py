"""Find the latest approved submission tied to a specific issue.

Called by `.github/workflows/process-submission.yml` on `issues.reopened`
events. The output flows into `create_submission_pr.py --supersedes <id>`,
which is what makes the new submission a revision (Phase 2.5).

How it works:
  1. Walk `submissions/*/*.yml` in the working tree.
  2. Filter to entries whose `issue_number` matches.
  3. Filter to entries with `status: approved` (so superseded entries
     don't surface as the revision target; only the latest active does).
  4. Sort by approved_date descending, then by submission_id descending,
     and return the top result's submission_id.

The filesystem is the source of truth here — we don't need the GitHub
PR API. The submission YAML carries `issue_number`, which lets us
match without parsing PR bodies or doing GraphQL lookups.

Exits 0 with empty stdout when no matching submission is found (e.g.
the issue was closed without a merge, or it's a fresh issue that
shouldn't ever hit this script — the workflow guards on event action).
This is deliberate: a missing match means "treat as fresh submission",
not an error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import yaml


def find_latest_approved_for_issue(repo_root: Path, issue_number: int) -> Optional[str]:
    """Return the submission_id of the latest approved submission whose
    `issue_number` matches, or None if no match.

    "Latest" sorts by approved_date descending. When two entries share an
    approved_date (unlikely but possible — e.g. testing), the lexically
    greater submission_id wins, which naturally favours `-v3` over `-v2`
    over the bare original.
    """
    submissions_dir = repo_root / "submissions"
    if not submissions_dir.exists():
        return None

    matches: list[dict] = []
    for yaml_file in submissions_dir.rglob("*.yml"):
        try:
            with open(yaml_file, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("issue_number") != issue_number:
            continue
        if data.get("status") != "approved":
            continue
        matches.append(data)

    if not matches:
        return None

    matches.sort(
        key=lambda d: (
            d.get("approved_date") or "",
            d.get("submission_id") or "",
        ),
        reverse=True,
    )
    return matches[0].get("submission_id")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--issue", type=int, required=True,
                   help="Issue number to look up.")
    p.add_argument("--repo-root", default=".", type=Path,
                   help="Working tree root (default: current directory).")
    args = p.parse_args(argv)

    repo_root = args.repo_root.resolve()
    submission_id = find_latest_approved_for_issue(repo_root, args.issue)
    if submission_id:
        print(submission_id)
    # Otherwise print nothing — caller treats empty stdout as "no match".
    return 0


if __name__ == "__main__":
    sys.exit(main())
