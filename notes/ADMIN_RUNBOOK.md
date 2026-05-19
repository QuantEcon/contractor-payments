# Admin Runbook — Operational scenarios

Day-to-day operational reference for the human running the
contractor-payments system. **Internal only — not published.** The
companion contractor-facing guide will live at
`docs/contractor-guide/` (Phase 4).

> **Status of this doc:** living draft. Each scenario captures both
> *what currently happens* and *whether the engine handles it well*.
> The "Dev notes" callouts mark places where the current engine has a
> gap and a development adjustment may be warranted. Discuss before
> implementing.

---

## At a glance

| Scenario | Engine handles cleanly? | Dev gap? |
|---|---|---|
| [1. Typical month](#1-typical-month) | ✅ | — |
| [2. Resubmission before merge (contractor edits issue)](#2-resubmission-before-merge--contractor-edits-the-issue) | ✅ | — |
| [3. Admin requests changes via PR review](#3-admin-requests-changes-via-pr-review) | ⚠️ Partial | Convention documented (Path A is the supported flow) |
| [4. Post-merge correction — revision or independent second invoice](#4-post-merge-correction--revision-or-supplemental) | ✅ | Two-mechanism model implemented + E2E verified (Phase 2.5) |
| [5. PR closed without merging](#5-pr-closed-without-merging) | ⚠️ Partial | Minor |
| [6. Workflow failure mid-pipeline](#6-workflow-failure-mid-pipeline) | ⚠️ Partial | Yes — no targeted re-run |
| [7. Contract end / renewal](#7-contract-end--renewal) | ⚠️ Partial | Minor — no date enforcement |
| [8. Wrong currency / contract mismatch](#8-wrong-currency--contract-mismatch) | ✅ | — |
| [9. Concurrent submissions](#9-concurrent-submissions) | ⚠️ Rare race | Low priority |

---

## 1. Typical month

**Situation.** Contractor submits a timesheet or milestone invoice for
the most recent completed period. No edits, no errors.

**What happens (the golden path).** Phase 3c (deferred submission) flow:

1. Contractor opens a GitHub issue via the form
   (`hourly-timesheet.yml` or `milestone-invoice.yml`). The issue is a
   **draft** — no workflow fires, no PR opens.
2. Contractor edits the issue body over the period to add entries (one
   row per day worked for timesheets, one row per milestone for
   invoices).
3. Optionally: contractor comments `/validate` to check that their
   entries parse cleanly. The engine posts a sentinel-marked comment
   with computed totals (hours × rate, or amount × N). No PR opens.
   `/validate` can be re-run as many times as needed; the same comment
   updates in place.
4. When ready, contractor comments `/submit` (or applies the `submit`
   label).
5. `process-submission.yml` fires within ~30s. Parses the issue body,
   generates `submissions/YYYY-MM/<id>.yml`, renders a PDF + PNG
   preview, opens a PR with the PNG embedded inline, **closes and
   locks the originating issue** with a handoff comment linking to the
   PR (e.g. *"Submission filed — see #32..."*), and removes the
   `submit` label if applied.
6. Admin reviews the PR (visual PDF check via the PNG, sanity-check
   amounts/period/contract).
7. Admin approves + merges the PR.
8. `process-approved.yml` fires on merge:
   - Re-renders the PDF + PNG with approval metadata (amber → green
     banner; approver + date stamped on both).
   - Appends the entry to `ledger/<contract-id>.yml`.
   - Updates the pinned ledger issue with the new running total.
   - Sends approval email (To: PSL when `testing_mode: false`, Cc the
     QuantEcon reviewer; To: reviewer only when `testing_mode: true`).
   - Posts an audit comment on **both** the original submission issue
     and the merged PR.
   - Applies the `processed` label to the PR.
   - Auto-deletes the submission branch.

**Reminders.** On the 1st of each month, `period-reminders.yml` fires
on each contractor repo and posts a sentinel-marked reminder on any
open draft issue whose period has ended without a `/submit`.
Idempotent per (issue, period). Catches contractors who forgot to file.

**Admin actions.**

- Review PR within ~24h to keep contractor experience tight.
- For monthly cadence, expect ~5–10 PRs per month total across all
  contractors.

**Time to PSL inbox.** Sub-minute from merge once `testing_mode: false`.

---

## 2. Resubmission before merge — corrections after `/submit`

**Situation.** Contractor already ran `/submit` (a PR is open), then
noticed an error.

**Phase 3c behaviour.** Once `/submit` fires, the originating issue is
**closed and locked**. Editing the issue body no longer regenerates
the PR (the `issues.edited` trigger was removed in Phase 3c). The PR
is canonical for any corrections.

**Two paths to fix.**

**Path A (recommended) — admin edits the PR branch.** The admin has
write access and can:

1. Check out `submission/issue-N` locally.
2. Edit the submission YAML directly (the `.yml` under `submissions/<period>/`).
3. Re-render the PDF + PNG: `python -m scripts.generate_pdf --submission ... --settings ... --output ...`
4. Commit + push. The PR auto-updates.

Useful for small fixes (a typo in the description, an amount nudged by
one digit) where it's cheaper than a full revision cycle.

**Path B — close the PR, reopen the issue, edit + `/submit`.** Heavier
but cleaner if the contractor wants to redo the whole submission:

1. Admin closes the PR without merging (no `Closes #N` cleanup needed —
   the issue is already closed and locked).
2. Admin unlocks the issue if needed (`gh issue unlock`) and reopens
   it (`gh issue reopen`).
3. Contractor edits the body to fix the errors.
4. Contractor re-runs `/submit`. The engine opens a fresh PR (the old
   branch is force-pushed since no open PR exists on it).

**Admin actions.**

- For most pre-merge corrections, take Path A — it's lower friction.
- If the contractor wants to drive the fix themselves, use Path B.
- Acknowledge any contractor comments on the PR explaining what
  changed.
- Re-review the updated PDF before merging.

**Contractor-facing nuance.** Contractors should know **post-`/submit`
edits go via the PR, not the issue**. The issue is locked as a signal
that the PR is now canonical. This is covered in the
[Corrections and revisions](https://quantecon.github.io/contractor-payments/contractor-guide/corrections/)
guide.

---

## 3. Admin requests changes via PR review

**Situation.** Admin reviewing the PR spots an error and wants the
contractor to fix it.

**What happens (current behaviour).**

There are two paths and they behave differently:

**Path A — admin requests changes; contractor edits the issue body.**
Works cleanly — same as Scenario 2. Workflow re-runs, PR auto-updates.
**This is the recommended path.**

**Path B — admin or contractor edits the YAML directly on the PR branch.**
Possible because both have write access to the branch. **But the PDF
and PNG will NOT auto-regenerate** — the workflow only fires on
`issues.opened` and `issues.edited`, not on push to a submission
branch. The PR ends up with a YAML/PDF mismatch.

> **Dev note — possible adjustment.**
> If we want to support direct YAML edits on the PR branch as a
> first-class path, add a trigger on `push` to `submission/**` branches
> that re-renders the PDF + PNG and force-pushes. Risk: feedback loops
> (the workflow itself pushes; needs `[skip ci]` discipline) and added
> CI minutes. **Recommended for now: don't add it.** Document Path A
> as the only supported correction path; ignore direct branch edits.

**Admin actions.**

- Add a PR review comment listing the corrections needed. Reference
  the original issue (`#N`) so the contractor knows where to make the
  edit.
- Wait for the workflow to re-fire after the contractor edits.
- Re-review and merge.

---

## 4. Post-merge correction — revision or supplemental

**Situation.** A submission has been merged. The email has gone to
PSL. Later, an error or omission is discovered.

> **Engine status.** The two-mechanism model below is **implemented in
> Phase 2.5** (see [PLAN.md §8](../PLAN.md#phase-25--revision--supplemental-handling))
> and E2E-verified on `contractor-engine-test` on 2026-05-18. The
> "Workaround until Phase 2.5 lands" section at the bottom is kept
> for historical reference and as the fallback recovery path if the
> automated flow ever needs to be bypassed.
>
> **One supported-path nuance.** The two triggers (reopen+edit vs.
> new issue) both work, but the cleanest contractor UX is to **just
> edit the closed issue body** — no need to reopen first. The engine
> detects revision intent from filesystem state (is there an approved
> submission for this issue already?), not from the event action.
> Edit-only avoids the dual-event firing of reopen-then-edit.

### The two-mechanism model

The accounting principle: once a document has been issued, it stays a
record. There are two distinct cases:

- **Revision** — the original was wrong; issue a corrected document
  that supersedes it. Original is preserved as evidence but is voided
  in effect.
- **Independent second invoice in the same period** — the original
  was correct for what it covered, and a separate invoice is needed
  for additional work (or a delivered milestone that arrived after
  the first invoice). The two invoices are conceptually independent
  and just happen to share a calendar period.

The trigger between the two cases is the *action* the
contractor / admin takes — different actions signal different intent:

| Trigger | Intent | When to use | Identifier |
|---|---|---|---|
| **Reopen the closed issue + edit body** | **Revision** — supersede the previous version | Post-merge but pre-PSL-payment. Within ~1–4 weeks of merge, before PSL has run their batch. | `{base}-v2`, `-v3`... |
| **Open a new issue for the same period** | **Independent second invoice** — conceptually unrelated to the first; both count | Post-PSL-payment correction (issue a separate invoice for the missed amount, don't try to revise the paid one), OR a normal case of two genuinely separate invoices that happen to share a period (e.g. two milestones delivered in the same month). | `{base}-B`, `-C`, `-D`... (uniqueness suffix only) |

**Key semantic distinction.** Only the revision flow carries
cross-document semantics in the engine (supersede metadata, PDF
banner, cross-comment on the previous PR, email subject prefix). The
`-B`, `-C` suffix is **purely an identifier uniqueness mechanism** —
those invoices are independent records that just happen to share a
period. The engine doesn't track or render any relationship between
them.

**Judgment rule of thumb.** Admin uses the payments@ inbox to gauge
PSL state and pick the right trigger:
- *"Caught the error within ~a week of merge, PSL hasn't paid yet"* →
  **revision** (reopen the issue).
- *"PSL has already replied / paid / it's been over a month"* →
  payment has likely settled → file a separate invoice (**new issue**)
  rather than try to revise a paid invoice.
- *"PSL replied with a query before paying"* → still pre-payment →
  **revision** with the corrections PSL requested.
- *"PSL paid the original; contractor realised they forgot a
  deliverable"* → **new issue** for the missing amount. Original
  stays as paid; the second invoice records what's still owed.
- *"Two milestones delivered in the same month, billed separately"* →
  **new issue** — these are independent invoices that just share a
  period. The `-B` suffix is just for ID uniqueness; no special
  handling.

### What the engine does (Phase 2.5 target behaviour)

**For a revision (reopen-triggered):**

1. Workflow detects `issues.reopened` and identifies the previous
   (merged) PR via the issue's PR cross-reference.
2. Generates new submission `{base}-v2` with `supersedes:
   <previous-id>` stamped in the YAML.
3. PDF renders with a **"REVISION — supersedes <previous-id>"** banner
   at the top.
4. On merge:
   - Ledger removes the superseded entry, appends the new one.
   - Pinned ledger issue refreshes (superseded entries shown struck-through
     for audit trail, with a link to the revision).
   - Comment on the previous (closed) PR: "**Superseded by #{new-pr}**.
     The PDF and audit trail above remain as the record of what was
     originally sent to PSL."
   - Email subject prefix changes to `[QuantEcon] {Type} REVISION
     approved — ...` so PSL spots the correction.

**For an independent second invoice (new-issue-triggered, same period):**

1. Workflow detects collision against committed submissions, applies
   the next unused letter (B, C, D...) **for ID uniqueness only**.
2. Generates `{base}-B` with no special metadata — it's just a normal
   submission with a different suffix.
3. PDF renders normally (no banner — there's nothing to declare).
4. On merge:
   - Ledger appends as a new entry (both `-base` and `-B` count
     independently toward `amount_to_date`).
   - Pinned ledger issue refreshes with both entries visible.
   - No cross-comment between PRs (they're independent invoices).
   - No email subject change.

### Admin actions (Phase 2.5 target — automated)

For a revision:
1. Either you or the contractor reopens the original closed issue.
2. Edit the issue body with the corrected entries.
3. Wait for the workflow to open the `-v2` PR.
4. Review + merge as normal. Engine handles ledger, cross-references,
   email subject.

For an independent second invoice (same period):
1. Either you or the contractor opens a fresh issue for the same
   period.
2. Fill in the new entries (only the new amount, not the total — this
   is a separate invoice, not a rewrite).
3. Review + merge the `-B` PR. Engine handles the unique ID and the
   ledger append; nothing else is special about it.

### Workaround until Phase 2.5 lands

The current engine produces `-v2` for any same-period collision (no
distinction between revision and supplemental). Admin must reconcile
the ledger by hand:

1. Merge the resubmission PR.
2. Decide intent (revision or supplemental).
3. **If revision:** edit `ledger/<contract-id>.yml` to remove the
   superseded entry. Recompute `totals.amount_to_date`,
   `claims_count` / `submissions_count` (and `hours_to_date` for
   hourly).
4. **If independent second invoice:** no ledger edit needed (the
   second entry is already appended, which is the correct outcome).
5. In either case: post a comment manually on the previous PR
   explaining the relationship to the new PR.
6. If you edited the ledger: locally run
   `python -m scripts.update_ledger_issue --ledger ledger/<id>.yml --repo <owner>/<contractor-repo>`
   to refresh the pinned issue, or wait for the next merge to refresh
   it.

### Accounting principle

The original PDF stays in `generated_pdfs/` as the audit record of
what was sent to PSL on the original date. We don't rewrite history.

- Revision: the ledger reflects the **economic** truth (one payment
  for that period); the PDFs reflect the **paperwork** truth (two
  documents issued, second supersedes first). Superseded entries are
  rendered struck-through in the pinned ledger issue with a link to
  the revision, and excluded from the running totals.
- Independent second invoice: the ledger reflects the economic truth
  (two real payments owed); both PDFs are authoritative. No special
  rendering — both entries display normally.

---

## 5. PR closed without merging

**Situation.** Admin decides not to approve the submission as-is and
closes the PR without merging.

**What happens (current behaviour).**

- The PR is marked closed; the `submission/issue-N` branch persists
  (auto-delete only fires on merge, not close).
- The originating issue stays open with the `pending-review` label.
- The submission YAML, PDF, and PNG were never committed to `main`.
- No ledger entry, no email, no audit comment.

> **Dev note — minor gap.**
> Nothing automatically signals to the contractor that their
> submission was rejected. They have to notice the PR was closed and
> the issue stayed open. **Possible adjustments:**
> - Add a workflow trigger on `pull_request.closed` (not merged) that
>   posts a comment on the originating issue: "PR was closed without
>   merging — please address admin feedback and resubmit."
> - Add a `not-approved` or `revisions-requested` label.
> - Auto-delete the abandoned branch.
>
> Low priority — admin should leave a PR review comment explaining
> the decision before closing, which makes the contractor's next step
> clear. Revisit if rejected PRs become common.

**Admin actions.**

- Leave a PR review comment explaining what's wrong and what the
  contractor should do (typically: "please edit the issue body" or
  "please open a new issue for a different period").
- Close the PR.
- The contractor edits the issue (Scenario 2) or opens a fresh issue
  (Scenario 4 / fresh submission).

---

## 6. Workflow failure mid-pipeline

**Situation.** `process-approved.yml` fails on one of its seven steps.
Most common causes:

- SMTP outage / credential rotation (email step fails).
- Transient `gh` API rate limit.
- A `git push` collision (unlikely, but possible if two
  approvals run within seconds).

**What happens (current behaviour).**

Steps run in order: `finalize_approval` → `update_ledger` → commit →
`update_ledger_issue` → `notify_email` → `notify_comment` → apply
label.

If step N fails, steps N+1..7 are skipped. The state at that point is
inconsistent: e.g. if `notify_email` fails, the ledger has been
updated and committed, the pinned issue is refreshed, but no email
was sent and no audit comment was posted.

> **Dev note — gap: no targeted re-run path.**
> The whole workflow is wired to fire on `pull_request.merged`. If it
> fails mid-way, you can't re-run *just* the failed step from the
> GitHub Actions UI in a way that produces correct output — `git push`
> would fail on the already-committed steps, etc.
>
> **Workaround:** re-run individual scripts locally with the right
> args. The scripts are designed to be idempotent where possible:
> - `notify_email` — safe to re-run (sends another email).
> - `notify_comment` — safe to re-run (posts another comment; not
>   idempotent against itself but the duplicate is obvious).
> - `update_ledger` — **NOT safe to re-run**; raises on duplicate
>   `submission_id`.
> - `finalize_approval` — re-running mutates the YAML again; safe but
>   noisy.
>
> **Possible adjustment:** add a `workflow_dispatch` trigger that
> takes a PR number + step name and runs just that step. Useful for
> manual recovery. Defer until we see what failures actually look
> like in practice — the failure modes will shape the right UX.

**Admin actions.**

1. Inspect the failed run in the Actions UI; identify which step
   failed.
2. **If the failure was after the ledger commit** (i.e. ledger is
   updated, pinned issue is refreshed, but email/comment didn't fire):
   - Re-run `notify_email` and `notify_comment` locally with the
     paths from the failed PR.
   - Apply the `processed` label manually via `gh pr edit`.
3. **If the failure was at or before the ledger commit**: the workflow
   can probably be re-run from the Actions UI ("Re-run failed jobs")
   if the underlying cause (e.g. transient API error) is gone. If
   not, fix the cause first.

### Known failure mode — bot push rejected by branch protection

Symptom in the workflow log:

```
remote: error: GH006: Protected branch update failed for refs/heads/main.
remote: - Changes must be made through a pull request.
```

`process-approved.yml` does a `[skip ci]` push to `main` after
re-rendering the PDF + updating the ledger. Legacy branch protection
on `main` rejects this push because the `github-actions[bot]` token
is not a human admin, and legacy protection only offers
all-or-nothing exemption via `enforce_admins`.

**Fix:** the production setup must use an **org-level ruleset** with
the GitHub Actions integration listed as a bypass actor (see PLAN §8
Phase 3b). Repo-level rulesets cannot grant bypass to the
`github-actions` system app — the API rejects with "Actor GitHub
Actions integration must be part of the ruleset source or owner
organization". An org admin sets this up once on `QuantEcon`
targeting `contractor-*` repos; new contractor repos inherit it
automatically.

**Recovery if it fires:** re-run the workflow ("Re-run failed jobs")
after removing/correcting the protection. The pipeline is
re-entrant in this case — `finalize_approval` is idempotent against
already-stamped YAMLs (it re-writes the same content), and the
ledger entry hasn't been committed yet at the push-rejection point.

---

## 7. Contract end / renewal

**Situation.** A contract's `end_date` is approaching or has passed.
A new contract is being set up.

**What happens (current behaviour).**

- **No date-based enforcement.** The engine doesn't validate
  submission `period` against `contract.start_date` / `end_date`.
  Submissions outside the contract window will still parse and create
  a PR. **Admin catches this in PR review.**
- **Renewals**: a new `contracts/<new-id>.yml` is added. The
  contractor's issue-form contract dropdown is **statically defined**
  in `.github/ISSUE_TEMPLATE/{hourly-timesheet,milestone-invoice}.yml`
  — adding a new contract requires editing those forms to include the
  new contract ID in the dropdown.
- **Ledger continuity**: the new contract gets its own ledger file
  and its own pinned ledger issue. The old contract's ledger issue
  stays open (or gets closed by Phase 3b's rollover helper when
  built).

> **Dev note — minor gap.**
> Adding a new contract is a multi-step manual process today:
>   1. Write `contracts/<new-id>.yml`.
>   2. Edit both issue-form dropdowns to include the new ID.
>   3. Open the initial ledger issue manually
>      (`scripts/update_ledger_issue.py` against an empty ledger).
>   4. Write the issue number into `<new-id>.yml` as `ledger_issue:`.
>   5. Optionally close the predecessor's ledger issue with a "this
>      contract has ended, see ledger for `<new-id>`" comment.
>
> Phase 3b's contract-renewal helper is supposed to automate steps
> 2–5. **Adjustment recommended when first real renewal happens:**
> generate the issue-form dropdowns from the `contracts/` directory
> at workflow time, instead of committing the dropdown statically.
> Eliminates step 2 entirely.

**Admin actions.**

- Run through the 5-step process above when a new contract starts.
- When the old contract ends, lock its pinned ledger issue (it's
  already locked from comments via `update_ledger_issue.py`) and
  optionally update its body with a note linking to the new contract.

---

## 8. Wrong currency / contract mismatch

**Situation.** Submission's data doesn't match the contract — e.g.,
contractor entered USD amounts on a contract that's denominated in
AUD; or used `hours` on a milestone contract.

**What happens (current behaviour).**

The parser validates against `contracts/<id>.yml`:

- **Currency.** Determined by the contract, not the form — the
  submission YAML inherits `currency` from the contract. Contractor
  doesn't pick currency; can't get it wrong.
- **Type mismatch (hourly form on a milestone contract or vice
  versa).** Detected: the parser looks up the contract's `type` field
  and refuses if the submission type doesn't match. Sentinel error
  comment posted on the issue with a clear message; `parse-error`
  label applied.
- **Period outside contract window.** Not enforced (see Scenario 7).

**Admin actions.**

- If the contractor sees a parse-error comment, they edit the issue
  to use the correct form. The label drops and the workflow re-runs.

---

## 9. Concurrent submissions

**Situation.** Two issues are opened on the same contractor repo
within seconds of each other (improbable in practice — monthly
cadence, single contractor per repo — but theoretically possible).

**What happens (current behaviour).**

Each issue gets its own workflow run. Each creates a separate branch
(`submission/issue-N`, `submission/issue-N+1`) and a separate PR. No
direct collision on branch names.

**Edge case.** If both submissions are for the **same period** (e.g.
contractor opens two issues for May 2026 by mistake), the `-vN`
suffix logic runs against the committed state of `main`. Since
neither is committed yet (both are mid-flight), both will pick the
same submission ID. The second to commit will get a Git push error
when the workflow tries to add a file that conflicts.

> **Dev note — low-priority race.**
> The race is real but the practical risk is near-zero (monthly
> cadence, single contractor per repo, ~30s workflow runtime). If it
> ever bites, the fix is a small change in `create_submission_pr.py`
> to check open PRs as well as committed state when computing the
> `-vN` suffix. Defer until observed.

**Admin actions.**

- If it happens: close one of the two PRs (Scenario 5) and tell the
  contractor to consolidate into the surviving one (via issue edit,
  Scenario 2).

---

## Open dev questions surfaced by this runbook

Capturing the development implications in one place for triage:

1. ~~**Ledger double-count on revisions (Scenario 4).**~~ ✅ Resolved
   in Phase 2.5 — two-mechanism model implemented and E2E-verified on
   `contractor-engine-test` (2026-05-18). Revisions supersede via
   filesystem-based detection; independent second invoices in the
   same period get a `-{LETTER}` uniqueness suffix with no special
   semantics. See PLAN §8 Phase 2.5 for the verified flow.
2. **No close-without-merge feedback to contractor (Scenario 5).**
   Low priority; revisit if rejected PRs become common.
3. **No targeted re-run for failed workflow step (Scenario 6).**
   Defer until failures are observed and the right UX is clear.
4. **Contract dropdown is statically committed (Scenario 7).** Could
   generate at workflow time from `contracts/`. Adjustment recommended
   when first real renewal hits.
5. **No date-based enforcement on submission period vs. contract
   window (Scenario 7).** Admin catches in PR review. Low priority.
6. **Direct-edit-PR-branch path doesn't re-render PDF (Scenario 3).**
   Decision: document Path A as the only supported correction path,
   don't add `push`-triggered re-render. Capture in contractor guide.
7. **Concurrent same-period submissions (Scenario 9).** Theoretical
   race; defer.
8. ~~**Display of superseded ledger entries in the pinned issue.**~~
   ✅ Decided (2026-05-18): struck-through with a link to the
   revision, and excluded from the running totals. Keeps the audit
   trail discoverable while keeping totals accurate.

Items 1 and 8 are resolved (Phase 2.5). Items 2–7 are quality-of-life
improvements to revisit once we have live operational data; none
block Phase 4.
