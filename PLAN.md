# QuantEcon Timesheets — Implementation Plan

**Status:** Working draft. Iterating before any implementation work begins.
**Source issue:** [QuantEcon/admin#3 — PRJ: QuantEcon Timesheet Management System](https://github.com/QuantEcon/admin/issues/3)

This document supersedes the architecture described in the source issue where it diverges, and records the rationale for those divergences.

---

## 1. Goals

A GitHub-native system that lets QuantEcon research assistants submit timesheets (and later invoices and reimbursements), have them reviewed and approved via PR, and produce a clean PDF + notification for the PSL Foundation payments manager on approval.

Constraints that shape the design:

- Compensation data is sensitive — RAs must not see each others' rates, hours, or totals.
- QuantEcon does not host webapps; static GitHub Pages + GitHub Actions is the only ops surface available.
- RAs are GitHub-familiar; minimal git operations are acceptable.
- Scale: 5–10 active RAs, monthly cadence.
- No third-party GitHub Actions for the financial-data path — supply-chain risk is real here.

---

## 2. Architectural decision — per-RA private repos

The source issue assumes a single shared `timesheets` repo with all RAs as collaborators. We are not doing that: any collaborator in such a repo would see every other RA's contract, hours, hourly rate, ledger totals, and PR review history. That is a confidentiality leak for compensation data and is unacceptable.

Considered alternatives:

| Option | Verdict |
|---|---|
| Single shared repo with all RAs as collaborators | Rejected — leaks all compensation data across RAs |
| Single submission repo + per-RA archive | Rejected — the submission point itself still leaks |
| Custom web application | Rejected — QuantEcon has no webapp ops; over-engineering at 5–10 RAs/month |
| Bot-mediated single repo (form → bot → PR) | Rejected — has the web-app downsides without the upside |
| **Per-RA private repos with shared reusable workflows** | **Selected** |

Per-RA repos give privacy by construction (RA can only access their own repo), preserve every benefit of the GitHub-native approach (free auth, free audit trail, native PR review UX, no hosting), and at 5–10 RAs the onboarding overhead is a single scripted command.

---

## 3. Repository topology

```
QuantEcon/timesheets                  ← this repo (the "core")
├── .github/workflows/                ← reusable workflows referenced by RA repos
│   ├── issue-to-pr.yml               (reusable; called from RA repos)
│   └── process-approved.yml          (reusable; called from RA repos)
├── scripts/                          ← Python scripts checked out at workflow runtime
│   ├── parse_issue.py
│   ├── create_submission_pr.py
│   ├── update_ledger.py
│   ├── generate_pdf.py
│   └── notify.py
├── templates/                        ← Typst PDF templates
│   └── timesheet.typ
├── ra-template/                      ← files copied into each new RA repo on onboarding
│   ├── .github/ISSUE_TEMPLATE/hourly-timesheet.yml
│   ├── .github/workflows/issue-to-pr.yml      (thin caller of the reusable)
│   ├── .github/workflows/process-approved.yml (thin caller of the reusable)
│   ├── .github/CODEOWNERS
│   ├── config/settings.yml
│   ├── contracts/.gitkeep
│   ├── submissions/.gitkeep
│   ├── ledger/.gitkeep
│   ├── generated_pdfs/.gitkeep
│   └── README.md
├── onboarding/
│   └── create_ra_repo.sh             ← script: create QuantEcon/timesheets-{handle} + seed it
├── docs/
│   ├── RA_GUIDE.md                   ← end-user docs for submitting RAs
│   └── ADMIN_GUIDE.md                ← runbook for admin (onboarding, reviewing, troubleshooting)
└── PLAN.md                           ← this file
```

```
QuantEcon/timesheets-{ra-handle}      ← one per RA, private
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   └── hourly-timesheet.yml
│   ├── workflows/
│   │   ├── issue-to-pr.yml           (calls reusable workflow from QuantEcon/timesheets)
│   │   └── process-approved.yml      (calls reusable workflow from QuantEcon/timesheets)
│   └── CODEOWNERS                    (auto-requests admin on every PR)
├── config/settings.yml               (PAYMENTS_MANAGER handle, defaults)
├── contracts/{contract-id}.yml       (this RA's contracts only)
├── submissions/<YYYY-MM>/*.yml       (auto-populated)
├── ledger/{contract-id}.yml          (auto-populated; running totals)
├── generated_pdfs/<YYYY-MM>/*.pdf    (auto-populated on merge)
└── README.md                         (RA-facing how-to)
```

Access control on each RA repo:
- The RA — Write (so they can push edits to their submission PR branches).
- The admin (`mmcky` initially) — Admin.
- The payments manager — Read (so they can see PDFs and get notifications).

---

## 4. v1 scope — what we are shipping first

| Decision | Choice | Notes |
|---|---|---|
| Submission types | Hourly timesheets only | Invoice and reimbursement forms deferred to v2. |
| Contract listing | Static dropdown, hand-maintained per-RA repo | Few contracts per RA; the maintenance is trivial. |
| Approval notification | GitHub notifications (no email/SMTP in v1) | Workflow comments on the closed issue tagging `@PAYMENTS_MANAGER` with a link to the committed PDF blob and the workflow artifact. Real SMTP can be added later without changing anything upstream. |
| PDF generation | Typst, rendered in CI | PDF committed to `generated_pdfs/<YYYY-MM>/` on `main` and also uploaded as a workflow artifact. |
| Ledger / running totals | Included in v1 | One `ledger/{contract-id}.yml` per contract; updated on merge. |
| Receipts | Out of scope for v1 | Timesheets don't need receipts. Reimbursements (v2) will revisit storage. |
| Currency | AUD default | Multi-currency deferred. |
| Cross-RA reporting | Out of scope for v1 | Nice-to-have; can be added later as an admin script that queries the org via the API. |

---

## 5. Workflow in practice

**RA submitting a timesheet:**
1. RA opens `github.com/QuantEcon/timesheets-{theirhandle}` (bookmarked).
2. *Issues → New Issue → 📋 Hourly Timesheet → fill out form → submit.*
3. `issue-to-pr.yml` runs, parses the form into a YAML file in `submissions/<YYYY-MM>/`, opens a PR titled `Submission: [Timesheet] {contractor} - {period}` linking the issue.
4. CODEOWNERS auto-requests review from admin. RA gets a notification; admin gets a notification.
5. If corrections are needed: the RA edits the PR branch directly (they have Write), or the admin requests changes via PR review.
6. Admin approves and merges.

**On merge:**
1. `process-approved.yml` runs, identifies the new submission file in the diff.
2. Updates `ledger/{contract-id}.yml` with the new totals.
3. Renders Typst template → PDF, commits PDF to `generated_pdfs/<YYYY-MM>/` and uploads as a workflow artifact.
4. Comments on the now-closed issue: `@{PAYMENTS_MANAGER} Approved — PDF: <blob URL> · Artifact: <run URL>`.
5. Applies `processed` label.

**Admin onboarding a new RA:**
1. From the core repo: `./onboarding/create_ra_repo.sh {github-handle} {contractor-name}`.
2. Script creates `QuantEcon/timesheets-{handle}`, seeds it from `ra-template/`, adds the RA + payments manager as collaborators, sets branch protection on `main`.
3. Admin hand-writes a contract YAML in `contracts/` of the new repo.
4. Admin sends the RA a link to the repo + the RA guide.

---

## 6. Build phases

### Phase 0 — Planning (now)
- [x] Create `QuantEcon/timesheets` repo
- [x] Draft `PLAN.md`
- [ ] Review and freeze v1 decisions with Matt

### Phase 1 — Core mechanics (single test repo)
Build everything against one disposable test repo first; only generalize to "core + per-RA" once the end-to-end loop works.
- [ ] `scripts/parse_issue.py` — parse Issue Form body into structured YAML
- [ ] `scripts/create_submission_pr.py` — branch + commit + open PR via `gh` CLI
- [ ] `.github/workflows/issue-to-pr.yml` — first version, non-reusable, tested in place
- [ ] Issue Form `hourly-timesheet.yml`
- [ ] End-to-end test: open an issue → PR appears with correct YAML

### Phase 2 — PDF + ledger + notification
- [ ] `templates/timesheet.typ` — Typst template, QuantEcon-branded
- [ ] `scripts/generate_pdf.py` — render Typst with submission data
- [ ] `scripts/update_ledger.py` — running totals
- [ ] `scripts/notify.py` — comment on closed issue, tag `@PAYMENTS_MANAGER`
- [ ] `.github/workflows/process-approved.yml`
- [ ] End-to-end test: merge a PR → PDF in repo + ledger updated + issue commented

### Phase 3 — Generalize to core + per-RA topology
- [ ] Convert both workflows to `workflow_call` reusable form
- [ ] Build `ra-template/` directory with thin caller workflows
- [ ] Build `onboarding/create_ra_repo.sh`
- [ ] Verify private-repo reusable workflow permissions at the org level
- [ ] Dogfood with one real RA repo

### Phase 4 — Docs + dogfooding
- [ ] `docs/RA_GUIDE.md`
- [ ] `docs/ADMIN_GUIDE.md`
- [ ] Onboard one or two real RAs and iterate on friction

### v2 (future)
- Milestone invoice form
- Reimbursement form (revisit receipt storage)
- SMTP email delivery
- Cross-RA aggregator / admin dashboard
- Multi-currency

---

## 7. Resolved decisions

| Decision | Choice | Why |
|---|---|---|
| Repo topology | Per-RA private repos under QuantEcon org | Privacy by construction; matches scale. |
| Shared logic | Reusable workflows + scripts in `QuantEcon/timesheets`, referenced by RA repos | Single source of truth; updates propagate without re-templating. |
| Notification path | GitHub comment + @-mention + artifact | No SMTP integration in v1; revisit if it doesn't suit the payments manager's workflow. |
| v1 submission types | Hourly timesheets only | Prove the loop end-to-end on the simplest case first. |
| Contract listing | Static dropdown in each RA's issue form | Few contracts per RA; cost of maintenance is trivial. |
| Ledger in v1 | Yes | Adding it later means re-running history; cheaper now. |
| Submission method | Issue Form → auto-PR | Inherited from source issue; sound. |
| PDF tooling | Typst | Inherited from source issue; sound. |
| External Actions | None | Inherited from source issue; financial-data path stays in-house. |

---

## 8. Open items

- **Payments manager handle.** Need the GitHub username for the PSL Foundation payments manager so we can wire `PAYMENTS_MANAGER` in `config/settings.yml` and CODEOWNERS. Matt to confirm.
- **Admin handle(s).** Confirm whether the admin reviewer is just `mmcky` or a team handle (e.g. `@QuantEcon/admins`).
- **Org settings — private-repo reusable workflows.** GitHub requires that the source repo (`QuantEcon/timesheets`) explicitly grant access to be called from private workflows in other repos. We'll need an org admin to toggle this once Phase 3 starts.
- **Org settings — Actions on private repos.** Confirm the QuantEcon org has GitHub Actions enabled on private repos and that there is sufficient runner-minutes budget.
- **RA email differentiation.** Matt's concern: the payments manager may not easily map GitHub email aliases to real names for payment processing. Mitigation in v1: the PDF will display the RA's real name (from the contract YAML) prominently, and the notification comment will include the real name too — not just the GitHub handle.
- **Receipt storage policy.** Deferred until v2 introduces reimbursements. Likely options: in-repo (simple but bloats git) vs. external object store.

---

## 9. Security posture

- All RA repos are private. The core repo is also private during development; can be made public later if it's ever useful to share the workflow logic as a reference implementation (no secrets or RA data live in the core repo by design).
- No third-party GitHub Actions in any workflow on the financial-data path. `actions/checkout` and `actions/setup-python` are the only first-party Actions used.
- Branch protection on `main` for every RA repo: PR required, 1 review required, no force-push, no direct push.
- GitHub Secrets used only for credentials the workflow needs (none in v1; SMTP credentials when email is added in v2).
- Python dependencies kept minimal: stdlib + `pyyaml`. No transitive footprint.

---

## 10. Working notes

- Local working directory: `/Users/mmcky/work/quantecon/timesheets/`
- This `PLAN.md` is the source of truth for the project plan. Update it in PRs as decisions evolve; don't let conversation context become the only record.
