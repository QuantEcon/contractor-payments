<!--
Contractor onboarding email template.

Sent by the admin after `onboarding/new-contractor.py` completes, to welcome
the contractor and invite them to their new repo (the manual "onboarding email"
step referenced in PLAN.md and docs/index.md).

Usage: fill the {{placeholders}}, delete any guidance comments, and send as
plain text. Placeholder reference:
  {{first_name}}   preferred first/given name (e.g. Kenko, Emile)
  {{contract_id}}  e.g. QE-PSL-2026-005
  {{rate}}         hourly rate *with* currency symbol, e.g. $31 or ¥2,200
  {{max_hours}}    monthly cap, e.g. 30  (drop the "up to … hours/month" clause if uncapped)
  {{period}}       human date range, e.g. 1 Jun–15 Dec 2026
  {{repo_url}}     https://github.com/QuantEcon/contractor-<handle>

Hourly variant shown below. For a milestone contract: swap "timesheets" →
"invoices", "Hourly Timesheet" → "Milestone Invoice", drop the rate/hours
clause, and point the step-by-step link at .../contractor-guide/submit-invoice/.

The "Sandbox Testing" bullet — and the matching "two GitHub invitations" line —
are for contractors granted access to QuantEcon/test-contractor-payments. If you
don't grant sandbox access, delete that bullet and change the invitation line to
the single-repo form: "You'll have a GitHub invitation waiting for the repository
above — accept it (check your email, or the repo page) to get started."
-->

Subject: Welcome to QuantEcon — your contractor repository

Hi {{first_name}},

Welcome aboard! I've set up your private repository for submitting timesheets under your QuantEcon RA contract ({{contract_id}} — {{rate}}/hour, up to {{max_hours}} hours/month, {{period}}):

{{repo_url}}

You'll have two GitHub invitations waiting—one for the repository above and one for the sandbox below. Accept both to get started (check your email, or open each repo's page and click "Accept").

How it works:

  - Open a new Issue in that repo.
  - Choose the Hourly Timesheet template.
  - Add a row for each day you work through the month.
  - Comment /validate when you're ready to submit to check formatting
  - Comment /submit when you're ready—I'll review and approve, and the rest (PDF, records, payment paperwork to PSL) is handled automatically.

Resources:

  - Manual (Tutorials & Screenshots): https://quantecon.github.io/contractor-payments/
  - Step-by-Step Guide: https://quantecon.github.io/contractor-payments/contractor-guide/submit-timesheet/
  - Sandbox Testing: You also have access to `QuantEcon/test-contractor-payments` if you'd like to try the flow in a sandbox first.

Any questions, just reply to this email.
