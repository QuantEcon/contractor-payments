# Submitting a reimbursement claim

This walks through claiming out-of-pocket expenses — travel,
accommodation, equipment, software, conference fees — with receipts
attached. The flow is the same as the
[timesheet tutorial](submit-timesheet.md) — an issue you draft, then
`/validate` and `/submit` to file it — but a claim carries **receipts**,
and those travel with your paperwork all the way to the payment
processor.

Reimbursements don't belong to a contract: if your repo has the
**🧾 Reimbursement Claim** template, claims are enabled for you. (If you
think they should be and the template isn't there, ask your QuantEcon
administrator.)

## The ground rules

- **One currency per claim.** Every line item in a claim uses the same
  currency, picked from the form's dropdown. If a trip produced receipts
  in two currencies, file two claims — a second claim in the same month
  is fine; the system numbers it separately (`-B`).
- **One row per receipt line, receipts attached.** Each expense row
  should be backed by a receipt file dragged into the form's
  **Receipts** box (PDF, PNG, or JPG).
- **Categories come from your repo's allowed list.** The form shows the
  list; the parser rejects anything else. The list lives in
  `config/reimbursements.yml` in your repo — ask your administrator if
  a category you need is missing.
- **Same-day rows are fine.** Flight + hotel + dinner on one date is
  the normal case (unlike timesheets, which allow one row per day).
- **Dates outside the claim month are allowed** for trips that span a
  month boundary — the admin sees a note and reviews. File the claim
  against the month you're claiming it in.

## 1. Open a draft

Go to your contractor repo's **Issues** tab and click **New Issue**.
Pick the **🧾 Reimbursement Claim** template.

Fill in the form:

- **Year** + **Month** — the period this claim is filed *against*
  (usually the month the expenses landed, but a trip that started in the
  previous month is fine — see the ground rules).
- **Expense Entries** — one row per receipt line:

  ```text
  Date | Amount | Category | Description
  2026-05-31 | 512.40 | travel | Flight SYD–NRT
  2026-06-01 | 184.50 | accommodation | Hotel, one night
  2026-06-01 | 62.35 | meals | Conference dinner
  ```

  Keep the header row in place. Amounts are plain numbers — no currency
  symbols, the Currency dropdown covers that. Descriptions may contain
  `|`.

- **Currency** — the single currency for every row above.
- **Total** — the sum of your rows. The parser cross-checks this
  against the entries and rejects a mismatch, so a typo in either place
  gets caught before anything is filed.
- **Trip / project context** (optional) — one or two lines of context
  that print on the claim PDF, e.g. *"PyCon JP — invited talk on
  QuantEcon lectures"*. Useful when individual rows don't justify
  themselves.
- **Receipts** — **drag and drop your receipt files into this box.**
  GitHub uploads them and inserts links. One attachment per receipt;
  PDF, PNG, or JPG. Keep individual files under ~10 MB and the bundle
  under ~15 MB so the approval email goes through.
- **Confirmation** — tick it.

Submit the form. **This files nothing yet** — the issue is your draft.
Edit the body to add rows and drag in more receipts as the month goes
on.

## 2. Check your draft with `/validate`

When the entries look complete, comment **`/validate`** on the issue.
The engine parses your draft and replies with a summary: line-item
count, receipts found, and the computed total. Fix anything it flags
(category not in the allowed list, total mismatch, unreadable dates)
by editing the issue body, then `/validate` again — the reply updates
in place.

## 3. File with `/submit`

Comment **`/submit`** (or apply the `submit` label). The engine:

1. downloads your receipt attachments and **commits them into the repo**
   under `receipts/<period>/<claim-id>/` — that's the durable audit copy;
2. opens a Pull Request with the claim YAML, the rendered claim PDF
   (with a PNG preview inline), and your receipts listed with sizes;
3. closes and locks the issue — corrections now go via the PR branch
   (or reopen + `/submit` for a revision, same as the other types — see
   [Corrections and revisions](corrections.md)).

Your administrator reviews the PR — checking the rows against the
receipts — and merges. On merge, the claim PDF is re-rendered with the
green **APPROVED** block, the running reimbursements ledger updates,
and the payment processor receives an email with the **claim PDF and
every receipt file attached**.

## Worked example

A two-day conference trip, claimed in June, paid in AUD:

```text
Date | Amount | Category | Description
2026-05-31 | 512.40 | travel | Flight SYD–NRT (departed prior month)
2026-06-01 | 184.50 | accommodation | Hotel, one night
2026-06-01 | 62.35 | meals | Conference dinner
2026-06-02 | 41.00 | travel | Airport train
```

- Currency: `AUD` · Total: `800.25` · four receipt files dragged into
  the Receipts box.
- The May 31 flight produces a non-blocking note (outside the June
  period) — expected for a trip spanning the boundary; the admin sees
  it on the PR and approves.

If the same trip also had a `12,800 JPY` taxi receipt, that becomes a
**second claim** with Currency `JPY` — same month, filed the same way.

## Troubleshooting

- **"Receipts section has no attachments"** — the box needs at least
  one *uploaded file* (drag and drop), not a pasted external link.
- **"Total doesn't match the sum"** — recompute one or the other; the
  error shows both numbers.
- **"category … is not in this repo's allowed list"** — pick from the
  list shown at the top of the form, or ask your administrator to add
  the category to `config/reimbursements.yml`.
- **Very large claims** — keep descriptions short; a claim with more
  than ~20 line items (or long wrapping descriptions) can overflow the
  one-page PDF and fail to render. Split it into two claims.
- **The bot didn't respond** — same recovery as the other types; see
  [Corrections and revisions](corrections.md).
