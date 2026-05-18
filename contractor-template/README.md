# $CONTRACTOR_NAME (@$CONTRACTOR_HANDLE)

Payment artefacts for **$CONTRACTOR_NAME**. This repo is part of the
QuantEcon contractor-payments system — submissions are filed as issues,
processed by reusable workflows in
[`QuantEcon/contractor-payments`](https://github.com/QuantEcon/contractor-payments),
and approved by the admin.

## Submitting

1. Go to **Issues → New Issue**.
2. Pick a template:
   - 📋 **Hourly Timesheet** — monthly hours on an hourly contract.
   - 🎯 **Milestone Invoice** — a milestone delivered on a milestone contract.
3. Fill out the form and submit. A PR is opened automatically with a
   rendered PDF preview; the admin reviews and merges.

See the
[Contractor Guide](https://github.com/QuantEcon/contractor-payments/blob/main/docs/CONTRACTOR_GUIDE.md)
for the full walkthrough.

## What's in this repo

| Path | Purpose |
|---|---|
| `config/settings.yml` | Contractor identity (name, GitHub handle, email, optional address). |
| `contracts/` | One YAML per contract (hourly or milestone). Authoritative source of rates and milestone schedules. |
| `submissions/<YYYY-MM>/` | Submission YAMLs, one per approved (or pending) claim. |
| `ledger/` | Running per-contract totals, auto-updated on approval. |
| `generated_pdfs/<YYYY-MM>/` | Rendered PDF and PNG preview for each submission. |
| `.github/ISSUE_TEMPLATE/` | The Issue Forms that the contractor fills out. |
| `.github/workflows/` | Thin wrappers around the reusable workflows in `QuantEcon/contractor-payments`. |
