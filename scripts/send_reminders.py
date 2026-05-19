"""Scan a contractor repo for open draft submissions whose period has
ended, and post a sentinel-marked reminder comment on each. Idempotent
across re-runs: the sentinel encodes the period, so the same draft + same
period won't get a second reminder.

Phase 3c (deferred submission) makes the issue a long-lived draft. This
script catches the natural failure mode — RA forgot to `/submit` after
the period closed.

Triggered by a per-repo scheduled workflow (`period-reminders.yml`) that
runs monthly on day 1 in UTC. The script reads the fiscal host timezone
from `templates/fiscal-host.yml` (engine repo) for period-close
arithmetic, falling back to UTC.

Usage:
  python -m scripts.send_reminders [--repo OWNER/NAME] [--fiscal-host PATH] [--dry-run]

See PLAN §8 Phase 3c.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml


SUBMISSION_LABELS = ("timesheet", "milestone-invoice")


def sentinel_for(period: str) -> str:
    return f"<!-- submission-reminder:{period} -->"


# ─── Pure helpers (testable) ────────────────────────────────────────────────

def _section_value(body: str, heading: str) -> Optional[str]:
    """Pull the content under `### {heading}` in a GitHub Issue Forms body."""
    pattern = re.compile(
        rf"^###\s+{re.escape(heading)}\s*$\n(.*?)(?=^###\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(body or "")
    if not m:
        return None
    text = m.group(1).strip()
    return text or None


def extract_period(body: str) -> Optional[str]:
    """Return 'YYYY-MM' parsed from the form's Year + Month sections, or None.

    The form serialises Year as `2026` and Month as `04 — April`; we keep the
    leading two digits of the month value.
    """
    year = _section_value(body, "Year")
    month = _section_value(body, "Month")
    if not year or not month:
        return None
    year_m = re.match(r"^\s*(\d{4})\s*$", year)
    month_m = re.match(r"^\s*(\d{2})\b", month)
    if not year_m or not month_m:
        return None
    return f"{year_m.group(1)}-{month_m.group(1)}"


def is_period_closed(period: str, now: datetime) -> bool:
    """True if `period` (YYYY-MM) has ended at the moment `now` (tz-aware)."""
    year, month = period.split("-")
    year_i, month_i = int(year), int(month)
    if month_i == 12:
        next_year, next_month = year_i + 1, 1
    else:
        next_year, next_month = year_i, month_i + 1
    period_end = datetime(next_year, next_month, 1, tzinfo=now.tzinfo)
    return now >= period_end


def submission_type_from_labels(labels: list[str]) -> Optional[str]:
    if "timesheet" in labels:
        return "timesheet"
    if "milestone-invoice" in labels:
        return "milestone_invoice"
    return None


def render_reminder_comment(period: str, submission_type: str) -> str:
    """Render the reminder comment body with the period-encoded sentinel."""
    type_label = "timesheet" if submission_type == "timesheet" else "invoice"
    lines = [
        f"🔔 **Reminder — period `{period}` has ended**",
        "",
        f"This {type_label} hasn't been submitted yet. When you're ready:",
        "",
        "- Comment **`/validate`** to check that your entries parse cleanly.",
        f"- Comment **`/submit`** (or apply the `submit` label) to file the {type_label}. "
        f"A Pull Request will open with the rendered PDF.",
        "",
        f"If you have nothing to file for `{period}`, you can close this issue.",
        "",
        sentinel_for(period),
    ]
    return "\n".join(lines) + "\n"


def resolve_now(fiscal_host_path: Path) -> datetime:
    """Now in the payer's timezone (matches resolve_payer_today behaviour)."""
    tz_name = None
    if fiscal_host_path.exists():
        with open(fiscal_host_path, encoding="utf-8") as f:
            fiscal_host = yaml.safe_load(f) or {}
        tz_name = fiscal_host.get("psl_foundation", {}).get("timezone")
    tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    return datetime.now(tz)


# ─── GitHub API via gh ──────────────────────────────────────────────────────

def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def list_open_submission_issues(repo: str) -> list[dict]:
    """Return open issues carrying a submission label. Deduped by issue number."""
    seen: dict[int, dict] = {}
    for label in SUBMISSION_LABELS:
        result = _run([
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--label", label,
            "--json", "number,title,body,labels",
            "--limit", "200",
        ])
        for it in json.loads(result.stdout):
            num = int(it["number"])
            if num in seen:
                continue
            it["_labels"] = [l["name"] for l in it.get("labels", [])]
            seen[num] = it
    return list(seen.values())


def comment_exists_for_period(repo: str, issue: int, period: str) -> bool:
    sentinel = sentinel_for(period)
    result = _run([
        "gh", "api", "--paginate",
        f"repos/{repo}/issues/{issue}/comments",
    ])
    comments = json.loads(result.stdout)
    if isinstance(comments, dict):
        comments = [comments]
    return any(sentinel in c.get("body", "") for c in comments)


def post_comment(repo: str, issue: int, body: str) -> None:
    fd, name = tempfile.mkstemp(suffix=".md", prefix="reminder-")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        _run([
            "gh", "issue", "comment", str(issue),
            "--repo", repo,
            "--body-file", str(path),
        ])
    finally:
        path.unlink(missing_ok=True)


# ─── Orchestration ──────────────────────────────────────────────────────────

def process_issue(
    repo: str,
    issue: dict,
    now: datetime,
    dry_run: bool = False,
) -> str:
    """Decide what to do with one open submission issue. Returns a one-line
    status for logging."""
    number = int(issue["number"])
    body = issue.get("body") or ""
    labels = issue.get("_labels") or []

    submission_type = submission_type_from_labels(labels)
    if submission_type is None:
        return f"#{number}: skipped (no submission label)"

    period = extract_period(body)
    if period is None:
        return f"#{number}: skipped (couldn't extract period from body)"

    if not is_period_closed(period, now):
        return f"#{number}: skipped (period {period} not yet closed)"

    if comment_exists_for_period(repo, number, period):
        return f"#{number}: skipped (already reminded for {period})"

    body_text = render_reminder_comment(period, submission_type)
    if dry_run:
        return f"#{number}: would remind for {period} (dry-run)"
    post_comment(repo, number, body_text)
    return f"#{number}: reminded for {period}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument(
        "--fiscal-host",
        default="engine/templates/fiscal-host.yml",
        help="Path to the engine repo's fiscal-host.yml (for timezone).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Log what would be posted; don't post anything.")
    args = parser.parse_args(argv)

    if not args.repo:
        print("ERROR: --repo or GITHUB_REPOSITORY env var is required.", file=sys.stderr)
        return 2

    now = resolve_now(Path(args.fiscal_host))
    print(f"Reminder scan at {now.isoformat()} (tz={now.tzinfo}).")

    issues = list_open_submission_issues(args.repo)
    print(f"Found {len(issues)} open submission issue(s).")

    for issue in issues:
        try:
            status = process_issue(args.repo, issue, now, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001 — keep scanning other issues on error
            status = f"#{issue.get('number')}: ERROR {e}"
        print(status)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
