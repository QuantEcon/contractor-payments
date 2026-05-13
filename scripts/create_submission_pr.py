"""Create a submission PR from a parsed timesheet issue.

Pipeline:
  1. Load the parsed submission JSON (parse_issue.py --output-json).
  2. Load the referenced contract from contracts/{contract_id}.yml.
  3. Enrich the submission with metadata (id, dates, submitter) and computed
     totals (rate, amount, currency derived from the contract).
  4. Write the submission YAML to submissions/{period}/{submission_id}.yml.
  5. Create a branch, commit, push.
  6. Open a PR with `Closes #{issue}` in the body.

The pure data-transformation helpers (`enrich_submission`,
`generate_submission_id`, `format_currency_amount`) are unit-testable.
The git/gh orchestration runs against a real working tree and remote.

Phase 1 only handles the "no PR exists yet" path. If a branch already exists
for this issue, the script exits cleanly without clobbering — the workflow
decides what to do (regeneration is deferred per PLAN §4.4).
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

from scripts.generate_pdf import DEFAULT_PNG_PPI, render_submission_pdf, render_submission_png


# ─── Pure data transformations (testable) ───────────────────────────────────

_TYPE_SLUG = {
    "timesheet": "timesheet",
    "milestone_invoice": "invoice",
}


def generate_submission_id(
    github_handle: str,
    period: str,
    submission_type: str = "timesheet",
) -> str:
    """Period-based submission ID, e.g. `mmcky-timesheet-2026-06` or
    `mmcky-invoice-2025-11`.

    Pure function — does not check for collisions. Use
    `resolve_collision_suffix` against the working tree to apply a
    `-v2`, `-v3` suffix when a submission for the same period already
    exists (see PLAN §v1.1 for the revision/supersede rationale).
    """
    slug = _TYPE_SLUG.get(submission_type, "submission")
    return f"{github_handle}-{slug}-{period}"


def resolve_collision_suffix(repo_root: Path, base_id: str, period: str) -> str:
    """Walk the repo's `submissions/<period>/` directory and return the
    first un-used variant of `base_id` (the bare ID, then `-v2`, `-v3`, …).

    Only the working tree's main is consulted; in-flight branch state
    isn't considered (we expect collisions only after the prior submission
    was approved and merged).
    """
    submissions_dir = repo_root / "submissions" / period
    if not submissions_dir.exists():
        return base_id
    candidate = base_id
    n = 2
    while (submissions_dir / f"{candidate}.yml").exists():
        candidate = f"{base_id}-v{n}"
        n += 1
    return candidate


def resolve_payer_today(fiscal_host_path: Path) -> str:
    """Today's date in the payer's timezone, ISO-formatted.

    Reads `psl_foundation.timezone` from `templates/fiscal-host.yml`; falls
    back to UTC if the file or field is missing. Policy: document issue dates
    use the fiscal host's locale so paperwork lines up with the fiscal host's
    books regardless of where the contractor lives.
    """
    tz_name = None
    if fiscal_host_path.exists():
        with open(fiscal_host_path, encoding="utf-8") as f:
            fiscal_host = yaml.safe_load(f) or {}
        tz_name = fiscal_host.get("psl_foundation", {}).get("timezone")
    tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


def format_currency_amount(amount: float, currency: str) -> float | int:
    """Format an amount appropriately for the currency.

    JPY has no fractional units, so we return an int. AUD/USD return a float
    rounded to two decimals.
    """
    if currency.upper() == "JPY":
        return int(round(amount))
    return round(amount, 2)


def enrich_submission(
    submission: dict,
    contract: dict,
    *,
    submitter: str,
    submission_id: str,
    issue_number: int,
    submitted_date: str,
) -> dict:
    """Combine the parser's submission with contract data + metadata.

    Branches on `submission["type"]`:
    - `timesheet` — requires an `hourly` contract; computes amount from hours × rate.
    - `milestone_invoice` — requires a `milestone` contract; amount is the sum
      of the contractor-entered milestone entries (verified against the
      contract's notes by the admin during PR review, not in code).

    Returns a fully-formed submission dict ready to be written as YAML.
    Raises ValueError if the contract is malformed or the type doesn't match.
    """
    contract_id_in_submission = submission["contract_id"]
    contract_id_in_contract = contract.get("contract_id")
    if contract_id_in_submission != contract_id_in_contract:
        raise ValueError(
            f"Contract ID mismatch: submission says `{contract_id_in_submission}`, "
            f"contract file says `{contract_id_in_contract}`."
        )

    submission_type = submission.get("type", "timesheet")
    contract_type = contract.get("type")

    common = {
        "submission_id": submission_id,
        "contract_id": contract_id_in_submission,
        "contract_start_date": contract.get("start_date"),
        "contract_end_date": contract.get("end_date"),
        "type": submission_type,
        "period": submission["period"],
        "submitted_date": submitted_date,
        "submitted_by": submitter,
        "issue_number": issue_number,
        "notes": submission.get("notes", ""),
        "status": "pending",
        "approved_by": None,
        "approved_date": None,
    }

    if submission_type == "timesheet":
        if contract_type != "hourly":
            raise ValueError(
                f"Contract `{contract_id_in_contract}` is type `{contract_type}`, "
                f"but a hourly timesheet submission requires a `hourly` contract."
            )
        terms = contract.get("terms", {})
        if "hourly_rate" not in terms or "currency" not in terms:
            raise ValueError(
                f"Contract `{contract_id_in_contract}` is missing required terms "
                f"(`hourly_rate` and `currency`)."
            )
        hourly_rate = float(terms["hourly_rate"])
        currency = terms["currency"]
        total_hours = submission["totals"]["hours"]
        amount = format_currency_amount(total_hours * hourly_rate, currency)
        rate_display = format_currency_amount(hourly_rate, currency)

        return {
            **common,
            "entries": sorted(submission["entries"], key=lambda e: e["date"]),
            "totals": {
                "hours": total_hours,
                "rate": rate_display,
                "amount": amount,
                "currency": currency,
            },
        }

    if submission_type == "milestone_invoice":
        if contract_type != "milestone":
            raise ValueError(
                f"Contract `{contract_id_in_contract}` is type `{contract_type}`, "
                f"but a milestone invoice submission requires a `milestone` contract."
            )
        currency = contract.get("currency")
        if not currency:
            raise ValueError(
                f"Milestone contract `{contract_id_in_contract}` is missing a "
                f"top-level `currency` field."
            )
        entries = sorted(submission["entries"], key=lambda e: e["date"])
        total_amount = format_currency_amount(
            sum(e["amount"] for e in entries),
            currency,
        )
        # Normalise per-entry amount display formatting too, so the YAML +
        # PDF agree on rounding for the contract's currency.
        for e in entries:
            e["amount"] = format_currency_amount(e["amount"], currency)

        return {
            **common,
            "entries": entries,
            "totals": {
                "amount": total_amount,
                "currency": currency,
            },
        }

    raise ValueError(f"Unknown submission type `{submission_type}`.")


def render_pr_body(
    issue_number: int,
    submitter: str,
    submission: dict,
    submission_path_rel: str,
    pdf_path_rel: Optional[str] = None,
    png_url: Optional[str] = None,
    warnings: Optional[list[dict]] = None,
) -> str:
    """Compose the PR body. Includes `Closes #N` so merge closes the issue.

    `png_url` is a full raw URL to the rendered preview image so reviewers
    see it inline in the PR description without leaving the review surface.
    """
    totals = submission["totals"]
    submission_type = submission.get("type", "timesheet")
    type_label = {
        "timesheet": "Timesheet",
        "milestone_invoice": "Milestone Invoice",
    }.get(submission_type, "Submission")

    lines = [
        f"Auto-generated from issue #{issue_number} (@{submitter}).",
        "",
        f"**Type:** {type_label}",
        f"**Period:** `{submission['period']}`",
        f"**Contract:** `{submission['contract_id']}`",
    ]
    if submission_type == "timesheet":
        lines.append(f"**Total hours:** {totals['hours']}")
    else:
        lines.append(f"**Milestones claimed:** {len(submission['entries'])}")
    lines.append(f"**Total amount:** {totals['amount']} {totals['currency']}")
    lines.append("")
    if png_url:
        lines.extend([
            "### Preview",
            "",
            f"![{type_label} preview]({png_url})",
            "",
        ])
    if pdf_path_rel:
        lines.extend([
            f"📄 **PDF (authoritative):** [`{pdf_path_rel}`]({pdf_path_rel})  ·  "
            f"📋 [YAML]({submission_path_rel})",
        ])
    else:
        lines.append(f"Submission YAML: [`{submission_path_rel}`]({submission_path_rel})")
    lines.extend(["", f"Closes #{issue_number}"])
    if warnings:
        lines.append("")
        lines.append("**Parse warnings (non-blocking):**")
        for w in warnings:
            lines.append(f"- {w['message']}")
    return "\n".join(lines) + "\n"


def branch_name_for_issue(issue_number: int) -> str:
    """Branch names stay issue-numbered so re-firing the workflow on an
    edit lands on the same branch (collision-free without filesystem
    lookups). The submission_id inside the branch is period-based."""
    return f"submission/issue-{issue_number}"


# ─── git / gh shell wrappers ────────────────────────────────────────────────

def _run(args: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check, cwd=cwd)


def remote_branch_exists(branch: str, cwd: Optional[Path] = None) -> bool:
    """Check whether a branch already exists on origin."""
    result = _run(["git", "ls-remote", "--heads", "origin", branch], cwd=cwd)
    return bool(result.stdout.strip())


def create_branch(branch: str, cwd: Optional[Path] = None) -> None:
    _run(["git", "checkout", "-b", branch], cwd=cwd)


def stage_and_commit(
    paths: list[Path],
    issue_number: int,
    cwd: Optional[Path] = None,
    submission_type: str = "timesheet",
) -> None:
    for p in paths:
        _run(["git", "add", str(p)], cwd=cwd)
    type_label = {
        "timesheet": "timesheet",
        "milestone_invoice": "milestone invoice",
    }.get(submission_type, "submission")
    _run([
        "git", "commit", "-m",
        f"Add {type_label} submission from #{issue_number}",
    ], cwd=cwd)


def push_branch(branch: str, cwd: Optional[Path] = None) -> None:
    _run(["git", "push", "-u", "origin", branch], cwd=cwd)


def open_pr(
    issue_title: str,
    body: str,
    cwd: Optional[Path] = None,
    extra_labels: Optional[list[str]] = None,
) -> str:
    """Open a PR and return its URL."""
    extra_labels = extra_labels or []
    fd, name = tempfile.mkstemp(suffix=".md", prefix="pr-body-")
    body_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        cmd = [
            "gh", "pr", "create",
            "--title", f"Submission: {issue_title}",
            "--body-file", str(body_path),
            "--label", "submission",
        ]
        for label in extra_labels:
            cmd.extend(["--label", label])
        result = _run(cmd, cwd=cwd)
        return result.stdout.strip()
    finally:
        body_path.unlink(missing_ok=True)


# ─── Orchestration ──────────────────────────────────────────────────────────

def write_submission_yaml(submission: dict, repo_root: Path) -> Path:
    """Write the submission YAML and return the path relative-friendly Path."""
    period = submission["period"]
    submission_id = submission["submission_id"]
    out_dir = repo_root / "submissions" / period
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{submission_id}.yml"
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            submission, f,
            default_flow_style=False, sort_keys=False, allow_unicode=True, width=100,
        )
    return out_path


def submission_pdf_path(submission: dict, repo_root: Path) -> Path:
    """Mirror the submission YAML path under generated_pdfs/ as PDF."""
    period = submission["period"]
    submission_id = submission["submission_id"]
    return repo_root / "generated_pdfs" / period / f"{submission_id}.pdf"


def submission_png_path(submission: dict, repo_root: Path) -> Path:
    """Mirror the submission YAML path under generated_pdfs/ as PNG preview."""
    period = submission["period"]
    submission_id = submission["submission_id"]
    return repo_root / "generated_pdfs" / period / f"{submission_id}.png"


def detect_repo_owner_name(cwd: Path) -> Optional[str]:
    """Return `owner/name` for the current repo via `gh`, or None on failure."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            capture_output=True, text=True, check=True, cwd=cwd,
        )
        return result.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


