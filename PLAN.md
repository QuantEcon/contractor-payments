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
QuantEcon/timesheets                  ← engine: workflows, scripts, Typst, contractor-template, onboarding script
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
├── contractor-template/              ← files seeded into each new contractor repo
│   │                                   (onboarding script applies string.Template
│   │                                    substitution to every text file at copy time —
│   │                                    no `.template` suffix convention needed)
│   ├── .github/ISSUE_TEMPLATE/hourly-timesheet.yml   (contains $CONTRACT_OPTIONS)
│   ├── .github/ISSUE_TEMPLATE/config.yml
│   ├── .github/workflows/issue-to-pr.yml             (thin caller of the reusable)
│   ├── .github/workflows/process-approved.yml        (thin caller of the reusable)
│   ├── .github/CODEOWNERS                            (contains $ADMIN)
│   ├── config/settings.yml                           (contains $CONTRACTOR_NAME etc.)
│   ├── contracts/.gitkeep
│   ├── submissions/.gitkeep
│   ├── ledger/.gitkeep
│   ├── generated_pdfs/.gitkeep
│   └── README.md                                     (contractor-facing how-to)
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

## 4. Inside a contractor repo

Four kinds of files shape how a contractor repo works: identity/routing config (§4.1), contract terms (§4.2), the submission form (§4.3), and the validation behaviour wired around the form (§4.4). The first three are admin-authored; the contractor only interacts with the form itself.

### 4.1 `config/settings.yml` — contractor identity + routing

```yaml
contractor:
  name: Jane Doe
  github: janedoe
  email: jane.doe@example.com

admin: mmcky
payments_manager: psl-payments-handle  # GitHub handle, used in @-mentions
```

Written once by the onboarding script; rarely changes afterwards. Currency is **not** a global default — it lives on each contract (§4.2).

### 4.2 `contracts/{contract-id}.yml` — contract terms

```yaml
contract_id: jane-doe-hourly-2025
type: hourly                  # hourly | milestone | (others later)
status: active                # active | ended

start_date: 2025-01-01
end_date: 2025-12-31

terms:
  hourly_rate: 45.00
  currency: AUD               # ISO 4217 — AUD | USD | JPY supported in v1
  max_hours_per_month: 40

project: python-lectures      # free-form

notes: |
  Continuing from 2024 contract.
```

One file per contract. To renew, the admin copies an existing contract file, edits the dates and rate, gives it a new `contract_id`, and marks the old one `ended`.

**Currency handling:** each contract specifies its own currency. Supported ISO 4217 codes in v1: `AUD`, `USD`, `JPY`. The Typst template renders amounts with the ISO code as a suffix (e.g. `45.00 AUD`, `30.00 USD`, `5000 JPY`) — clean and unambiguous, no symbol conventions. `JPY` is rendered without decimal places; `AUD` and `USD` use two. Other ISO codes can be added when a real contractor needs one.

### 4.3 `.github/ISSUE_TEMPLATE/hourly-timesheet.yml` — the submission form

The interface contractors interact with. GitHub renders this YAML as a web form on the "New Issue" page; on submit, GitHub serialises the field values into the issue body as markdown. `scripts/parse_issue.py` then parses that markdown into a structured submission YAML.

**Form fields:**

1. **Contract** (dropdown, required) — populated with the contractor's active contract IDs. Onboarding script writes the initial list; admin edits the list when a contract is renewed.
2. **Period** (dropdown, required) — month in `YYYY-MM` form. Twelve options per year; admin edits the list annually.
3. **Time Entries** (textarea, required) — **one row per day worked**, pipe-delimited `YYYY-MM-DD | hours | description`. Variable rows: contractor only enters days they actually worked, not a fixed grid of 30 rows.
4. **Additional notes** (textarea, optional) — free text.
5. **Confirmation** (checkbox, required) — single ack of accuracy.

**The form file** (post-substitution example):

```yaml
name: 📋 Hourly Timesheet
description: Submit a monthly timesheet for hours worked on an hourly contract.
title: "Timesheet submission"
labels: ["timesheet", "pending-review"]

body:
  - type: markdown
    attributes:
      value: |
        ## Hourly Timesheet Submission

        Fill out the fields below. On submission, a Pull Request will be
        automatically created with the structured data. An admin will
        review and merge; on merge a PDF is generated and the payments
        manager is notified.

        **Corrections after submitting:** edit this issue (the PR will
        be regenerated), or edit the PR branch directly if you're
        comfortable with git.

  - type: dropdown
    id: contract
    attributes:
      label: Contract
      description: Which contract does this timesheet apply to?
      options:
        - jane-doe-hourly-2025   # populated by onboarding/new-contractor.py
    validations:
      required: true

  - type: dropdown
    id: period
    attributes:
      label: Period
      description: Which month is this timesheet for?
      options:
        - "2025-01"
        - "2025-02"
        # ... twelve months for the current year
        - "2025-12"
    validations:
      required: true

  - type: textarea
    id: entries
    attributes:
      label: Time Entries
      description: |
        Enter one row per day worked, in the format:
        `YYYY-MM-DD | hours | description`

        Hours may be fractional (e.g. 4.5). Descriptions may contain
        any text — the parser splits on the first two `|` only.
      placeholder: |
        2025-01-06 | 3.5 | NumPy lecture exercises review
        2025-01-13 | 5.0 | Plotting examples
        2025-01-20 | 4.0 | CI pipeline fixes
      render: text
    validations:
      required: true

  - type: textarea
    id: notes
    attributes:
      label: Additional notes (optional)
      placeholder: e.g. "Travel time on the 15th not included."
    validations:
      required: false

  - type: checkboxes
    id: confirmation
    attributes:
      label: Confirmation
      options:
        - label: I confirm that the hours and descriptions above are accurate.
          required: true
```

