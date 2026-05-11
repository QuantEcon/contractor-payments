# QuantEcon Timesheets — Implementation Plan

**Status:** Working draft. Tightened to v1 timesheet processing only.
**Source issue:** [QuantEcon/admin#3 — PRJ: QuantEcon Timesheet Management System](https://github.com/QuantEcon/admin/issues/3)
**Related (broader vision, separate track):** [QuantEcon/admin#5 — PRJ: QuantEcon admin infrastructure](https://github.com/QuantEcon/admin/issues/5)

---

## 1. Goals

Ship a GitHub-native system that lets QuantEcon contractors submit timesheets, have them reviewed via PR, and produce a clean PDF + GitHub notification on approval. Nothing more.

Constraints:

- Compensation data is sensitive — contractors must not see each others' rates, hours, or totals.
- QuantEcon does not host webapps; GitHub Pages + GitHub Actions is the only ops surface.
- Contractors are GitHub-familiar.
- Scale: 5–10 active contractors, monthly cadence.
- No third-party GitHub Actions on any financial-data path.
- **Ship the timesheet loop first.** Broader admin infrastructure (centralized contractor data, cross-contractor reporting, encryption-at-rest, contract lifecycle automation) is captured separately and is not in scope here.

---

## 2. Architectural decision — per-contractor private repos

A single shared repo with all contractors as collaborators would leak every contractor's rate, hours, and totals to every other contractor. Unacceptable for compensation data.

Selected: one private repo per contractor under the QuantEcon org, named `QuantEcon/contractor-{github-handle}`. Privacy by construction; preserves all GitHub-native benefits; at 5–10 contractors the onboarding overhead is a single scripted command.

**Why `contractor-{handle}` and not `timesheets-{handle}`:** the repo will later absorb other contractor-related artefacts (invoices, reimbursements, end-of-year statements, contract documents) without renaming. The system we're building is the first feature, not the only one.

---

## 3. Repository topology

Two repos:

```
QuantEcon/timesheets                  ← engine: workflows, scripts, Typst, ra-template, onboarding script
QuantEcon/contractor-{handle}         ← per-contractor private repo
```

### 3.1 `QuantEcon/timesheets` (this repo) — the engine

```
QuantEcon/timesheets/
├── .github/workflows/                ← reusable workflows for contractor repos
│   ├── issue-to-pr.yml               (workflow_call; called from contractor repos)
│   └── process-approved.yml          (workflow_call; called from contractor repos)
├── scripts/                          ← run in CI; checked out at workflow runtime
│   ├── parse_issue.py
│   ├── create_submission_pr.py
│   ├── update_ledger.py
│   ├── generate_pdf.py
│   └── notify.py
├── onboarding/
│   └── new-contractor.py             ← interactive setup script (see §5)
├── templates/
│   └── timesheet.typ                 (Typst PDF template)
├── ra-template/                      ← files seeded into each new contractor repo
│   ├── .github/ISSUE_TEMPLATE/hourly-timesheet.yml
│   ├── .github/workflows/issue-to-pr.yml          (thin caller of the reusable)
│   ├── .github/workflows/process-approved.yml     (thin caller of the reusable)
│   ├── .github/CODEOWNERS
│   ├── config/settings.yml.template               (contractor identity, admin, payments manager)
│   ├── contracts/.gitkeep
│   ├── submissions/.gitkeep
│   ├── ledger/.gitkeep
│   ├── generated_pdfs/.gitkeep
│   └── README.md.template            (contractor-facing how-to)
├── docs/
│   ├── CONTRACTOR_GUIDE.md           (for the submitting contractor)
│   └── ADMIN_GUIDE.md                (onboarding, reviewing, editing contracts)
└── PLAN.md                           (this file)
```

### 3.2 `QuantEcon/contractor-{handle}` — per contractor, private

```
QuantEcon/contractor-{handle}/
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   └── hourly-timesheet.yml      (contract dropdown lists this contractor's active contracts)
│   ├── workflows/
│   │   ├── issue-to-pr.yml           (calls reusable from QuantEcon/timesheets)
│   │   └── process-approved.yml      (calls reusable from QuantEcon/timesheets)
│   └── CODEOWNERS                    (auto-requests admin on every PR)
├── config/settings.yml               (contractor identity, admin, payments manager handles)
├── contracts/<contract-id>.yml       (admin-edited; see §4)
├── submissions/<YYYY-MM>/*.yml       (auto-populated)
├── ledger/<contract-id>.yml          (auto-populated on merge)
├── generated_pdfs/<YYYY-MM>/*.pdf    (auto-populated on merge)
└── README.md
```

Access control:
- The contractor — **Write** (so they can push edits to their own submission PR branches).
- The admin (`mmcky` initially) — **Admin**.
- The payments manager — **Read** (so they can see PDFs and get notifications).

---

## 4. Data inside a contractor repo

Two configuration files. Both admin-edited by hand. No CLI, no central store — they live where the submissions live.

### 4.1 `config/settings.yml` — contractor identity + routing

```yaml
contractor:
  name: Jane Doe
  github: janedoe
  email: jane.doe@example.com

admin: mmcky
payments_manager: psl-payments-handle  # GitHub handle, used in @-mentions

defaults:
  currency: AUD
```

Written once by the onboarding script; rarely changes afterwards.

### 4.2 `contracts/{contract-id}.yml` — contract terms

```yaml
contract_id: jane-doe-hourly-2025
type: hourly                  # hourly | milestone | (others later)
status: active                # active | ended

start_date: 2025-01-01
end_date: 2025-12-31

terms:
  hourly_rate: 45.00
  max_hours_per_month: 40

project: python-lectures      # free-form

notes: |
  Continuing from 2024 contract.
```

One file per contract. To renew, the admin copies an existing contract file, edits the dates and rate, gives it a new `contract_id`, and marks the old one `ended`. Currency comes from `settings.yml` unless overridden.

The `contracts/` directory is the source of truth for which contracts an issue-form dropdown should list — the workflow generates the dropdown from active contracts at submission time.

---

## 5. Onboarding — `onboarding/new-contractor.py`

A single interactive Python script. Stdlib `argparse` + `pyyaml` + `subprocess` to `gh`. Run from a clone of `QuantEcon/timesheets`.

### What it does

1. Prompts for (with reasonable defaults where applicable):
   - GitHub handle of the new contractor
   - Real name
   - Email
   - Payments manager GitHub handle (defaulted from a config or prior run)
   - First contract: type, start date, end date, rate (or milestone list), project name
2. Creates `QuantEcon/contractor-{handle}` as a private repo.
3. Seeds the repo from `ra-template/`, substituting prompted values into `config/settings.yml`, `README.md`, `CODEOWNERS`, and the contract YAML.
4. Generates `contracts/{contract-id}.yml` from the prompted contract details.
5. Adds the contractor (Write), admin (Admin), and payments manager (Read) as collaborators via `gh api`.
6. Sets branch protection on `main` (PR required, 1 review).
7. Pushes the initial commit.
8. Prints the contractor-facing URL and next steps.

### What it does **not** do

- No contract PDF generation (contracts are YAML metadata; no signed PDF in v1).
- No contract renewal / end automation — admin edits YAML by hand.
- No central record of which contractors exist (you can `gh repo list QuantEcon --topic contractor` if you tag the repos, or list `contractor-*` repos via `gh repo list`).
- No batch operations or template re-sync — when workflows in `QuantEcon/timesheets` change, contractor repos that reference them via reusable workflows pick up the change automatically. Files copied from `ra-template/` are only re-synced manually if needed.

### Implementation notes

- Idempotent for re-runs: if the repo already exists, the script reports and exits non-zero rather than overwriting.
- Substitution uses stdlib `string.Template`.
- All GitHub operations use `gh` CLI subprocess calls; no Python GitHub libraries.

---

## 6. v1 scope

| Decision | Choice | Notes |
|---|---|---|
| Submission types | Hourly timesheets only | Invoices and reimbursements deferred. |
| Per-contractor repo name | `QuantEcon/contractor-{github-handle}` | Future-proof for other contractor artefacts. |
| Contract data | Plaintext YAML in each contractor's repo | Admin-edited by hand. |
| Contract listing on issue form | Auto-generated from `contracts/*.yml` with `status: active` | Workflow regenerates dropdown when contracts change. |
| Approval notification | GitHub comment + @-mention + workflow artifact | No SMTP in v1. |
| PDF generation | Typst in CI on PR merge | Committed to `generated_pdfs/<YYYY-MM>/` + uploaded as artifact. |
| Ledger / running totals | Yes | One `ledger/<contract-id>.yml` per contract; updated on merge. |
| Onboarding | Interactive Python script | See §5. |
| Encryption at rest | None | Each repo is one contractor; blast radius is naturally scoped. |
| Receipts | Out of scope | No reimbursements in v1. |
| Currency | AUD default | Set in `config/settings.yml`. |
| Cross-contractor reporting | Out of scope | Captured in the broader admin infrastructure issue. |

---

## 7. Workflow in practice

### 7.1 Contractor submitting a timesheet

1. Contractor opens `github.com/QuantEcon/contractor-{theirhandle}` (bookmarked).
2. *Issues → New Issue → 📋 Hourly Timesheet → fill out form → submit.*
3. `issue-to-pr.yml` parses the form into a YAML file in `submissions/<YYYY-MM>/`, opens a PR linking the issue.
4. CODEOWNERS auto-requests review from admin. Contractor + admin get notifications.
5. Corrections: contractor edits the PR branch directly, or admin requests changes via PR review.
6. Admin approves and merges.

### 7.2 On merge

1. `process-approved.yml` identifies the new submission in the diff.
2. Updates `ledger/<contract-id>.yml` with the new totals.
3. Renders Typst → PDF; commits to `generated_pdfs/<YYYY-MM>/`; uploads as workflow artifact.
4. Comments on the now-closed issue: `@{payments_manager} Approved — {real name} — PDF: <blob URL> · Artifact: <run URL>`.
5. Applies `processed` label.

### 7.3 Admin onboarding a new contractor

1. `python onboarding/new-contractor.py` — answer the prompts.
2. Script creates the repo, seeds it, adds collaborators, pushes. Prints the URL.
3. Admin sends the URL + `docs/CONTRACTOR_GUIDE.md` to the new contractor.

### 7.4 Admin renewing a contract

1. In the contractor's repo, copy `contracts/{old-contract-id}.yml` to `contracts/{new-contract-id}.yml`.
2. Edit dates / rate / status as needed.
3. Mark the old contract `status: ended`.
4. Commit and push.

That's the renewal flow. No CLI, no ceremony. The dropdown on the issue form picks up the change next time it runs.

---

## 8. Build phases

### Phase 0 — Planning (in progress)
- [x] Create `QuantEcon/timesheets`
- [x] Tighten `PLAN.md` to v1 scope
- [x] Open broader infrastructure issue in `QuantEcon/admin` ([#5](https://github.com/QuantEcon/admin/issues/5))
- [ ] Freeze v1 decisions

### Phase 1 — Timesheets engine in a single test repo
Build everything against one disposable test repo before generalising to reusable workflows.
- [ ] `scripts/parse_issue.py` — parse Issue Form body
- [ ] `scripts/create_submission_pr.py` — branch + commit + PR via `gh`
- [ ] `.github/ISSUE_TEMPLATE/hourly-timesheet.yml`
- [ ] `.github/workflows/issue-to-pr.yml` (in-place, non-reusable)
- [ ] End-to-end test: open an issue → PR appears with correct YAML

### Phase 2 — Merge processing
- [ ] `templates/timesheet.typ` — QuantEcon-branded Typst template
- [ ] `scripts/generate_pdf.py`
- [ ] `scripts/update_ledger.py`
- [ ] `scripts/notify.py`
- [ ] `.github/workflows/process-approved.yml`
- [ ] End-to-end test: merge a PR → PDF + ledger + comment + label

### Phase 3 — Reusable workflows + ra-template + onboarding
- [ ] Convert both workflows to `workflow_call` reusable form
- [ ] Verify private-repo reusable workflow permissions at the org level
- [ ] Build `ra-template/` (thin caller workflows + templated config + READMEs)
- [ ] `onboarding/new-contractor.py`
- [ ] Onboard one disposable real `contractor-test` repo end-to-end

### Phase 4 — Docs + first real contractors
- [ ] `docs/CONTRACTOR_GUIDE.md`
- [ ] `docs/ADMIN_GUIDE.md` (onboarding runbook, editing contracts, troubleshooting)
- [ ] Onboard a small number of real contractors; iterate on friction

### v2+ (future, in scope of this PLAN if needed)
- Milestone invoice form
- Reimbursement form (revisit receipt storage)
- SMTP email delivery

### Extracted to the broader admin infrastructure issue (not v2 of timesheets)
- Centralized contractor / contract data store
- `qemanager`-style admin CLI
- Contract lifecycle automation (renewals, end-dates, status tracking)
- Contract PDF generation
- `git-crypt` encryption-at-rest posture
- Cross-contractor reporting
- Personnel data plumbing (mailing lists, GitHub team membership, payment-platform exports)

---

## 9. Resolved decisions

| Decision | Choice | Why |
|---|---|---|
| Repo topology | Per-contractor private repos `QuantEcon/contractor-{handle}` | Privacy by construction; future-proof name. |
| Contract / contractor data | Plaintext YAML in each contractor's repo (`config/settings.yml` + `contracts/*.yml`) | Co-located with submissions; admin-edited by hand. |
| Shared logic | Reusable workflows + scripts in `QuantEcon/timesheets` | Single source of truth. |
| Onboarding | Interactive Python script `onboarding/new-contractor.py` | Asks the right questions; populates the repo cleanly. |
| Notification | GitHub comment + @-mention + workflow artifact | No SMTP in v1. |
| v1 submission types | Hourly timesheets only | Prove the loop end-to-end on the simplest case. |
| Ledger in v1 | Yes | Cheap now; expensive to backfill later. |
| Encryption at rest | None | Each repo holds one contractor's data; access is naturally scoped. Revisit if a centralized store is later built. |
| External Actions | None on the financial-data path | Inherited from source issue. |

---

## 10. Open items

- **Payments manager GitHub handle.** Needed for CODEOWNERS and `settings.yml`. Matt to confirm.
- **Admin handle(s).** Just `mmcky`, or also a team handle?
- **Org settings — reusable workflows in private repos.** Needs to be enabled on QuantEcon before Phase 3. Org admin action.
- **Actions on private repos / runner-minute budget.** Confirm enabled + headroom.
- **Real-name surfacing.** Mitigation for the payments manager being unable to map GitHub handles → real names: every PDF and notification surfaces the contractor's real name from `settings.yml`.

---

## 11. Security posture

- All contractor repos are private. The engine repo could later be made public as a reference implementation; by design it holds no data.
- No third-party GitHub Actions on any financial-data path. Only `actions/checkout` and `actions/setup-python` from first-party `actions/*`.
- Branch protection on `main` for every contractor repo: PR required, 1 review required, no force-push.
- GitHub Secrets only for credentials the workflow needs (none in v1; SMTP credentials when email arrives later).
- Python deps minimal: stdlib + `pyyaml`.
- **No bank accounts, tax IDs, or other payment credentials in any repo.** Reference an external store (1Password, etc.) by stable ID if needed.

---

## 12. Working notes

- Local working dirs:
  - `/Users/mmcky/work/quantecon/timesheets/` (this repo)
  - `/Users/mmcky/work/quantecon/contractor-{handle}/` (per-contractor, cloned as needed)
- Local toolchain: `typst` (`brew install typst`), Python 3.12+, `gh` CLI.
- This `PLAN.md` is the source of truth for the project plan. Update it in PRs as decisions evolve.
