# QuantEcon Contractor Payments

This site is the step-by-step guide for QuantEcon contractors filing
timesheets, invoices, and reimbursement claims through GitHub.

## Where to start

If your contract is **hourly**, see:

[**→ Submit a timesheet**](contractor-guide/submit-timesheet.md)

If your contract is **milestone-based** (a fixed amount per
deliverable), see:

[**→ Submit a milestone invoice**](contractor-guide/submit-invoice.md)

If you're **claiming expenses** (travel, equipment, software — with
receipts), see:

[**→ Submit a reimbursement claim**](contractor-guide/submit-reimbursement.md)

If something went wrong with a previous submission and you need to fix
it, see:

[**→ Corrections and revisions**](contractor-guide/corrections.md)

## How the flow works in one paragraph

You file each submission as a GitHub **Issue** in your own contractor
repo. The issue is a *draft* — fill in what you know, then edit it
over days or weeks as you work. When you're ready, comment `/validate`
to check that your entries parse cleanly (with computed totals), and
`/submit` to file. A Pull Request opens automatically with the
rendered PDF; your QuantEcon administrator reviews and merges it, and
the payment processor is notified by email. You don't need to do
anything with the PR — just file the issue.

## Questions?

The GitHub issue templates in your contractor repo include inline
instructions for each field. For anything else, contact the QuantEcon
administrator listed in your onboarding email.