A sibling `config.yml` disables blank issues and points contractors at the guide:

```yaml
# .github/ISSUE_TEMPLATE/config.yml
blank_issues_enabled: false
contact_links:
  - name: How to submit a timesheet
    url: https://github.com/QuantEcon/timesheets/blob/main/docs/CONTRACTOR_GUIDE.md
    about: Step-by-step guide with screenshots
```

**Parser tolerances** — `parse_issue.py` accepts common variations:

- Date formats: `YYYY-MM-DD` canonical; also accept `YYYY/MM/DD` and `DD-MM-YYYY` if the month is unambiguous.
- Hour-unit suffixes stripped: `4.5`, `4.5h`, `4.5 hrs` all parse to `4.5`.
- Delimiters: `|` canonical; also accept `,` or tab if consistently used in the input (emit a non-blocking warning comment to the issue).
- Whitespace normalised; blank lines and obvious header rows skipped.
- **Description content may contain `|`** — parser splits on the first two pipes only, so the third "field" captures everything after.

**Parser must reject** with line-specific errors:

- Date that can't be parsed at all.
- Date outside the selected `Period`.
- Two rows with the same date (duplicate-day check).
- Hours ≤ 0 or > 24.
- Missing fields (fewer than three pipe-separated segments).

### 4.4 Submission validation and failure handling

Validation runs across three layers so that good submissions sail through, bad submissions get specific feedback, and admins only see well-formed PRs.

**Layer 1 — Form constraints.** Dropdowns for `Contract` and `Period` make those fields typo-proof. Required fields and the confirmation checkbox are enforced by GitHub at submit time.

**Layer 2 — CI parsing.** On `issues: opened` and `issues: edited`, the workflow runs `parse_issue.py`. Outcomes:

- **Parse succeeds, no PR exists:** workflow creates a branch, commits the structured submission YAML, opens a PR with `Closes #{issue-number}` in the body. Removes any previous error comment from the issue.
- **Parse succeeds, PR already exists** (contractor edited the issue to fix something post-submission): workflow regenerates the submission YAML on the existing PR branch, force-pushes, posts a comment on the PR noting the regeneration. *Deferred from first ship — until built, contractors fix post-submission issues by editing the PR branch directly.*
- **Parse fails:** no PR is created or modified. Workflow posts an error comment on the issue (or updates the existing one) and applies a `parse-error` label. Issue stays open.

