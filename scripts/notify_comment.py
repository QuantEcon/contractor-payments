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

One comment per target, updated in place: the body ends with SENTINEL, and a
re-run (`Re-run failed jobs` after a partial failure) edits the existing
comment instead of adding another. So a first run that couldn't send the email
reads "not sent", and the re-run that sends it turns the same comment into
"sent".

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

from scripts.money import format_hours, format_money


# Same emoji conventions used in the running-ledger issue body, so admins
# learn one visual vocabulary across both surfaces.

# Marks the approval-summary comment so a re-run finds and updates it.
SENTINEL = "<!-- contractor-payments-approval-summary -->"


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        raise RuntimeError(f"{path} is empty.")
    return data


def _fmt_amount(amount, currency: str) -> str:
    """Currency-aware display (JPY: no decimals; AUD/USD: 2), half-up."""
    return format_money(amount, currency)


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
            f"{format_hours(totals.get('hours_to_date', 0))} hours)."
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
        source = email_summary.get("testing_mode_source")
        src_suffix = f" (via {source})" if source else ""
        mode_note = f" — `testing_mode=true`{src_suffix}, PSL not contacted" if testing_mode else ""
        dry_run = email_summary.get("dry_run", False)
        if dry_run:
            email_line = (
                f"📧 **Email:** dry-run only (no message sent). "
                f"Would have gone to {recipients}{mode_note}."
            )
        elif email_summary.get("already_sent"):
            email_line = (
                f"📧 **Email:** sent to {recipients} at {sent_at}{mode_note} "
                f"by an earlier run; not sent again."
            )
        elif email_summary.get("resent"):
            email_line = (
                f"📧 **Email:** re-sent on request to {recipients} at {sent_at}{mode_note}."
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
        SENTINEL,
    ]
    return "\n".join(lines) + "\n"


_LOCKED_MARKER = "issue is locked"


def _gh_issue(verb: str, target_number: int, repo: Optional[str],
              *extra: str) -> subprocess.CompletedProcess:
    cmd = ["gh", "issue", verb, str(target_number), *extra]
    if repo:
        cmd.extend(["--repo", repo])
    return subprocess.run(cmd, capture_output=True, text=True)


def _write_body(body: str) -> Path:
    fd, path = tempfile.mkstemp(suffix=".md", prefix="approval-comment-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return Path(path)


def _run_unlocking(target_number: int, repo: Optional[str], attempt, what: str) -> None:
    """Run `attempt()` (a gh call returning CompletedProcess); if GitHub refuses
    because the issue is locked, unlock, retry once and re-lock.

    Submission issues are *locked* when they are filed, and GitHub rejects
    comments on a locked issue outright:

        GraphQL: Unable to create comment because issue is locked (addComment)

    That made this audit comment — the record that the submission was merged,
    ledgered and emailed — impossible to post on the normal happy path, and
    because nothing gated on failure it also silently skipped the `processed`
    label on every affected submission. The lock is restored in a `finally`,
    because leaving a submission issue unlocked is a worse outcome than a
    missing comment.
    """
    result = attempt()
    if result.returncode == 0:
        return

    if _LOCKED_MARKER not in (result.stderr or "").lower():
        raise RuntimeError(
            f"{what} failed (exit {result.returncode}). "
            f"stderr:\n{result.stderr}"
        )

    print(f"Issue #{target_number} is locked; unlocking to post the audit "
          f"comment, then re-locking.", file=sys.stderr)
    unlock = _gh_issue("unlock", target_number, repo)
    if unlock.returncode != 0:
        raise RuntimeError(
            f"issue #{target_number} is locked and `gh issue unlock` failed "
            f"(exit {unlock.returncode}), so the audit comment cannot be "
            f"posted. stderr:\n{unlock.stderr}"
        )
    try:
        retry = attempt()
        if retry.returncode != 0:
            raise RuntimeError(
                f"{what} failed after unlocking (exit "
                f"{retry.returncode}). stderr:\n{retry.stderr}"
            )
    finally:
        relock = _gh_issue("lock", target_number, repo)
        if relock.returncode != 0:
            print(f"WARNING: could not re-lock issue #{target_number} "
                  f"({relock.stderr.strip() or 'gh failed'}). Re-lock it by "
                  f"hand: gh issue lock {target_number}"
                  + (f" --repo {repo}" if repo else ""),
                  file=sys.stderr)


def post_comment(target_number: int, body: str, *, repo: Optional[str] = None) -> None:
    """Post a new comment on the issue or PR via `gh issue comment --body-file`.

    Works for both issues and PRs because they share GitHub's issue
    numbering and the issue-comment endpoint accepts either. Handles a locked
    issue (see `_run_unlocking`).
    """
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    body_path = _write_body(body)
    try:
        _run_unlocking(
            target_number, repo,
            lambda: _gh_issue("comment", target_number, repo, "--body-file", str(body_path)),
            "gh issue comment",
        )
    finally:
        body_path.unlink(missing_ok=True)


def find_summary_comment(target_number: int, *, repo: Optional[str] = None) -> Optional[int]:
    """ID of the existing approval-summary comment on the issue or PR, if any.

    Only comments by a bot count, so a person pasting the marker into their
    own comment can't redirect the update. The jq filter runs per page under
    --paginate, so this finds the comment however long the thread is.
    """
    repo = repo or os.environ.get("GITHUB_REPOSITORY") or "{owner}/{repo}"
    jq = (f'.[] | select(.user.type == "Bot" and (.body | contains({json.dumps(SENTINEL)})))'
          f' | .id')
    result = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/issues/{target_number}/comments",
         "--jq", jq],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not list comments on #{target_number} (exit "
            f"{result.returncode}). stderr:\n{result.stderr}"
        )
    ids = [int(x) for x in result.stdout.split()]
    return ids[0] if ids else None


def upsert_comment(target_number: int, body: str, *, repo: Optional[str] = None) -> str:
    """Update the existing approval-summary comment, or post one. Returns
    "updated" or "posted"."""
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    existing = find_summary_comment(target_number, repo=repo)
    if existing is None:
        post_comment(target_number, body, repo=repo)
        return "posted"
    body_path = _write_body(body)
    try:
        _run_unlocking(
            target_number, repo,
            lambda: subprocess.run(
                ["gh", "api", "--method", "PATCH",
                 f"repos/{repo or '{owner}/{repo}'}/issues/comments/{existing}",
                 "-F", f"body=@{body_path}"],
                capture_output=True, text=True,
            ),
            f"updating comment {existing}",
        )
    finally:
        body_path.unlink(missing_ok=True)
    return "updated"


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

    outcome = upsert_comment(args.issue, body, repo=args.repo)
    print(f"Approval summary {outcome} on issue #{args.issue}")
    if args.pr is not None:
        outcome = upsert_comment(args.pr, body, repo=args.repo)
        print(f"Approval summary {outcome} on PR #{args.pr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
