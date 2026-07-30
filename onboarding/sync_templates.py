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

All three paths validate config/reimbursements.yml's shape first (see
`validate_reimbursements_config`): a config the engine can only read
ambiguously — a non-list `allowed_categories`, an unsubstituted placeholder —
stops the sync with an explanation instead of quietly shipping an allowlist
that rejects every claim.

The caller workflow files (.github/workflows/) carry no substitutions and
are synced verbatim from contractor-template/ so per-repo plumbing tracks
the engine (e.g. the `reimbursement` label gate added in Phase 5).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from string import Template
from typing import Callable, Optional

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

REIMBURSEMENT_LEDGER_TITLE = "📒 Running ledger — Reimbursements"


# ─── Reimbursement config validation ────────────────────────────────────────

class ReimbursementConfigError(ValueError):
    """`config/reimbursements.yml` has a shape the engine will not guess at.

    Raised at sync/onboarding time — while the admin is still at the keyboard —
    rather than letting a malformed config reach a contractor's claim weeks
    later, where the symptom is a baffling validation failure.
    """


def validate_reimbursements_config(config: dict, path: Path) -> None:
    """Reject reimbursement configs whose shape silently breaks the parser.

    Three failure modes are worth a loud error here:

      - **Unsubstituted template literals.** `project: $REIMBURSEMENT_PROJECT`
        and friends are truthy strings, so every downstream truthiness test
        passes and the placeholder text ends up on a claim PDF (or, for
        `ledger_issue`, makes this module believe the ledger is already wired).
      - **`allowed_categories` that isn't a list.** `allowed_categories:
        travel, meals` — forgetting the brackets — parses as the *string*
        `"travel, meals"`, and `parse_issue`'s membership test then compares
        each claim's category against that one string, rejecting every real
        category.
      - **An explicitly empty `allowed_categories`.** `parse_issue` guards the
        check with `if allowed_categories:`, so `[]` means "no restriction" —
        the exact opposite of how it reads to whoever edits the file. "Reject
        every category" is never a useful intent, so we refuse to pick a
        reading: omit the key for no restriction, list categories to restrict.
    """
    for key in ("project", "allowed_categories", "ledger_issue"):
        value = config.get(key)
        if isinstance(value, str) and value.startswith("$"):
            raise ReimbursementConfigError(
                f"{path}: `{key}` still holds the template placeholder "
                f"`{value}`. This file is seeded from "
                f"contractor-template/config/reimbursements.yml and its "
                f"placeholders must be filled in — set a real value "
                f"(`ledger_issue: null` before the ledger issue exists)."
            )

    if "allowed_categories" in config:
        categories = config["allowed_categories"]
        if not isinstance(categories, list):
            raise ReimbursementConfigError(
                f"{path}: `allowed_categories` must be a YAML list, but it "
                f"parsed as {type(categories).__name__} "
                f"({categories!r}). Write it as a list:\n"
                f"    allowed_categories: [travel, meals]\n"
                f"(a block list with `- travel` per line works too). A bare "
                f"comma-separated string is one string, and the category "
                f"check would then reject every claim."
            )
        if not categories:
            raise ReimbursementConfigError(
                f"{path}: `allowed_categories` is an empty list, which is "
                f"ambiguous — the parser reads an empty list as 'no category "
                f"restriction', not 'reject every category'. To accept any "
                f"category, delete the `allowed_categories` key; to restrict, "
                f"list the categories: allowed_categories: [travel, meals]."
            )
        bad = [c for c in categories
               if not isinstance(c, str) or not c.strip()]
        if bad:
            raise ReimbursementConfigError(
                f"{path}: `allowed_categories` contains {bad!r} — every entry "
                f"must be a non-empty category name (a dangling `-` in a "
                f"block list produces a null entry)."
            )

    ledger_issue = config.get("ledger_issue")
    # `bool` is an `int` subclass, so `ledger_issue: yes` would slip through.
    if ledger_issue is not None and (
        isinstance(ledger_issue, bool) or not isinstance(ledger_issue, int)
    ):
        raise ReimbursementConfigError(
            f"{path}: `ledger_issue` must be the pinned ledger issue's number "
            f"or null, not {ledger_issue!r}. Use `ledger_issue: null` and run "
            f"`sync_templates.py --init-reimbursement-ledger` to wire it."
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
        # Every entry point (retrofit CLI, onboarding, ledger init) comes
        # through here, so this is the one gate that sees every config edit.
        validate_reimbursements_config(reimbursements, reimbursements_path)
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
    """Reminder bullets for the claim form's allowed-category block.

    No `allowed_categories` key means no restriction (see
    `validate_reimbursements_config` — an empty list is a config error, so the
    only way to land here is by omitting the key). Say that plainly: the old
    "ask the admin" text told the contractor to chase a restriction that isn't
    being enforced."""
    categories = (reimbursements or {}).get("allowed_categories") or []
    if not categories:
        return ("        - _(this repo doesn't restrict categories — use a "
                "short, descriptive one)_")
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
                      dry_run: bool,
                      on_created: Optional[Callable[[int], None]] = None,
                      ) -> Optional[int]:
    """Create a pinned + locked issue with the `ledger` label. Returns the
    issue number, or None under dry-run. (Generalised from
    new-contractor.py's contract-ledger variant.)

    `on_created` is called with the issue number the moment the issue exists,
    *before* the pin/lock calls — that's the caller's chance to record the
    number durably (e.g. write it into a config) so a later failure can't
    orphan the issue and make a retry create a second one.

    Pinning and locking are deliberately best-effort: GitHub caps a repo at
    three pinned issues, and each contract ledger already consumes one, so the
    cap is reachable in normal use. Losing the pin is cosmetic — the issue
    itself is the record — so we warn loudly with the manual command rather
    than abort a flow that has already created (and recorded) the issue."""
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
    if on_created is not None:
        on_created(n)
    for verb in ("pin", "lock"):
        r = subprocess.run(["gh", "issue", verb, str(n), "--repo", repo],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"WARNING: couldn't {verb} issue #{n} in {repo} "
                  f"({r.stderr.strip() or 'gh failed'}). The issue exists and "
                  f"is recorded; run `gh issue {verb} {n} --repo {repo}` by "
                  f"hand (GitHub allows at most 3 pinned issues per repo).",
                  file=sys.stderr)
    return n


