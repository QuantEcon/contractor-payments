# Reimbursement claims — design (Phase 5, proposed)

**Status:** proposal for review. Nothing here is built yet.
**Replaces:** the issue-form design in PLAN.md §4.7 and the Phase 5 build
plan in §8, which [PR #4](https://github.com/QuantEcon/contractor-payments/pull/4) implemented and then parked. The full
reasoning is in [`notes/PHASE5-SHELVED.md`](https://github.com/QuantEcon/contractor-payments/blob/phase-5-reimbursement/notes/PHASE5-SHELVED.md) on the
`phase-5-reimbursement` branch.

---

## 1. Summary

Claimants submit expenses through a **Google Form** in QuantEcon's Google
Workspace, attaching a receipt to **each expense line**. Receipts are stored
in an **admin-only Drive folder** and never enter git. The engine turns each
response into a **claim packet**: one PDF with a summary page followed by
every receipt on its own page, captioned with the line it supports. An
admin approves the claim and the packet is emailed to PSL.

The rollout is phased (§10). **Phase 5a** is a command an admin runs per
claim, with no new credentials, repos or automation. At about a dozen claims
a year that may be all that is needed. **Phase 5b** automates it with PR
approval in a private, admin-only repo, reusing the approval checks and email
path the timesheet engine already has.

## 2. Why not GitHub issues

[PR #4](https://github.com/QuantEcon/contractor-payments/pull/4) built reimbursements on the timesheet pattern (issue form → PR → merge)
and measured these problems:

| Problem | Consequence |
|---|---|
| Dragged-in image attachments lose their filenames | PSL receives `03-55070d4e-….png` instead of `receipt-taxi.png` |
| Nothing links a receipt to a line item | Entries sort by date, attachments by drag order; PSL matches them by hand |
| `GITHUB_TOKEN` can't fetch issue attachments (404) | A personal access token is needed, and the workflow runs in the contractor's repo |
| Claims can't be centralised | A shared repo would show every claimant's receipts to every other claimant, in git history |

The underlying mismatch: for timesheets and invoices **the data is the
deliverable**, so GitHub's diff-and-review model fits. For reimbursements
**the evidence is the deliverable**, and receipts are what GitHub handles
worst. Moving contractors to Triage, as proposed in [PR #13](https://github.com/QuantEcon/contractor-payments/pull/13), would
remove the credential-theft objection, but not the other three.

## 3. Goals and constraints

**Goals**

1. PSL receives one self-explanatory PDF per approved claim: every receipt
   labelled with the line it supports, totals computed by the engine.
2. Claimants need no GitHub account, and can submit from a phone camera.
   This covers RAs and one-off payees such as speakers and visitors.
3. Receipts and claimant details are visible only to admins (and PSL).
4. An approval trail comparable to the timesheet flow: who approved, when,
   and exactly what was sent.

**Constraints carried over from PLAN.md**

- QuantEcon runs no web app; Google Workspace and GitHub Actions are the
  only operational surfaces.
- No third-party GitHub Actions on a financial-data path.
- No bank details or tax IDs anywhere in the system.
- The engine repo is public: code only, no claim data.
- Volume is low (about 12 claims a year), so simplicity beats automation.

**Non-goals:** currency conversion, policy enforcement beyond flagging
(per-diem rules, tax treatment), and paying claimants. PSL pays.

## 4. Overview

```
Claimant (no GitHub account)
  └─ Google Form (QuantEcon Workspace, sign-in required)
       claimant + purpose → Expense 1 … Expense N (each with its own receipt upload) → declaration
       Form emails the claimant a copy of their answers
        │
        ▼
Responses Sheet + uploads folder (Drive, owned by the payments account, admins only)
        │
        ▼  5a: admin runs build_packet        5b: scheduled Action in QuantEcon/reimbursements
Claim packet PDF + claim record
        │
        ▼  5a: admin reviews the packet         5b: admin merges the claim PR
Approval → email to PSL (packet attached, Cc reviewer) → ledger → claimant notified
```

## 5. Intake — the Google Form

Owned by the payments account (the mailbox that already sends approval
emails), not a personal account.

**Section 1 — Claimant**

| Field | Type | Notes |
|---|---|---|
| Email | Collected automatically (verified sign-in) | Identity of the claimant |
| Full name | Short answer | As it should appear on the claim |
| Payout currency | Dropdown (the currencies PSL can pay, plus Other) | See §8. **Other** branches to a one-question section asking for the ISO code (below) |
| Purpose | Paragraph | Trip or event and why the expenses were needed |
| Pre-approval reference | Short answer, optional | Required by policy above a threshold (§12) |
| Corrects claim | Short answer, optional | Previous claim ID, for a revision (§9) |

**Sections 2 to 11 — Expense 1 … Expense 10** (one section each, built by
duplicating the first in the Forms editor)

| Field | Type | Notes |
|---|---|---|
| Date | Date | Date on the receipt |
| Category | Dropdown | e.g. travel, accommodation, meals, conference fees, equipment, software, other |
| Description | Short answer | What it was |
| Amount on receipt | Short answer, number validation | In the receipt's currency |
| Receipt currency | Dropdown (common codes, plus Other) | |
| Receipt currency code | Short answer, optional, regex `^[A-Z]{3}$` | Required when the dropdown says Other; `build_packet` errors if it is missing |
| Amount charged in payout currency | Short answer, number validation | From the card or bank statement; equals the receipt amount when currencies match |
| Receipt | File upload, 1–5 files, PDF or image, 10 MB | **This line's** receipt: linkage by construction |
| Add another expense? | Multiple choice, go to section by answer | Yes → next Expense section; No → Declaration |

**Currency codes.** Forms can't show a field conditionally inside a
section, so "Other" is handled two ways. For the payout currency, the answer
branches to a one-question section ("Payout currency code", short answer,
regular-expression validation `^[A-Z]{3}$`) that then continues to Expense 1.
For receipt currencies, each expense section carries an optional code field
with the same validation, which `build_packet` requires whenever the dropdown
says Other. Either way the engine then checks the code against its ISO 4217
table (§7), so a well-formed but non-existent code like `XYZ` is caught
before anything is rendered.

Google Forms has no repeating groups. Branching between duplicated sections
gives the same result for up to ten lines, without code. A claim with more
lines is split across two submissions; at this volume that is acceptable.

**Final section — Declaration:** a required checkbox ("these expenses were
incurred for QuantEcon work, are not claimed elsewhere, and the receipts are
genuine") and optional notes.

**Settings:** require sign-in (file upload needs it); collect verified email
addresses; send responders a copy of their responses; response editing off,
so corrections arrive as a new response that references the old one.

## 6. Storage

- **Responses Sheet** — one row per claim. In 5a the admin adds columns
  for the claim ID, status, approver, approval date, date sent to PSL and a
  packet link, so the Sheet is the 5a ledger.
- **Uploads folder** — where Forms stores uploaded receipts. Shared only with
  admins.
- **Packets folder** — one subfolder per claim (`QE-RC-2026-007/`) holding
  the renamed receipts and the claim packet PDF.
- **Retention** — keep for the period PSL requires (§12), then delete. Drive
  makes this a folder operation; git would make it a history rewrite.

Receipts are never committed to any repository.

## 7. The claim packet

The packet is the deliverable. It replaces "a PDF plus loose attachments".

**Claim ID:** `QE-RC-YYYY-NNN`, sequential within the year, following the
`QE-{PAYER}-YYYY-NNN` contract-ID convention. The next number is the first
one not already used in the packets folder (5a) or the ledger (5b).

**Page 1 (and continuation pages if long)** — rendered by
`templates/reimbursement.typ`, in the same visual language as the timesheet
and invoice templates:

- header: PSL and QuantEcon logos, PSL address, title `QUANTECON REIMBURSEMENT CLAIM`;
- claim ID, submitted date (payer's timezone), claimant name and email,
  purpose, pre-approval reference, project code (assigned by the admin at
  review);
- line table: `# | Date | Category | Description | On receipt | Claimed`,
  where *On receipt* is the original amount and currency and *Claimed* is
  in the payout currency;
- total claimed in the payout currency, computed by the engine (the claimant
  never types a total);
- receipt index: line → renamed file → page number in this packet;
- approval block: amber "PENDING REVIEW" or green "APPROVED BY … ON …",
  as on timesheets; a REVISION banner when the claim corrects another.

**Receipt pages** — for each line, in line order:

- **Image receipts** (PNG, JPEG) are placed one per page by Typst, scaled to
  fit, under a caption bar: `Line 3 of 7 · 2026-11-04 · Taxi · 42.50 AUD on receipt · 42.50 AUD claimed`.
- **PDF receipts** are appended after their caption with pypdf. Preferred:
  the caption is stamped onto each page. Fallback: a one-line caption page
  before the receipt.

**Renamed files:** `{claim_id}-{line:02d}-{date}-{category}.{ext}`, for
example `QE-RC-2026-007-03-2026-11-04-travel.pdf`. Uploaded names are never
trusted.

**Checks while building** (errors stop the packet; warnings are printed on
the review surface and in the packet's index):

| Check | Error or warning |
|---|---|
| File type sniffed from magic bytes is PDF, PNG or JPEG (not the extension) | Error. HEIC from iPhones: convert or reject, see §12 |
| Every line has at least one receipt | Warning (an admin may accept a missing receipt with a reason) |
| Every currency code is a real ISO 4217 code, looked up in a table the engine carries (code → decimal places, e.g. JPY 0, KWD 3) | Error |
| Payout currency is one PSL can pay (a configured list, §12) | Error |
| Amounts positive, within the currency's decimal places (via the Decimal money helper `scripts/money.py` proposed in [PR #13](https://github.com/QuantEcon/contractor-payments/pull/13); 5a extends its decimal-places lookup from "JPY or two" to the ISO table) | Error |
| Category in the allowed list | Warning |
| Date not in the future; not older than the policy limit | Error / warning |
| Receipt currency ≠ payout currency but claimed amount equals receipt amount | Warning (probably not converted) |
| Same receipt file (sha256) used on two lines or in a previous claim | Warning |

## 8. Currency

Each line records **what the receipt says** (amount and currency) and **what
the claimant was charged in their payout currency**. Claims total in the
payout currency only, and the engine never converts currencies. The card or
bank statement is the authority for the claimed amount, which matches how
most reimbursements are paid. If PSL prefers a published rate (for example
the rate on the expense date), the claimed amount becomes computed rather
than entered. Only §5 and §7 change. See §12.

## 9. Approval, notification, corrections

### Phase 5a — manual

1. The Form emails the claimant their answers on submit.
2. The admin opens the Drive folder (Google Drive for desktop syncs it
   locally) and the responses Sheet, then runs:

   ```bash
   python -m reimbursements.build_packet --responses responses.csv --row 7 --uploads "<synced uploads folder>" --out "<synced packets folder>"
   ```

   This assigns the claim ID, renames and type-checks receipts, renders the
   packet (pending) and prints the checks.
3. The admin reviews the packet. To approve, they re-run with
   `--approve --approver <handle>`, which re-renders it as approved. With
   `--send` it also emails PSL (packet attached, Cc reviewer) through the
   existing `notify_email` path, using SMTP credentials from the admin's own
   environment. The admin records the result in the Sheet.
4. To reject or ask for changes, the admin emails the claimant. The claimant
   corrects by submitting a new response with *Corrects claim* set; the new
   packet carries the REVISION banner.

No CI credentials, no new repository, no scheduled jobs.

### Phase 5b — automated, PR approval

A new **private** repository `QuantEcon/reimbursements` whose only
collaborators are admins. It holds claim records (YAML) and the ledger; the
code stays in this public engine repo, reached through a thin caller
workflow, as contractor repos do today.

1. **Bridge** (scheduled hourly, and on demand): reads new Sheet rows,
   downloads receipts from Drive, runs `build_packet`, uploads the pending
   packet to the claim's Drive folder, and opens a PR adding
   `claims/YYYY/QE-RC-YYYY-NNN.yml` (line items, Drive file IDs, sha256s,
   no images) and a summary-page PNG. The PR body shows the line table,
   warnings and a link to the packet in Drive.
2. **Review:** the admin opens the packet from the PR link and checks the
   receipts.
3. **Merge = approval:** the same checks as [PR #13](https://github.com/QuantEcon/contractor-payments/pull/13) (`verify_approval`: the
   merger is in `quantecon.approvers`, the PR touched only `claims/` and
   `previews/`, totals recompute), then: re-render as approved → email PSL
   the packet → append to `ledger/<claimant>.yml` (per payout currency) →
   email the claimant "approved" → update the Sheet row.
4. **Close without merge = rejection:** the closing comment is emailed to the
   claimant.
5. **Revision:** a response with *Corrects claim* produces a PR with
   `supersedes:`; on merge the ledger marks the old entry superseded, using
   the timesheet engine's logic.

`testing_mode` applies exactly as for timesheets: while it's on, emails go
to the reviewer only.

## 10. Rollout

| Phase | Builds | Done when |
|---|---|---|
| **5a** | Google Form; `reimbursements/` package (`build_packet` CLI, packet template with receipt pages, checks); runbook section | One real claim processed end to end and PSL confirms the packet works for them |
| **5b** | `QuantEcon/reimbursements` repo; bridge workflow; Google credential (§11); PR approval; YAML ledger; claimant emails | End to end on a test form: submit → PR → refused non-approver merge → approved merge → testing-mode email |
| **5c** (optional) | A custom form (Apps Script web app, or GitHub Pages posting to Apps Script) with a real line-item table | Only if the Form's section-per-line UX proves a problem |

Move from 5a to 5b when the manual step becomes the bottleneck, or when an
audit trail stronger than the Sheet is needed.

## 11. Security and privacy

- **Receipts** live only in Drive folders shared with admins. Nothing with
  receipt images is committed to git; 5b's summary PNG contains names and
  amounts only, in a repo only admins can read.
- **5a credentials:** none new. Drive access is the admin's own account; email
  uses the admin's own SMTP setup or is sent by hand.
- **5b credentials:** one Google service account with read access to the
  responses Sheet and uploads folder and write access to the packets folder.
  Preferably keyless (GitHub OIDC → Google Workload Identity Federation,
  restricted to `QuantEcon/reimbursements` on `main`, done with plain `curl`
  to respect the no-third-party-Actions rule). Otherwise a JSON key as a
  **repository** secret of `QuantEcon/reimbursements` — **never an org
  secret**, because org secrets visible to private repos are visible to every
  contractor repo.
- **Claimants hold no GitHub access**, so nothing they control runs with these
  credentials.
- **Engine repo** stays public with no data: the packet builder handles
  claimant data only at runtime.

## 12. Open questions

**For PSL**

1. Do they already have an expense-claim process or form? If so, the best
   design may be to feed it rather than replace it.
2. What evidence do they need: itemised receipts only, or also the card or
   bank statement line when currencies differ?
3. Do they accept "amount charged in payout currency" (§8), or require a
   specific exchange-rate source?
4. Retention period for receipts and claim records.
5. Is one PDF per claim the right shape for their books, or would they also
   like a CSV line export?
6. Which payout currencies can they pay? This becomes the configured list
   the payout-currency dropdown and the §7 check use.

**For QuantEcon**

7. Pre-approval threshold for travel, and who grants it.
8. Category list and per-category limits, if any.
9. Whether file upload works for respondents outside the QuantEcon domain:
   uploads need a Google sign-in, and Workspace settings can restrict them
   to internal users. Test before announcing the form.
10. HEIC (iPhone) photos: convert to JPEG in `build_packet`, or ask claimants to
   upload JPEG/PDF.
11. Who owns the Form and folders: the payments mailbox (recommended) or an
    admin's account.

## 13. Reuse

| From | Reused for |
|---|---|
| [PR #4](https://github.com/QuantEcon/contractor-payments/pull/4) (`phase-5-reimbursement`) | `templates/reimbursement.typ` as the summary-page starting point; magic-byte type sniffing from `scripts/fetch_receipts.py`; per-currency ledger shape; category allowlist validation |
| Engine (`main` + [PR #13](https://github.com/QuantEcon/contractor-payments/pull/13), not yet merged) | `scripts/money.py`; `verify_approval` and the approver list; `notify_email` (attachments, testing mode, Reply-To); revision/supersede ledger logic; fiscal-host timezone for dates; Typst toolchain and render tests |
| Not reused | Issue form, attachment-URL parsing and token fetch, per-repo reimbursement config, `sync_templates` form gating |

## 14. Alternatives considered

| Option | Why not (for now) |
|---|---|
| GitHub issues in contractor repos | Measured problems in §2 |
| GitHub Pages form | Pages is static: something still has to hold the credential and receive files, so it needs Apps Script or a Worker behind it. Kept as the 5c upgrade path. |
| Commercial expense tool | Per-user cost and another login for about 12 claims a year; PSL would still want a packet. Revisit at much higher volume, or if PSL already uses one (§12, Q1). |
| Workspace only (Sheet checkbox approval, Apps Script builds the packet) | Simplest to host, but Apps Script can't merge PDFs natively, and approval lives in Sheet history rather than a reviewed PR. Phase 5a is close to this without those weaknesses. |
