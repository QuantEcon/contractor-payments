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
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from scripts.generate_pdf import render_submission_pdf


# ─── Pure data transformations (testable) ───────────────────────────────────

def generate_submission_id(github_handle: str, issue_number: int) -> str:
    """Stable ID derived from submitter + issue number.

    Issue numbers are unique within a repo so there's no collision risk.
    """
    return f"{github_handle}-timesheet-{issue_number}"


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
    issue_number: int,
    submitted_date: str,
) -> dict:
    """Combine the parser's submission with contract data + metadata.

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

    contract_type = contract.get("type")
    if contract_type != "hourly":
        raise ValueError(
            f"Contract `{contract_id_in_contract}` is type `{contract_type}`, "
            f"but only `hourly` is supported in v1."
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

    enriched = {
        "submission_id": generate_submission_id(submitter, issue_number),
        "contract_id": contract_id_in_submission,
        "type": "timesheet",
        "period": submission["period"],
        "submitted_date": submitted_date,
        "submitted_by": submitter,
        "issue_number": issue_number,
        "entries": submission["entries"],
        "totals": {
            "hours": total_hours,
            "rate": rate_display,
            "amount": amount,
            "currency": currency,
        },
        "notes": submission.get("notes", ""),
        "status": "pending",
        "approved_by": None,
        "approved_date": None,
    }
    return enriched


def render_pr_body(
    issue_number: int,
    submitter: str,
    submission: dict,
    submission_path_rel: str,
    pdf_path_rel: Optional[str] = None,
    warnings: Optional[list[dict]] = None,
) -> str:
    """Compose the PR body. Includes `Closes #N` so merge closes the issue."""
    totals = submission["totals"]
    lines = [
        f"Auto-generated from issue #{issue_number} (@{submitter}).",
        "",
        f"**Period:** `{submission['period']}`",
        f"**Contract:** `{submission['contract_id']}`",
        f"**Total hours:** {totals['hours']}",
        f"**Total amount:** {totals['amount']} {totals['currency']}",
        "",
    ]
    if pdf_path_rel:
        lines.extend([
            f"📄 **PDF preview:** [`{pdf_path_rel}`]({pdf_path_rel})",
            f"📋 Submission YAML: [`{submission_path_rel}`]({submission_path_rel})",
            "",
            "_Click the PDF in the file tree to review what will be sent to the payments manager on approval._",
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


def stage_and_commit(paths: list[Path], issue_number: int, cwd: Optional[Path] = None) -> None:
    for p in paths:
        _run(["git", "add", str(p)], cwd=cwd)
    _run([
        "git", "commit", "-m",
        f"Add timesheet submission from #{issue_number}",
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
    """Mirror the submission YAML path under generated_pdfs/."""
    period = submission["period"]
    submission_id = submission["submission_id"]
    return repo_root / "generated_pdfs" / period / f"{submission_id}.pdf"


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
                   default=date.today().isoformat(),
                   help="ISO date for `submitted_date` (default: today).")
    p.add_argument("--repo-root", default=".",
                   help="Working tree root (default: current directory).")
    p.add_argument("--templates-dir", default="templates",
                   help="Templates directory (relative to --repo-root). Default: templates.")
    p.add_argument("--settings-file", default="config/settings.yml",
                   help="Path to settings.yml (relative to --repo-root). Default: config/settings.yml.")
    p.add_argument("--skip-pdf", action="store_true",
                   help="Skip PDF rendering (useful for local dry-runs without typst installed).")
    args = p.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    templates_dir = (repo_root / args.templates_dir).resolve()
    settings_path = (repo_root / args.settings_file).resolve()

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

    submission = enrich_submission(
        parsed,
        contract,
        submitter=args.issue_author,
        issue_number=args.issue_number,
        submitted_date=args.submitted_date,
    )

    # Write the YAML.
    yaml_path = write_submission_yaml(submission, repo_root)
    yaml_rel = yaml_path.relative_to(repo_root).as_posix()
    print(f"Wrote {yaml_rel}")

    # Render the PDF (preview state — approval block will say "PENDING REVIEW").
    pdf_rel: Optional[str] = None
    paths_to_stage: list[Path] = [yaml_path]
    if not args.skip_pdf:
        pdf_path = submission_pdf_path(submission, repo_root)
        render_submission_pdf(
            submission_path=yaml_path,
            settings_path=settings_path,
            template_dir=templates_dir,
            output_path=pdf_path,
        )
        pdf_rel = pdf_path.relative_to(repo_root).as_posix()
        paths_to_stage.append(pdf_path)
        print(f"Wrote {pdf_rel}")

    # Git: branch, commit, push.
    create_branch(branch, cwd=repo_root)
    stage_and_commit(paths_to_stage, args.issue_number, cwd=repo_root)
    push_branch(branch, cwd=repo_root)

    # Open PR.
    body = render_pr_body(
        issue_number=args.issue_number,
        submitter=args.issue_author,
        submission=submission,
        submission_path_rel=yaml_rel,
        pdf_path_rel=pdf_rel,
        warnings=warnings,
    )
    pr_url = open_pr(args.issue_title, body, cwd=repo_root)
    print(f"Opened PR: {pr_url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