def find_ledger_issue(repo: str, title: str = REIMBURSEMENT_LEDGER_TITLE,
                      ) -> Optional[int]:
    """Number of an existing open `ledger`-labelled issue with `title`, if any.

    Recovers from a half-finished init: before the config write moved ahead of
    the pin/lock calls, a failure there left an orphan ledger issue that every
    retry duplicated. Returns None when nothing matches or when `gh` can't
    answer — the caller then creates one, which is the pre-existing behaviour."""
    r = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--label", "ledger",
         "--state", "open", "--limit", "100", "--json", "number,title"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"WARNING: couldn't list existing ledger issues in {repo} "
              f"({r.stderr.strip() or 'gh failed'}); proceeding as if none "
              f"exist.", file=sys.stderr)
        return None
    try:
        issues = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        # Returning None here means "no existing ledger issue", and the caller
        # acts on that by creating one — so swallowing this silently is how a
        # duplicate pinned ledger gets made, the exact thing this lookup is
        # here to prevent. Warn like the returncode branch above.
        print(f"WARNING: could not parse `gh issue list` output for {repo} "
              f"as JSON; treating as 'no ledger issue found'. If one already "
              f"exists, this will create a duplicate.", file=sys.stderr)
        return None
    numbers = sorted(i["number"] for i in issues if i.get("title") == title)
    if not numbers:
        return None
    if len(numbers) > 1:
        print(f"WARNING: {repo} has {len(numbers)} open issues titled "
              f"\"{title}\" (#{', #'.join(str(n) for n in numbers)}) — reusing "
              f"the oldest; close the duplicates by hand.", file=sys.stderr)
    return numbers[0]


