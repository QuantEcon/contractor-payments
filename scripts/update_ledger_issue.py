"""Render the ledger YAML as a markdown table and update its pinned issue.

Companion to `scripts.update_ledger` — that one writes the YAML, this one
edits the GitHub issue body so contractors and admins have a discoverable,
notification-driven view of their running totals without any new UI.

Lookup chain:
  1. Read ledger/<contract-id>.yml for the data.
  2. Read contracts/<contract-id>.yml for the `ledger_issue: <N>` field
     (written by Phase 3b onboarding). If absent, this script logs a
     warning and exits 0 — the YAML write already happened, so we don't
     want to fail the merge workflow for the missing issue link.
  3. Render a markdown body.
  4. `gh api PATCH /repos/{owner}/{repo}/issues/{N}` with the new body.

Failure-isolated from update_ledger.py — if this step fails (e.g. API
hiccup), the ledger YAML is still committed and the next approval picks
up where it left off.

See PLAN.md §8 Phase 2.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml


MARKER_PREFIX = "ledger-issue-marker"


# ─── Formatting helpers ─────────────────────────────────────────────────────

def _fmt_amount(amount: float, currency: str) -> str:
    """Pretty-print an amount for the currency. JPY: no decimals. AUD/USD: 2."""
    if currency.upper() == "JPY":
        return f"{int(round(amount)):,}"
    return f"{amount:,.2f}"


def _last_approval(items: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Return (last_approved_date, last_approved_by) across all items.
    The list isn't guaranteed sorted by approval date — but if approvals
    happen in chronological order (which they do under the merge workflow),
    the last item is the most recent."""
    if not items:
        return None, None
    last = items[-1]
    return last.get("approved_date"), last.get("approved_by")


# ─── Body rendering ─────────────────────────────────────────────────────────

def render_hourly_body(ledger: dict, contract: dict) -> str:
    """Render a markdown body for an hourly ledger."""
    contract_id = ledger["contract_id"]
    currency = ledger["currency"]
    totals = ledger["totals"]
    submissions = ledger.get("submissions", [])
    last_date, last_by = _last_approval(submissions)

    lines = [
        f"# 📒 Running ledger — {contract_id}",
        "",
        "> Auto-updated by the approval workflow. Don't edit manually — your",
        "> changes will be overwritten on the next approval.",
        "",
        f"**Contract:** `{contract_id}` (Hourly — {currency})  ",
        f"**Period:** {contract.get('start_date', '—')} → {contract.get('end_date', '—')}  ",
        f"**Status:** {contract.get('status', '—').capitalize()}",
        "",
        "## Summary",
        "",
        "| Submissions | Hours to date | Amount to date | Last approved |",
        "|---|---|---|---|",
        f"| {totals['submissions_count']} "
        f"| {totals['hours_to_date']} "
        f"| {_fmt_amount(totals['amount_to_date'], currency)} {currency} "
        f"| {last_date or '—'}{' by @' + last_by if last_by else ''} |",
        "",
    ]

    if submissions:
        lines.extend([
            "## Approved timesheets",
            "",
            "| Period | Submission | Hours | Rate | Amount | Approved |",
            "|---|---|---|---|---|---|",
        ])
        for s in submissions:
            sub_id = s["submission_id"]
            sub_link = f"[`{sub_id}`](submissions/{s['period']}/{sub_id}.yml)"
            lines.append(
                f"| {s['period']} "
                f"| {sub_link} "
                f"| {s['hours']} "
                f"| {_fmt_amount(s['rate'], currency)} {currency} "
                f"| {_fmt_amount(s['amount'], currency)} {currency} "
                f"| {s['approved_date']} by @{s['approved_by']} |"
            )
        lines.append("")
    else:
        lines.extend([
            "## Approved timesheets",
            "",
            "_No submissions approved yet._",
            "",
        ])

    lines.append(f"<!-- {MARKER_PREFIX}:{contract_id} -->")
    return "\n".join(lines)


