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
from scripts.parse_issue import cross_check_milestone_ids


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

    Pure function — does not check for collisions. The caller applies one
    of two suffix schemes depending on the trigger (see PLAN §8 Phase 2.5):

    - `resolve_revision_suffix` — for reopen-triggered revisions; appends
      `-v2`, `-v3`, ... and carries supersede semantics.
    - `resolve_uniqueness_suffix` — for fresh-issue submissions that
      collide on the period; appends `-B`, `-C`, ... purely for ID
      uniqueness (no semantic relationship to the original).
    """
    slug = _TYPE_SLUG.get(submission_type, "submission")
    return f"{github_handle}-{slug}-{period}"


def resolve_revision_suffix(repo_root: Path, supersedes_id: str, period: str) -> str:
    """Compute the next available `-vN` suffix for a revision.

    `supersedes_id` is the previous merged submission this revision targets
    (e.g. `mmcky-invoice-2026-02` for a first revision, or
    `mmcky-invoice-2026-02-v2` for a revision-of-revision). The returned
    ID appends `-v2`, `-v3`, ... walking the chain past whatever's already
    committed.

    The chain anchor is computed by stripping any trailing `-vN` from
    `supersedes_id`. The new revision belongs to the same chain.
    """
    anchor = _strip_revision_suffix(supersedes_id)
    submissions_dir = repo_root / "submissions" / period
    if not submissions_dir.exists():
        # supersedes_id was passed but nothing's committed — degenerate
        # state. Treat as first revision off the anchor.
        return f"{anchor}-v2"
    n = 2
    while (submissions_dir / f"{anchor}-v{n}.yml").exists():
        n += 1
    return f"{anchor}-v{n}"


def resolve_uniqueness_suffix(repo_root: Path, base_id: str, period: str) -> str:
    """Compute the next available `-{LETTER}` suffix from B onward to keep
    a fresh submission's ID unique when the base already exists in
    `submissions/<period>/`.

    Unlike `-vN`, this suffix carries **no semantic relationship** to the
    original — it's purely for filesystem uniqueness when two independent
    invoices happen to share a period (e.g. two milestones delivered in
    the same calendar month).

    Returns `base_id` unchanged when no collision exists. When committed
    files already use one or more letters, picks the next unused letter.
    Capped at Z; if exceeded (vanishingly unlikely with monthly cadence),
    raises so the failure is loud rather than silent.
    """
    submissions_dir = repo_root / "submissions" / period
    if not submissions_dir.exists():
        return base_id
    if not (submissions_dir / f"{base_id}.yml").exists():
        return base_id
    # base exists — find next available letter from B onward.
    for letter in "BCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = f"{base_id}-{letter}"
        if not (submissions_dir / f"{candidate}.yml").exists():
            return candidate
    raise RuntimeError(
        f"Exhausted A–Z suffix space for {base_id} in {period}. "
        f"This indicates 26+ independent invoices in a single period, "
        f"which is far beyond expected use. Investigate before extending."
    )


def _strip_revision_suffix(submission_id: str) -> str:
    """Return the chain anchor by removing a trailing `-vN` if present.

    `mmcky-invoice-2026-02-v3` → `mmcky-invoice-2026-02`
    `mmcky-invoice-2026-02-B-v2` → `mmcky-invoice-2026-02-B`
    `mmcky-invoice-2026-02-B` → `mmcky-invoice-2026-02-B` (unchanged)
    `mmcky-invoice-2026-02` → `mmcky-invoice-2026-02` (unchanged)
    """
    return re.sub(r"-v\d+$", "", submission_id)


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
    supersedes: Optional[str] = None,
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
        # PSL funding/billing code — rendered as "Project" on the PDF. Absent
        # on contracts that pre-date the field (None → template omits the line).
        "project": contract.get("project"),
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
    if supersedes:
        # Revision: stamp both the immediate predecessor and the chain anchor.
        # `revision_of` is the bare original (or `-B` etc. for a revision of an
        # independent invoice); `supersedes` is the most recent merged version
        # this revision replaces.
        common["supersedes"] = supersedes
        common["revision_of"] = _strip_revision_suffix(supersedes)

    if submission_type == "timesheet":
        if contract_type != "hourly":
            raise ValueError(
                f"Contract `{contract_id_in_contract}` is type `{contract_type}`, "
                f"but a hourly timesheet submission requires a `hourly` contract."
            )
        terms = contract.get("terms", {})
        required = ("hourly_rate", "currency", "max_hours_per_month")
        missing = [k for k in required if k not in terms]
        if missing:
            raise ValueError(
                f"Contract `{contract_id_in_contract}` is missing required terms: "
                f"{', '.join(f'`{k}`' for k in missing)}."
            )
        hourly_rate = float(terms["hourly_rate"])
        currency = terms["currency"]
        # max_hours_per_month is required on the contract but may be null
        # (explicit "uncapped" — admin chose not to set a monthly cap).
        raw_cap = terms["max_hours_per_month"]
        max_hours_per_month = float(raw_cap) if raw_cap is not None else None
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
                "max_hours_per_month": max_hours_per_month,
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
    if submission_type == "timesheet":
        cap = totals.get("max_hours_per_month")
        if cap is not None and totals["hours"] > cap:
            lines.extend([
                f"> ⚠️ **Above contract cap** — submitted hours "
                f"**{totals['hours']}** exceed the contract's "
                f"`max_hours_per_month` of **{cap}**. "
                f"Confirm out-of-band approval before merging.",
                "",
            ])
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


def find_open_pr_for_branch(branch: str, cwd: Optional[Path] = None) -> Optional[int]:
    """Return the open PR number for `branch` if one exists, else None.

    Used to distinguish "in-progress submission, regenerate in place"
    (open PR exists) from "stale branch leftover from a previous merged
    submission, treat as fresh" (no open PR). A stale branch can stick
    around when auto-delete-on-merge didn't fire — we don't want to
    block fresh revisions in that case.
    """
    result = _run(
        ["gh", "pr", "list", "--head", branch, "--state", "open",
         "--json", "number", "--jq", "first | .number // empty"],
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return int(out) if out else None


def create_branch(branch: str, cwd: Optional[Path] = None) -> None:
    _run(["git", "checkout", "-b", branch], cwd=cwd)


def checkout_existing_branch(branch: str, cwd: Optional[Path] = None) -> None:
    """Fetch and check out an existing remote branch.

    Used when the workflow re-fires on an issue whose PR is already open
    (pre-merge edits) or whose previous branch is being reused for a
    revision after auto-delete. We regenerate the submission artifacts
    on top of the existing branch and force-push.
    """
    _run(["git", "fetch", "origin", f"{branch}:{branch}"], cwd=cwd)
    _run(["git", "checkout", branch], cwd=cwd)


# Directories the engine owns outright on a submission branch. Everything
# under them for a given issue is generated, so a resubmit is free to
# replace the previous run's output wholesale.
_ARTIFACT_DIRS = ("submissions", "generated_pdfs")


def purge_branch_artifacts(
    base_ref: str,
    cwd: Optional[Path] = None,
    *,
    dirs: tuple[str, ...] = _ARTIFACT_DIRS,
) -> list[Path]:
    """Delete every engine artifact this branch added on top of `base_ref`.

    Called on the update path (open PR, same issue re-submitted) after the
    branch is checked out, before the new artifacts are written. Without it
    a resubmit only ever *adds*, because `stage_and_commit` is handed just
    the paths it wrote this run. The damaging case is a corrected period: the
    run writes a *second* submission YAML, both land on the branch, and
    `process-approved.yml` then approves whichever one sorts first — which for
    a forward correction is the claim the contractor abandoned.

    Scoped to what the branch itself contributed (`base_ref...HEAD`) so
    artifacts already merged on the base branch — the original behind a `-vN`
    revision, say — are left alone. `dirs` narrows it further: only purge what
    this run will regenerate, so `--skip-pdf` keeps the committed PDF rather
    than dropping it. Returns the deleted paths so the caller can stage the
    removals.
    """
    result = _run(
        ["git", "diff", "--name-only", "--diff-filter=AM",
         f"{base_ref}...HEAD", "--", *dirs],
        cwd=cwd,
    )
    root = Path(cwd) if cwd else Path(".")
    removed: list[Path] = []
    for rel in result.stdout.split("\n"):
        rel = rel.strip()
        if not rel:
            continue
        path = root / rel
        if path.is_file():
            path.unlink()
            removed.append(path)

    # Prune the period directories the deletions just emptied. Git does not
    # track empty directories, so this is tidiness rather than correctness —
    # but it keeps the working tree a faithful picture of the commit.
    artifact_roots = {root / d for d in dirs}
    for path in removed:
        parent = path.parent
        while parent not in artifact_roots and parent != root:
            try:
                parent.rmdir()
            except OSError:
                break  # not empty, or gone already
            parent = parent.parent
    return removed


def stage_and_commit(
    paths: list[Path],
    issue_number: int,
    cwd: Optional[Path] = None,
    submission_type: str = "timesheet",
    *,
    update: bool = False,
) -> bool:
    """Stage and commit the listed paths.

    Returns True if a commit was created, False if there were no staged
    changes (no-op — possible when re-running on an existing branch
    where the regenerated artifacts are identical to what's already
    committed). Callers should treat False as "nothing to push".

    Staged with `git add --all -- <path>` so that *removals* are recorded
    too: on the update path `purge_branch_artifacts` deletes the previous
    run's output, and a plain `git add <path>` of a vanished file leaves the
    deletion unstaged — the branch would then keep both copies.
    """
    if paths:
        _run(["git", "add", "--all", "--", *(str(p) for p in paths)], cwd=cwd)
    # Has anything been staged? `git diff --cached --quiet` returns 0
    # when there are no staged diffs, non-zero otherwise.
    result = _run(["git", "diff", "--cached", "--quiet"], cwd=cwd, check=False)
    if result.returncode == 0:
        return False
    type_label = {
        "timesheet": "timesheet",
        "milestone_invoice": "milestone invoice",
    }.get(submission_type, "submission")
    verb = "Update" if update else "Add"
    _run([
        "git", "commit", "-m",
        f"{verb} {type_label} submission from #{issue_number}",
    ], cwd=cwd)
    return True


def push_branch(branch: str, cwd: Optional[Path] = None, *, force: bool = False) -> None:
    cmd = ["git", "push", "-u", "origin", branch]
    if force:
        cmd.insert(2, "--force-with-lease")
    _run(cmd, cwd=cwd)


def open_pr(
    issue_title: str,
    body: str,
    cwd: Optional[Path] = None,
    extra_labels: Optional[list[str]] = None,
    is_revision: bool = False,
) -> str:
    """Open a PR and return its URL.

    Revisions get a `(revision)` suffix on the PR title so reviewers see
    the relationship at a glance in the PR list.
    """
    extra_labels = extra_labels or []
    fd, name = tempfile.mkstemp(suffix=".md", prefix="pr-body-")
    body_path = Path(name)
    title_suffix = " (revision)" if is_revision else ""
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        cmd = [
            "gh", "pr", "create",
            "--title", f"Submission: {issue_title}{title_suffix}",
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


def update_pr_body(pr_number: int, body: str, cwd: Optional[Path] = None) -> bool:
    """Rewrite an open PR's description. Returns True on success.

    The push updates a PR's *commits*, never its body, so on the resubmit path
    the body kept describing the first submission — stale total, stale period,
    stale line items — while the YAML and PDF underneath it moved on. That body is the admin's approval decision surface, so it has to
    track the branch.

    Deliberately non-fatal: by the time this runs the submission is already
    committed and pushed, and losing the submission over a failed `gh` call
    would be far worse than an out-of-date description. The caller warns.
    """
    fd, name = tempfile.mkstemp(suffix=".md", prefix="pr-body-")
    body_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        result = _run(
            ["gh", "pr", "edit", str(pr_number), "--body-file", str(body_path)],
            cwd=cwd, check=False,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)
            return False
        return True
    finally:
        body_path.unlink(missing_ok=True)


def warn_stale_pr_body(pr_number: int, cwd: Optional[Path] = None) -> bool:
    """Comment on the PR that its description is out of date. Returns True if
    the comment landed.

    The fallback for `update_pr_body` failing. The run stays green in that
    case — the submission is committed and pushed — so the only other signal
    is a stderr line in a workflow log nobody opens, leaving the approver
    reading stale numbers with no indication they are stale.

    Never raises: this is already the degraded path.
    """
    note = (
        "> [!WARNING]\n"
        "> **This description is out of date.** The submission was updated "
        "and re-pushed, but refreshing this description failed.\n"
        ">\n"
        "> Review the committed submission YAML and the rendered PDF in "
        "**Files changed** — those are current. The totals and line items "
        "above may describe an earlier version of this submission."
    )
    result = _run(
        ["gh", "pr", "comment", str(pr_number), "--body", note],
        cwd=cwd, check=False,
    )
    if result.returncode != 0:
        print(
            f"WARNING: could not comment the stale-description caveat on PR "
            f"#{pr_number} either — this log is the only record.",
            file=sys.stderr,
        )
        return False
    return True


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
    p.add_argument("--supersedes", default=None,
                   help="Submission ID of the previous merged submission this one "
                        "revises. Set by the workflow on `issues.reopened` events "
                        "after looking up the issue's latest merged PR. When set, "
                        "this submission gets a `-vN` suffix off the supersedes "
                        "chain anchor, `supersedes` + `revision_of` metadata "
                        "stamped in the YAML, and `(revision)` appended to the PR "
                        "title. See PLAN §8 Phase 2.5.")
    args = p.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    templates_dir = (repo_root / args.templates_dir).resolve()
    settings_path = (repo_root / args.settings_file).resolve()

    if args.submitted_date is None:
        args.submitted_date = resolve_payer_today(templates_dir / "fiscal-host.yml")

    # Branch + PR state. Two cases that we used to conflate:
    #
    # - Open PR exists on this branch → in-progress submission.
    #   Regenerate artifacts on top of the existing branch; the PR
    #   auto-updates from the force-push. (Covers pre-merge edits and
    #   reopen-then-edit on a revision.)
    # - No open PR → either this is a fresh issue, OR a previous PR for
    #   this issue was merged/closed and the branch lingered (e.g.
    #   auto-delete-on-merge didn't fire). In both cases, treat as fresh:
    #   force-push the new content to the (possibly stale) branch and
    #   open a NEW PR.
    branch = branch_name_for_issue(args.issue_number)
    branch_exists_remotely = remote_branch_exists(branch, cwd=repo_root)
    open_pr_number = find_open_pr_for_branch(branch, cwd=repo_root) if branch_exists_remotely else None
    has_open_pr = open_pr_number is not None
    if branch_exists_remotely and not has_open_pr:
        print(
            f"Branch `{branch}` exists on origin but has no open PR — "
            f"treating as a fresh submission and opening a new PR. "
            f"(Stale branch likely from a previously merged PR.)",
            file=sys.stderr,
        )

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

    is_revision = bool(args.supersedes)
    if is_revision:
        submission_id = resolve_revision_suffix(
            repo_root, args.supersedes, parsed["period"],
        )
        print(
            f"Revision: superseding `{args.supersedes}` with `{submission_id}`.",
            file=sys.stderr,
        )
    else:
        submission_id = resolve_uniqueness_suffix(repo_root, base_id, parsed["period"])
        if submission_id != base_id:
            print(
                f"Collision detected on `{base_id}` — using uniqueness suffix "
                f"`{submission_id}`. This is an independent second invoice in the "
                f"same period, not a revision.",
                file=sys.stderr,
            )

    submission = enrich_submission(
        parsed,
        contract,
        submitter=args.issue_author,
        submission_id=submission_id,
        issue_number=args.issue_number,
        submitted_date=args.submitted_date,
        supersedes=args.supersedes,
    )

    # Phase 3b: non-blocking cross-check between submitted milestone IDs and
    # the contract's pre-declared schedule. Surfaces as warnings on the PR
    # body so the admin sees typos at review time without the engine
    # rejecting otherwise-valid submissions. No-op for hourly contracts and
    # for milestone contracts that don't carry a structured `milestones[]`
    # list yet.
    for w in cross_check_milestone_ids(submission, contract):
        warnings.append({"message": w.message})

    # If there's an open PR for this branch, switch to the branch BEFORE
    # writing any artifacts (untracked files would conflict with the
    # branch's tracked content on checkout). Stale branches without an
    # open PR are force-pushed past — they don't need a checkout.
    purged: list[Path] = []
    if has_open_pr:
        # Capture the default-branch tip we were checked out on before
        # switching, so the purge below can tell "added by this branch"
        # from "already merged on the base branch".
        base_ref = _run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root
        ).stdout.strip()
        checkout_existing_branch(branch, cwd=repo_root)
        # The resubmit replaces the previous run's output rather than
        # accumulating alongside it. See purge_branch_artifacts.
        purge_dirs = _ARTIFACT_DIRS
        if args.skip_pdf:
            # Nothing will regenerate them, so leave the committed PDFs alone.
            purge_dirs = tuple(d for d in purge_dirs if d != "generated_pdfs")
        purged = purge_branch_artifacts(
            base_ref, cwd=repo_root, dirs=purge_dirs
        )
        for path in purged:
            print(f"Removed stale {path.relative_to(repo_root).as_posix()}")

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
    # `purged` first so the removals are staged even when the resubmit
    # rewrites a different period/id and never touches those paths again.
    paths_to_stage: list[Path] = [*purged, yaml_path]
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
    # - has_open_pr: we've already checked out the branch; commit + force-push.
    # - branch_exists_remotely but no open PR: stale branch; create a fresh
    #   local branch (overwriting the lingering remote on push).
    # - branch doesn't exist: normal create.
    if has_open_pr:
        pass  # already on the branch
    elif branch_exists_remotely:
        # Create the local branch fresh; we'll force-push to overwrite
        # whatever's on origin.
        create_branch(branch, cwd=repo_root)
    else:
        create_branch(branch, cwd=repo_root)

    committed = stage_and_commit(
        paths_to_stage, args.issue_number, cwd=repo_root,
        submission_type=submission_type,
        update=has_open_pr,
    )
    if committed:
        # Force-push when the remote already has the branch (open PR or
        # stale branch). Normal push for a truly new branch.
        push_branch(branch, cwd=repo_root, force=branch_exists_remotely)
    elif has_open_pr:
        print("No content changes — branch already reflects the latest "
              "submission state. Skipping push.", file=sys.stderr)
    else:
        # Nothing staged on a fresh branch is anomalous, but not fatal.
        print("No content changes detected; branch left in current state.",
              file=sys.stderr)

    body = render_pr_body(
        issue_number=args.issue_number,
        submitter=args.issue_author,
        submission=submission,
        submission_path_rel=yaml_rel,
        pdf_path_rel=pdf_rel,
        png_url=png_url,
        warnings=warnings,
    )

    if has_open_pr:
        # PR already open on this branch; the push updated its commits, but
        # GitHub never re-derives the description — so refresh it here or the
        # admin reviews the *first* submission's numbers against the current
        # PDF. `png_url` is already scoped to this branch, so the preview
        # points at the image just committed.
        print(f"Updated existing PR #{open_pr_number} on branch `{branch}`.")
        if update_pr_body(open_pr_number, body, cwd=repo_root):
            print(f"Refreshed PR #{open_pr_number} description.")
        else:
            # The submission is committed and pushed at this point; a failed
            # description update must not throw that away.
            print(
                f"WARNING: could not refresh PR #{open_pr_number}'s description "
                f"— it still describes an earlier submission. The pushed YAML "
                f"and PDF are the current ones: review those, not the PR body, "
                f"or re-run /submit to retry.",
                file=sys.stderr,
            )
            # The run stays green, so a log line is invisible to the person who
            # matters. The PR body is the approval-decision surface — put the
            # caveat where the approver will actually see it.
            warn_stale_pr_body(open_pr_number, cwd=repo_root)
        return 0

    # Fresh branch → open a new PR.
    pr_url = open_pr(
        args.issue_title, body, cwd=repo_root,
        extra_labels=[submission_type.replace("_", "-")],
        is_revision=is_revision,
    )
    print(f"Opened PR: {pr_url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
