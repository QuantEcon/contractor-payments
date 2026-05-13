"""Append an approved submission to its ledger.

Called by `.github/workflows/process-approved.yml` after
`scripts.finalize_approval` has stamped the submission YAML with approval
metadata. Pure file mutation; no external services. The companion
`scripts.update_ledger_issue` renders this ledger as a GitHub issue body
for human consumption.

Ledger layout (per contract):
  ledger/<contract-id>.yml

For hourly contracts:
  contract_id, type=hourly, currency,
  submissions[], totals.{hours_to_date, amount_to_date, submissions_count}

For milestone contracts:
  contract_id, type=milestone, currency,
  claims[], totals.{amount_to_date, claims_count}

Errors out if the same submission_id is already in the ledger (treated
as a workflow bug to surface, not silently dedup).

See PLAN.md §8 Phase 2.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import yaml


# Map submission.type → ledger.type. Ledger reuses the contract-side type
# names (`hourly`, `milestone`) rather than the submission-side names so
# that `contracts/<id>.yml.type` and `ledger/<id>.yml.type` agree.
_SUBMISSION_TO_LEDGER_TYPE = {
    "timesheet": "hourly",
    "milestone_invoice": "milestone",
}


def _empty_ledger(submission: dict) -> dict:
    """Construct an empty ledger shell matching the submission's contract.

    Used when no ledger file exists yet (first approved submission for the
    contract). The shape branches on submission type to give hourly its
    `submissions` list + `hours_to_date` total, milestone its `claims` list
    + per-claim count.
    """
    submission_type = submission["type"]
    ledger_type = _SUBMISSION_TO_LEDGER_TYPE.get(submission_type)
    if ledger_type is None:
        raise ValueError(f"Unknown submission type `{submission_type}`.")

    currency = submission["totals"]["currency"]
    base = {
        "contract_id": submission["contract_id"],
        "type": ledger_type,
        "currency": currency,
    }
    if ledger_type == "hourly":
        return {
            **base,
            "submissions": [],
            "totals": {
                "hours_to_date": 0,
                "amount_to_date": 0,
                "submissions_count": 0,
            },
        }
    # milestone
    return {
        **base,
        "claims": [],
        "totals": {
            "amount_to_date": 0,
            "claims_count": 0,
        },
    }


def _build_entry(submission: dict) -> dict:
    """Build a single ledger entry from an approved submission.

    Hourly entries record the rolled-up totals (hours, rate, amount).
    Milestone entries preserve the full per-milestone breakdown — useful
    when an admin needs to look back at "which specific milestones were
    paid in this submission" without opening the submission YAML itself.
    """
    submission_type = submission["type"]
    totals = submission["totals"]

    base = {
        "submission_id": submission["submission_id"],
        "period": submission["period"],
        "approved_date": submission["approved_date"],
        "approved_by": submission["approved_by"],
    }

    if submission_type == "timesheet":
        return {
            **base,
            "hours": totals["hours"],
            "rate": totals["rate"],
            "amount": totals["amount"],
        }
    if submission_type == "milestone_invoice":
        return {
            **base,
            "entries": submission["entries"],
            "amount": totals["amount"],
        }
    raise ValueError(f"Unknown submission type `{submission_type}`.")


def append_submission(submission: dict, ledger: dict) -> dict:
    """Pure transform: append a submission to a ledger and recompute totals.

    Validates that the ledger and submission agree on contract_id, type,
    and currency. Raises on duplicate submission_id.
    """
    # Cross-checks
    if ledger.get("contract_id") != submission["contract_id"]:
        raise ValueError(
            f"Contract ID mismatch: ledger says `{ledger.get('contract_id')}`, "
            f"submission says `{submission['contract_id']}`."
        )

    expected_ledger_type = _SUBMISSION_TO_LEDGER_TYPE.get(submission["type"])
    if ledger.get("type") != expected_ledger_type:
        raise ValueError(
            f"Type mismatch: ledger says `{ledger.get('type')}`, "
            f"submission says `{submission['type']}` "
            f"(expected ledger type `{expected_ledger_type}`)."
        )

    ledger_currency = ledger.get("currency")
    submission_currency = submission["totals"]["currency"]
    if ledger_currency != submission_currency:
        raise ValueError(
            f"Currency mismatch: ledger says `{ledger_currency}`, "
            f"submission says `{submission_currency}`."
        )

    list_key = "submissions" if ledger["type"] == "hourly" else "claims"
    items = list(ledger.get(list_key, []))

    # Idempotency: duplicate submission_id is treated as a workflow bug.
    submission_id = submission["submission_id"]
    if any(item["submission_id"] == submission_id for item in items):
        raise ValueError(
            f"Submission `{submission_id}` is already in the ledger. "
            f"process-approved.yml should only fire once per merge — investigate "
            f"the workflow run history before retrying."
        )

    items.append(_build_entry(submission))

    out = dict(ledger)
    out[list_key] = items
    if ledger["type"] == "hourly":
        total_hours = sum(item["hours"] for item in items)
        total_amount = sum(item["amount"] for item in items)
        out["totals"] = {
            "hours_to_date": round(total_hours, 2),
            "amount_to_date": total_amount,
            "submissions_count": len(items),
        }
    else:
        total_amount = sum(item["amount"] for item in items)
        out["totals"] = {
            "amount_to_date": total_amount,
            "claims_count": len(items),
        }
    return out


def ledger_path_for_submission(submission: dict, repo_root: Path) -> Path:
    """Canonical ledger path: `ledger/<contract-id>.yml` relative to repo root."""
    return repo_root / "ledger" / f"{submission['contract_id']}.yml"


def load_or_create_ledger(ledger_path: Path, submission: dict) -> dict:
    """Load existing ledger from disk; or build an empty shell from the
    submission if no file exists yet."""
    if ledger_path.exists():
        with open(ledger_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if data is not None else _empty_ledger(submission)
    return _empty_ledger(submission)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--submission", required=True, type=Path,
                   help="Path to the approved submission YAML.")
    p.add_argument("--repo-root", default=".", type=Path,
                   help="Working tree root (default: current directory). "
                        "Determines where ledger/<contract-id>.yml lives.")
    args = p.parse_args(argv)

    repo_root = args.repo_root.resolve()

    with open(args.submission, encoding="utf-8") as f:
        submission = yaml.safe_load(f)

    ledger_path = ledger_path_for_submission(submission, repo_root)
    ledger = load_or_create_ledger(ledger_path, submission)
    updated = append_submission(submission, ledger)

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            updated, f,
            default_flow_style=False, sort_keys=False, allow_unicode=True, width=100,
        )

    print(f"Updated: {ledger_path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
