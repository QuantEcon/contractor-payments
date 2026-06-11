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
    """Return (last_approved_date, last_approved_by) across active items.

    Superseded entries are skipped — their approval is no longer the most
    recent authoritative state. Returns the approval metadata of the most
    recent active entry; if none, returns (None, None).
    """
    active = [item for item in items if item.get("status") != "superseded"]
    if not active:
        return None, None
    last = active[-1]
    return last.get("approved_date"), last.get("approved_by")


def _submission_link(period: str, submission_id: str) -> str:
    """Markdown link to the submission YAML."""
    return f"[`{submission_id}`](submissions/{period}/{submission_id}.yml)"


def _submission_cell(item: dict) -> str:
    """Render the Submission column cell, including the supersedes arrow
    for superseded entries.

    Active entry: just the link.
    Superseded entry: ~~original~~ → [revision-id] (link), so the audit
    trail forward-references the replacement without breaking the column
    structure.
    """
    link = _submission_link(item["period"], item["submission_id"])
    if item.get("status") == "superseded":
        successor = item.get("superseded_by")
        if successor:
            successor_link = _submission_link(item["period"], successor)
            return f"~~{link}~~ → {successor_link}"
        return f"~~{link}~~"
    return link


def _strike(active_cell: str, is_superseded: bool) -> str:
    """Wrap a markdown cell in strikethrough if the row is superseded.

    Avoids striking through cells that already carry markdown structure
    that doesn't render well with `~~...~~` (the link cell uses
    `_submission_cell` directly instead)."""
    if is_superseded:
        return f"~~{active_cell}~~"
    return active_cell


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
            is_superseded = s.get("status") == "superseded"
            period_cell = _strike(s["period"], is_superseded)
            hours_cell = _strike(str(s["hours"]), is_superseded)
            rate_cell = _strike(f"{_fmt_amount(s['rate'], currency)} {currency}", is_superseded)
            amount_cell = _strike(f"{_fmt_amount(s['amount'], currency)} {currency}", is_superseded)
            approved_cell = _strike(
                f"{s['approved_date']} by @{s['approved_by']}", is_superseded,
            )
            lines.append(
                f"| {period_cell} | {_submission_cell(s)} | {hours_cell} "
                f"| {rate_cell} | {amount_cell} | {approved_cell} |"
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
            is_superseded = c.get("status") == "superseded"
            period_cell = _strike(c["period"], is_superseded)
            milestones = ", ".join(f"#{e['id']}" for e in c.get("entries", []))
            milestones_cell = _strike(milestones, is_superseded)
            amount_cell = _strike(f"{_fmt_amount(c['amount'], currency)} {currency}", is_superseded)
            approved_cell = _strike(
                f"{c['approved_date']} by @{c['approved_by']}", is_superseded,
            )
            lines.append(
                f"| {period_cell} | {_submission_cell(c)} | {milestones_cell} "
                f"| {amount_cell} | {approved_cell} |"
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


def render_reimbursement_body(ledger: dict, reimbursements_config: dict) -> str:
    """Render a markdown body for the per-repo reimbursements ledger.

    Contractor-level (no contract): summary is a per-currency table since
    claims are single-currency but the ledger spans currencies. The funding
    project renders per claim — it can vary over time if the repo's
    `config/reimbursements.yml` is updated between claims.
    """
    totals = ledger.get("totals", {})
    claims = ledger.get("claims", [])
    last_date, last_by = _last_approval(claims)

    lines = [
        "# 📒 Running ledger — Reimbursements",
        "",
        "> Auto-updated by the approval workflow. Don't edit manually — your",
        "> changes will be overwritten on the next approval.",
        "",
        "## Summary",
        "",
    ]

    if totals:
        lines.extend([
            "| Currency | Claims | Amount to date |",
            "|---|---|---|",
        ])
        for currency, bucket in totals.items():
            lines.append(
                f"| {currency} | {bucket['claims_count']} "
                f"| {_fmt_amount(bucket['amount_to_date'], currency)} {currency} |"
            )
        lines.append("")
        if last_date:
            lines.extend([
                f"**Last approved:** {last_date}{' by @' + last_by if last_by else ''}",
                "",
            ])
    else:
        lines.extend(["_No claims approved yet._", ""])

    if claims:
        lines.extend([
            "## Approved claims",
            "",
            "| Period | Submission | Project | Amount | Approved |",
            "|---|---|---|---|---|",
        ])
        for c in claims:
            is_superseded = c.get("status") == "superseded"
            currency = c.get("currency", "")
            period_cell = _strike(c["period"], is_superseded)
            project_cell = _strike(f"`{c.get('project') or '—'}`", is_superseded)
            amount_cell = _strike(
                f"{_fmt_amount(c['amount'], currency)} {currency}", is_superseded,
            )
            approved_cell = _strike(
                f"{c['approved_date']} by @{c['approved_by']}", is_superseded,
            )
            lines.append(
                f"| {period_cell} | {_submission_cell(c)} | {project_cell} "
                f"| {amount_cell} | {approved_cell} |"
            )
        lines.append("")
    else:
        lines.extend([
            "## Approved claims",
            "",
            "_No claims approved yet._",
            "",
        ])

    lines.append(f"<!-- {MARKER_PREFIX}:reimbursements -->")
    return "\n".join(lines)


def render_body(ledger: dict, contract: dict) -> str:
    """Pick the right renderer based on ledger.type. For reimbursement
    ledgers the second argument is the repo's reimbursements config rather
    than a contract."""
    ledger_type = ledger.get("type")
    if ledger_type == "hourly":
        return render_hourly_body(ledger, contract)
    if ledger_type == "milestone":
        return render_milestone_body(ledger, contract)
    if ledger_type == "reimbursement":
        return render_reimbursement_body(ledger, contract)
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
    p.add_argument("--reimbursements", type=Path, default=None,
                   help="Path to config/reimbursements.yml (reimbursement ledgers "
                        "only — holds the `ledger_issue` number). Default: "
                        "config/reimbursements.yml under --repo-root.")
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

    # Reimbursement ledgers are contractor-level: no contract to load, and
    # the pinned-issue number lives in config/reimbursements.yml instead.
    if ledger.get("type") == "reimbursement":
        reimbursements_path = (
            args.reimbursements
            if args.reimbursements is not None
            else repo_root / "config" / "reimbursements.yml"
        )
        reimbursements_config: dict = {}
        if reimbursements_path.exists():
            with open(reimbursements_path, encoding="utf-8") as f:
                reimbursements_config = yaml.safe_load(f) or {}

        body = render_reimbursement_body(ledger, reimbursements_config)

        if args.dry_run:
            print(body)
            return 0

        issue_number = reimbursements_config.get("ledger_issue")
        if not issue_number:
            print(
                "WARN: config/reimbursements.yml has no `ledger_issue` field. "
                "Skipping issue update; YAML write already succeeded.",
                file=sys.stderr,
            )
            return 0

        update_issue_body(int(issue_number), body, repo=args.repo)
        print(f"Updated issue #{issue_number} on {args.repo or os.environ.get('GITHUB_REPOSITORY')}")
        return 0

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