def raw_url(owner_name: str, branch: str, path_in_repo: str) -> str:
    """GitHub raw-content URL for a file on a specific branch.

    For private repos this URL requires the viewer to be authenticated to
    the repo, which is the case for anyone reviewing the PR — so markdown
    image embeds in PR bodies resolve correctly."""
    return f"https://github.com/{owner_name}/raw/{branch}/{path_in_repo}"


def load_contract(repo_root: Path, contract_id: str) -> dict:
    path = repo_root / "contracts" / f"{contract_id}.yml"
    if not path.exists():
        raise FileNotFoundError(
            f"Contract file not found: {path}. Has it been deployed to this repo?"
        )
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── CLI ────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--submission-file", required=True,
                   help="JSON from parse_issue.py --output-json.")
    p.add_argument("--errors-file",
                   help="JSON from parse_issue.py --output-errors-json (for warnings).")
    p.add_argument("--issue-number", type=int, required=True)
    p.add_argument("--issue-author", required=True,
                   help="GitHub handle of the issue submitter.")
    p.add_argument("--issue-title", required=True)
    p.add_argument("--submitted-date",
                   default=None,
                   help="ISO date for `submitted_date`. Default: today in the fiscal "
                        "host's timezone (read from templates/fiscal-host.yml: "
                        "psl_foundation.timezone). Falls back to UTC if "
                        "fiscal-host.yml or the timezone field is missing.")
    p.add_argument("--repo-root", default=".",
                   help="Working tree root (default: current directory).")
    p.add_argument("--templates-dir", default="templates",
                   help="Templates directory (relative to --repo-root). Default: templates.")
    p.add_argument("--settings-file", default="config/settings.yml",
                   help="Path to settings.yml (relative to --repo-root). Default: config/settings.yml.")
    p.add_argument("--skip-pdf", action="store_true",
                   help="Skip PDF and PNG rendering (useful for local dry-runs without typst installed).")
    p.add_argument("--png-ppi", type=int, default=DEFAULT_PNG_PPI,
                   help=f"PNG preview resolution in pixels per inch (default: {DEFAULT_PNG_PPI}).")
    args = p.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    templates_dir = (repo_root / args.templates_dir).resolve()
    settings_path = (repo_root / args.settings_file).resolve()

    if args.submitted_date is None:
        args.submitted_date = resolve_payer_today(templates_dir / "fiscal-host.yml")

    # Bail early if a branch already exists for this issue. Phase 1 doesn't
    # handle regeneration; that's deferred per PLAN §4.4.
    branch = branch_name_for_issue(args.issue_number)
    if remote_branch_exists(branch, cwd=repo_root):
        print(
            f"Branch `{branch}` already exists on origin — skipping "
            f"create_submission_pr. Post-submission edits go on the PR "
            f"branch directly (Phase 1 doesn't regenerate).",
            file=sys.stderr,
        )
        return 0  # not an error; workflow continues

    # Load inputs.
    with open(args.submission_file, encoding="utf-8") as f:
        parsed = json.load(f)

    warnings: list[dict] = []
    if args.errors_file:
        with open(args.errors_file, encoding="utf-8") as f:
            errs_data = json.load(f)
        warnings = errs_data.get("warnings", [])

    contract = load_contract(repo_root, parsed["contract_id"])

    submission_type = parsed.get("type", "timesheet")
    base_id = generate_submission_id(
        args.issue_author, parsed["period"], submission_type=submission_type
    )
    submission_id = resolve_collision_suffix(repo_root, base_id, parsed["period"])
    if submission_id != base_id:
        print(
            f"Submission ID collision detected on `{base_id}` — using `{submission_id}`. "
            f"This is a revision; see PLAN §v1.1 for the manual supersede workflow.",
            file=sys.stderr,
        )

    submission = enrich_submission(
        parsed,
        contract,
        submitter=args.issue_author,
        submission_id=submission_id,
        issue_number=args.issue_number,
        submitted_date=args.submitted_date,
    )

    # Write the YAML.
    yaml_path = write_submission_yaml(submission, repo_root)
    yaml_rel = yaml_path.relative_to(repo_root).as_posix()
    print(f"Wrote {yaml_rel}")

    # Render the PDF + PNG preview (both in pending state — approval block
    # says "PENDING REVIEW"). PDF is the authoritative artifact for the
    # payments manager; PNG is the inline preview embedded in the PR body
    # so reviewers see it without leaving the PR.
    pdf_rel: Optional[str] = None
    png_url: Optional[str] = None
    paths_to_stage: list[Path] = [yaml_path]
    if not args.skip_pdf:
        pdf_path = submission_pdf_path(submission, repo_root)
        png_path = submission_png_path(submission, repo_root)
        render_submission_pdf(
            submission_path=yaml_path,
            settings_path=settings_path,
            template_dir=templates_dir,
            output_path=pdf_path,
        )
        render_submission_png(
            submission_path=yaml_path,
            settings_path=settings_path,
            template_dir=templates_dir,
            output_path=png_path,
            ppi=args.png_ppi,
        )
        pdf_rel = pdf_path.relative_to(repo_root).as_posix()
        png_rel = png_path.relative_to(repo_root).as_posix()
        paths_to_stage.extend([pdf_path, png_path])
        print(f"Wrote {pdf_rel}")
        print(f"Wrote {png_rel}")

        # Compose the raw URL for the PNG so it embeds inline in the PR body.
        # Relative paths in PR bodies resolve against the default branch, so
        # we need an absolute raw URL pointing at this PR's branch.
        owner_name = detect_repo_owner_name(repo_root)
        if owner_name:
            png_url = raw_url(owner_name, branch, png_rel)

    # Git: branch, commit, push.
    create_branch(branch, cwd=repo_root)
    stage_and_commit(
        paths_to_stage, args.issue_number, cwd=repo_root,
        submission_type=submission_type,
    )
    push_branch(branch, cwd=repo_root)

    # Open PR.
    body = render_pr_body(
        issue_number=args.issue_number,
        submitter=args.issue_author,
        submission=submission,
        submission_path_rel=yaml_rel,
        pdf_path_rel=pdf_rel,
        png_url=png_url,
        warnings=warnings,
    )
    pr_url = open_pr(
        args.issue_title, body, cwd=repo_root,
        extra_labels=[submission_type.replace("_", "-")],
    )
    print(f"Opened PR: {pr_url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
