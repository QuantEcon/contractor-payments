#!/usr/bin/env python3
"""Regenerate a contractor repo's issue forms, caller workflows, and labels
from its own config — the single source of truth for which submission types
the repo offers.

Presence rule (PLAN §9 "Conditional issue-form availability"):

    hourly-timesheet.yml     iff ≥1 `status: active` hourly contract
    milestone-invoice.yml    iff ≥1 `status: active` milestone contract
    reimbursement-claim.yml  iff config/reimbursements.yml exists

Forms with nothing to offer are **deleted** — this replaces the earlier
`"(no contracts yet)"` placeholder seeding, which left dead forms visible
in the New Issue chooser. Contract dropdowns and reminder blocks are
rebuilt from ALL active contracts (onboarding only ever knew the first
one; renewals previously meant hand-editing the form YAML).

Used three ways:

  - retrofit / refresh an existing repo (the CLI):
        python onboarding/sync_templates.py --repo-dir contractors/contractor-X [--dry-run]
    Run it after adding/renewing/ending a contract or adding
    config/reimbursements.yml — the relevant forms appear/disappear.
  - onboarding: new-contractor.py calls `sync_issue_templates()` after
    writing the repo's config, so seeding and retrofit can never drift.
  - one-time reimbursement enablement:
        python onboarding/sync_templates.py --repo-dir ... --init-reimbursement-ledger
    creates the pinned "Running ledger — Reimbursements" issue and writes
    its number into config/reimbursements.yml.

The caller workflow files (.github/workflows/) carry no substitutions and
are synced verbatim from contractor-template/ so per-repo plumbing tracks
the engine (e.g. the `reimbursement` label gate added in Phase 5).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template
from typing import Optional

import yaml

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts.setup_labels import LABELS as WORKFLOW_LABELS, create_label  # noqa: E402
from scripts.update_ledger import empty_ledger  # noqa: E402
from scripts.update_ledger_issue import render_reimbursement_body  # noqa: E402

TEMPLATE_DIR = ENGINE_ROOT / "contractor-template"

# Files this script owns in a contractor repo. Identity files seeded at
# onboarding (settings.yml, README, CODEOWNERS) are deliberately NOT synced —
# they may carry manual edits.
FORM_FILES = {
    "hourly": ".github/ISSUE_TEMPLATE/hourly-timesheet.yml",
    "milestone": ".github/ISSUE_TEMPLATE/milestone-invoice.yml",
    "reimbursement": ".github/ISSUE_TEMPLATE/reimbursement-claim.yml",
}
WORKFLOW_FILES = (
    ".github/workflows/issue-to-pr.yml",
    ".github/workflows/process-approved.yml",
    ".github/workflows/period-reminders.yml",
)


# ─── Repo state (pure) ──────────────────────────────────────────────────────

def load_repo_state(repo_dir: Path) -> tuple[list[dict], Optional[dict]]:
    """Read (contracts, reimbursements_config) from a contractor repo
    checkout. Contracts are every parseable YAML under contracts/;
    reimbursements_config is None when config/reimbursements.yml is absent
    (reimbursements disabled)."""
    contracts: list[dict] = []
    contracts_dir = repo_dir / "contracts"
    if contracts_dir.is_dir():
        for path in sorted(contracts_dir.glob("*.yml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("contract_id"):
                contracts.append(data)

    reimbursements: Optional[dict] = None
    reimbursements_path = repo_dir / "config" / "reimbursements.yml"
    if reimbursements_path.exists():
        reimbursements = yaml.safe_load(
            reimbursements_path.read_text(encoding="utf-8")
        ) or {}
    return contracts, reimbursements


def active_contracts(contracts: list[dict], contract_type: str) -> list[dict]:
    return [
        c for c in contracts
        if c.get("type") == contract_type and c.get("status") == "active"
    ]


# ─── Substitution blocks (pure) ─────────────────────────────────────────────

def _options_yaml(contract_ids: list[str]) -> str:
    """Indented options block for an Issue Form contract dropdown. Callers
    only render a form when it has ≥1 contract, so the empty case never
    ships — kept as a loud sentinel rather than invalid YAML."""
    if not contract_ids:
        return '        - "(no contracts yet)"'
    return "\n".join(f'        - "{cid}"' for cid in contract_ids)


def _hourly_reminder(contracts: list[dict]) -> str:
    lines = []
    for c in contracts:
        terms = c.get("terms", {})
        lines.append(
            f"        - `{c['contract_id']}` — "
            f"{terms.get('currency', '?')}, {terms.get('hourly_rate', '?')}/hour"
        )
    return "\n".join(lines) if lines else "        - _(no hourly contracts yet)_"


def _milestone_reminder(contracts: list[dict]) -> str:
    lines = [
        f"        - `{c['contract_id']}` — {c.get('currency', '?')} (milestone)"
        for c in contracts
    ]
    return "\n".join(lines) if lines else "        - _(no milestone contracts yet)_"


def _categories_reminder(reimbursements: Optional[dict]) -> str:
    categories = (reimbursements or {}).get("allowed_categories") or []
    if not categories:
        return "        - _(no categories configured — ask the admin)_"
    return "\n".join(f"        - `{c}`" for c in categories)


def build_substitutions(
    contracts: list[dict],
    reimbursements: Optional[dict],
) -> dict[str, str]:
    """Placeholder map for the issue-form templates, built from ALL active
    contracts (generalises onboarding's first-contract-only seeding)."""
    hourly = active_contracts(contracts, "hourly")
    milestone = active_contracts(contracts, "milestone")
    return {
        "CONTRACT_OPTIONS": _options_yaml([c["contract_id"] for c in hourly]),
        "MILESTONE_CONTRACT_OPTIONS": _options_yaml(
            [c["contract_id"] for c in milestone]
        ),
        "HOURLY_CONTRACT_REMINDER": _hourly_reminder(hourly),
        "MILESTONE_CONTRACT_REMINDER": _milestone_reminder(milestone),
        "REIMBURSEMENT_CATEGORIES_REMINDER": _categories_reminder(reimbursements),
    }


def render_issue_templates(
    contracts: list[dict],
    reimbursements: Optional[dict],
    *,
    template_dir: Path = TEMPLATE_DIR,
) -> dict[str, Optional[str]]:
    """Compute the desired state of the three issue forms.

    Returns {relpath: content-or-None}; None means "this form must not
    exist" (the presence rule). Content is the engine template with
    placeholders substituted.
    """
    substitutions = build_substitutions(contracts, reimbursements)
    wanted = {
        "hourly": bool(active_contracts(contracts, "hourly")),
        "milestone": bool(active_contracts(contracts, "milestone")),
        "reimbursement": reimbursements is not None,
    }
    out: dict[str, Optional[str]] = {}
    for kind, relpath in FORM_FILES.items():
        if not wanted[kind]:
            out[relpath] = None
            continue
        source = (template_dir / relpath).read_text(encoding="utf-8")
        out[relpath] = Template(source).safe_substitute(substitutions)
    return out


def plan_sync(
    repo_dir: Path,
    *,
    template_dir: Path = TEMPLATE_DIR,
) -> list[tuple[str, str, Optional[str]]]:
    """Compute the sync plan: [(relpath, action, content)] with action in
    {write, delete, unchanged}. Covers the issue forms (presence rule +
    substitutions) and the caller workflows (verbatim engine copies)."""
    contracts, reimbursements = load_repo_state(repo_dir)
    desired = render_issue_templates(
        contracts, reimbursements, template_dir=template_dir,
    )
    for relpath in WORKFLOW_FILES:
        desired[relpath] = (template_dir / relpath).read_text(encoding="utf-8")

    plan: list[tuple[str, str, Optional[str]]] = []
    for relpath, content in sorted(desired.items()):
        target = repo_dir / relpath
        if content is None:
            plan.append((relpath, "delete" if target.exists() else "unchanged", None))
        elif not target.exists():
            plan.append((relpath, "write", content))
        elif target.read_text(encoding="utf-8") != content:
            plan.append((relpath, "write", content))
        else:
            plan.append((relpath, "unchanged", None))
    return plan


def apply_plan(repo_dir: Path, plan: list[tuple[str, str, Optional[str]]]) -> list[str]:
    """Apply write/delete actions. Returns the relpaths that changed."""
    changed: list[str] = []
    for relpath, action, content in plan:
        target = repo_dir / relpath
        if action == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            changed.append(relpath)
        elif action == "delete":
            target.unlink()
            changed.append(relpath)
    return changed


def sync_issue_templates(repo_dir: Path) -> list[str]:
    """Convenience wrapper used by onboarding: plan + apply in one call.
    Returns the changed relpaths."""
    return apply_plan(repo_dir, plan_sync(repo_dir))


# ─── Pinned reimbursements ledger issue ─────────────────────────────────────

def open_pinned_issue(repo: str, title: str, body: str, *,
                      dry_run: bool) -> Optional[int]:
    """Create a pinned + locked issue with the `ledger` label. Returns the
    issue number, or None under dry-run. (Generalised from
    new-contractor.py's contract-ledger variant.)"""
    if dry_run:
        print(f"  [dry-run] gh issue create --title \"{title}\" --label ledger")
        print("  [dry-run] gh issue pin/lock <N>")
        return None

    create_label("ledger", "Pinned running-totals issue", "5319e7", repo)

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        body_path = Path(f.name)
    try:
        r = subprocess.run(
            ["gh", "issue", "create", "--repo", repo, "--title", title,
             "--label", "ledger", "--body-file", str(body_path)],
            capture_output=True, text=True, check=True,
        )
    finally:
        body_path.unlink(missing_ok=True)

    url = r.stdout.strip().splitlines()[-1]
    match = re.search(r"/issues/(\d+)$", url)
    if not match:
        print(f"WARNING: couldn't parse issue number from `{url}`.",
              file=sys.stderr)
        return None
    n = int(match.group(1))
    subprocess.run(["gh", "issue", "pin", str(n), "--repo", repo], check=True)
    subprocess.run(["gh", "issue", "lock", str(n), "--repo", repo], check=True)
    return n


def init_reimbursement_ledger(repo_dir: Path, repo: str, *,
                              dry_run: bool) -> Optional[int]:
    """Create the pinned reimbursements ledger issue and write its number
    into config/reimbursements.yml. No-op (with a warning) when the config
    is absent or the issue is already wired."""
    reimbursements_path = repo_dir / "config" / "reimbursements.yml"
    if not reimbursements_path.exists():
        print("WARN: config/reimbursements.yml not found — nothing to init.",
              file=sys.stderr)
        return None
    config = yaml.safe_load(reimbursements_path.read_text(encoding="utf-8")) or {}
    if config.get("ledger_issue"):
        print(f"Reimbursements ledger issue already wired "
              f"(#{config['ledger_issue']}); nothing to do.")
        return None

    body = render_reimbursement_body(empty_ledger(ledger_type="reimbursement"), config)
    issue_number = open_pinned_issue(
        repo, "📒 Running ledger — Reimbursements", body, dry_run=dry_run,
    )
    if issue_number is None:
        return None

    config["ledger_issue"] = issue_number
    with reimbursements_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)
    print(f"Wired ledger_issue: {issue_number} into config/reimbursements.yml")
    return issue_number