def render_milestone_body(ledger: dict, contract: dict) -> str:
    """Render a markdown body for a milestone ledger."""
    contract_id = ledger["contract_id"]
    currency = ledger["currency"]
    totals = ledger["totals"]
    claims = ledger.get("claims", [])
    last_date, last_by = _last_approval(claims)

    lines = [
        f"# 📒 Running ledger — {contract_id}",
        "",
        "> Auto-updated by the approval workflow. Don't edit manually — your",
        "> changes will be overwritten on the next approval.",
        "",
        f"**Contract:** `{contract_id}` (Milestone — {currency})  ",
        f"**Period:** {contract.get('start_date', '—')} → {contract.get('end_date', '—')}  ",
        f"**Status:** {contract.get('status', '—').capitalize()}",
        "",
        "## Summary",
        "",
        "| Claims | Amount to date | Last approved |",
        "|---|---|---|",
        f"| {totals['claims_count']} "
        f"| {_fmt_amount(totals['amount_to_date'], currency)} {currency} "
        f"| {last_date or '—'}{' by @' + last_by if last_by else ''} |",
        "",
    ]

    if claims:
        lines.extend([
            "## Approved claims",
            "",
            "| Period | Submission | Milestones claimed | Amount | Approved |",
            "|---|---|---|---|---|",
        ])
        for c in claims:
            sub_id = c["submission_id"]
            sub_link = f"[`{sub_id}`](submissions/{c['period']}/{sub_id}.yml)"
            milestones = ", ".join(f"#{e['id']}" for e in c.get("entries", []))
            lines.append(
                f"| {c['period']} "
                f"| {sub_link} "
                f"| {milestones} "
                f"| {_fmt_amount(c['amount'], currency)} {currency} "
                f"| {c['approved_date']} by @{c['approved_by']} |"
            )
        lines.append("")
    else:
        lines.extend([
            "## Approved claims",
            "",
            "_No claims approved yet._",
            "",
        ])

    lines.append(f"<!-- {MARKER_PREFIX}:{contract_id} -->")
    return "\n".join(lines)


def render_body(ledger: dict, contract: dict) -> str:
    """Pick the right renderer based on ledger.type."""
    ledger_type = ledger.get("type")
    if ledger_type == "hourly":
        return render_hourly_body(ledger, contract)
    if ledger_type == "milestone":
        return render_milestone_body(ledger, contract)
    raise ValueError(f"Unknown ledger type `{ledger_type}`.")


# ─── GitHub API call ────────────────────────────────────────────────────────

def update_issue_body(issue_number: int, body: str, *, repo: Optional[str] = None) -> None:
    """Edit the issue's body via `gh issue edit --body-file`.

    `repo` is owner/name; defaults to the `GITHUB_REPOSITORY` env var.
    Body is written to a temp file (rather than passed on argv) to avoid
    shell quoting / length issues with rich markdown.
    """
    import tempfile
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    fd, path = tempfile.mkstemp(suffix=".md", prefix="ledger-issue-")
    body_path = Path(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        cmd = ["gh", "issue", "edit", str(issue_number),
               "--body-file", str(body_path)]
        if repo:
            cmd.extend(["--repo", repo])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"gh issue edit failed (exit {result.returncode}). stderr:\n{result.stderr}"
            )
    finally:
        body_path.unlink(missing_ok=True)


# ─── CLI entry point ────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ledger", required=True, type=Path,
                   help="Path to the ledger YAML.")
    p.add_argument("--contract", type=Path, default=None,
                   help="Path to the contract YAML. Default: derived from the ledger's "
                        "contract_id (looks at contracts/<contract-id>.yml relative to --repo-root).")
    p.add_argument("--repo-root", default=".", type=Path,
                   help="Working tree root (default: current directory).")
    p.add_argument("--repo", default=None,
                   help="GitHub owner/name. Default: $GITHUB_REPOSITORY.")
    p.add_argument("--dry-run", action="store_true",
                   help="Render the body to stdout instead of editing the issue. "
                        "Useful for local development without an issue number.")
    args = p.parse_args(argv)

    repo_root = args.repo_root.resolve()

    # Load ledger.
    with open(args.ledger, encoding="utf-8") as f:
        ledger = yaml.safe_load(f)
    if ledger is None:
        print(f"ERROR: ledger {args.ledger} is empty.", file=sys.stderr)
        return 1

    # Load contract.
    contract_path = args.contract
    if contract_path is None:
        contract_path = repo_root / "contracts" / f"{ledger['contract_id']}.yml"
    if not contract_path.exists():
        print(
            f"ERROR: contract YAML not found at {contract_path}. "
            f"Either pass --contract or check the working tree.",
            file=sys.stderr,
        )
        return 1
    with open(contract_path, encoding="utf-8") as f:
        contract = yaml.safe_load(f)

    body = render_body(ledger, contract)

    if args.dry_run:
        print(body)
        return 0

    issue_number = contract.get("ledger_issue")
    if not issue_number:
        # Don't fail the workflow — Phase 3b onboarding may not have run for this
        # contract yet (e.g. pre-Phase-3b contractor-engine-test), or the field
        # is intentionally absent. Log and exit 0 so the merge pipeline continues.
        print(
            f"WARN: contract `{ledger['contract_id']}` has no `ledger_issue` field. "
            f"Skipping issue update; YAML write already succeeded.",
            file=sys.stderr,
        )
        return 0

    update_issue_body(int(issue_number), body, repo=args.repo)
    print(f"Updated issue #{issue_number} on {args.repo or os.environ.get('GITHUB_REPOSITORY')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
