# QuantEcon Contractor Payments

GitHub-native engine for processing contractor payment paperwork at QuantEcon:
hourly timesheets, milestone invoices, and (post-launch) reimbursement claims.
Contractors submit via a GitHub Issue Form, an admin reviews via PR, and an
approved PDF is emailed to the fiscal host on merge.

> **Status:** ready to onboard first real contractors. Phases 1, 1.5, 2,
> 2.5, 3a, 3b, 3c shipped; Phase 4 docs + first onboardings in progress.
> Engine handles hourly timesheets and milestone invoices end-to-end, from
> deferred-submission draft through `/submit`, PR review + merge, ledger
> update, and email to PSL Foundation. See [PLAN.md](PLAN.md#at-a-glance)
> for the live phase checklist. Contractor-facing docs at
> <https://quantecon.github.io/contractor-payments/>.

## What this repo is

The **engine** for the system — reusable GitHub Actions workflow, parsing
scripts, Typst PDF templates, and the planning doc. It contains **no contractor
data**.

Each contractor has their own private repo (`QuantEcon/contractor-{handle}`)
that:
- holds their contract YAML, submitted issues, generated PDFs, and ledger;
- runs a thin caller workflow that `uses:` the reusable workflow in this repo.

```
QuantEcon/contractor-payments       ← THIS REPO (public)
  ├── .github/workflows/
  │     └── process-submission.yml  ← workflow_call reusable
  ├── scripts/                      ← parser, PR-creator, PDF renderer
  ├── templates/                    ← Typst PDF templates + fiscal-host config
  └── contractor-template/          ← seed files for new contractor repos

QuantEcon/contractor-{handle}       ← per-contractor (private)
  ├── .github/workflows/
  │     └── issue-to-pr.yml         ← thin caller, references this repo @main
  ├── .github/ISSUE_TEMPLATE/       ← submission forms
  ├── config/settings.yml           ← contractor identity
  ├── contracts/*.yml               ← contract terms
  ├── submissions/<period>/*.yml    ← auto-populated
  └── generated_pdfs/<period>/      ← auto-populated
```

## How a submission flows

1. **Draft.** Contractor opens their private repo → New Issue → picks the
   right submission template (Hourly Timesheet / Milestone Invoice) and fills
   it out. The issue is a *draft* — the contractor edits the body over days
   or weeks as they accumulate entries.
2. **Validate (optional).** Contractor comments `/validate` to check that
   their entries parse cleanly. The engine posts a sentinel-marked comment
   with computed totals — no PR opens.
3. **Submit.** When ready, contractor comments `/submit` (or applies the
   `submit` label). The thin caller workflow fires `process-submission.yml`
   in this repo. Scripts parse the issue body, write a structured submission
   YAML, render PDF + PNG via Typst, open a PR on the contractor's repo
   with the PNG embedded inline, and close+lock the originating issue.
4. **Review.** Admin reviews the PR — verifies the contract reference,
   amounts, description.
5. **Approve.** Admin merges. The engine re-renders the PDF with the
   approval block, updates the ledger, emails the approved PDF to PSL
   Foundation (with the QuantEcon admin Cc'd), and posts an audit comment
   on the closed issue.

## Why this exists

QuantEcon contracts a small team of researchers (RAs, course developers) paid
by [PSL Foundation](https://www.psl.org) as fiscal host. Before this system,
timesheets were ad-hoc emails and spreadsheets — slow for the admin team,
opaque for the contractors, and prone to data drift between what was approved
and what was paid.

This system gives:

- **Contractors:** a clear form, a structured submission record, and a PDF
  they (and PSL) can rely on.
- **Admins:** GitHub-native review via PR — diffs, labels, history, and audit
  trail without leaving the dev tooling.
- **PSL:** a consistently formatted PDF emailed on approval, ready to process.
- **No webapp to run.** Just GitHub Actions, scripts, and templates.

## Architecture decisions

The headline decisions are listed in
[PLAN.md §9 Resolved decisions](PLAN.md#9-resolved-decisions). The most
load-bearing:

- **Per-contractor private repos** — each payee gets their own `contractor-{handle}` repo. Compensation data is naturally scoped; no cross-contractor leakage.
- **Reusable workflow architecture** — the pipeline lives once, in this repo;
  contractor repos are thin callers. Engine updates propagate immediately.
- **Fiscal-host timezone for document dates** — all paperwork uses PSL Foundation's locale (`America/New_York`) regardless of where the contractor lives, so dates line up with the payer's books.
- **Email to PSL, not @-mentions** — PSL receives approval PDFs via email
  (SMTP from a QuantEcon service-account mailbox); GitHub comments stay as
  the internal audit trail. Recipient addresses live as GitHub org-level
  Variables, never in committed files.

## Reading this repo

| Looking for | Start here |
|---|---|
| What's built and what's next | [PLAN.md — At a glance](PLAN.md#at-a-glance) |
| Full design | [PLAN.md](PLAN.md) |
| Contractor-facing guide | <https://quantecon.github.io/contractor-payments/> |
| Admin operational runbook | [notes/ADMIN_RUNBOOK.md](notes/ADMIN_RUNBOOK.md) (internal) |
| How submissions are parsed | [scripts/parse_issue.py](scripts/parse_issue.py) |
| How PDFs are rendered | [scripts/generate_pdf.py](scripts/generate_pdf.py) + [templates/timesheet.typ](templates/timesheet.typ) + [templates/invoice.typ](templates/invoice.typ) |
| Reusable workflow | [.github/workflows/process-submission.yml](.github/workflows/process-submission.yml) |
| Per-contractor repo template | [contractor-template/](contractor-template/) |

## Operating

This is QuantEcon's operational repo, not a general-purpose library. If you're
running a similar setup elsewhere, the design (PLAN.md) is more useful to you
than the code, which is intentionally specific to QuantEcon + PSL.

Source tracking issues for the design:
[QuantEcon/admin#3](https://github.com/QuantEcon/admin/issues/3) (this system),
[QuantEcon/admin#5](https://github.com/QuantEcon/admin/issues/5) (broader admin
infrastructure).

## License

To be determined — likely MIT or similar. Open an issue if licensing matters
for your use case.