def write_ledger_issue(path: Path, issue_number: int) -> None:
    """Set `ledger_issue:` in an existing reimbursements config, in place.

    A targeted line rewrite rather than a `yaml.safe_dump` round-trip: the
    seeded file's comments explain each key (and which tool writes it), and
    dumping the parsed dict back stripped every one of them."""
    text = path.read_text(encoding="utf-8")
    # `(?m)^ledger_issue:` — column 0 only, so a commented-out or nested
    # occurrence is left alone. `.*$` also takes any trailing comment on that
    # line, which is intended: a hand-added "# wired by X" note would be stale
    # once we've rewritten the value. (The shipped template carries no such
    # comment — its explanation sits in the header block.)
    new_text, count = re.subn(
        r"(?m)^ledger_issue:.*$", f"ledger_issue: {issue_number}", text, count=1,
    )
    if count == 0:
        # Hand-written config without the key — append rather than lose the wire.
        sep = "" if text.endswith("\n") or not text else "\n"
        new_text = f"{text}{sep}ledger_issue: {issue_number}\n"
    path.write_text(new_text, encoding="utf-8")


def init_reimbursement_ledger(repo_dir: Path, repo: str, *,
                              dry_run: bool) -> Optional[int]:
    """Create the pinned reimbursements ledger issue and write its number
    into config/reimbursements.yml. No-op (with a warning) when the config
    is absent or the issue is already wired.

    Safe to re-run. The config write happens the moment the issue exists
    (before pin/lock), and an already-open ledger issue is adopted rather than
    duplicated — a retry after a mid-flight failure converges instead of
    leaving another orphan behind."""
    reimbursements_path = repo_dir / "config" / "reimbursements.yml"
    if not reimbursements_path.exists():
        print("WARN: config/reimbursements.yml not found — nothing to init.",
              file=sys.stderr)
        return None
    config = yaml.safe_load(reimbursements_path.read_text(encoding="utf-8")) or {}
    # Catches the unsubstituted `$REIMBURSEMENT_LEDGER_ISSUE` literal, which is
    # truthy and used to report the ledger as already wired.
    validate_reimbursements_config(config, reimbursements_path)
    if config.get("ledger_issue"):
        print(f"Reimbursements ledger issue already wired "
              f"(#{config['ledger_issue']}); nothing to do.")
        return None

    if not dry_run:
        existing = find_ledger_issue(repo)
        if existing is not None:
            print(f"Reusing existing ledger issue #{existing} in {repo} "
                  f"(left unwired by an earlier run).")
            write_ledger_issue(reimbursements_path, existing)
            print(f"Wired ledger_issue: {existing} into "
                  f"config/reimbursements.yml")
            return existing

    body = render_reimbursement_body(empty_ledger(ledger_type="reimbursement"), config)
    issue_number = open_pinned_issue(
        repo, REIMBURSEMENT_LEDGER_TITLE, body, dry_run=dry_run,
        # Record before pin/lock: an orphan issue is worse than an unpinned one.
        on_created=lambda n: write_ledger_issue(reimbursements_path, n),
    )
    if issue_number is None:
        return None

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

    try:
        plan = plan_sync(repo_dir)
    except ReimbursementConfigError as exc:
        # Config shape errors are the admin's typo, not a bug — a traceback
        # would bury the fix instructions the message carries.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
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

    # Stage exactly the paths we changed. Staging their top-level directories
    # instead (`git add --all .github config`) swept up anything else the admin
    # had in flight in the contractor repo — a half-edited CODEOWNERS, which
    # this module deliberately does not sync, or local scratch under
    # .github/ — into an auto-pushed commit. `--all --` still records
    # deletions for the listed paths.
    subprocess.run(["git", "add", "--all", "--", *sorted(changed)],
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
