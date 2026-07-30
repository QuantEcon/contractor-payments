"""Post or clear a `/validate` result comment on a submission issue.

Phase 3c's pre-flight check: when a contractor comments `/validate` on a
draft submission issue, the engine parses the issue body without opening
a PR and posts a sentinel-marked comment summarising the result. Re-runs
update the same comment in place rather than spamming a new one — same
upsert pattern as `post_error_comment.py`.

Three subcommands:

    success --submission-file s.json --contract-file contracts/X.yml --issue 42
    error   --errors-file errs.json --issue 42
    clear   --issue 42

The `success` path enriches the parser output against the referenced
contract (via `create_submission_pr.enrich_submission`) so the comment can
display computed totals (hours × rate, currency-aware formatting) — same
numbers the contractor will see on the eventual PR / PDF.

See PLAN §8 Phase 3c.
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

from scripts.create_submission_pr import (
    enrich_reimbursement,
    enrich_submission,
    format_currency_amount,
)


SENTINEL = "<!-- submission-validate-result -->"


# ─── Pure rendering (testable) ──────────────────────────────────────────────

def render_success_comment(enriched: dict) -> str:
    """Render a positive validate-result comment from an enriched submission."""
    submission_type = enriched["type"]
    period = enriched["period"]
    totals = enriched["totals"]
    entries = enriched["entries"]

    lines: list[str] = []
    lines.append("✅ **Validation passed — ready to submit**")
    lines.append("")
    lines.append("Parsed cleanly. Here's the summary you'll see on the PR:")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    if submission_type == "reimbursement":
        # Contractor-level: no contract row; the funding project stands in.
        lines.append(f"| Project | `{enriched['project']}` |")
    else:
        lines.append(f"| Contract | `{enriched['contract_id']}` |")
    lines.append(f"| Period | `{period}` |")

    if submission_type == "timesheet":
        currency = totals["currency"]
        lines.append(f"| Entries | {len(entries)} day{'' if len(entries) == 1 else 's'} |")
        lines.append(f"| Hours | {totals['hours']} |")
        lines.append(f"| Rate | {totals['rate']} {currency}/hour |")
        lines.append(f"| **Total** | **{totals['amount']} {currency}** |")
    elif submission_type == "milestone_invoice":
        currency = totals["currency"]
        lines.append(f"| Milestones | {len(entries)} |")
        lines.append(f"| **Total** | **{totals['amount']} {currency}** |")
    elif submission_type == "reimbursement":
        currency = totals["currency"]
        receipts = enriched.get("receipts", [])
        lines.append(f"| Line items | {len(entries)} |")
        # Validate mode does no downloads — this is the count of attachment
        # links found in the Receipts box, fetched + committed at /submit.
        lines.append(f"| Receipts found | {len(receipts)} |")
        lines.append(f"| **Total** | **{totals['amount']} {currency}** |")
    else:
        # Defensive — unknown type. Surface what we have without computed totals.
        lines.append(f"| Type | `{submission_type}` |")

    lines.append("")
    lines.append(
        "When ready, comment `/submit` (or apply the `submit` label) to file this submission. "
        "A Pull Request will open with the rendered PDF; an admin reviews and merges."
    )
    lines.append("")
    lines.append(SENTINEL)
    return "\n".join(lines) + "\n"


def render_error_comment(
    errors: list[dict],
    warnings: Optional[list[dict]] = None,
) -> str:
    """Render a negative validate-result comment from parser errors."""
    warnings = warnings or []
    lines: list[str] = []
    lines.append("❌ **Validation failed — not ready to submit**")
    lines.append("")
    lines.append("I couldn't parse this submission. Here's what I found:")
    lines.append("")

    for err in errors:
        msg = err["message"]
        line_no = err.get("line")
        if line_no is not None:
            lines.append(f"- **Line {line_no}:** {msg}")
        else:
            lines.append(f"- {msg}")

    lines.append("")
    lines.append(
        "Edit the issue body to fix these, then comment `/validate` again to re-check."
    )

    if warnings:
        lines.append("")
        lines.append("_Notes (non-blocking):_")
        for w in warnings:
            lines.append(f"- {w['message']}")

    lines.append("")
    lines.append(SENTINEL)
    return "\n".join(lines) + "\n"


# ─── GitHub API via gh ──────────────────────────────────────────────────────

def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, check=True, **kwargs
    )


def find_existing_comment_id(repo: str, issue: int) -> Optional[int]:
    result = _run([
        "gh", "api", "--paginate",
        f"repos/{repo}/issues/{issue}/comments",
    ])
    comments = json.loads(result.stdout)
    if isinstance(comments, dict):
        comments = [comments]
    for c in comments:
        if SENTINEL in c.get("body", ""):
            return int(c["id"])
    return None


def _write_body_tempfile(body: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".md", prefix="validate-result-")
    path = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def update_comment(repo: str, comment_id: int, body: str) -> None:
    body_path = _write_body_tempfile(body)
    try:
        _run([
            "gh", "api", "--method", "PATCH",
            f"repos/{repo}/issues/comments/{comment_id}",
            "-F", f"body=@{body_path}",
        ])
    finally:
        body_path.unlink(missing_ok=True)


def create_comment(repo: str, issue: int, body: str) -> None:
    body_path = _write_body_tempfile(body)
    try:
        _run([
            "gh", "issue", "comment", str(issue),
            "--repo", repo,
            "--body-file", str(body_path),
        ])
    finally:
        body_path.unlink(missing_ok=True)


def delete_comment(repo: str, comment_id: int) -> None:
    _run([
        "gh", "api", "--method", "DELETE",
        f"repos/{repo}/issues/comments/{comment_id}",
    ])


# ─── Orchestration ──────────────────────────────────────────────────────────

def post_success(
    repo: str,
    issue: int,
    submission: dict,
    contract: dict,
    submitter: str,
) -> None:
    enriched = enrich_submission(
        submission,
        contract,
        submitter=submitter,
        submission_id="(dry-run)",
        issue_number=issue,
        submitted_date="(dry-run)",
    )
    _upsert_success_comment(repo, issue, enriched)


def post_success_reimbursement(
    repo: str,
    issue: int,
    submission: dict,
    reimbursements_config: dict,
    submitter: str,
) -> None:
    """Reimbursement variant: enrich against config/reimbursements.yml
    (contractor-level — no contract). No receipt downloads in validate mode;
    the comment reports the attachment links the parser found."""
    enriched = enrich_reimbursement(
        submission,
        reimbursements_config,
        submitter=submitter,
        submission_id="(dry-run)",
        issue_number=issue,
        submitted_date="(dry-run)",
    )
    _upsert_success_comment(repo, issue, enriched)


def _upsert_success_comment(repo: str, issue: int, enriched: dict) -> None:
    body = render_success_comment(enriched)
    existing = find_existing_comment_id(repo, issue)
    if existing is not None:
        update_comment(repo, existing, body)
        print(f"Updated existing validate-result comment ({existing}).")
    else:
        create_comment(repo, issue, body)
        print("Posted new validate-result success comment.")


def post_error(
    repo: str,
    issue: int,
    errors: list[dict],
    warnings: list[dict],
) -> None:
    if not errors:
        raise ValueError("post_error called with no errors — nothing to report")
    body = render_error_comment(errors, warnings)
    existing = find_existing_comment_id(repo, issue)
    if existing is not None:
        update_comment(repo, existing, body)
        print(f"Updated existing validate-result comment ({existing}).")
    else:
        create_comment(repo, issue, body)
        print("Posted new validate-result error comment.")


def clear(repo: str, issue: int) -> None:
    existing = find_existing_comment_id(repo, issue)
    if existing is not None:
        delete_comment(repo, existing)
        print(f"Deleted validate-result comment ({existing}).")
    else:
        print("No validate-result comment to clear.")


# ─── CLI ────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    p_success = sub.add_parser("success", help="Post or update the validate-result comment on parse success.")
    p_success.add_argument("--submission-file", required=True,
                           help="JSON output from parse_issue.py (--output-json).")
    p_success.add_argument("--contract-file",
                           help="Path to contracts/{contract_id}.yml in the contractor "
                                "repo. Required for timesheet / milestone submissions.")
    p_success.add_argument("--reimbursements-file",
                           help="Path to config/reimbursements.yml in the contractor "
                                "repo. Required for reimbursement submissions.")
    p_success.add_argument("--submitter", required=True,
                           help="GitHub handle of the submitter (issue author).")
    p_success.add_argument("--issue", type=int, required=True)
    p_success.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))

    p_error = sub.add_parser("error", help="Post or update the validate-result comment on parse failure.")
    p_error.add_argument("--errors-file", required=True,
                         help="JSON output from parse_issue.py (--output-errors-json).")
    p_error.add_argument("--issue", type=int, required=True)
    p_error.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))

    p_clear = sub.add_parser("clear", help="Delete the validate-result comment if present.")
    p_clear.add_argument("--issue", type=int, required=True)
    p_clear.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))

    args = parser.parse_args(argv)

    if not args.repo:
        print("ERROR: --repo or GITHUB_REPOSITORY env var is required.", file=sys.stderr)
        return 2

    if args.mode == "success":
        with open(args.submission_file, encoding="utf-8") as f:
            submission = json.load(f)
        if submission.get("type") == "reimbursement":
            if not args.reimbursements_file:
                print("ERROR: --reimbursements-file is required for "
                      "reimbursement submissions.", file=sys.stderr)
                return 2
            with open(args.reimbursements_file, encoding="utf-8") as f:
                reimbursements_config = yaml.safe_load(f) or {}
            post_success_reimbursement(
                args.repo, args.issue, submission, reimbursements_config,
                args.submitter,
            )
        else:
            if not args.contract_file:
                print("ERROR: --contract-file is required for timesheet / "
                      "milestone submissions.", file=sys.stderr)
                return 2
            with open(args.contract_file, encoding="utf-8") as f:
                contract = yaml.safe_load(f)
            post_success(args.repo, args.issue, submission, contract, args.submitter)
    elif args.mode == "error":
        with open(args.errors_file, encoding="utf-8") as f:
            data = json.load(f)
        errors = data.get("errors", [])
        warnings = data.get("warnings", [])
        if not errors:
            print("No errors in errors-file — nothing to post.", file=sys.stderr)
            return 0
        post_error(args.repo, args.issue, errors, warnings)
    elif args.mode == "clear":
        clear(args.repo, args.issue)

    return 0


if __name__ == "__main__":
    sys.exit(main())
