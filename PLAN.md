# QuantEcon Contractor Payments — Implementation Plan

**Status:** Working draft. v1 scope spans Hourly Timesheet + Milestone Invoice; Reimbursement Claim deferred to Phase 5.
**Source issue:** [QuantEcon/admin#3 — PRJ: QuantEcon Timesheet Management System](https://github.com/QuantEcon/admin/issues/3)
**Related (broader vision, separate track):** [QuantEcon/admin#5 — PRJ: QuantEcon admin infrastructure](https://github.com/QuantEcon/admin/issues/5)

---

## At a glance

Phase progress — high-level summary. Detailed task lists per phase live in [§8 Build phases](#8-build-phases).

- [x] **Phase 0** — Planning
- [x] **Phase 1** — Hourly Timesheet engine
- [x] **Phase 1.5** — Milestone Invoice engine
- [x] **Phase 3a** — Reusable workflows (engine code centralised; contractor repos are thin callers)
- [x] **Phase 2** — Merge processing + email notify to PSL (engine code complete; partial E2E verified through ledger update + pinned-issue refresh; SMTP credentials needed to unlock the email step)
- [ ] 🛑 **BREAK** — testing phase  ← **current target** (set SMTP secrets per `notes/EMAIL_SETUP.md`, then run full E2E with `testing_mode: true` keeping PSL off the recipient list)
- [ ] **Phase 3b** — Onboarding script for new contractor repos
- [ ] **Phase 4** — Docs + first real contractors (flip `testing_mode: false` here)
- [ ] **Phase 5** — Reimbursement Claim engine (post-launch)

## Contents

1. [Goals](#1-goals)
2. [Architectural decision — per-contractor private repos](#2-architectural-decision--per-contractor-private-repos)
3. [Repository topology](#3-repository-topology)
4. [Inside a contractor repo](#4-inside-a-contractor-repo)
   - 4.1 [`config/settings.yml` — contractor identity + routing](#41-configsettingsyml--contractor-identity--routing)
   - 4.2 [`contracts/{contract-id}.yml` — contract terms](#42-contractscontract-idyml--contract-terms)
   - 4.3 [Submission forms — overview](#43-submission-forms--overview)
   - 4.4 [Hourly Timesheet form](#44-githubissue_templatehourly-timesheetyml--hourly-timesheet-form)
   - 4.5 [Submission validation and failure handling](#45-submission-validation-and-failure-handling)
   - 4.6 [Milestone Invoice form](#46-githubissue_templatemilestone-invoiceyml--milestone-invoice-form)
   - 4.7 [Reimbursement Claim form](#47-githubissue_templatereimbursement-claimyml--reimbursement-claim-form)
   - 4.8 [Generic submission YAML shape](#48-generic-submission-yaml-shape)
5. [Onboarding script](#5-onboarding--onboardingnew-contractorpy)
6. [v1 scope](#6-v1-scope)
7. [Workflow in practice](#7-workflow-in-practice)
8. [Build phases](#8-build-phases)
9. [Resolved decisions](#9-resolved-decisions)
10. [Open items](#10-open-items)
11. [Security posture](#11-security-posture)
12. [Working notes](#12-working-notes)

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
QuantEcon/contractor-payments                  ← engine: workflows, scripts, Typst, contractor-template, onboarding script
QuantEcon/contractor-{handle}         ← per-contractor private repo
```

### 3.1 `QuantEcon/contractor-payments` (this repo) — the engine

End-state layout. Phase-status check-off lives in §8.

```
QuantEcon/contractor-payments/
├── .github/workflows/                ← reusable workflows for contractor repos (Phase 3)
│   ├── issue-to-pr.yml               (workflow_call; called from contractor repos)
│   └── process-approved.yml          (workflow_call; called from contractor repos)
├── scripts/                          ← run in CI; checked out at workflow runtime
│   ├── __init__.py
│   ├── parse_issue.py                (built — §4.3, §4.4)
│   ├── create_submission_pr.py       (built — renders PDF + PNG, opens PR)
│   ├── post_error_comment.py         (built — sentinel comment on parse fail)
│   ├── generate_pdf.py               (built — PDF + PNG via Typst)
│   ├── update_ledger.py              (Phase 2)
│   └── notify.py                     (Phase 2)
├── tests/
│   ├── __init__.py
│   ├── test_parse_issue.py           (49 cases)
│   ├── test_create_submission_pr.py  (24 cases)
│   └── test_post_error_comment.py    (7 cases)
├── onboarding/
│   └── new-contractor.py             ← interactive setup script (Phase 3, see §5)
├── templates/
│   ├── timesheet.typ                 (Typst single-page A4 template)
│   ├── fiscal-host.yml                  (PSL Foundation address; single source across repos)
│   └── assets/
│       ├── quantecon-logo.png
│       └── psl-foundation-logo.png
├── contractor-template/              ← files seeded into each new contractor repo
│   │                                   (onboarding script applies string.Template
│   │                                    substitution to every text file at copy time —
│   │                                    no `.template` suffix convention needed)
│   ├── .github/ISSUE_TEMPLATE/hourly-timesheet.yml   (contains $CONTRACT_OPTIONS)
│   ├── .github/ISSUE_TEMPLATE/config.yml
│   ├── .github/workflows/issue-to-pr.yml             (Phase 1: full inline workflow;
│   │                                                  Phase 3 refactor: thin caller)
│   ├── .github/workflows/process-approved.yml        (Phase 2 / Phase 3)
│   ├── .github/CODEOWNERS                            (contains $ADMIN)
│   ├── config/settings.yml                           (contains $CONTRACTOR_NAME etc.)
│   ├── contracts/.gitkeep
│   ├── submissions/.gitkeep
│   ├── ledger/.gitkeep
│   ├── generated_pdfs/.gitkeep
│   └── README.md                                     (contractor-facing how-to)
├── docs/                             ← MkDocs Material source; published to GitHub Pages
│   ├── index.md                      (placeholder landing — "guide coming soon")
│   └── contractor-guide/             (Phase 4 — submit-timesheet, submit-invoice, corrections)
├── notes/                            ← internal dev/ops runbooks; NOT published
│   └── EMAIL_SETUP.md                (SMTP setup runbook for §10 credentials)
├── mkdocs.yml                        (site config — Material theme, nav)
├── .github/workflows/docs.yml        (build + deploy to Pages via artifact actions)
├── pyproject.toml                    (project metadata; deps: pyyaml + pytest)
├── .gitignore
└── PLAN.md                           (this file)
```

### 3.2 `QuantEcon/contractor-{handle}` — per contractor, private

End-state layout (Phase 3 onwards). In Phase 1 the test repo also carries local copies of `scripts/` and `templates/`; once Phase 3 lands reusable workflows, contractor repos hold only the thin caller workflows and reference the engine repo for scripts and templates.

```
QuantEcon/contractor-{handle}/
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   ├── hourly-timesheet.yml      (contract dropdown filtered to hourly contracts)
│   │   ├── milestone-invoice.yml     (contract dropdown filtered to milestone contracts)
│   │   ├── reimbursement-claim.yml   (Phase 5; see §8)
│   │   └── config.yml                (blank issues disabled)
│   ├── workflows/
│   │   ├── issue-to-pr.yml           (calls reusable from QuantEcon/contractor-payments)
│   │   └── process-approved.yml      (calls reusable from QuantEcon/contractor-payments)
│   └── CODEOWNERS                    (auto-requests admin on every PR)
├── config/settings.yml               (contractor identity, admin, payments manager handles, optional address)
├── contracts/<contract-id>.yml       (admin-edited; see §4)
├── submissions/<YYYY-MM>/*.yml       (auto-populated)
├── ledger/<contract-id>.yml          (auto-populated on merge)
├── generated_pdfs/<YYYY-MM>/         (auto-populated)
│   ├── <id>.pdf                      (authoritative — sent to payments manager)
│   └── <id>.png                      (preview — embedded inline in PR body)
└── README.md
```

Phase 3 onboarding seeds both `hourly-timesheet.yml` and `milestone-invoice.yml` unconditionally. The reimbursement template lands in **Phase 5** alongside the multi-select onboarding feature, which lets the admin opt repos in or out of any of the three template types (useful once reimbursement-only payees exist).

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
  address: |                                   # optional, multi-line
    Research School of Economics
    Australian National University
    Canberra, ACT 2601
    Australia

admin: mmcky
```

Written once by the onboarding script; rarely changes afterwards.

- **Currency** is not a global default — it lives on each contract (§4.2).
- **Address** is optional but recommended for tax-invoice compliance (Australian tax invoices over $1,000 AUD must identify the supplier; address is one accepted way). Renders on the PDF only when populated.
- **Fiscal-host config doesn't live here.** QuantEcon and PSL Foundation addresses, the document-date timezone, and the email notification recipients all live in the engine repo's `templates/fiscal-host.yml` as the single source of truth across every contractor repo (§9). Per-contractor `settings.yml` only carries contractor identity.
- **The payments manager isn't a GitHub handle anymore.** PSL receives approvals by email (§6, §8 Phase 2), so there's no per-contractor `payments_manager:` field — the recipient is centralised in `fiscal-host.yml.notifications.psl_to`.

### 4.2 `contracts/{contract-id}.yml` — contract terms

The contract is the authorization for **labor claims** (hourly or milestone). Reimbursements are *not* tied to a contract — they're contractor-level and authorized per-claim via the approval flow (see §4.6).

Two contract types:

**Hourly contract:**

```yaml
contract_id: QE-PSL-2026-001
type: hourly                  # hourly | milestone
status: active                # active | ended

start_date: 2026-01-01
end_date: 2026-12-31

terms:
  hourly_rate: 45.00
  currency: AUD               # ISO 4217 — AUD | USD | JPY supported in v1
  max_hours_per_month: 40

project: python-lectures      # free-form

ledger_issue: 5               # GitHub issue # for the auto-updated ledger view (Phase 2)

notes: |
  Continuing from 2025 contract.
```

**Milestone contract:**

```yaml
contract_id: QE-IUJ-2025-002
type: milestone
status: active

start_date: 2025-09-01
end_date: 2026-02-28

currency: JPY                 # default currency for milestone claims

project: iuj-visit

ledger_issue: 6               # GitHub issue # for the auto-updated ledger view (Phase 2)

notes: |
  Six monthly payments of ¥77,000 (total ¥462,000), payable 15th of each
  month from Sep 2025 through Feb 2026.
```

The `ledger_issue` field is written by Phase 3b's onboarding script when it opens the ledger issue; the approval workflow reads it to know which issue to edit. Optional — if missing, the workflow skips the issue update (the YAML side still gets the entry).

Lightweight by design: the contract declares that it's a milestone contract and what currency claims are denominated in, but does **not** pre-enumerate the milestones. The contractor enters each milestone row at submission time via the issue form (§4.6) — same UX shape as a timesheet entry. The admin verifies the row against the contract's `notes` during PR review.

> **Future improvement.** A heavier variant — admin pre-declares a `milestones[]` schedule in the contract, contractor picks from a dropdown, parser auto-prevents double-claims — was considered and deferred. The pre-defined schedule gives source-of-truth payment data, automatic total-contract-value tracking, and machine-enforced double-claim prevention, at the cost of admin setup time and rigidity. Worth revisiting when the broader admin infrastructure ([QuantEcon/admin#5](https://github.com/QuantEcon/admin/issues/5)) adds a centralized contract data store, where milestone schedules become structured data the admin tooling can manage.

**Contract ID convention.** QuantEcon uses `QE-{PAYER}-YYYY-NNN`:
- `QE` — QuantEcon
- `{PAYER}` — paying entity (`PSL` for PSL Foundation, others as needed)
- `YYYY` — contract year
- `NNN` — sequential within year, zero-padded

The system doesn't enforce this format (it accepts any string), but the onboarding script will pre-fill it as the default when creating new contracts.

One file per contract. To renew, the admin copies an existing contract file, edits the dates and rate (or milestone schedule), gives it a new `contract_id`, and marks the old one `ended`.

**Currency handling:** each contract specifies its own currency. Supported ISO 4217 codes in v1: `AUD`, `USD`, `JPY`. The Typst template renders amounts with the ISO code as a suffix (e.g. `45.00 AUD`, `30.00 USD`, `77000 JPY`) — clean and unambiguous, no symbol conventions. `JPY` is rendered without decimal places; `AUD` and `USD` use two. Other ISO codes can be added when a real contractor needs one.

### 4.3 Submission forms — overview

Each contractor repo exposes three issue-template options on the "New Issue" page:

| Template | Filename | Filed against | What it claims |
|---|---|---|---|
| 📋 Hourly Timesheet | `hourly-timesheet.yml` | Hourly contract | Hours worked in a month |
| 🎯 Milestone Invoice | `milestone-invoice.yml` | Milestone contract | A specific milestone delivered |
| 🧾 Reimbursement Claim | `reimbursement-claim.yml` | Contractor (no contract) | Out-of-pocket expenses |

GitHub renders each YAML template as a web form on the "New Issue" page; on submit, GitHub serialises the field values into the issue body as markdown. `scripts/parse_issue.py` then parses that markdown into a structured submission YAML (§4.7).

All three follow the same engine flow: form submitted → parser runs → PR opened with structured YAML + PDF + PNG preview → admin reviews and merges → ledger updated + payments-manager notified (Phase 2).

### 4.4 `.github/ISSUE_TEMPLATE/hourly-timesheet.yml` — Hourly Timesheet form

The interface contractors interact with. GitHub renders this YAML as a web form on the "New Issue" page; on submit, GitHub serialises the field values into the issue body as markdown. `scripts/parse_issue.py` then parses that markdown into a structured submission YAML.

**Form fields:**

1. **Contract** (dropdown, required) — populated with the contractor's active contract IDs. Onboarding script writes the initial list; admin edits the list when a contract is renewed.
2. **Year** (dropdown, required) — 4-digit year. Short list (~3-4 entries: current year ± a year for back/forward catch-up); admin appends one entry when the year rolls over.
3. **Month** (dropdown, required) — static list `01 — January` through `12 — December`. Parser takes the leading two digits; the friendly suffix is for readability.
4. **Time Entries** (textarea, required) — **one row per day worked**, pipe-delimited `YYYY-MM-DD | hours | description`. Variable rows: contractor only enters days they actually worked, not a fixed grid of 30 rows.
5. **Additional notes** (textarea, optional) — free text.
6. **Confirmation** (checkbox, required) — single ack of accuracy.

The `period` is computed as `{year}-{month[:2]}` by the parser (e.g. `2026-07`). Splitting Year and Month means the admin only edits the Year list annually, not the full twelve-row Period list. Same pattern across all three submission forms (§4.6, §4.7).

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
        - QE-PSL-2026-001   # populated by onboarding/new-contractor.py
    validations:
      required: true

  - type: dropdown
    id: year
    attributes:
      label: Year
      description: Calendar year.
      options:
        - "2025"
        - "2026"
        - "2027"
    validations:
      required: true

  - type: dropdown
    id: month
    attributes:
      label: Month
      description: Calendar month.
      options:
        - "01 — January"
        - "02 — February"
        - "03 — March"
        - "04 — April"
        - "05 — May"
        - "06 — June"
        - "07 — July"
        - "08 — August"
        - "09 — September"
        - "10 — October"
        - "11 — November"
        - "12 — December"
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
        2026-04-06 | 3.5 | NumPy lecture exercises review
        2026-04-13 | 5.0 | Plotting examples
        2026-04-20 | 4.0 | CI pipeline fixes
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
    url: https://github.com/QuantEcon/contractor-payments/blob/main/docs/CONTRACTOR_GUIDE.md
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

### 4.5 Submission validation and failure handling

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

### 4.6 `.github/ISSUE_TEMPLATE/milestone-invoice.yml` — Milestone Invoice form

For contractors on a milestone contract. The contract is lightweight metadata (§4.2); the contractor enters the milestone row themselves at submission time — same UX shape as a timesheet entry, with `Hours` replaced by `Amount` and one row per milestone claimed.

**Form fields:**

1. **Contract** (dropdown, required) — populated with the contractor's active *milestone* contract IDs (hourly contracts excluded).
2. **Year** + **Month** (dropdowns, both required) — same two-dropdown pattern as §4.4. Parser combines them to `YYYY-MM`.
3. **Milestone entries** (textarea, required) — **one row per milestone claimed**, pipe-delimited `ID | YYYY-MM-DD | amount | description`. Typically a single row (one milestone per submission); multi-row supported for **catch-up submissions** when an RA forgot to file for a prior month, or for the rare case of two milestones delivered in one period. The `ID` is the milestone number from the contract's schedule (e.g. `3` for "Payment 3 of 6") — the contractor reads it off the contract's `notes` (§4.2) and types it in. Currency is fixed by the contract.
4. **Additional notes** (textarea, optional) — free text.
5. **Confirmation** (checkbox, required) — single ack.

**Parser tolerances** — same lenient rules as timesheet rows:
- Date formats: `YYYY-MM-DD` canonical; `YYYY/MM/DD` and `DD-MM-YYYY` accepted when unambiguous.
- Delimiters: `|` canonical; `,` or tab accepted with a non-blocking warning.
- Description may contain `|` — parser splits on first three pipes.
- ID is a free-form string (typically an integer like `3`, but the system accepts any token).

**Parser must reject:**

- Date that can't be parsed.
- Date outside the selected `Period` is **allowed without warning** for milestone submissions — catch-up cases legitimately reference dates from prior months. (Contrast with timesheets, where out-of-period dates are rejected.)
- Amount ≤ 0.
- Missing fields (fewer than four pipe-separated segments).
- Duplicate `ID` within the same submission.

**Admin responsibility on review.** Because the contract doesn't enumerate milestones, the admin verifies during PR review that: (a) the amount matches the contract's stated schedule (in `contract.notes`), (b) this milestone hasn't already been claimed in a prior submission. The merged ledger (`ledger/{contract_id}.yml`) is the cumulative record to check against.

**Submission ID:** `{handle}-invoice-{period}` (e.g. `mmcky-invoice-2025-11`). Period-based for consistency with timesheets; collision suffix `-vN` applies the same way (§v1.1).

### 4.7 `.github/ISSUE_TEMPLATE/reimbursement-claim.yml` — Reimbursement Claim form

Filed against the **contractor**, not a specific contract. RAs and staff under a contract submit reimbursements here for out-of-pocket expenses (travel, equipment, software, etc.). Approval is per-claim via the standard PR review flow — there is no pre-authorization in a contract.

A single reimbursement claim covers one **period** (calendar month) and may bundle multiple line items incurred on different dates within that month — e.g. one trip with flight + hotel + meals across four days.

**Form fields:**

1. **Year** + **Month** (dropdowns, both required) — same two-dropdown pattern as §4.4. Parser combines them to `YYYY-MM`. The period is the month the claim is *filed against*, not necessarily when the expense was incurred (though usually the same month).
2. **Line items** (textarea, required) — one row per receipt, pipe-delimited: `YYYY-MM-DD | amount | category | description`. Currency is fixed for the submission (see field 3); per-line currency mixing is out of scope in v1. Categories must match the contractor repo's `config/settings.yml` allowed list.
3. **Currency** (dropdown, required) — ISO 4217 code; same supported list as contracts (`AUD | USD | JPY` in v1, extensible). One currency per submission.
4. **Total amount** (number, required) — contractor enters the total; parser verifies it matches the sum of line-item amounts (rejects on mismatch as a sanity check).
5. **Trip / project context** (textarea, optional) — free text. Useful for trips where line items don't individually justify their purpose.
6. **Receipts** — see "Receipt storage" below.
7. **Confirmation** (checkbox, required) — single ack.

**Receipt storage — DEFERRED.** Where receipts physically live (GitHub issue attachments? Committed PDFs in `receipts/<period>/`? External store?) is an open decision (see §10). The Reimbursement engine ships in **Phase 5** (post-launch — see §8) *after* this decision is made and the multi-currency design is settled; the form schema above will pick up a `receipts:` field and a per-line-item `currency` column then.

**Parser must reject:**

- Any line-item date outside the selected `Period` (warn rather than reject if a trip legitimately spans a month boundary — exact policy decided during build).
- Sum of line items ≠ stated total.
- Empty line items.
- Currency not in supported list.
- Category not in the contractor repo's allowed list.

**Submission ID:** `{handle}-reimbursement-{period}` (e.g. `mmcky-reimbursement-2025-09`). Collision suffix `-vN` applies if a second reimbursement is filed for the same month — a legitimate case (multiple trips in one month), distinct from revisions.

### 4.8 Generic submission YAML shape

After parsing, all three submission types persist to `submissions/{period}/{submission_id}.yml`. The engine layer (PDF render, ledger update, payments-manager notify) consumes a common shape with a `type` discriminator:

```yaml
# Common fields (all types)
submission_id: <handle>-<type>-<period>[-vN]
type: hourly | milestone_invoice | reimbursement
period: YYYY-MM
submitted_date: YYYY-MM-DD       # in payer's timezone (§9)
submitted_by: <github-handle>
issue_number: <int>
status: pending | approved | superseded
approved_by: <github-handle | null>
approved_date: YYYY-MM-DD | null

# Type-specific blocks (exactly one of the following groups present)

# --- hourly ---
contract_id: ...
entries:
  - {date: ..., hours: ..., description: ...}
totals:
  hours: ...
  rate: ...
  amount: ...
  currency: ...

# --- milestone_invoice ---
contract_id: ...
entries:
  - {id: ..., date: ..., amount: ..., description: ...}
totals:
  amount: ...       # sum of entries[].amount
  currency: ...     # from contract

# --- reimbursement ---
# contract_id intentionally absent — reimbursements are contractor-level
line_items:
  - {date: ..., amount: ..., category: ..., description: ..., receipt: <path>}
trip_context: |
  ...
totals:
  amount: ...       # sum of line_items[].amount
  currency: ...     # one currency per submission
```

This shared shape means Phase 2 merge processing — ledger update, approval re-render, payments-manager notify — runs the same pipeline for all three types, branching only at the render-template selection and the per-type ledger writer.

---

## 5. Onboarding — `onboarding/new-contractor.py`

A single interactive Python script. Stdlib `argparse` + `pyyaml` + `subprocess` to `gh`. Run from a clone of `QuantEcon/contractor-payments`.

### What it does

1. Prompts for (with reasonable defaults where applicable):
   - GitHub handle of the new contractor
   - Real name
   - Email
   - Payments manager GitHub handle (defaulted from a config or prior run)
   - First contract: type (hourly | milestone), start date, end date, rate (hourly) or schedule notes (milestone), **currency** (AUD / USD / JPY; validates against the v1 supported list), project name
2. Creates `QuantEcon/contractor-{handle}` as a private repo.
3. Seeds the repo from `contractor-template/`, substituting prompted values into `config/settings.yml`, `README.md`, `CODEOWNERS`, and the contract YAML. Both `hourly-timesheet.yml` and `milestone-invoice.yml` issue templates are seeded unconditionally; the contractor's "New Issue" page surfaces whichever ones the dropdowns aren't empty for (which is governed by the contract types they have).
4. Generates `contracts/{contract-id}.yml` from the prompted contract details.
5. Adds the contractor (Write), admin (Admin), and payments manager (Read) as collaborators via `gh api`.
6. Sets branch protection on `main` (PR required, 1 review).
7. **Creates the workflow labels** via `gh label create` (idempotent — skips any that already exist). Required because GitHub Issue Forms silently drop `labels:` values that don't exist on the repo, which would break the workflow's label-based routing:
   - `timesheet` — applied by the Hourly Timesheet form
   - `milestone-invoice` — applied by the Milestone Invoice form
   - `pending-review` — applied by both submission forms
   - `parse-error` — applied by the workflow on parse failure
   - `submission` — applied by the workflow when opening the submission PR
   - `processed` — applied by Phase 2 merge processing
   - (Phase 5) `reimbursement` — applied by the Reimbursement Claim form, added when Phase 5 lands
8. Pushes the initial commit.
9. Prints the contractor-facing URL and next steps.

**Phase 5 will add a multi-select** for which issue templates to seed (Hourly Timesheet / Milestone Invoice / Reimbursement Claim), letting the admin configure reimbursement-only payees or any other subset. Until then, the script seeds both Phase 1/1.5 templates by default.

### What it does **not** do

- No contract PDF generation (contracts are YAML metadata; no signed PDF in v1).
- No contract renewal / end automation — admin edits YAML by hand.
- No central record of which contractors exist (you can `gh repo list QuantEcon --topic contractor` if you tag the repos, or list `contractor-*` repos via `gh repo list`).
- No batch operations or template re-sync — when workflows in `QuantEcon/contractor-payments` change, contractor repos that reference them via reusable workflows pick up the change automatically. Files copied from `contractor-template/` are only re-synced manually if needed.

### Implementation notes

- Idempotent for re-runs: if the repo already exists, the script reports and exits non-zero rather than overwriting.
- Substitution uses stdlib `string.Template`.
- All GitHub operations use `gh` CLI subprocess calls; no Python GitHub libraries.

---

## 6. v1 scope

| Decision | Choice | Notes |
|---|---|---|
| Submission types | Hourly Timesheet, Milestone Invoice, Reimbursement Claim | All three planned architecturally (§4.3). Phase 1–4 ship Hourly + Milestone; Reimbursement deferred to **Phase 5 (post-launch)** because of multi-currency complexity and the receipt-storage open question (§10). |
| Per-contractor repo name | `QuantEcon/contractor-{github-handle}` | Future-proof for other contractor artefacts. |
| Contract data | Plaintext YAML in each contractor's repo | Admin-edited by hand. |
| Contract ID convention | `QE-PSL-YYYY-NNN` | Documented in §4.2. System accepts any string; onboarding pre-fills this format. |
| Contract listing on issue form | Static dropdown in the form YAML | Onboarding script seeds the initial list; admin edits on contract renewal. |
| Submission ID | `{handle}-timesheet-{period}` with `-v2`, `-v3` collision suffix | Period-based for readability; suffix handles re-submissions for the same period. v1.1 polish layer for explicit revision metadata (§8). |
| Approval notification | Email to PSL (Cc the QuantEcon reviewer) via SMTP with the approved PDF attached, plus an internal GitHub comment confirming the send. | PSL doesn't use GitHub; email is the natural delivery channel. The GitHub comment is verbose by design — gives admins operational visibility and confirms the email step ran. `fiscal-host.yml.notifications.testing_mode` flag gates PSL while we iterate (`vars.QUANTECON_EMAIL_REVIEWER` only during testing). |
| PDF generation | Typst in CI; rendered at PR-creation, regenerated at merge with approval metadata | Committed to `generated_pdfs/<YYYY-MM>/`. PR carries a "PENDING REVIEW" PDF; merge replaces it with the approved version. |
| PNG preview | Same template rendered to PNG; committed alongside the PDF | Embedded inline in the PR body via absolute raw URL so reviewers see the artifact in the PR description without leaving the review surface. Default 200 PPI, `--png-ppi` overrides. |
| Fiscal-host identity & policy | `templates/fiscal-host.yml` (engine repo) — PSL Foundation address + timezone + email notification recipients; QuantEcon logo-only (no address). | Single source of truth across all contractor repos. Holds *all* fiscal-host config that's identical across payees. |
| Document issue dates | Computed in the **payer's timezone** (`psl_foundation.timezone` in `fiscal-host.yml`, default `America/New_York`) | All contractors' submission/approval dates use the same locale as the payer's books, regardless of where the contractor lives. Falls back to UTC if the field is unset. |
| Contractor address | Optional `contractor.address` in `settings.yml` (multi-line) | Renders on the PDF only when populated. Recommended for tax-invoice compliance. |
| Ledger / running totals | Yes | One `ledger/<contract-id>.yml` per contract; updated on merge. |
| Onboarding | Interactive Python script | See §5. |
| Encryption at rest | None | Each repo is one contractor; blast radius is naturally scoped. |
| Receipt storage | Decision pending (§10) | Required before Reimbursement engine (Phase 5, post-launch) ships. Options on the table: GitHub issue attachments, committed PDFs in `receipts/<period>/`, or external store. |
| Currency | Per-contract; AUD, USD, JPY supported in v1 | Specified in each contract YAML (§4.2). JPY rendered without decimals; AUD/USD with two. ISO code as suffix, no symbols. |
| Cross-contractor reporting | Out of scope | Captured in the broader admin infrastructure issue. |

---

## 7. Workflow in practice

### 7.1 Contractor submitting a timesheet

1. Contractor opens `github.com/QuantEcon/contractor-{theirhandle}` (bookmarked).
2. *Issues → New Issue → 📋 Hourly Timesheet → fill out form → submit.*
3. `issue-to-pr.yml` parses the form, writes the submission YAML in `submissions/<YYYY-MM>/`, renders the "PENDING REVIEW" PDF and a PNG preview in `generated_pdfs/<YYYY-MM>/`, opens a PR with the PNG embedded inline in the description.
4. CODEOWNERS auto-requests review from admin. Contractor + admin get notifications.
5. Reviewer sees the PNG inline in the PR; clicks through to the PDF if they want the authoritative artifact.
6. Corrections: contractor edits the PR branch directly, or admin requests changes via PR review.
7. Admin approves and merges.

### 7.2 On merge

1. `process-approved.yml` identifies the merged submission.
2. Updates `ledger/<contract-id>.yml` with the new totals.
3. Re-renders the PDF with approval metadata (`approved_by`, `approved_date` set), replacing the "PENDING REVIEW" version that the PR carried.
4. Comments on the now-closed issue: `@{payments_manager} Approved — {real name} — PDF: <blob URL>`.
5. Applies `processed` label.

### 7.3 Admin onboarding a new contractor

1. `python onboarding/new-contractor.py` — answer the prompts.
2. Script creates the repo, seeds it, adds collaborators, pushes. Prints the URL.
3. Admin sends the repo URL + the docs site (https://quantecon.github.io/contractor-payments/) to the new contractor.

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
- [x] Create `QuantEcon/contractor-payments`
- [x] Tighten `PLAN.md` to v1 scope
- [x] Open broader infrastructure issue in `QuantEcon/admin` ([#5](https://github.com/QuantEcon/admin/issues/5))
- [x] Consistency pass on `PLAN.md`
- [ ] Resolve open items in §10 (payments manager handle, admin handle/team, org-level reusable-workflow setting, runner-minutes budget)

### Phase 1 — Timesheets engine in a single test repo ✅
Built everything against `QuantEcon/contractor-engine-test`. All three flows (valid submission, invalid submission, fix-and-retrigger) verified end-to-end against live GitHub. PDF + PNG preview rendering pulled forward from Phase 2 so reviewers see the actual artifact during PR review.

Engine scripts and templates:
- [x] `scripts/parse_issue.py` + tests — parser with lenient input handling and line-specific errors (§4.3, §4.4)
- [x] `scripts/create_submission_pr.py` + tests — period-based submission IDs with `-vN` collision suffix; renders PDF + PNG; opens PR with the PNG embedded inline in the body
- [x] `scripts/post_error_comment.py` + tests — sentinel-marked error comment on parse failure; updates in place on re-run
- [x] `scripts/generate_pdf.py` — Typst PDF + PNG with currency-aware display formatting; configurable PNG PPI
- [x] `templates/timesheet.typ` — single-page A4 template fitting up to 31 entries (worst-case month)
- [x] `templates/fiscal-host.yml` — PSL Foundation address; single source for both organisations
- [x] `templates/assets/{quantecon,psl-foundation}-logo.png` — branding

Form, workflow, test repo:
- [x] `contractor-template/.github/ISSUE_TEMPLATE/hourly-timesheet.yml` + `config.yml` (§4.3)
- [x] `contractor-template/.github/workflows/issue-to-pr.yml` — non-reusable Phase 1 form; installs Typst 0.13.0 via curl
- [x] `QuantEcon/contractor-engine-test` (private, disposable) seeded with the above + a hand-written `config/settings.yml` and one `contracts/QE-PSL-2026-001.yml`
- [x] End-to-end verified: valid submission → PR with YAML + PDF + PNG; invalid → sentinel error comment + label; edit-to-fix → state cleaned up; revision (same period re-submit) → `-v2` suffix applied

Project scaffold:
- [x] `pyproject.toml`, `.gitignore`, `tests/` (80 unit tests passing across three files)

### Phase 1.5 — Milestone Invoice engine ✅
Adds the second submission type alongside hourly. Engine pieces largely shared with hourly (parser plumbing, PR-creation flow, sentinel error comments).

Engine + form + tests:
- [x] **Contract schema extension** — `type: milestone` (lightweight metadata; admin verifies during PR review per §4.2 decision).
- [x] **`scripts/parse_issue.py`** — auto-detects submission type from issue body; new milestone path parses `ID | YYYY-MM-DD | amount | description` rows. Period dropdown split into Year + Month across all forms.
- [x] **`contractor-template/.github/ISSUE_TEMPLATE/milestone-invoice.yml`** — new form (§4.6).
- [x] **`templates/invoice.typ`** — Typst template (title `QUANTECON INVOICE`; 4-col ID/Date/Amount/Description table; single Amount payable row).
- [x] **`scripts/create_submission_pr.py`** — branches on submission type; submission ID becomes `{handle}-invoice-{period}`; PR gets type-specific label.
- [x] **`scripts/generate_pdf.py`** — selects template by submission type via a registry.
- [x] **`contractor-template/.github/workflows/issue-to-pr.yml`** — routes both `timesheet` and `milestone-invoice` labels through one pipeline (parser auto-detects).
- [x] **`scripts/setup_labels.py`** — idempotent label bootstrap (gap surfaced in this phase: GitHub Issue Forms silently drop unknown labels). Phase 3b's onboarding will call this.
- [x] 107 tests passing (51 hourly parser, 15 milestone parser, +20 misc).
- [x] End-to-end verified: opened a milestone-invoice issue on `contractor-engine-test`, workflow produced a PR with YAML + PDF + PNG, parse-error label cleanup confirmed.

Repo housekeeping:
- [x] Renamed `QuantEcon/timesheets` → `QuantEcon/contractor-payments`. Engine repo URL refs + local clone path updated.

### Phase 3a — Reusable workflows (pulled forward to stop sync drift) ✅
Phase 1.5 surfaced an operational risk: contractor repos carried their own copies of `scripts/` and `templates/`, so engine repo updates didn't propagate automatically. This phase replaced those copies with `workflow_call` references back into `QuantEcon/contractor-payments`, so every push to the engine repo is live on every contractor repo immediately.

- [x] **Engine repo workflow access** — `actions/permissions/access` set to `organization` on `QuantEcon/contractor-payments` so other org repos can call its reusable workflows.
- [x] **Engine repo visibility flipped to public** — required for `actions/checkout` on the engine repo from a contractor repo's workflow (the caller's `GITHUB_TOKEN` is scoped only to itself; a PAT would have added rotation overhead with no real benefit since the engine carries no data). Trade-off accepted; recipient emails now live in org-level Variables instead of committed files (see §9 Email recipient policy).
- [x] **Engine repo: [`.github/workflows/process-submission.yml`](.github/workflows/process-submission.yml)** — `on: workflow_call`. Two checkouts (contractor repo at working dir, engine at `./engine`). Scripts run with `PYTHONPATH=engine` and `--templates-dir engine/templates`.
- [x] **Contractor-template `.github/workflows/issue-to-pr.yml`** — collapsed to a thin caller: `uses: QuantEcon/contractor-payments/.github/workflows/process-submission.yml@main`, passes the `github.event.issue` context, applies the label-gate predicate, `secrets: inherit` for future SMTP credentials.
- [x] **`contractor-engine-test`** — workflow replaced with the thin caller; `scripts/` + `templates/` directories deleted (2,306 lines removed). Engine repo is now the only source of truth.
- [x] **End-to-end verification** — opened issue #13 on `contractor-engine-test` via the new thin caller; workflow ran cleanly through the reusable workflow, opened PR #14 with correct YAML + PDF + PNG.

### Phase 2 — Merge processing + email notify
On PR merge, the engine runs `process-approved.yml` (implemented as a reusable workflow). Designed generic so it covers all in-scope submission types (hourly + milestone); the same pipeline picks up reimbursement when Phase 5 lands.

**Status:** engine code complete. Partial E2E verified through step 6 (ledger-issue refresh); step 7 (email send) is gated on the SMTP credentials in §10. Once the credentials land, one fresh merge will exercise the full pipeline.

Approval re-render + ledger:
- [x] **Re-render PDF + PNG with approval metadata baked in** — `scripts/finalize_approval.py` ([4619305](https://github.com/QuantEcon/contractor-payments/commit/4619305)). Stamps the submission YAML with `status: approved`, `approved_by`, `approved_date` (default: today in fiscal-host timezone), then re-renders PDF + PNG via the existing render functions. The Typst template's existing pending-vs-approved conditional automatically flips the amber "PENDING REVIEW" block to the green "✓ APPROVED — by @... on ..." block.
- [x] **`scripts/update_ledger.py`** ([f348485](https://github.com/QuantEcon/contractor-payments/commit/f348485)) — appends the approved submission to `ledger/<contract-id>.yml`. Branches by type: hourly writes `submissions[]` + `hours_to_date`; milestone writes `claims[]` + `claims_count`. Idempotent against duplicate `submission_id` (raises). Pure file mutation; no external services. 17 unit tests covering both type branches, currency-aware rounding, and the cross-checks.
- [x] **`scripts/update_ledger_issue.py`** ([60bdd9c](https://github.com/QuantEcon/contractor-payments/commit/60bdd9c)) — renders the ledger YAML as a markdown table and edits the pinned GitHub issue in the contractor repo (located via `contract.ledger_issue`). Locked from comments so it stays automation-only. Marker comment `<!-- ledger-issue-marker:<contract-id> -->` in the body for safe identification. Skips with a warning (doesn't fail the workflow) when `ledger_issue` is absent from the contract YAML.

Email delivery to PSL:
- [x] **`scripts/notify_email.py`** ([60bdd9c](https://github.com/QuantEcon/contractor-payments/commit/60bdd9c)) — composes plain-text email body + PDF attachment, sends via stdlib `smtplib` + STARTTLS. Subject: `[QuantEcon] {Type} approved — {Real Name} — {Period} — {Amount} {Currency}`. Recipients: `vars.PSL_EMAIL` (To) + `vars.QUANTECON_EMAIL_REVIEWER` (Cc) when `testing_mode: false`; **`vars.QUANTECON_EMAIL_REVIEWER` only** when `testing_mode: true`. Reply-To set to `secrets.SMTP_FROM` (the payments@ alias) so PSL's "Reply" routes back to the sending mailbox (where the existing label/filter picks it up); "Reply All" additionally reaches the reviewer Cc. Dry-run smoke-tested locally — fixture composes cleanly with all the expected headers + attachment metadata.
- [x] **`scripts/notify_comment.py`** ([60bdd9c](https://github.com/QuantEcon/contractor-payments/commit/60bdd9c)) — posts the audit comment on the now-closed issue confirming approval + ledger update + email send (recipients + send timestamp + `testing_mode` flag). Verbose by design — three-line summary at a glance gives the admin team operational visibility, and surfaces partial failures (e.g. "email not sent — see workflow logs") rather than failing silently.
- [x] **Workflow ordering** — finalize_approval → update_ledger → commit → update_ledger_issue → notify_email → notify_comment → apply `processed` label. The comment runs last and reflects the email outcome via the JSON summary that `notify_email --output-summary` writes for it.
- [x] **`.github/workflows/process-approved.yml`** ([c190514](https://github.com/QuantEcon/contractor-payments/commit/c190514) + [59102f5](https://github.com/QuantEcon/contractor-payments/commit/59102f5) fix) — engine reusable workflow on `workflow_call`. Two checkouts (contractor repo + engine repo at `./engine`), Python + Typst setup, then chains the five scripts. Pushes the re-rendered files + ledger update back to main with `[skip ci]`. Caller workflow lives in `contractor-template/.github/workflows/process-approved.yml`; thin caller filters to merged PRs with the `submission` label.
- [x] **GitHub org-level secrets** — all five SMTP secrets (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`) set on `QuantEcon` (Private repositories visibility). `SMTP_USER=admin@quantecon.org` (the authenticated mailbox), `SMTP_FROM=payments@quantecon.org` (an alias of admin@), `SMTP_PASSWORD` is a dedicated Google app password.

Documentation:
- [x] **`notes/EMAIL_SETUP.md`** ([f9a5550](https://github.com/QuantEcon/contractor-payments/commit/f9a5550) + [4619305](https://github.com/QuantEcon/contractor-payments/commit/4619305)) — Gmail / Google Workspace setup runbook. Three options for the sending identity (alias of existing account / standalone user / Google Group — alias is the recommended path since it matches QuantEcon's actual setup). Walks through 2-Step Verification, dedicated app-password generation, secret population, local smoke test, troubleshooting, Reply-To handling.

End-to-end:
- [x] **Manual one-shot:** initial ledger issue ([#15](https://github.com/QuantEcon/contractor-engine-test/issues/15)) opened on `contractor-engine-test` for contract `QE-IUJ-2025-002` — pinned, locked, label `ledger`, body pre-populated with the empty-state markdown. Issue number written into `contracts/QE-IUJ-2025-002.yml` as `ledger_issue: 15`. (Phase 3b onboarding will automate this for real contractor repos.)
- [x] **Partial E2E verified** — opened test issue #16, workflow created PR #17, admin merge fired `process-approved` ([run 25780661113](https://github.com/QuantEcon/contractor-engine-test/actions/runs/25780661113)). 6 of 7 pipeline steps green: PDF/PNG flipped amber → green, ledger YAML committed, pinned issue #15 auto-refreshed to show the new claim. Email step failed as expected without `SMTP_PASSWORD`; comment + label steps skipped.
- [ ] **Full E2E** — pending SMTP credentials (see §10). Once those are set: open one more test submission + merge → confirm email lands in the `vars.QUANTECON_EMAIL_REVIEWER` mailbox (testing_mode keeps PSL off), audit comment posts on the issue, `processed` label appears on the PR.

---

### 🛑 BREAK — testing phase

Once Phase 3a + Phase 2 are implemented, **stop and test thoroughly** before continuing to Phase 3b. During this phase:

- `notifications.testing_mode` stays **true** — the mailbox referenced by `vars.QUANTECON_EMAIL_REVIEWER` receives all approval emails; `vars.PSL_EMAIL` is never contacted.
- Iterate on email content, subject lines, PDF attachment formatting, edge cases (empty notes, multi-row milestones, currencies, etc.).
- Verify the full loop on `contractor-engine-test`: submit → review → merge → email → comment → ledger → label.
- Decide when to flip `testing_mode: false` — that's the cutover to PSL receiving real emails. Likely done at the start of Phase 4, after at least one full month of internal-only testing.

---

### Phase 3b — Onboarding script for new contractor repos
- [ ] **`onboarding/new-contractor.py`** per §5 — seeds both Hourly Timesheet and Milestone Invoice templates unconditionally; creates the contractor repo; adds collaborators; sets branch protection; creates labels via `scripts/setup_labels.py`. Multi-select for templates deferred to Phase 5.
- [ ] **Opens the initial ledger issue** for the first contract (per §8 Phase 2's `update_ledger_issue.py` design). Pins it to the repo's Issues tab. Locks it from comments. Writes the issue number back into `contracts/<contract-id>.yml` as `ledger_issue: <N>` so the approval workflow can find it. Also covers contract-renewal: a small helper opens a fresh ledger issue and closes the predecessor when a new contract YAML is added.
- [ ] Spin up `QuantEcon/contractor-onboarding-test` via the script; run the full submit → merge loop end-to-end via the reusable workflow.

### Phase 4 — Docs + first real contractors

**Docs site** — MkDocs Material on GitHub Pages, deployed via Actions artifact (no `gh-pages` branch). Public site source lives in `docs/`; internal runbooks live in `notes/`.

- [x] **Scaffold + landing page** (commit [18cd80e](https://github.com/QuantEcon/contractor-payments/commit/18cd80e)) — `mkdocs.yml`, `docs/index.md` placeholder ("guide coming soon"), gh-pages branch workflow. Moved `EMAIL_SETUP.md` from `docs/` to `notes/` so it's not published as part of the public site.
- [x] **Switched to Pages artifact deploy** (commit [4b174ce](https://github.com/QuantEcon/contractor-payments/commit/4b174ce)) — `.github/workflows/docs.yml` uses `actions/upload-pages-artifact` + `actions/deploy-pages`; `gh-pages` branch deleted. Repo Pages source set to "GitHub Actions". Site live at https://quantecon.github.io/contractor-payments/.
- [ ] **Contractor guide pages** (under `docs/contractor-guide/`):
  - [ ] `submit-timesheet.md` — hourly timesheet walk-through with screenshots
  - [ ] `submit-invoice.md` — milestone invoice walk-through with screenshots
  - [ ] `corrections.md` — how to amend a submission before/after PR merge
- [ ] **Fix broken doc URLs in `contractor-template/`** (see §10) — `ISSUE_TEMPLATE/config.yml` and the two issue templates currently point at `blob/main/docs/CONTRACTOR_GUIDE.md` which never existed. Repoint at the published site URLs once `submit-timesheet.md` lands.
- [ ] Admin guide — deferred; the admin runbook content can live in `notes/` or as a separate non-public section. Decide before flipping `testing_mode`.
- [ ] Flip `notifications.testing_mode` to `false` — PSL starts receiving real approval emails.
- [ ] Onboard a small number of real contractors; iterate on friction.

### Phase 5 — Reimbursement Claim engine + multi-select onboarding (post-launch)

Deferred to a standalone phase because reimbursements are materially more complex than timesheets and invoices: they involve **multi-currency** receipts (a single trip may produce receipts in 2-3 currencies), **receipt storage** (an unresolved open question — see §10), ad-hoc authorisation (no pre-existing contract to check against), and tax-category handling that varies by jurisdiction. Bundling these into Phase 1.5 / 2 would have slowed the launch; running them as a post-launch addition lets real Phase 4 contractors stress-test the simpler types first.

**Phase 5 build:**

- [ ] **Receipt-storage policy resolved** (where receipts live, PII handling, size limits, multi-page) — see §10.
- [ ] **Multi-currency design** — does a single reimbursement carry multiple currencies (per-line-item currency), or is each currency a separate submission? Decision drives both the form shape and the PDF render.
- [ ] **`scripts/parse_reimbursement_issue.py`** (or branch in `parse_issue.py`) — handles line items with date/amount/category/currency/description; validates totals (per-currency if multi-currency); validates category against `config/settings.yml` allowed list.
- [ ] **`config/settings.yml` extension** — add `reimbursement.allowed_categories: [...]` per contractor.
- [ ] **`contractor-template/.github/ISSUE_TEMPLATE/reimbursement-claim.yml`** — the new form (§4.7), updated for multi-currency.
- [ ] **`templates/reimbursement.typ`** — Typst template (title `QUANTECON REIMBURSEMENT`; line-item table; trip-context block; receipts appendix per policy).
- [ ] **`scripts/create_submission_pr.py`** — extend for the third type.
- [ ] **`onboarding/new-contractor.py`** — add the multi-select for issue templates. From this phase forward, an admin can configure a payee as reimbursement-only (e.g. one-off speakers, honorarium recipients) or as a full contractor with all three types. Also adds the `reimbursement.allowed_categories` prompt.
- [ ] End-to-end test against `contractor-engine-test` (or a new `contractor-reimbursement-test`): submit a reimbursement claim with multiple line items (and multi-currency if that design wins), verify the merge flow.

### v1.1 — Revision / supersede handling (build when first real correction happens)

If an approved (merged) timesheet turns out to be wrong, the right move is to **supersede** the original with a corrected version rather than cancel or rewrite git history. Phase 1 already lays the groundwork — period-based submission IDs with `-v2`, `-v3` collision suffix — so a correction just means opening a new issue for the same period. The collision suffix takes care of the ID.

The v1 baseline: when a correction is needed, admin opens a new issue, the workflow auto-suffixes the submission ID, the second PR carries a corrected YAML + PDF, admin merges. Manual reconciliation otherwise. No special revision detection in code.

v1.1 builds the polish layer once we know what real corrections look like in practice:

- [ ] Detect that a `-vN` submission is a revision: when the `-v2` (or higher) suffix is applied, set `supersedes: <original-id>` in the submission YAML.
- [ ] Render a **"REVISION — supersedes &lt;original-id&gt;"** banner at the top of the PDF when `supersedes` is set.
- [ ] On merge of a revision PR, update the original YAML on `main` with `status: superseded` and `superseded_by: <new-id>`.
- [ ] Auto-comment on the superseded (closed) PR with a link to the revision.
- [ ] Adjust the on-merge notification language for the payments manager: "Revision of earlier submission — please use this version."

Rationale for deferring: corrections are rare; the right workflow shape is informed by real cases (how often, who initiates, before-or-after-payment). The Phase 1 collision suffix handles the rare case without ceremony. We build the polish when there's volume to justify it.

The accounting principle is what governs this: every issued invoice number stays a record, even after correction. Cancellation isn't a thing in good practice — supersession is. Cash-side reconciliation for already-paid invoices stays a manual process outside the timesheet system.

### v2+ (future, in scope of this PLAN if needed)
- SMTP email delivery (currently @-mention only)
- Additional submission types beyond the three planned (none identified yet)

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
| Contract ID convention | `QE-PSL-YYYY-NNN` | QuantEcon's existing numbering scheme. System accepts any string; onboarding pre-fills this format. |
| Shared logic | Reusable workflows + scripts in `QuantEcon/contractor-payments` | Single source of truth. |
| Onboarding | Interactive Python script `onboarding/new-contractor.py` | Asks the right questions; populates the repo cleanly. |
| Submission ID | `{handle}-timesheet-{period}` with `-vN` collision suffix | Period-based for readability; suffix on collision handles revisions. v1.1 layer adds explicit supersede metadata. |
| Notification | Email to PSL (Cc admin) + internal GitHub comment | PSL doesn't use GitHub; email is the natural delivery for the approved PDF. Internal comment is operational audit (confirms the email step succeeded). See §8 Phase 2. |
| Email mechanism | Google Workspace SMTP from a QuantEcon service-account mailbox (sender lives in `secrets.SMTP_USER`/`SMTP_FROM` — see Email credentials row) | QuantEcon already owns the Google Workspace; no third-party transactional service needed at this volume (well under Gmail's 2,000/day limit). Switch to Postmark/Mailgun later if deliverability ever becomes an issue. |
| Email recipient policy | Recipient addresses live as GitHub **org-level Variables** (`vars.PSL_EMAIL`, `vars.QUANTECON_EMAIL_REVIEWER`), not in any file in this (public) engine repo | Engine repo is public for `actions/checkout` access; literal email addresses in committed files would be harvested for spam. Variables keep recipients private without auth/PAT overhead. |
| Email credentials | GitHub **org-level** secrets (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`) | Scoped at the org so every contractor repo's reusable-workflow run can read them without per-repo setup. Never in any YAML; never committed. |
| Email recipients & testing | `templates/fiscal-host.yml.notifications` block with `psl_to`, `quantecon_cc`, and a `testing_mode` flag. While `testing_mode: true` is set, mail goes to `quantecon_cc` only — PSL is never contacted. | Lets us iterate on email content / formatting on `contractor-engine-test` without ever spamming PSL. Single-line flip (`testing_mode: false`) in Phase 4 cuts over to live PSL delivery. |
| v1 submission types | Hourly Timesheet + Milestone Invoice + Reimbursement Claim (all three planned architecturally; phased build per §8) | Engine generic across types from day one; per-type build phases keep scope tight. |
| Milestone contract shape | Lightweight metadata only — contractor enters the milestone row in the form (§4.6), admin verifies against `contract.notes` during PR review | Trivial admin setup; admin already reviews every PR so the eyeball check covers double-claim prevention. Pre-declared `milestones[]` schedule deferred — revisit alongside [admin#5](https://github.com/QuantEcon/admin/issues/5). |
| Reimbursement contract relationship | Reimbursements are **contractor-level**, not contract-level | RA/staff expenses are ad-hoc, hard to pre-authorize in a contract; authorization happens per-claim via PR review. Reimbursements live in the contractor repo without a `contract_id` reference. |
| Issue-template seeding | Phase 3 onboarding seeds both Hourly Timesheet and Milestone Invoice unconditionally. Multi-select (incl. Reimbursement) added in **Phase 5**. | With two templates, all payees get both — multi-select adds friction without benefit. Multi-select lands alongside Reimbursement when the third type makes selectivity meaningful (e.g. reimbursement-only payees). Workflow file is identical across all repos either way — routing is by label, unused branches inert. |
| Engine repo name | `QuantEcon/contractor-payments` | Scope grew beyond timesheets; "contractor-payments" pairs naturally with the `contractor-{handle}` payee repos. Renamed from `QuantEcon/timesheets` during Phase 1.5 alignment. |
| Ledger in v1 | Yes — one `ledger/<contract-id>.yml` per contract; one pinned GitHub issue per contract as the consumption surface | YAML stays the structured source of truth; the auto-updated pinned issue gives contractor + admin a discoverable, notification-driven view of running totals without any new UI. Cheap to maintain (rendered from YAML), no backfill problem. Cross-contractor reporting and dashboards remain post-launch territory (see [QuantEcon/admin#5](https://github.com/QuantEcon/admin/issues/5)). |
| Encryption at rest | None | Each repo holds one contractor's data; access is naturally scoped. Revisit if a centralized store is later built. |
| Currency | Per-contract field; AUD / USD / JPY in v1 | QuantEcon already has real contractors in all three. Currency lives on each contract; PDF renders ISO code as suffix, no symbols; JPY without decimals. |
| Reviewer-facing artifact | PDF (authoritative) + PNG preview (inline in PR body) | GitHub doesn't render PDFs in PR diffs; images do. PNG embed closes the review loop without leaving the PR; PDF is what the payments manager receives. |
| Fiscal-host identity & policy file | `templates/fiscal-host.yml` (engine repo) — renamed from `branding.yml` once it grew beyond addresses to also hold the document-date timezone and email notification recipients. | "Fiscal host" precisely names PSL's relationship to QuantEcon (sponsored-project / fiscal-sponsorship context). Single source of truth across all contractor repos. |
| Document-date timezone | Payer's locale (`psl_foundation.timezone` in `fiscal-host.yml`, default `America/New_York`) | Paperwork lines up with payer's books; contractor locale irrelevant. UTC fallback if unset. |
| Contractor address | Optional `contractor.address` in `settings.yml` (multi-line) | Recommended for tax-invoice compliance; renders only when populated. No bank/tax-ID data ever — that policy carries through from earlier. |
| External Actions | None on the financial-data path | Inherited from source issue. |

---

## 10. Open items

- **Admin handle(s).** Just `mmcky`, or also a team handle?
- ~~**Org settings — reusable workflows in private repos.**~~ ✅ Resolved during Phase 3a (commit [461d24a](https://github.com/QuantEcon/contractor-payments/commit/461d24a)): set `actions/permissions/access=organization` on the engine repo, and flipped engine repo visibility to public so `actions/checkout` works without a PAT. See §9 Email recipient policy for the recipient-address handling that the public-visibility decision drove.
- **Actions on private repos / runner-minute budget.** Confirm enabled + headroom.
- ~~**SMTP credentials for the QuantEcon service-account mailbox**~~ ✅ Resolved: all five org secrets (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`) set on `QuantEcon`. Sending identity is `payments@quantecon.org` (alias of `admin@quantecon.org`); authentication uses a dedicated Google app password on admin@. Unblocks the full Phase 2 E2E.
- ~~**Org-level recipient variables.**~~ ✅ Resolved: `vars.PSL_EMAIL` (PSL Foundation contact) and `vars.QUANTECON_EMAIL_REVIEWER` (QuantEcon human reviewer/approver, Cc) are set as org-level Variables on `QuantEcon`. Visibility: Private repositories.
- **Real-name surfacing.** Mitigation for the payments manager being unable to map GitHub handles → real names: every PDF and notification email surfaces the contractor's real name from `settings.yml`.
- **Broken doc URLs in `contractor-template/`.** `contractor-template/.github/ISSUE_TEMPLATE/config.yml` and the "Need help?" link inside both `hourly-timesheet.yml` and `milestone-invoice.yml` point at `blob/main/docs/CONTRACTOR_GUIDE.md` — a path that never existed and won't, since the guide is now a published MkDocs site. Repoint at `https://quantecon.github.io/contractor-payments/contractor-guide/submit-timesheet/` (and the invoice equivalent) once those pages land in Phase 4. Tracked in the Phase 4 task list.
- **Receipt storage for Reimbursement Claims.** Gates Phase 5 (post-launch). Decision spans: where receipts physically live (committed PDFs in `receipts/<period>/`? GitHub issue attachments? external store?), how PII is handled (card numbers, addresses on the receipt itself), file size and multi-page limits, and how receipts surface in the rendered PDF (inline thumbnails? appendix pages? references only?). The reimbursement form schema (§4.7) and the merge-processing PDF render both depend on this.
- **Multi-currency for Reimbursement Claims.** Also gates Phase 5. A single trip may produce receipts in 2-3 currencies. Decision: per-line-item currency (one submission spans multiple currencies) vs one-currency-per-submission (file separate claims). Drives the form shape, the parser, the PDF render, and the ledger schema for reimbursements.

---

## 11. Security posture

- All contractor repos are private. The engine repo could later be made public as a reference implementation; by design it holds no data.
- No third-party GitHub Actions on any financial-data path. Only `actions/checkout` and `actions/setup-python` from first-party `actions/*`.
- Branch protection on `main` for every contractor repo: PR required, 1 review required, no force-push.
- GitHub Secrets only for credentials the workflow needs. From Phase 2: SMTP credentials (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`) live as **org-level** secrets on `QuantEcon`, so every contractor repo's reusable-workflow run reads them without per-repo setup. Never in any YAML; never committed.
- **Email content is sensitive.** Approval emails carry the contractor's real name, contract ID, period, amount, and an attached PDF with the same data. In transit: TLS via SMTP submission port 587. At rest: in the PSL recipient's inbox + Cc on the QuantEcon admin mailbox. We accept this — the recipient is the fiscal host, the email is what triggers payment, and there's no way to deliver value to PSL without the data being present at the receiving end. The `notifications.testing_mode` flag in `templates/fiscal-host.yml` keeps PSL off the recipient list until we're confident the pipeline is working cleanly (see §8 BREAK).
- **No email addresses in committed files.** The engine repo is public to allow `actions/checkout` from contractor repos. Recipient addresses (`vars.PSL_EMAIL`, `vars.QUANTECON_EMAIL_REVIEWER`) and sender credentials (`secrets.SMTP_*`) are stored only as GitHub org-level Variables / Secrets — never committed. Git history was scrubbed of pre-policy literal addresses via `git filter-repo` at the introduction of this rule.
- Python deps minimal: stdlib + `pyyaml`.
- **No bank accounts, tax IDs, or other payment credentials in any repo.** Reference an external store (1Password, etc.) by stable ID if needed.

---

## 12. Working notes

- Local working dirs:
  - `/Users/mmcky/work/quantecon/contractor-payments/` (engine, this repo)
  - `/Users/mmcky/work/quantecon/contractor-engine-test/` (Phase 1/2 test repo)
  - `/Users/mmcky/work/quantecon/contractor-onboarding-test/` (Phase 3 — not yet created)
  - `/Users/mmcky/work/quantecon/contractor-{handle}/` (real contractors, post-Phase 4)
- Local toolchain: `typst` (`brew install typst`), Python 3.12+, `gh` CLI, `pypdf` (dev — used to assert single-page output in worst-case tests).
- Running the engine locally:
  - Tests: `pytest tests/` (80 cases, ~0.1s).
  - Render a PDF from a submission YAML: `python -m scripts.generate_pdf --submission ... --settings ... --templates templates --output ...`.
  - Engine scripts assume the module-form invocation (`python -m scripts.create_submission_pr ...`) because `create_submission_pr` imports from `scripts.generate_pdf`.
- This `PLAN.md` is the source of truth for the project plan. Update it in PRs as decisions evolve.
