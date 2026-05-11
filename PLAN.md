# QuantEcon Timesheets — Implementation Plan

**Status:** Working draft. Iterating before any implementation work begins.
**Source issue:** [QuantEcon/admin#3 — PRJ: QuantEcon Timesheet Management System](https://github.com/QuantEcon/admin/issues/3)

This document supersedes the architecture in the source issue where it diverges, and records the rationale for those divergences.

---

## 1. Goals

A GitHub-native system that lets QuantEcon research assistants submit timesheets (and later invoices and reimbursements), have them reviewed and approved via PR, and produce a clean PDF + notification for the PSL Foundation payments manager on approval.

A second goal — newly added: a small **personnel/contracts data store** (`QuantEcon/contractors`) that becomes the single source of truth for who QuantEcon's RAs are, what contracts they hold, and the history thereof. This data drives the timesheets system and can feed other QuantEcon admin processes over time (reporting, mailing-list/team membership, payment exports).

Constraints that shape the design:

- Compensation data is sensitive — RAs must not see each others' rates, hours, or totals.
- QuantEcon does not host webapps; static GitHub Pages + GitHub Actions is the only ops surface available.
- RAs are GitHub-familiar; minimal git operations are acceptable.
- Scale: 5–10 active RAs, monthly cadence.
- No third-party GitHub Actions on any financial-data path — supply-chain risk is real here.
- **Keep it simple, but useful enough to automate the real pain points.** Don't build abstractions that don't earn their keep.

---

## 2. Architectural decisions

### 2.1 Per-RA private repos (not a single shared repo)

The source issue assumes a single shared `timesheets` repo with all RAs as collaborators. We are not doing that: any collaborator in such a repo would see every other RA's contract, hours, hourly rate, ledger totals, and PR review history. That is a confidentiality leak for compensation data and is unacceptable.

Selected: per-RA private repos under the QuantEcon org, with shared logic in reusable workflows. Privacy by construction; preserves every GitHub-native benefit (free auth, free audit trail, native PR review UX, no hosting); at 5–10 RAs the onboarding overhead is a single CLI command.

### 2.2 Centralised contractor + contract data store

Contracts are admin-authored, admin-managed data that happens to be relevant to RA repos. Keeping the source of truth scattered across N RA repos is a mismatch that grows with every RA. Instead:

- **`QuantEcon/contractors`** is the source of truth for contractor identity, active contracts, and historical contracts. Admin-only access.
- The relevant active contract YAML (+ generated PDF) is **deployed** into each RA's `timesheets-{handle}` repo by the admin CLI, so the RA's workflow has the data locally without needing cross-repo access.
- A change to a contract in `QuantEcon/contractors` is propagated to the RA repo by re-running `qemanager sync`.

This also gives us a clean foundation for downstream uses of the personnel data (annual reporting, team-membership management, payment-platform exports), without scope-creeping the timesheets project itself.

### 2.3 An admin CLI: `qemanager`

A Python CLI in `QuantEcon/timesheets` is the admin's primary interface to both repos. Subcommands cover the lifecycle: onboard a contractor, create / renew / end a contract, generate the contract PDF, deploy contracts to RA repos, push template updates, eventually report across RAs.

The CLI is admin-only. RAs never run it; they only interact with their own `timesheets-{handle}` repo via the GitHub web UI.

---

## 3. Repository topology

Three repos working together:

```
QuantEcon/timesheets                  ← engine: tooling, templates, CLI, reusable workflows
QuantEcon/contractors                 ← data: personnel + contracts (admin-only)
QuantEcon/timesheets-{ra-handle}      ← one per RA: submissions + derived contracts + ledger
```

### 3.1 `QuantEcon/timesheets` (this repo) — the engine

```
QuantEcon/timesheets/
├── .github/workflows/                ← reusable workflows for RA repos
│   ├── issue-to-pr.yml               (workflow_call; thin caller in each RA repo)
│   └── process-approved.yml          (workflow_call; thin caller in each RA repo)
├── scripts/                          ← run in CI; checked out at workflow runtime
│   ├── parse_issue.py
│   ├── create_submission_pr.py
│   ├── update_ledger.py
│   ├── generate_pdf.py               (timesheet PDF; also used by qemanager for contract PDFs)
│   └── notify.py
├── qemanager/                        ← Python admin CLI package
│   ├── __init__.py
│   ├── __main__.py                   (entry point: `python -m qemanager` or `qemanager`)
│   └── commands/
│       ├── contractor.py             (add, list, view, archive)
│       ├── contract.py               (create, renew, end, list, regenerate-pdf)
│       ├── onboard.py                (create timesheets-{handle}, seed template, add collaborators)
│       └── sync.py                   (push active contracts + template updates to an RA repo)
├── templates/
│   ├── timesheet.typ                 (PDF: monthly timesheets)
│   └── contract.typ                  (PDF: contracts)
├── ra-template/                      ← files seeded into each new RA repo on onboarding
│   ├── .github/ISSUE_TEMPLATE/hourly-timesheet.yml   (contract dropdown auto-populated by qemanager sync)
│   ├── .github/workflows/issue-to-pr.yml              (thin caller of the reusable)
│   ├── .github/workflows/process-approved.yml         (thin caller of the reusable)
│   ├── .github/CODEOWNERS
│   ├── config/settings.yml                            ($PAYMENTS_MANAGER, $ADMIN substituted in)
│   ├── README.md                                       (RA-facing how-to)
│   └── .gitkeep stubs for submissions/, ledger/, contracts/, generated_pdfs/
├── docs/
│   ├── RA_GUIDE.md                   (for submitting RAs)
│   ├── ADMIN_GUIDE.md                (runbook; complements `qemanager --help`)
│   └── CONTRACTOR_DATA_MODEL.md      (schema reference)
├── pyproject.toml                    (declares the qemanager entry point)
└── PLAN.md                           (this file)
```

### 3.2 `QuantEcon/contractors` — personnel + contract data

```
QuantEcon/contractors/                ← private, admin-only
├── contractors/
│   ├── jane-doe.yml                  (identity record only — see §4.1)
│   └── john-smith.yml
├── contracts/
│   ├── jane-doe-hourly-2024.yml      (one file per contract; immutable after first submission)
│   ├── jane-doe-hourly-2025.yml
│   └── john-smith-milestone-2025.yml
├── contract_pdfs/                    (generated by qemanager; committed alongside YAML)
│   ├── jane-doe-hourly-2024.pdf
│   ├── jane-doe-hourly-2025.pdf
│   └── john-smith-milestone-2025.pdf
└── README.md                         (points to docs in QuantEcon/timesheets)
```

No GitHub Actions in this repo for v1 — `qemanager` runs Typst locally on the admin's machine and commits YAML + PDF in one go. Single execution path. (A CI consistency check that re-renders and diffs is a nice-to-have we can add later.)

### 3.3 `QuantEcon/timesheets-{ra-handle}` — per RA, private

```
QuantEcon/timesheets-{handle}/        ← one per RA, private
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   └── hourly-timesheet.yml      (contract dropdown listing this RA's active contracts only)
│   ├── workflows/
│   │   ├── issue-to-pr.yml           (calls reusable from QuantEcon/timesheets)
│   │   └── process-approved.yml      (calls reusable from QuantEcon/timesheets)
│   └── CODEOWNERS                    (auto-requests admin on every PR)
├── config/settings.yml               ($PAYMENTS_MANAGER, $ADMIN)
├── contracts/                        (derived view; deployed by qemanager sync)
│   ├── jane-doe-hourly-2025.yml
│   └── jane-doe-hourly-2025.pdf
├── submissions/<YYYY-MM>/*.yml       (auto-populated by submissions)
├── ledger/<contract-id>.yml          (auto-populated on merge)
├── generated_pdfs/<YYYY-MM>/*.pdf    (auto-populated on merge)
└── README.md                         (RA-facing how-to)
```

Access control:
- The RA — **Write** (so they can push edits to their own submission PR branches).
- The admin (`mmcky` initially) — **Admin**.
- The payments manager — **Read** (so they can see PDFs and get notifications).

---

## 4. Contractor & contract data model

### 4.1 Contractor record — identity only

```yaml
# contractors/jane-doe.yml
contractor_id: jane-doe
github: janedoe
name: Jane Doe
email: jane.doe@example.com
status: active           # active | inactive | archived
joined_date: 2024-06-01

# Optional fields that downstream processes might want:
affiliations: ["ANU", "QuantEcon"]
notes: |
  Joined for the JAX lecture series; continuing on Python lectures in 2025.
```

The contractor file is **identity and metadata only**. Contract terms (rate, hours, dates, currency) live in separate contract files. This keeps the contractor record stable across renewals and makes contract history a flat directory rather than nested YAML.

### 4.2 Contract record — the contract itself

```yaml
# contracts/jane-doe-hourly-2025.yml
contract_id: jane-doe-hourly-2025
contractor_id: jane-doe
type: hourly                    # hourly | milestone | (others later)
status: active                  # active | ended | archived

start_date: 2025-01-01
end_date: 2025-12-31            # null for open-ended; rare

terms:
  hourly_rate: 45.00
  currency: AUD
  max_hours_per_month: 40

project: python-lectures        # free-form; useful for cross-RA reporting

# Renewal lineage (optional — set by `qemanager contract renew`)
renewed_from: jane-doe-hourly-2024
renewed_as: null                # set when this contract is itself renewed

# Processing routing (per-contract override for the global default)
processing:
  send_to: payments@pslfoundation.org   # optional override
  cc: []

notes: |
  Continuing work on Python lectures from 2024 contract.
```

One file per contract. Files are **functionally immutable once submissions have been booked against them** — to change terms mid-contract, end the old contract and create a new one. (We don't enforce this in code in v1; we document it as policy and rely on git history.)

### 4.3 Contract lifecycle

Three states, transitions managed by the CLI:

| State | Meaning |
|---|---|
| `active` | Current; can receive submissions. |
| `ended` | Past end date, explicitly ended, or renewed. No new submissions. Ledger is finalised. |
| `archived` | Old; hidden from default `qemanager` listings to keep the active picture clean. |

**Renewal:** `qemanager contract renew <existing-contract-id> [--end <new-end>] [--rate <new-rate>]` creates a new contract record copying terms from the existing one (with overrides), sets `renewed_from` / `renewed_as` to link the two, and marks the old one `ended`. The new contract gets a fresh ID (typically `{handle}-{type}-{year+1}`).

**Multiple concurrent contracts:** an RA can hold more than one active contract simultaneously (e.g. hourly + a milestone). Each has its own contract file, its own ledger file in the RA repo, and appears as its own option in the contract dropdown on the issue form.

**Gaps:** an RA can have a period with no active contracts and then return. The contractor record stays; new contract records get created when they return.

### 4.4 Deployment to RA repos — what `qemanager sync` does

When contracts change in `QuantEcon/contractors`, the RA's repo must be updated. The CLI handles this idempotently:

1. Read all `active` contracts where `contractor_id == <handle>` from `QuantEcon/contractors`.
2. Write each contract YAML + PDF into `QuantEcon/timesheets-{handle}/contracts/`.
3. Remove any contract files in the RA repo that no longer correspond to active contracts (these are now ended; they remain in `QuantEcon/contractors/` for history but should drop out of the dropdown).
4. Regenerate `.github/ISSUE_TEMPLATE/hourly-timesheet.yml` so the contract dropdown lists exactly the current active contract IDs.
5. Optionally re-sync template files from `ra-template/` (controlled by a flag).
6. Commit and push.

---

## 5. Admin CLI — `qemanager`

Entry point: `qemanager` (installed via `pip install -e .` from a clone of `QuantEcon/timesheets`).
Implementation: Python stdlib `argparse` + `pyyaml` + `subprocess` to `gh` and `typst`. No other deps.

### 5.1 v1 commands (must have)

| Command | Effect |
|---|---|
| `qemanager contractor add <handle> --name "..." --email ...` | Create `QuantEcon/contractors/contractors/{handle}.yml`. Commit + push. |
| `qemanager contract create <handle> --type hourly --rate <X> --start <YYYY-MM-DD> --end <YYYY-MM-DD> [--project <p>]` | Write contract YAML, render contract PDF via Typst, commit both to `QuantEcon/contractors`. |
| `qemanager contract renew <contract-id> [--end <date>] [--rate <X>]` | Create new contract linked to existing; mark old as `ended`. |
| `qemanager contract end <contract-id> [--end-date <date>]` | Mark contract `ended`; finalise ledger snapshot on next sync. |
| `qemanager onboard <handle>` | Create `QuantEcon/timesheets-{handle}`, seed from `ra-template/` (substituting `$PAYMENTS_MANAGER`, `$ADMIN`, `$RA_HANDLE`, `$RA_NAME`), add collaborators, set branch protection, run `sync`. |
| `qemanager sync <handle>` | Push active contracts + dropdown regeneration to the RA repo (§4.4). |

### 5.2 Deferred (post-v1, build when needed)

- `qemanager contractor list / view / archive`
- `qemanager contract list [--handle <h>] [--status active]`
- `qemanager contract regenerate-pdf <contract-id>`
- `qemanager sync --all` (batch sync for template rollouts)
- `qemanager report` (cross-RA totals)

### 5.3 Implementation notes

- `qemanager` operates on **local clones** of both repos. The expected layout is:
  ```
  /Users/mmcky/work/quantecon/timesheets/        ← this repo
  /Users/mmcky/work/quantecon/contractors/       ← QuantEcon/contractors clone
  /Users/mmcky/work/quantecon/timesheets-*/      ← per-RA clones (auto-cloned on first use)
  ```
- The CLI commits + pushes on each operation. No long-running state; every command is idempotent if re-run.
- Substitution into `ra-template/` uses stdlib `string.Template` (`$variable` / `${variable}`). No Jinja2.
- Contract PDF generation: CLI renders Typst locally and commits the PDF in the same commit as the YAML. Admin needs `typst` installed locally.

---

## 6. v1 scope

| Decision | Choice | Notes |
|---|---|---|
| Submission types | Hourly timesheets only | Invoice and reimbursement forms deferred. |
| Contract listing on the issue form | Static dropdown, auto-populated by `qemanager sync` | RA never edits the issue template; admin owns it. |
| Approval notification | GitHub notifications (no SMTP) | Workflow comments on the closed issue tagging `@PAYMENTS_MANAGER` with links to the PDF blob + workflow artifact. |
| PDF generation — timesheets | Typst, rendered in CI on PR merge | Committed to `generated_pdfs/<YYYY-MM>/` and uploaded as a workflow artifact. |
| PDF generation — contracts | Typst, rendered locally by `qemanager contract create` | Committed alongside the YAML. |
| Ledger / running totals | In v1 | One `ledger/{contract-id}.yml` per contract; updated on merge. |
| Receipts | Out of scope | Timesheets don't need them; revisit with reimbursements. |
| Currency | AUD default | Multi-currency deferred. |
| Cross-RA reporting | Out of scope | Easy to add later; data model supports it. |

---

## 7. Workflow in practice

### 7.1 RA submitting a timesheet

1. RA opens `github.com/QuantEcon/timesheets-{theirhandle}` (bookmarked).
2. *Issues → New Issue → 📋 Hourly Timesheet → fill out form (contract dropdown lists their active contracts) → submit.*
3. `issue-to-pr.yml` runs, parses the form into a YAML file in `submissions/<YYYY-MM>/`, opens a PR titled `Submission: [Timesheet] {contractor} - {period}` linking the issue.
4. CODEOWNERS auto-requests review from admin. RA + admin get notifications.
5. Corrections: RA edits the PR branch directly (they have Write), or admin requests changes via PR review.
6. Admin approves and merges.

### 7.2 On merge

1. `process-approved.yml` identifies the new submission file in the diff.
2. Updates `ledger/{contract-id}.yml` with the new totals.
3. Renders Typst → PDF; commits the PDF to `generated_pdfs/<YYYY-MM>/`; also uploads as workflow artifact.
4. Comments on the now-closed issue: `@{PAYMENTS_MANAGER} Approved — {real RA name} — PDF: <blob URL> · Artifact: <run URL>`.
5. Applies `processed` label.

### 7.3 Admin onboarding a new RA

1. `qemanager contractor add janedoe --name "Jane Doe" --email jane@example.com`
2. `qemanager contract create janedoe --type hourly --rate 45 --start 2025-01-01 --end 2025-12-31 --project python-lectures`
3. `qemanager onboard janedoe`
4. CLI prints the RA repo URL; admin sends it to the RA along with `docs/RA_GUIDE.md`.

### 7.4 Admin renewing a contract (year roll-over)

1. `qemanager contract renew janedoe-hourly-2025 --end 2026-12-31`
2. `qemanager sync janedoe`
3. Done — the RA's issue form now lists `janedoe-hourly-2026`, and `janedoe-hourly-2025` is `ended` in `QuantEcon/contractors`.

---

## 8. Build phases

### Phase 0 — Planning (in progress)
- [x] Create `QuantEcon/timesheets`
- [x] Draft `PLAN.md`
- [ ] Finalise contractor / contract schemas with Matt
- [ ] Create `QuantEcon/contractors` (empty private repo)
- [ ] Freeze v1 decisions

### Phase 1 — Data model + CLI foundations
Build the data side first so we have something to operate on.
- [ ] `qemanager` package skeleton (`pyproject.toml`, `argparse` entry point)
- [ ] `qemanager contractor add` — writes contractor YAML
- [ ] `qemanager contract create` — writes contract YAML
- [ ] `templates/contract.typ` — Typst template for contract PDFs
- [ ] Contract PDF generation wired into `qemanager contract create`
- [ ] First end-to-end: create one real contractor + contract, PDF generated and committed

### Phase 2 — Timesheets engine (single test repo)
Build the submission loop against one disposable test repo before generalising.
- [ ] `scripts/parse_issue.py`
- [ ] `scripts/create_submission_pr.py`
- [ ] `.github/workflows/issue-to-pr.yml` (non-reusable, in-place test)
- [ ] `ra-template/.github/ISSUE_TEMPLATE/hourly-timesheet.yml`
- [ ] End-to-end: open an issue → PR appears with correct YAML

### Phase 3 — Merge processing
- [ ] `templates/timesheet.typ` (QuantEcon-branded)
- [ ] `scripts/generate_pdf.py` (timesheet PDF rendering)
- [ ] `scripts/update_ledger.py`
- [ ] `scripts/notify.py`
- [ ] `.github/workflows/process-approved.yml`
- [ ] End-to-end: merge a PR → PDF in repo + ledger updated + issue commented

### Phase 4 — Reusable workflows + onboarding
- [ ] Convert both workflows to `workflow_call` reusable form
- [ ] Verify private-repo reusable workflow permissions at the org level
- [ ] `ra-template/` thin caller workflows
- [ ] `qemanager onboard` — create RA repo, seed template, add collaborators, branch protection
- [ ] `qemanager sync` — deploy contracts + regenerate dropdown
- [ ] Dogfood with one real RA

### Phase 5 — Docs + first real RAs
- [ ] `docs/RA_GUIDE.md`
- [ ] `docs/ADMIN_GUIDE.md`
- [ ] `docs/CONTRACTOR_DATA_MODEL.md`
- [ ] Onboard a small number of real RAs; iterate on friction

### v2+ (future)
- Milestone invoice form + reimbursement form (revisit receipt storage)
- SMTP email delivery
- Cross-RA aggregator / admin dashboard
- Multi-currency
- Contract template variants for different engagement types

---

## 9. Resolved decisions

| Decision | Choice | Why |
|---|---|---|
| Repo topology | Per-RA private repos | Privacy by construction; matches scale. |
| Contractor / contract source of truth | `QuantEcon/contractors` (admin-only) | Single source; foundation for downstream personnel-data uses. |
| Contract data file shape | One YAML file per contract; contractor file is identity only | Clean history; renewals don't mutate prior records. |
| Contract PDF generation | Typst, run locally by `qemanager`, committed alongside YAML | Single execution path; admin needs Typst anyway. |
| Admin interface | Python CLI `qemanager` | Right surface for the operation count; help text serves as runbook. |
| Templating engine | Stdlib `string.Template` for file generation; Typst's own YAML import for PDFs | Zero new Python deps. |
| Shared logic | Reusable workflows + scripts in `QuantEcon/timesheets` | Single source of truth; updates propagate without re-templating. |
| Notification path | GitHub comment + @-mention + workflow artifact | No SMTP in v1; revisit if it doesn't suit the payments manager. |
| v1 submission types | Hourly timesheets only | Prove the loop end-to-end on the simplest case first. |
| Ledger in v1 | Yes | Adding it later means re-running history; cheaper now. |
| Submission method | Issue Form → auto-PR | Inherited from source issue. |
| External Actions | None | Inherited from source issue; financial-data path stays in-house. |

---

## 10. Open items

- **Payments manager handle.** GitHub username for the PSL Foundation payments manager — needed for `$PAYMENTS_MANAGER` in CODEOWNERS and notifications. Matt to confirm.
- **Admin handle(s).** Just `mmcky`, or also a team handle? Affects CODEOWNERS.
- **Sensitive payment details** (bank accounts, tax IDs). Do these belong in `QuantEcon/contractors` (private GitHub repo is reasonable access control but not encryption-at-rest), or in an external store (1Password, etc.)? Recommend: **out of git** for v1; the contractor record carries a pointer or note, not the data itself.
- **Contract template content.** What should `templates/contract.typ` actually contain? Need a real example contract to model the layout on. Matt to share a recent paper contract or sketch the required fields.
- **Org-level setting: reusable workflows in private repos.** Needs to be enabled on QuantEcon before Phase 4. Org admin action.
- **Actions on private repos / runner-minute budget.** Confirm Actions are enabled and there's headroom.
- **RA email differentiation for the payments manager.** Mitigation: PDF and notification both surface the RA's real name (from the contract YAML), not just the GitHub handle.
- **Receipt storage policy.** Deferred until v2.

---

## 11. Security posture

- All three repo types are private. Engine repo (`QuantEcon/timesheets`) could later be made public as a reference implementation — by design it contains no contractor or financial data.
- No third-party GitHub Actions on any financial-data path. Only `actions/checkout` and `actions/setup-python` from first-party `actions/*`.
- Branch protection on `main` for every RA repo: PR required, 1 review required, no force-push, no direct push.
- GitHub Secrets only for credentials the workflow needs (none in v1; SMTP credentials when email arrives in v2).
- Python deps minimal: stdlib + `pyyaml`. No transitive footprint.
- Contractor data store (`QuantEcon/contractors`) does not hold sensitive payment details in v1 — see Open Items.

---

## 12. Working notes

- Local working dirs:
  - `/Users/mmcky/work/quantecon/timesheets/` (this repo)
  - `/Users/mmcky/work/quantecon/contractors/` (to be cloned once the repo exists)
  - `/Users/mmcky/work/quantecon/timesheets-{handle}/` (per-RA, auto-cloned by `qemanager` when needed)
- This `PLAN.md` is the source of truth for the project plan. Update it in PRs as decisions evolve; don't let conversation context become the only record.
