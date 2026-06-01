#!/usr/bin/env python3
"""Onboard a new contractor: create QuantEcon/contractor-{handle} from
the contractor-template/ skeleton and seed it with the contractor's first
contract.

Usage:
  python onboarding/new-contractor.py           # interactive
  python onboarding/new-contractor.py --handle alice --name "Alice Q" ...
  python onboarding/new-contractor.py --dry-run --handle alice ...

Flags-with-prompt-fallback: any missing flag triggers an interactive
prompt. --dry-run prints the plan without side effects. --yes skips
the final confirmation.

See PLAN.md §5 for the full spec.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from string import Template
from typing import Optional

import yaml

# Make `scripts.*` importable when running from the engine repo root.
ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts.setup_labels import LABELS as WORKFLOW_LABELS, create_label  # noqa: E402
from scripts.update_ledger import empty_ledger  # noqa: E402
from scripts.update_ledger_issue import render_body  # noqa: E402


# ─── Constants ──────────────────────────────────────────────────────────────

ORG = "QuantEcon"
DEFAULT_ADMIN = "mmcky"
TEMPLATE_DIR = ENGINE_ROOT / "contractor-template"
CLONES_DIR = ENGINE_ROOT / "contractors"

SUPPORTED_CURRENCIES = {"AUD", "USD", "JPY"}
CONTRACT_TYPES = {"hourly", "milestone"}

# Default contract ID pattern (see PLAN §4.2). System accepts any string;
# this is a prompt suggestion, not a validation rule.
DEFAULT_PAYER = "PSL"


# ─── Resolved inputs ────────────────────────────────────────────────────────

@dataclass
class Inputs:
    handle: str
    name: str
    email: str
    address: str  # may be empty
    admin: str
    project: str
    contract_id: str
    contract_type: str
    start_date: str
    end_date: str
    currency: str
    hourly_rate: Optional[float] = None
    max_hours_per_month: Optional[float] = None
    testing_mode: bool = True
    milestones: list[dict] = field(default_factory=list)


# ─── Subprocess helper ──────────────────────────────────────────────────────

def run(cmd: list[str], *, dry_run: bool,
        cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Run a write-side shell command, or print + skip under --dry-run.

    Read-side commands (the pre-flight checks for gh auth and existing-repo
    detection) call subprocess.run directly so they fire even under
    dry-run — we want the dry-run to faithfully preview whether a real
    run would succeed.
    """
    if dry_run:
        print(f"  [dry-run] {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=True, text=True)


# ─── Argparse ───────────────────────────────────────────────────────────────

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config",
                   help="Path to a YAML file containing the contractor + "
                        "contract details. CLI flags override values from "
                        "the file. See onboarding/contractors/example.yml.")
    p.add_argument("--handle", help="GitHub handle of the new contractor.")
    p.add_argument("--name", help="Contractor's real name (free text).")
    p.add_argument("--email", help="Contractor email.")
    p.add_argument("--address-file",
                   help="Path to a text file containing the contractor's "
                        "postal address (optional, multi-line allowed).")
    p.add_argument("--admin", default=DEFAULT_ADMIN,
                   help=f"Admin handle for CODEOWNERS (default: {DEFAULT_ADMIN}).")
    p.add_argument("--project", help="Free-form project name (e.g. python-lectures).")
    p.add_argument("--contract-id",
                   help="Contract ID, e.g. QE-PSL-2026-001. Suggested format "
                        "QE-{PAYER}-YYYY-NNN; system accepts any string.")
    p.add_argument("--contract-type", choices=sorted(CONTRACT_TYPES),
                   help="hourly or milestone.")
    p.add_argument("--start-date", help="Contract start date (YYYY-MM-DD).")
    p.add_argument("--end-date", help="Contract end date (YYYY-MM-DD).")
    p.add_argument("--currency", choices=sorted(SUPPORTED_CURRENCIES),
                   help=f"ISO 4217 currency ({', '.join(sorted(SUPPORTED_CURRENCIES))}).")
    p.add_argument("--hourly-rate", type=float,
                   help="Hourly rate (hourly contracts only).")
    p.add_argument("--max-hours-per-month", type=float,
                   help="Cap on hours per month (hourly contracts only). "
                        "Submissions exceeding this are flagged in the PR "
                        "as information for the approver — not blocked. "
                        "Pair with --no-max-hours-per-month to declare the "
                        "contract uncapped instead.")
    p.add_argument("--no-max-hours-per-month", action="store_true",
                   help="Declare an hourly contract as uncapped (no monthly "
                        "ceiling). Required on hourly contracts unless "
                        "--max-hours-per-month is supplied.")
    p.add_argument("--milestone-notes-file",
                   help="Path to a YAML file containing the structured "
                        "`milestones:` list (milestone contracts only). "
                        "If omitted, $EDITOR opens a template.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--production", action="store_true",
                      help="Set notifications.testing_mode=false for this repo: "
                           "approved submissions email PSL Foundation (production). "
                           "Default is testing mode (emails stay with the reviewer).")
    mode.add_argument("--testing", action="store_true",
                      help="Force notifications.testing_mode=true (the default): "
                           "approval emails stay internal to the QuantEcon reviewer.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan; skip all side effects.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip the final confirmation prompt.")
    return p.parse_args(argv)


