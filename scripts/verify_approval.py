"""Verify a merged submission PR before it is treated as approved.

Runs in `process-approved.yml` before anything is stamped, ledgered or
emailed. Refuses (exit 1) when:

- the person who merged isn't on the engine's approver list
  (`quantecon.approvers` in `templates/fiscal-host.yml` — engine-side, so a
  contractor repo can't widen it);
- the PR changed anything outside `submissions/` and `generated_pdfs/`;
- the submission's totals don't match what its entries and the contract on
  `main` produce (a hand-edited YAML, a changed rate, a duplicate day...);
- an hourly entry falls outside the contract window, or a milestone was
  already paid.

On refusal it writes a Markdown report (`--report`) that the workflow's
failure comment includes, so the admin sees why and what to do.

CLI:
  python -m scripts.verify_approval \\
      --submission submissions/2026-05/jdoe-timesheet-2026-05.yml \\
      --changed-files /tmp/changed_files.txt \\
      --merged-by mmcky \\
      --fiscal-host engine/templates/fiscal-host.yml \\
      --report /tmp/verify_report.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import yaml

from scripts.contract_checks import (
    check_against_contract,
    is_safe_contract_id,
    verify_merge,
    verify_totals,
)


def load_approvers(fiscal_host_path: Path) -> list[str]:
    """`quantecon.approvers` from the engine's fiscal-host.yml; [] if absent."""
    if not fiscal_host_path.exists():
        return []
    with open(fiscal_host_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    approvers = (data.get("quantecon") or {}).get("approvers") or []
    return [str(a).strip().lstrip("@") for a in approvers if str(a).strip()]


def _load_yaml(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else None


def verify(
    *,
    submission: dict,
    repo_root: Path,
    changed_files: list[str],
    merged_by: str,
    approvers: list[str],
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a merged submission."""
    errors = verify_merge(changed_files, merged_by, approvers)
    warnings: list[str] = []

    # A hand-edited file can parse as a list or a scalar. Refuse with a
    # report rather than crash on `.get`, which would leave the failure
    # comment with no reason in it.
    if not isinstance(submission, dict):
        errors.append(
            f"The submission file isn't a YAML mapping (it parsed as "
            f"{type(submission).__name__}), so it can't be verified."
        )
        return errors, warnings

    contract_id = submission.get("contract_id")
    if not is_safe_contract_id(contract_id):
        errors.append(f"Contract ID `{contract_id}` isn't valid.")
        return errors, warnings
    contract = _load_yaml(repo_root / "contracts" / f"{contract_id}.yml")
    if contract is None:
        errors.append(f"`contracts/{contract_id}.yml` doesn't exist on `main`.")
        return errors, warnings

    errors.extend(verify_totals(submission, contract))

    ledger = _load_yaml(repo_root / "ledger" / f"{contract_id}.yml")
    exclude = [s for s in (submission.get("submission_id"), submission.get("supersedes")) if s]
    contract_errors, contract_warnings = check_against_contract(
        submission, contract, ledger=ledger,
        exclude_submission_ids=exclude, check_status=False,
    )
    errors.extend(contract_errors)
    warnings.extend(contract_warnings)

    submitter = submission.get("submitted_by")
    if submitter and merged_by and submitter.lower() == merged_by.lower():
        warnings.append(f"@{merged_by} approved their own submission.")
    return errors, warnings


def render_report(errors: list[str]) -> str:
    lines = ["**Approval refused — nothing was stamped, ledgered or emailed.**", ""]
    lines += [f"- {e}" for e in errors]
    lines += [
        "",
        "**What to do:** revert this merge on `main` (that removes the submission "
        "files it added), fix the cause above, then reopen the original issue and "
        "`/submit` again so the engine regenerates the PR from the issue. An "
        "authorised approver then merges the new PR. Contract or settings changes "
        "go in a separate admin PR, merged first.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--submission", required=True, type=Path)
    p.add_argument("--changed-files", required=True, type=Path,
                   help="Newline-separated paths the PR changed.")
    p.add_argument("--merged-by", required=True)
    p.add_argument("--fiscal-host", required=True, type=Path,
                   help="Engine templates/fiscal-host.yml (holds the approver list).")
    p.add_argument("--repo-root", default=".", type=Path)
    p.add_argument("--report", type=Path, default=None,
                   help="Write a Markdown refusal report here when verification fails.")
    args = p.parse_args(argv)

    with open(args.submission, encoding="utf-8") as f:
        submission = yaml.safe_load(f) or {}
    changed = args.changed_files.read_text(encoding="utf-8").splitlines()

    errors, warnings = verify(
        submission=submission,
        repo_root=args.repo_root.resolve(),
        changed_files=changed,
        merged_by=args.merged_by,
        approvers=load_approvers(args.fiscal_host),
    )
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        if args.report:
            args.report.write_text(render_report(errors), encoding="utf-8")
        return 1
    print("Verified: approver authorised, PR scope clean, totals match the contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
