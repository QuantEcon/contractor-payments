"""Check a parsed submission against its contract and ledger (submit/validate).

Runs right after `scripts.parse_issue` in `process-submission.yml`. It folds
its findings into the parser's errors JSON, so contract problems reach the
contractor through the same "needs a fix" comment as parse errors:

- the contract ID isn't valid, or has no `contracts/<id>.yml`;
- the contract is the wrong type for the form, or has ended;
- hourly dates fall outside the contract window;
- a milestone amount has more decimals than the currency allows;
- a milestone was already paid (per `ledger/<id>.yml`).

Non-blocking findings (milestone amount differs from the contract schedule,
milestone dates outside the window) are added as warnings, which the PR body
and the `/validate` comment both show.

On any error the submission JSON is deleted: the workflow decides
"parsed OK" by whether that file exists, so this turns the run into an
ordinary data-error run. Exit status is 0 whenever the check itself ran;
a non-zero exit means an engine fault.

CLI:
  python -m scripts.check_submission \\
      --submission-file /tmp/submission.json \\
      --errors-file /tmp/errors.json \\
      --issue 42
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import yaml

from scripts.contract_checks import check_against_contract, is_safe_contract_id
from scripts.find_previous_submission import find_latest_approved_for_issue


def _load_yaml(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else None


def run_checks(
    submission: dict,
    repo_root: Path,
    issue_number: Optional[int],
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a parsed submission in `repo_root`."""
    contract_id = submission.get("contract_id")
    if not is_safe_contract_id(contract_id):
        return [
            f"Contract `{contract_id}` isn't a valid contract ID. Pick a contract "
            f"from the dropdown."
        ], []

    contract = _load_yaml(repo_root / "contracts" / f"{contract_id}.yml")
    if contract is None:
        return [
            f"Contract `{contract_id}` isn't set up in this repository (no "
            f"`contracts/{contract_id}.yml`). Pick a contract from the dropdown, or "
            f"ask your administrator."
        ], []

    ledger = _load_yaml(repo_root / "ledger" / f"{contract_id}.yml")

    # A revision re-claims the milestones of the submission it replaces.
    exclude: list[str] = []
    if issue_number is not None:
        previous = find_latest_approved_for_issue(repo_root, issue_number)
        if previous:
            exclude.append(previous)

    return check_against_contract(
        submission, contract, ledger=ledger, exclude_submission_ids=exclude,
    )


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--submission-file", required=True, type=Path,
                   help="JSON from parse_issue.py --output-json. Deleted if checks fail.")
    p.add_argument("--errors-file", required=True, type=Path,
                   help="JSON from parse_issue.py --output-errors-json. Updated in place.")
    p.add_argument("--issue", type=int, default=None,
                   help="Issue number, used to recognise revisions.")
    p.add_argument("--repo-root", default=".", type=Path,
                   help="Contractor repo working tree (default: current directory).")
    args = p.parse_args(argv)

    with open(args.submission_file, encoding="utf-8") as f:
        submission = json.load(f)
    report = {"ok": True, "errors": [], "warnings": []}
    if args.errors_file.exists():
        with open(args.errors_file, encoding="utf-8") as f:
            report = json.load(f)

    errors, warnings = run_checks(submission, args.repo_root.resolve(), args.issue)

    report.setdefault("errors", []).extend({"line": None, "message": m} for m in errors)
    report.setdefault("warnings", []).extend({"message": m} for m in warnings)
    report["ok"] = not report["errors"]
    with open(args.errors_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        args.submission_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
