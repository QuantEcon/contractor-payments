"""Post a verbose internal-audit comment on the closed submission issue
and (optionally) on the merged PR.

Confirms three things in one place so admins can see at a glance what
happened on this merge:
  1. The submission was approved (by whom, when).
  2. The ledger was updated (one-line summary + running total).
  3. The approval email was sent (recipients + timestamp + testing_mode).

Posts identical content on both targets. Why both: the issue is the
contractor-facing surface (they opened it; closing it is the visible
"done" signal); the PR is the admin-facing surface (anyone reviewing the
merged PR later sees what happened without hunting for the originating
issue). One comment body, two destinations.

Designed to be the LAST step in `.github/workflows/process-approved.yml`.
That ordering means the comment reflects the actual outcome of every
preceding step — including whether the email send succeeded.

Reads the email summary from a JSON file written by `scripts.notify_email`
(see its `--output-summary` flag).

See PLAN.md §8 Phase 2.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml


# Same emoji conventions used in the running-ledger issue body, so admins
# learn one visual vocabulary across both surfaces.


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        raise RuntimeError(f"{path} is empty.")
    return data


def _fmt_amount(amount: float, currency: str) -> str:
    if currency.upper() == "JPY":
        return f"{int(round(amount)):,}"
    return f"{amount:,.2f}"


_TYPE_LABEL = {
    "timesheet": "Timesheet",
    "milestone_invoice": "Milestone Invoice",
    "reimbursement": "Reimbursement Claim",
}


def compose_comment(
    *,
    submission: dict,
    ledger: dict,
    email_summary: Optional[dict],
    issue_number: int,
) -> str:
    """Render the markdown body. Pure function — all inputs already loaded.

    `email_summary` may be None if the email step was skipped or failed
    (we still want to post the comment so the admin sees the partial
    outcome). When present, it's the JSON dict written by notify_email.
    """
    submission_type = submission.get("type", "timesheet")
    type_label = _TYPE_LABEL.get(submission_type, "Submission")
    contract_id = submission["contract_id"]
    period = submission["period"]
    approver = submission.get("approved_by", "—")
    approved_date = submission.get("approved_date", "—")
    currency = submission["totals"].get("currency", "")
    amount = submission["totals"].get("amount", 0)
    amount_display = f"{_fmt_amount(amount, currency)} {currency}".strip()

    # Ledger running totals
    if ledger.get("type") == "hourly":
        totals = ledger.get("totals", {})
        ledger_line = (
            f"📒 **Ledger:** `{contract_id}` — added {amount_display} "
            f"(running total: {_fmt_amount(totals.get('amount_to_date', 0), currency)} {currency} "
            f"across {totals.get('submissions_count', 0)} submission(s); "
            f"{totals.get('hours_to_date', 0)} hours)."
        )
    else:  # milestone
        totals = ledger.get("totals", {})
        ledger_line = (
            f"📒 **Ledger:** `{contract_id}` — added {amount_display} "
            f"(running total: {_fmt_amount(totals.get('amount_to_date', 0), currency)} {currency} "
            f"across {totals.get('claims_count', 0)} claim(s))."
        )

    # Email summary line
    if email_summary is None:
        email_line = "📧 **Email:** ⚠️ not sent — see workflow logs."
    else:
        recipients = email_summary.get("to", "—")
        cc = email_summary.get("cc")
        if cc:
            recipients = f"{recipients} (Cc {cc})"
        sent_at = email_summary.get("sent_at", "—")
        testing_mode = email_summary.get("testing_mode", True)
        mode_note = " — `testing_mode=true`, PSL not contacted" if testing_mode else ""
        dry_run = email_summary.get("dry_run", False)
        if dry_run:
            email_line = (
                f"📧 **Email:** dry-run only (no message sent). "
                f"Would have gone to {recipients}{mode_note}."
            )
        else:
            email_line = (
                f"📧 **Email:** sent to {recipients} at {sent_at}{mode_note}."
            )

    lines = [
        f"✅ **{type_label} approved** by @{approver} on {approved_date}.",
        "",
        f"**Contract:** `{contract_id}`  ",
        f"**Period:** `{period}`  ",
        f"**Amount:** {amount_display}",
        "",
        ledger_line,
        email_line,
        "",
        "<!-- contractor-payments-approval-summary -->",
    ]
    return "\n".join(lines) + "\n"


def post_comment(target_number: int, body: str, *, repo: Optional[str] = None) -> None:
    """Post a new comment on the issue or PR via `gh issue comment --body-file`.

    Works for both issues and PRs because they share GitHub's issue
    numbering and the issue-comment endpoint accepts either.
    """
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    fd, path = tempfile.mkstemp(suffix=".md", prefix="approval-comment-")
    body_path = Path(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        cmd = ["gh", "issue", "comment", str(target_number),
               "--body-file", str(body_path)]
        if repo:
            cmd.extend(["--repo", repo])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"gh issue comment failed (exit {result.returncode}). stderr:\n{result.stderr}"
            )
    finally:
        body_path.unlink(missing_ok=True)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--submission", required=True, type=Path,
                   help="Path to the approved submission YAML.")
    p.add_argument("--ledger", required=True, type=Path,
                   help="Path to the updated ledger YAML.")
    p.add_argument("--email-summary", type=Path, default=None,
                   help="Path to the JSON summary written by notify_email.py "
                        "(its --output-summary). Omit if the email step was "
                        "skipped or failed — comment will reflect that.")
    p.add_argument("--issue", type=int, required=True,
                   help="Issue number to comment on (the submission's original issue).")
    p.add_argument("--pr", type=int, default=None,
                   help="Optional PR number to also comment on (the merged "
                        "submission PR). Posts the same body to both targets.")
    p.add_argument("--repo", default=None,
                   help="GitHub owner/name. Default: $GITHUB_REPOSITORY.")
    p.add_argument("--dry-run", action="store_true",
                   help="Render the comment to stdout instead of posting.")
    args = p.parse_args(argv)

    submission = _load_yaml(args.submission)
    ledger = _load_yaml(args.ledger)

    email_summary: Optional[dict] = None
    if args.email_summary and args.email_summary.exists():
        with open(args.email_summary, encoding="utf-8") as f:
            email_summary = json.load(f)

    body = compose_comment(
        submission=submission,
        ledger=ledger,
        email_summary=email_summary,
        issue_number=args.issue,
    )

    if args.dry_run:
        print(body)
        return 0

    post_comment(args.issue, body, repo=args.repo)
    print(f"Posted comment on issue #{args.issue}")
    if args.pr is not None:
        post_comment(args.pr, body, repo=args.repo)
        print(f"Posted comment on PR #{args.pr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
