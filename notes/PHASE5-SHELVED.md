# Phase 5 (reimbursement claims) — shelved 2026-07-30

This branch is **parked, not abandoned**. The code is complete, reviewed and
tested; the decision not to ship it is about fit, not quality. This note
records why, so the next person does not re-derive it — or re-enable the
feature without knowing what stopped it.

## The decision

**GitHub is a good fit for timesheets and invoices, and a poor one for
reimbursements.** The difference is structural rather than a matter of
polish:

- For timesheets and invoices, **the data is the deliverable**. Hours × rate,
  milestones against a contract schedule. The engine earns its place: it
  computes, validates against `contracts/<id>.yml`, checks
  `max_hours_per_month`, cross-checks milestone IDs, and keeps a ledger
  against contract value. The submission is self-contained — there is nothing
  to attach.
- For reimbursements, **the evidence is the deliverable**. The data is a
  trivial sum; the parser's only real computation is cross-checking a total
  the contractor already typed. The substance is the receipt images.

And receipts are precisely where GitHub is weakest. Each of these was
measured, not assumed:

| Problem | Evidence |
|---|---|
| Image attachments lose their filenames | GitHub anonymises them. `receipt-taxi.png` reaches the fiscal host as `03-55070d4e-6445-4df2-8efd-914dad928044.png`. Files added via the file-picker keep their names; dragged images do not — so the more receipts are photos, the less labelled the bundle becomes. |
| Nothing links a receipt to a line item | Entries are `sorted(by date)`; receipts are numbered by drag order. The two orderings are unrelated, so receipt `03` can back entry `1`. There is no linking field in the form or the YAML. |
| Receipts are not fetchable by the pipeline | Stage 0b measured it: `secrets.GITHUB_TOKEN` → **404**; a user PAT → **200**; no auth → **404**. The built-in job token cannot read `user-attachments`. |
| The credential is exfiltratable | A reusable workflow runs as a job in the *contractor's* repo, and contractors hold Write there, so any credential it uses can be printed by a workflow they add. |
| Claims cannot be centralised | A shared reimbursements repo would let every claimant read every other claimant's receipts — home addresses, travel dates, meal details — permanently, in git history. GitHub has no intra-repo ACLs, and the leak is via issue attachments, not just commits. |

The person carrying the cost is the fiscal host: they receive a claim with
UUID-named attachments and no stated correspondence to the line items, and
must open each one and match by amount. That is the actor with the least
context and the most obligation to check.

Weighed against roughly a dozen claims a year, the return did not justify
~1,700 lines plus a credential design plus the privacy question.

## What shipped instead

The engine-wide fixes found while building and reviewing this branch were
split onto `engine-fixes` (PR #6) and are independent of reimbursements —
several fix live production bugs in the timesheet flow. Nothing of value is
stranded here.

## What is on this branch

`fetch_receipts.py`, `templates/reimbursement.typ`, the claim form, the
reimbursement parser branch and ledger, `onboarding/sync_templates.py`, the
`contract_type: none` reimbursement-only payee path, and receipt fixtures
under `tests/fixtures/receipts/`. All tested; 533 tests passed at
`549b4b0`.

Note `onboarding/sync_templates.py` is Phase 5-only and carries useful
non-reimbursement work (the form-presence rule, config validation). If the
form-presence rule is ever wanted for timesheets/invoices alone, it can be
lifted from here.

## If you pick this up again

The trigger to revisit is **volume**, not the credential problem. If you are
reimbursing enough people that per-claimant repos become the binding
constraint, the only shape that gives both centralisation and privacy is a
web form writing into an admin-only repo, with claimants holding no repo
access at all. That needs a small backend — GitHub Pages is static, so there
is nowhere to keep a credential.

Two cheaper things to test first, either of which changes the calculus:

1. **A per-repo fine-grained PAT.** If the attachment CDN honours per-repo
   scoping, the credential a contractor could steal grants only what they
   already have, and the exfiltration objection disappears. Untested — it
   needs a PAT created in the browser.
2. **Contractors at Triage instead of Write.** They cannot then edit
   workflows, so any credential becomes safe. Note the blocker: the engine
   locks submission issues, and locked issues reject comments, so `/submit`
   on a revision would need the `submit` label instead. See the analysis in
   the conversation that produced this note.

Also worth knowing: receipts embedded *into* the claim PDF, one page per
receipt captioned with its line item, would remove the attachment-matching
problem entirely rather than improving it. That interacts with the
single-page assumption in the renderer.