**Layer 3 — PR review.** Admin merges or requests changes via standard PR review. Catches semantic errors (hours don't match the work described, wrong period selected, etc.) that no parser can detect.

**Error comment format.** Comments are written by the workflow with an HTML sentinel marker. On re-run after a failed edit, the workflow finds the previous comment by the sentinel and edits it in place — no comment spam.

```markdown
🤖 **Submission needs a fix**

I couldn't parse the time entries. Here's what I found:

- **Line 3:** couldn't read a date from `2025/01/05` — please use
  hyphens, e.g. `2025-01-05`.
- **Line 7:** date `2025-02-03` is outside the selected period
  `2025-01`. Either change the date or pick a different period.

To fix, **edit this issue** (click the ⋯ menu → Edit) and update
those lines. I'll re-check automatically when you save.

<!-- timesheet-parse-error -->
```

**Triggers and what the workflow ignores.**

- Re-runs on `issues: opened` and `issues: edited` only.
- Does not run on new comments — the issue body is the form data; comments are for human conversation.
- Does not auto-close issues on failure. Issues close only when the linked PR merges (via `Closes #N`).

**State cleanup on successful re-parse.**

- `parse-error` label removed.
- Previous error comment removed (or rewritten as a success acknowledgement — exact wording decided during build).
- PR opens at most once per issue (creation on first successful parse; updates via force-push on subsequent successful parses, once that path is built).

---

## 5. Onboarding — `onboarding/new-contractor.py`

A single interactive Python script. Stdlib `argparse` + `pyyaml` + `subprocess` to `gh`. Run from a clone of `QuantEcon/timesheets`.

### What it does

1. Prompts for (with reasonable defaults where applicable):
   - GitHub handle of the new contractor
   - Real name
   - Email
   - Payments manager GitHub handle (defaulted from a config or prior run)
   - First contract: type, start date, end date, rate (or milestone list), **currency** (AUD / USD / JPY; validates against the v1 supported list), project name
2. Creates `QuantEcon/contractor-{handle}` as a private repo.
3. Seeds the repo from `contractor-template/`, substituting prompted values into `config/settings.yml`, `README.md`, `CODEOWNERS`, and the contract YAML.
4. Generates `contracts/{contract-id}.yml` from the prompted contract details.
5. Adds the contractor (Write), admin (Admin), and payments manager (Read) as collaborators via `gh api`.
6. Sets branch protection on `main` (PR required, 1 review).
7. Pushes the initial commit.
8. Prints the contractor-facing URL and next steps.

### What it does **not** do

- No contract PDF generation (contracts are YAML metadata; no signed PDF in v1).
- No contract renewal / end automation — admin edits YAML by hand.
- No central record of which contractors exist (you can `gh repo list QuantEcon --topic contractor` if you tag the repos, or list `contractor-*` repos via `gh repo list`).
- No batch operations or template re-sync — when workflows in `QuantEcon/timesheets` change, contractor repos that reference them via reusable workflows pick up the change automatically. Files copied from `contractor-template/` are only re-synced manually if needed.

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
| Contract listing on issue form | Static dropdown in the form YAML | Onboarding script seeds the initial list from this contractor's active contracts; admin edits on contract renewal. See §4.3. |
| Approval notification | GitHub comment + @-mention + workflow artifact | No SMTP in v1. |
| PDF generation | Typst in CI on PR merge | Committed to `generated_pdfs/<YYYY-MM>/` + uploaded as artifact. |
| Ledger / running totals | Yes | One `ledger/<contract-id>.yml` per contract; updated on merge. |
| Onboarding | Interactive Python script | See §5. |
| Encryption at rest | None | Each repo is one contractor; blast radius is naturally scoped. |
| Receipts | Out of scope | No reimbursements in v1. |
| Currency | Per-contract; AUD, USD, JPY supported in v1 | Specified in each contract YAML (§4.2). JPY rendered without decimals; AUD/USD with two. ISO code as suffix, no symbols. |
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
4. Edit `.github/ISSUE_TEMPLATE/hourly-timesheet.yml` — add the new contract ID to the `Contract` dropdown options, remove the ended one if appropriate.
5. Commit and push.

No CLI, no ceremony. One additional file to edit beyond the contract YAML (the form's dropdown), captured here so it doesn't get missed.

---

## 8. Build phases

### Phase 0 — Planning (in progress)
- [x] Create `QuantEcon/timesheets`
- [x] Tighten `PLAN.md` to v1 scope
- [x] Open broader infrastructure issue in `QuantEcon/admin` ([#5](https://github.com/QuantEcon/admin/issues/5))
- [x] Consistency pass on `PLAN.md`
- [ ] Resolve open items in §10 (payments manager handle, admin handle/team, org-level reusable-workflow setting, runner-minutes budget)

### Phase 1 — Timesheets engine in a single test repo ✅
Built everything against `QuantEcon/contractor-engine-test`. All three flows (valid submission, invalid submission, fix-and-retrigger) verified end-to-end against live GitHub.
- [x] Create `QuantEcon/contractor-engine-test` (private, disposable) with a hand-written `config/settings.yml` and one `contracts/*.yml` for testing
- [x] `.github/ISSUE_TEMPLATE/hourly-timesheet.yml` — submission form (§4.3)
- [x] `.github/ISSUE_TEMPLATE/config.yml` — disable blank issues
- [x] `scripts/parse_issue.py` — parser with lenient input handling and line-specific errors (§4.3, §4.4)
- [x] `tests/test_parse_issue.py` — unit tests covering malformed inputs and edge cases
- [x] `scripts/create_submission_pr.py` — branch + commit + PR via `gh`
- [x] `scripts/post_error_comment.py` — sentinel-marked error comment on parse failure; updates in place on re-run (§4.4)
- [x] `.github/workflows/issue-to-pr.yml` (in-place, non-reusable) — wires parse → PR-or-error-comment, triggers on `issues: opened` and `issues: edited`
- [x] End-to-end test: valid issue → PR appears; invalid issue → error comment posted; edited issue with fix → PR appears, error comment cleared, label removed

### Phase 2 — Merge processing
- [ ] `templates/timesheet.typ` — QuantEcon-branded Typst template
- [ ] `scripts/generate_pdf.py`
- [ ] `scripts/update_ledger.py`
- [ ] `scripts/notify.py`
- [ ] `.github/workflows/process-approved.yml`
- [ ] End-to-end test: merge a PR → PDF + ledger + comment + label

### Phase 3 — Reusable workflows + contractor-template + onboarding
- [ ] Convert both workflows to `workflow_call` reusable form
- [ ] Verify private-repo reusable workflow permissions at the org level
- [ ] Build `contractor-template/` (thin caller workflows + templated config + READMEs)
- [ ] `onboarding/new-contractor.py`
- [ ] Spin up `QuantEcon/contractor-onboarding-test` via the onboarding script and run the full submit → merge loop end-to-end

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
| Currency | Per-contract field; AUD / USD / JPY in v1 | QuantEcon already has real contractors in all three. Currency lives on each contract; PDF renders ISO code as suffix, no symbols; JPY without decimals. |
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