# ─── Interactive prompts (only fire if a flag is missing) ───────────────────

def _prompt(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        print("  (required — please answer)")


def _prompt_choice(label: str, choices: set[str]) -> str:
    options = "/".join(sorted(choices))
    while True:
        raw = input(f"{label} ({options}): ").strip()
        if raw in choices:
            return raw
        print(f"  (must be one of: {options})")


def _prompt_max_hours_per_month() -> Optional[float]:
    """Prompt for the monthly cap. Returns a positive float, or None for
    an explicit "uncapped" declaration (typed as `none` / `null`). Loops
    on invalid input — empty is not accepted, the admin must make a
    deliberate choice."""
    while True:
        raw = input("Max hours/month (number, or `none` for uncapped): ").strip()
        if not raw:
            print("  (required — enter a number or `none`)")
            continue
        if raw.lower() in ("none", "null"):
            return None
        try:
            value = float(raw)
        except ValueError:
            print("  (must be a number or `none`)")
            continue
        if value <= 0:
            print("  (must be > 0)")
            continue
        return value


def _read_address_file(path: str) -> str:
    """Read an optional multi-line address from a file. Empty file → ''."""
    return Path(path).read_text(encoding="utf-8").rstrip("\n")


def _open_editor_for_milestones() -> list[dict]:
    """Open $EDITOR with a pre-filled milestone template, parse the result.

    Returns the parsed milestones[] list. Loops if YAML is invalid so the
    admin can fix typos without restarting the script.
    """
    template = (
        "# Edit the milestones below and save. Empty list is allowed but\n"
        "# the parser warning won't catch typos until you fill it in.\n"
        "milestones:\n"
        "  - id: 1\n"
        "    date: 2026-01-15\n"
        "    amount: 5000\n"
        "    description: Kick-off deliverable\n"
        "  # - id: 2\n"
        "  #   date: 2026-02-15\n"
        "  #   amount: 5000\n"
        "  #   description: ...\n"
    )
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
        f.write(template)
        tmp_path = Path(f.name)
    try:
        while True:
            subprocess.run([editor, str(tmp_path)], check=True)
            text = tmp_path.read_text(encoding="utf-8")
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError as e:
                print(f"  YAML error: {e}")
                input("  Press Enter to re-open the editor...")
                continue
            milestones = data.get("milestones") or []
            if not isinstance(milestones, list):
                print("  `milestones:` must be a list.")
                input("  Press Enter to re-open the editor...")
                continue
            return milestones
    finally:
        tmp_path.unlink(missing_ok=True)


def load_config(path: str) -> dict:
    """Load a contractor-input YAML and flatten it to the same keyspace as
    the CLI flags. Missing keys are simply absent — the caller fills the
    gaps from flags or prompts. Dates are coerced to strings because
    yaml.safe_load returns datetime.date objects for `YYYY-MM-DD`.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    contract = raw.get("contract") or {}

    def _str(v):
        # yaml.safe_load parses YYYY-MM-DD as datetime.date; the rest of
        # the script expects strings, so normalise here.
        if isinstance(v, date):
            return v.isoformat()
        return v

    flat = {
        "handle": raw.get("handle"),
        "name": raw.get("name"),
        "email": raw.get("email"),
        "address": raw.get("address"),  # actual text, not a file path
        "admin": raw.get("admin"),
        "project": raw.get("project"),
        "contract_id": contract.get("id"),
        "contract_type": contract.get("type"),
        "start_date": _str(contract.get("start_date")),
        "end_date": _str(contract.get("end_date")),
        "currency": contract.get("currency"),
        "hourly_rate": contract.get("hourly_rate"),
        "max_hours_per_month": contract.get("max_hours_per_month"),
        "milestones": contract.get("milestones"),
        "testing_mode": raw.get("testing_mode"),
    }
    cleaned = {k: v for k, v in flat.items() if v is not None}
    # Preserve explicit `max_hours_per_month: null` (uncapped) — distinct
    # from "key absent" so resolve_inputs can require a deliberate choice.
    if "max_hours_per_month" in contract:
        cleaned["max_hours_per_month"] = contract.get("max_hours_per_month")
    return cleaned


def resolve_inputs(args: argparse.Namespace) -> Inputs:
    """Materialise an Inputs from --config YAML + CLI flags + prompts.

    Precedence: CLI flag > config file > interactive prompt. Prompts only
    fire when stdin is a TTY and neither source supplied the value —
    keeps automation runs (CI, scripted dry-runs) non-blocking.
    """
    interactive = sys.stdin.isatty()
    config = load_config(args.config) if args.config else {}

    def pick(arg_val, key):
        """CLI flag wins if set; otherwise config; otherwise None."""
        return arg_val if arg_val is not None else config.get(key)

    handle = pick(args.handle, "handle") or _prompt("GitHub handle")
    name = pick(args.name, "name") or _prompt("Real name")
    email = pick(args.email, "email") or _prompt("Email")

    # Address: CLI passes a file path; config carries the actual text.
    if args.address_file:
        address = _read_address_file(args.address_file)
    elif "address" in config:
        address = (config["address"] or "").rstrip("\n")
    elif interactive:
        ask = input("Postal address file path (optional, blank to skip): ").strip()
        address = _read_address_file(ask) if ask else ""
    else:
        address = ""

    admin = pick(args.admin, "admin") or DEFAULT_ADMIN
    project = pick(args.project, "project") or _prompt(
        "Project name", default=f"{handle}-work",
    )

    default_contract = f"QE-{DEFAULT_PAYER}-{date.today().year}-001"
    contract_id = pick(args.contract_id, "contract_id") or _prompt(
        "Contract ID", default=default_contract,
    )
    contract_type = pick(args.contract_type, "contract_type") or _prompt_choice(
        "Contract type", CONTRACT_TYPES,
    )

    start_date = pick(args.start_date, "start_date") or _prompt(
        "Start date (YYYY-MM-DD)", default=str(date.today().replace(day=1)),
    )
    end_date = pick(args.end_date, "end_date") or _prompt(
        "End date (YYYY-MM-DD)",
        default=str(date(date.today().year, 12, 31)),
    )
    currency = pick(args.currency, "currency") or _prompt_choice(
        "Currency", SUPPORTED_CURRENCIES,
    )

    hourly_rate = None
    max_hours = None
    milestones: list[dict] = []

    if contract_type == "hourly":
        hourly_rate = pick(args.hourly_rate, "hourly_rate")
        if hourly_rate is None:
            hourly_rate = float(_prompt("Hourly rate"))
        else:
            hourly_rate = float(hourly_rate)
        # Cap is required on hourly contracts: a number caps monthly hours
        # (over-cap submissions are flagged in the PR for the approver to
        # consider); an explicit `null` / --no-max-hours-per-month declares
        # the contract uncapped. Three input paths:
        #   - --no-max-hours-per-month flag → uncapped
        #   - --max-hours-per-month <num> → numeric cap (CLI wins over config)
        #   - config has the key (numeric or null) → use config value
        #   - interactive prompt requiring a number or `none`
        if args.no_max_hours_per_month:
            max_hours = None
        elif args.max_hours_per_month is not None:
            max_hours = float(args.max_hours_per_month)
        elif "max_hours_per_month" in config:
            raw_cap = config["max_hours_per_month"]
            max_hours = float(raw_cap) if raw_cap is not None else None
        elif interactive:
            max_hours = _prompt_max_hours_per_month()
        else:
            raise SystemExit(
                "ERROR: hourly contracts require --max-hours-per-month <N> or "
                "--no-max-hours-per-month (or `max_hours_per_month: <N>|null` "
                "in the config file)."
            )
    else:
        if args.milestone_notes_file:
            data = yaml.safe_load(
                Path(args.milestone_notes_file).read_text(encoding="utf-8"),
            ) or {}
            milestones = data.get("milestones") or []
        elif "milestones" in config:
            milestones = config["milestones"] or []
        else:
            milestones = _open_editor_for_milestones()

    # Email mode: production (testing_mode=false) is an explicit opt-in, so both
    # the default and the silence-fallback are "testing" (never email PSL).
    if args.production:
        testing_mode = False
    elif args.testing:
        testing_mode = True
    elif "testing_mode" in config:
        testing_mode = bool(config["testing_mode"])
    else:
        testing_mode = True

    return Inputs(
        handle=handle, name=name, email=email, address=address, admin=admin,
        project=project, contract_id=contract_id, contract_type=contract_type,
        start_date=start_date, end_date=end_date, currency=currency,
        hourly_rate=hourly_rate, max_hours_per_month=max_hours,
        testing_mode=testing_mode, milestones=milestones,
    )


# ─── Input validation ───────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")


def validate(inputs: Inputs) -> list[str]:
    """Return a list of human-readable validation errors (empty = ok)."""
    errors: list[str] = []
    if not _HANDLE_RE.match(inputs.handle):
        errors.append(f"GitHub handle `{inputs.handle}` looks malformed.")
    if not _DATE_RE.match(inputs.start_date):
        errors.append(f"start_date `{inputs.start_date}` must be YYYY-MM-DD.")
    if not _DATE_RE.match(inputs.end_date):
        errors.append(f"end_date `{inputs.end_date}` must be YYYY-MM-DD.")
    if inputs.currency not in SUPPORTED_CURRENCIES:
        errors.append(f"currency `{inputs.currency}` not in {sorted(SUPPORTED_CURRENCIES)}.")
    if inputs.contract_type not in CONTRACT_TYPES:
        errors.append(f"contract_type `{inputs.contract_type}` invalid.")
    if inputs.contract_type == "hourly" and inputs.hourly_rate is None:
        errors.append("hourly contracts require --hourly-rate.")
    if (inputs.contract_type == "hourly"
            and inputs.max_hours_per_month is not None
            and inputs.max_hours_per_month <= 0):
        errors.append(
            f"max_hours_per_month must be > 0 "
            f"(got `{inputs.max_hours_per_month}`; use null/--no-max-hours-per-month for uncapped)."
        )
    if inputs.contract_type == "milestone":
        for i, m in enumerate(inputs.milestones):
            if "id" not in m or "date" not in m or "amount" not in m:
                errors.append(
                    f"milestones[{i}]: each entry needs id, date, amount "
                    f"(got keys: {sorted(m.keys())})"
                )
    return errors


# ─── Pre-flight checks ──────────────────────────────────────────────────────

def repo_full_name(handle: str) -> str:
    return f"{ORG}/contractor-{handle}"


def verify_gh_auth() -> None:
    """Confirm `gh` is installed and authenticated. Exit 1 if not."""
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        print("ERROR: `gh auth status` failed. Run `gh auth login` first.",
              file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(1)


def verify_repo_doesnt_exist(handle: str) -> None:
    """Hard-stop if the contractor repo already exists. Idempotency rule:
    we don't overwrite — admin deletes manually and re-runs."""
    full = repo_full_name(handle)
    r = subprocess.run(
        ["gh", "repo", "view", full, "--json", "name"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"ERROR: repo `{full}` already exists. Delete it manually "
              f"(`gh repo delete {full}`) and re-run.", file=sys.stderr)
        sys.exit(1)


_NO_PROTECTION_NOTICE = """\
  ⚠ Branch protection on `main` is currently DEFERRED.

  The contractor's repo will be created with no protection on `main`,
  which means anyone with Write access (the contractor + the admin)
  can in principle push directly without a PR. The intended approval
  flow (issue → PR → admin review → merge) is operationally enforced,
  not technically enforced.

  Background: the original Phase 3b design relied on an org-level
  ruleset granting bypass to the `github-actions` integration.
  GitHub's modern Rulesets UI no longer exposes that integration as
  a selectable bypass actor, and classic protection's
  `enforce_admins: false` doesn't admit the bot push either
  (PLAN.md §8 documents both findings). The durable fix is a
  dedicated GitHub App for the engine push — see
  `notes/ADMIN_RUNBOOK.md` → "Branch protection (deferred)" for the
  migration recipe. Migration is data-preserving; no need to
  recreate this contractor's repo when we land it.

  Proceeding without protection is acceptable for a private repo with
  a single trusted contractor + admin; revisit once the GitHub App
  path is implemented."""


def notify_protection_deferred(*, dry_run: bool) -> None:
    """Surface the deferred-protection decision at onboarding time.

    Replaces the old `verify_org_ruleset` pre-flight (which urged the
    admin to set up a ruleset we now know doesn't work — see PLAN.md
    §8 "Branch protection on `main` — DEFERRED"). Under --dry-run the
    notice prints but the script doesn't pause; under a real run the
    admin presses Enter to acknowledge before any repo is created."""
    print(_NO_PROTECTION_NOTICE)
    if not dry_run:
        input("\n  Press Enter to acknowledge and continue, "
              "or Ctrl-C to abort...")


# ─── Plan rendering ─────────────────────────────────────────────────────────

def render_plan(inputs: Inputs) -> str:
    lines = [
        "",
        "═" * 60,
        f"Plan for: {repo_full_name(inputs.handle)}",
        "═" * 60,
        f"  Contractor    : {inputs.name} (@{inputs.handle})",
        f"  Email         : {inputs.email}",
        f"  Address       : {inputs.address or '(none)'}",
        f"  Admin         : @{inputs.admin}",
        f"  Project       : {inputs.project}",
        f"  Contract      : {inputs.contract_id} ({inputs.contract_type}, "
        f"{inputs.currency})",
        f"  Period        : {inputs.start_date} → {inputs.end_date}",
    ]
    if inputs.contract_type == "hourly":
        lines.append(f"  Hourly rate   : {inputs.hourly_rate} {inputs.currency}")
        cap = inputs.max_hours_per_month
        lines.append(f"  Max hrs/month : {cap if cap is not None else 'uncapped'}")
    else:
        lines.append(f"  Milestones    : {len(inputs.milestones)} entries")
        for m in inputs.milestones:
            lines.append(
                f"    #{m['id']:>2} {m['date']}  "
                f"{m['amount']} {inputs.currency}  {m.get('description', '')}"
            )
    mode_str = ("TESTING — emails go to the QuantEcon reviewer only (PSL NOT contacted)"
                if inputs.testing_mode
                else "PRODUCTION — approved submissions email PSL Foundation")
    lines.append(f"  Email mode    : {mode_str}")
    lines += [
        "",
        "Steps:",
        f"  1. Create private repo {repo_full_name(inputs.handle)}",
        f"  2. Clone into {CLONES_DIR}/contractor-{inputs.handle}",
        f"  3. Seed from {TEMPLATE_DIR.relative_to(ENGINE_ROOT)}/",
        f"  4. Generate contracts/{inputs.contract_id}.yml",
        f"  5. Initial commit + push",
        f"  6. Create {len(WORKFLOW_LABELS)} workflow labels",
        f"  7. Set delete_branch_on_merge=true",
        f"  8. Add collaborators (contractor=Write, admin=Admin)",
        f"  9. Open pinned ledger issue + write its number back to the contract",
        "═" * 60,
        "",
    ]
    return "\n".join(lines)


# ─── Templating + file generation ───────────────────────────────────────────

def _format_address_yaml(address: str) -> str:
    """Render the contractor address as a YAML scalar for substitution into
    settings.yml. Empty → `null`; otherwise a block scalar so multi-line
    addresses parse cleanly. Indent matches the two-space settings.yml shape.
    """
    if not address.strip():
        return "null"
    indent = "    "
    lines = address.splitlines() or [""]
    body = "\n".join(f"{indent}{line}" for line in lines)
    return "|\n" + body


def _contract_options_yaml(contract_ids: list[str]) -> str:
    """Render contract IDs as the indented options block that substitutes
    into the Issue Form `$CONTRACT_OPTIONS` placeholder."""
    if not contract_ids:
        return '        - "(no contracts yet)"'
    return "\n".join(f'        - "{cid}"' for cid in contract_ids)


def _hourly_contract_reminder(inputs: "Inputs") -> str:
    """Render the active-hourly-contracts reminder block (substitutes into
    `$HOURLY_CONTRACT_REMINDER`). Each line is indented to match the
    enclosing `value: |` markdown block.

    Onboarding seeds the first contract; admins extend this list by hand
    when contracts are renewed (same pattern as the Contract dropdown)."""
    if inputs.contract_type != "hourly":
        return "        - _(no hourly contracts yet)_"
    return f"        - `{inputs.contract_id}` — {inputs.currency}, {inputs.hourly_rate}/hour"


def _milestone_contract_reminder(inputs: "Inputs") -> str:
    """Render the active-milestone-contracts reminder block (substitutes
    into `$MILESTONE_CONTRACT_REMINDER`). Each line indented to match the
    enclosing `value: |` markdown block."""
    if inputs.contract_type != "milestone":
        return "        - _(no milestone contracts yet)_"
    return f"        - `{inputs.contract_id}` — {inputs.currency} (milestone)"


def seed_repo(clone_dir: Path, inputs: Inputs, *, dry_run: bool) -> None:
    """Copy contractor-template/ into clone_dir, then substitute placeholders.

    Idempotent within a single run: removes the target if it already exists
    (caller guarantees this is a fresh dir under contractors/, which is
    gitignored and untracked)."""
    if dry_run:
        print(f"  [dry-run] copy {TEMPLATE_DIR} → {clone_dir} (preserving git history)")
    else:
        # The git clone already populated clone_dir; we copy template files
        # *on top* of the empty clone (which has only .git). shutil.copytree
        # would refuse to merge, so walk the template dir manually.
        for src in TEMPLATE_DIR.rglob("*"):
            rel = src.relative_to(TEMPLATE_DIR)
            dst = clone_dir / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    # Substitute placeholders in the templated files.
    hourly_ids = [inputs.contract_id] if inputs.contract_type == "hourly" else []
    milestone_ids = [inputs.contract_id] if inputs.contract_type == "milestone" else []
    substitutions = {
        "ADMIN": inputs.admin,
        "CONTRACTOR_NAME": inputs.name,
        "CONTRACTOR_HANDLE": inputs.handle,
        "CONTRACTOR_EMAIL": inputs.email,
        "CONTRACTOR_ADDRESS_BLOCK": _format_address_yaml(inputs.address),
        "CONTRACT_OPTIONS": _contract_options_yaml(hourly_ids),
        "MILESTONE_CONTRACT_OPTIONS": _contract_options_yaml(milestone_ids),
        "HOURLY_CONTRACT_REMINDER": _hourly_contract_reminder(inputs),
        "MILESTONE_CONTRACT_REMINDER": _milestone_contract_reminder(inputs),
        "TESTING_MODE": "true" if inputs.testing_mode else "false",
    }
    templated_files = [
        ".github/CODEOWNERS",
        ".github/ISSUE_TEMPLATE/hourly-timesheet.yml",
        ".github/ISSUE_TEMPLATE/milestone-invoice.yml",
        "config/settings.yml",
        "README.md",
    ]
    for rel in templated_files:
        path = clone_dir / rel
        if dry_run:
            print(f"  [dry-run] substitute placeholders in {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        path.write_text(Template(text).safe_substitute(substitutions),
                        encoding="utf-8")


def build_contract_yaml(inputs: Inputs) -> dict:
    """Build the contract dict that will be dumped to contracts/{id}.yml."""
    if inputs.contract_type == "hourly":
        contract = {
            "contract_id": inputs.contract_id,
            "type": "hourly",
            "status": "active",
            "start_date": inputs.start_date,
            "end_date": inputs.end_date,
            "terms": {
                "hourly_rate": inputs.hourly_rate,
                "currency": inputs.currency,
                # Always written: numeric = cap, null = explicitly uncapped.
                # enrich_submission rejects hourly contracts missing this key.
                "max_hours_per_month": inputs.max_hours_per_month,
            },
            "project": inputs.project,
        }
        return contract
    return {
        "contract_id": inputs.contract_id,
        "type": "milestone",
        "status": "active",
        "start_date": inputs.start_date,
        "end_date": inputs.end_date,
        "currency": inputs.currency,
        "project": inputs.project,
        "milestones": inputs.milestones,
    }


def write_contract(clone_dir: Path, contract: dict, *, dry_run: bool) -> Path:
    out_path = clone_dir / "contracts" / f"{contract['contract_id']}.yml"
    if dry_run:
        print(f"  [dry-run] write contract → contracts/{contract['contract_id']}.yml")
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(contract, f, sort_keys=False, allow_unicode=True)
    return out_path


# ─── Step execution ─────────────────────────────────────────────────────────

def execute(inputs: Inputs, *, dry_run: bool) -> Optional[str]:
    """Execute the plan. Returns the new repo's URL on success, or None
    on dry-run."""
    full = repo_full_name(inputs.handle)
    clone_dir = CLONES_DIR / f"contractor-{inputs.handle}"

    # 1. Create private repo.
    print("→ Creating private repo")
    run(["gh", "repo", "create", full, "--private",
         "--description", f"Payment artefacts for {inputs.name}"],
        dry_run=dry_run)

    # 2. Clone into contractors/.
    print("→ Cloning")
    if not dry_run:
        CLONES_DIR.mkdir(parents=True, exist_ok=True)
        if clone_dir.exists():
            # Defensive: contractors/ is gitignored, but if a previous
            # aborted run left a stale checkout for this handle, refuse
            # rather than overwrite.
            print(f"ERROR: stale clone at {clone_dir}. Remove it and re-run.",
                  file=sys.stderr)
            sys.exit(1)
    run(["gh", "repo", "clone", full, str(clone_dir)], dry_run=dry_run)

    # 3. Seed from template + substitute placeholders.
    print("→ Seeding from contractor-template/")
    seed_repo(clone_dir, inputs, dry_run=dry_run)

    # 4. Generate contracts/{id}.yml.
    print("→ Writing contract")
    contract = build_contract_yaml(inputs)
    write_contract(clone_dir, contract, dry_run=dry_run)

    # 5. Initial commit + push. Force the branch name to `main` so the
    # script doesn't depend on the local git `init.defaultBranch` config —
    # contractor repos are uniformly `main`-default.
    print("→ Initial commit + push")
    run(["git", "add", "."], dry_run=dry_run, cwd=clone_dir)
    run(["git", "commit", "-m",
         f"Initial onboarding: {inputs.name} (@{inputs.handle})"],
        dry_run=dry_run, cwd=clone_dir)
    run(["git", "branch", "-M", "main"], dry_run=dry_run, cwd=clone_dir)
    run(["git", "push", "-u", "origin", "main"],
        dry_run=dry_run, cwd=clone_dir)

    # 6. Workflow labels (idempotent).
    print("→ Creating workflow labels")
    if not dry_run:
        for name, description, color in WORKFLOW_LABELS:
            status = create_label(name, description, color, full)
            print(f"  {status}")
    else:
        for name, _, _ in WORKFLOW_LABELS:
            print(f"  [dry-run] gh label create {name} --repo {full}")

    # 7. Set delete_branch_on_merge so merged submission branches don't pile up.
    print("→ Setting delete_branch_on_merge=true")
    run(["gh", "api", "--method", "PATCH", f"repos/{full}",
         "-f", "delete_branch_on_merge=true"], dry_run=dry_run)

    # 8. Collaborators.
    print("→ Adding collaborators")
    run(["gh", "api", "--method", "PUT",
         f"repos/{full}/collaborators/{inputs.handle}",
         "-f", "permission=push"], dry_run=dry_run)
    if inputs.admin != inputs.handle:
        run(["gh", "api", "--method", "PUT",
             f"repos/{full}/collaborators/{inputs.admin}",
             "-f", "permission=admin"], dry_run=dry_run)

    # 9. Open the pinned ledger issue and write its number back to the contract.
    print("→ Opening pinned ledger issue")
    ledger_type = "hourly" if inputs.contract_type == "hourly" else "milestone"
    ledger = empty_ledger(
        ledger_type=ledger_type,
        contract_id=inputs.contract_id,
        currency=inputs.currency,
    )
    body = render_body(ledger, contract)
    issue_number = open_ledger_issue(full, contract, body, dry_run=dry_run)

    if issue_number is not None:
        print(f"→ Writing ledger_issue: {issue_number} back to contract")
        contract["ledger_issue"] = issue_number
        write_contract(clone_dir, contract, dry_run=dry_run)
        run(["git", "add", f"contracts/{inputs.contract_id}.yml"],
            dry_run=dry_run, cwd=clone_dir)
        run(["git", "commit", "-m",
             f"Wire up ledger issue #{issue_number} for {inputs.contract_id}"],
            dry_run=dry_run, cwd=clone_dir)
        run(["git", "push"], dry_run=dry_run, cwd=clone_dir)

    if dry_run:
        return None
    return f"https://github.com/{full}"


def open_ledger_issue(repo: str, contract: dict, body: str, *,
                      dry_run: bool) -> Optional[int]:
    """Create the pinned + locked ledger issue. Returns the issue number,
    or None under dry-run."""
    title = f"📒 Running ledger — {contract['contract_id']}"
    if dry_run:
        print(f"  [dry-run] gh issue create --title \"{title}\" --label ledger --body <body>")
        print(f"  [dry-run] gh issue pin <N>")
        print(f"  [dry-run] gh issue lock <N>")
        return None

    # Ensure the `ledger` label exists before applying it (it's not part of
    # WORKFLOW_LABELS, which covers submission-flow labels only).
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

    # `gh issue create` prints the issue URL on stdout; pull the number.
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


# ─── Main ───────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    verify_gh_auth()

    inputs = resolve_inputs(args)
    errors = validate(inputs)
    if errors:
        print("Validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    verify_repo_doesnt_exist(inputs.handle)
    notify_protection_deferred(dry_run=args.dry_run)

    print(render_plan(inputs))

    if not args.yes and not args.dry_run:
        confirm = input("Proceed? [y/N]: ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("Aborted.")
            return 1

    url = execute(inputs, dry_run=args.dry_run)
    if args.dry_run:
        print("\n[dry-run complete — no side effects performed]")
    else:
        print(f"\n✅ Done. Contractor repo: {url}")
        print(f"   Local clone: {CLONES_DIR}/contractor-{inputs.handle}")
        print( "   Next: share the repo URL with the contractor and confirm")
        print( "   they can open Issues → New Issue and see the templates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