# ─── CLI ────────────────────────────────────────────────────────────────────

def _detect_repo_slug(repo_dir: Path) -> Optional[str]:
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True, text=True, cwd=repo_dir,
    )
    return r.stdout.strip() or None if r.returncode == 0 else None


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo-dir", required=True, type=Path,
                   help="Path to a contractor repo checkout (e.g. "
                        "contractors/contractor-alice).")
    p.add_argument("--repo", default=None,
                   help="GitHub owner/name. Default: detected via gh from "
                        "--repo-dir's remote.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan; write nothing, push nothing.")
    p.add_argument("--no-push", action="store_true",
                   help="Apply + commit locally but skip the push.")
    p.add_argument("--init-reimbursement-ledger", action="store_true",
                   help="Create the pinned reimbursements ledger issue and "
                        "write its number into config/reimbursements.yml.")
    p.add_argument("--labels", action="store_true",
                   help="Also ensure the workflow labels exist on the repo "
                        "(idempotent; recommended for retrofits).")
    args = p.parse_args(argv)

    repo_dir = args.repo_dir.resolve()
    if not (repo_dir / ".git").exists():
        print(f"ERROR: {repo_dir} is not a git checkout.", file=sys.stderr)
        return 1

    plan = plan_sync(repo_dir)
    print(f"Sync plan for {repo_dir.name}:")
    for relpath, action, _ in plan:
        marker = {"write": "✏️ ", "delete": "🗑️ ", "unchanged": "  "}[action]
        print(f"  {marker}{action:<9} {relpath}")

    if args.dry_run:
        if args.labels:
            print("  [dry-run] would ensure workflow labels exist")
        if args.init_reimbursement_ledger:
            init_reimbursement_ledger(repo_dir, args.repo or "<repo>", dry_run=True)
        print("[dry-run complete — no changes made]")
        return 0

    repo = args.repo or _detect_repo_slug(repo_dir)

    changed = apply_plan(repo_dir, plan)

    if args.labels:
        if not repo:
            print("ERROR: couldn't detect repo slug for label creation; "
                  "pass --repo.", file=sys.stderr)
            return 1
        print("Ensuring workflow labels:")
        for name, description, color in WORKFLOW_LABELS:
            print(f"  {create_label(name, description, color, repo)}")

    if args.init_reimbursement_ledger:
        if not repo:
            print("ERROR: couldn't detect repo slug for issue creation; "
                  "pass --repo.", file=sys.stderr)
            return 1
        if init_reimbursement_ledger(repo_dir, repo, dry_run=False) is not None:
            changed.append("config/reimbursements.yml")

    if not changed:
        print("Everything already in sync; nothing to commit.")
        return 0

    subprocess.run(["git", "add", "--all", *{c.split("/")[0] for c in changed}],
                   cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-m",
         "Sync issue templates + workflows from engine contractor-template"],
        cwd=repo_dir, check=True,
    )
    if args.no_push:
        print("Committed locally (--no-push); push when ready.")
    else:
        subprocess.run(["git", "push"], cwd=repo_dir, check=True)
        print("Committed and pushed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
