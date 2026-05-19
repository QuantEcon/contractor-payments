# Corrections and revisions

Things go wrong. This page covers what to do at each stage of the
submission lifecycle when you need to change something — from "I
haven't submitted yet" all the way through "the payment processor
already has the wrong version".

The mechanism depends on **when** you noticed the problem. Find your
situation below.

## Before you've run `/submit`

You're still in the draft phase. Nothing has been filed, no PR exists.

**Just edit the issue body.** Open the ⋯ menu → Edit, fix the rows,
save. Run `/validate` again to confirm the change parses cleanly.

No special workflow, no labels to manage. The draft is yours until you
file it.

## After `/submit`, before the PR is merged

A Pull Request exists. Your administrator may already be reviewing it.

**The issue is now closed and locked**, so you can't edit the body
anymore. The PR is the authoritative artifact.

Your options:

1. **Ask your administrator to edit the PR branch directly.** They have
   write access and can adjust the YAML / re-render the PDF.
2. **Have your administrator close the PR**, then **reopen the original
   issue**, edit the body, and `/submit` again. This produces a fresh
   submission with the same identifier (no `-v2` suffix because the
   original was never merged).

If you're comfortable with git yourself, you can also push directly to
the PR branch — but the YAML format is strict and the PDF needs to be
regenerated, so this is normally an admin task.

## After the PR is merged, before the payment processor pays

The merge means the engine has updated your contract's running ledger,
re-rendered the PDF with the approval banner, and emailed the payment
processor (PSL Foundation, typically). But the actual payment usually
happens 1–4 weeks later, on the fiscal host's batch cycle.

In this window, mistakes are recoverable through a **revision**.

### How to file a revision

1. Find the original submission issue (it's closed and locked).
2. Click **Reopen issue**. The lock blocks new comments from
   non-collaborators but you, as the contractor, can still operate on
   it.
3. Click ⋯ → Edit the body. Make your corrections — change the rows,
   adjust the period, whatever needs to change.
4. Re-run `/validate` to confirm the corrected version parses.
5. Comment `/submit`.

The engine detects that a merged submission already exists for this
issue and treats the new one as a **revision**:

- The new submission ID gets a `-v2` suffix (or `-v3`, `-v4`, etc. for
  successive revisions). E.g. `mmcky-timesheet-2026-04` becomes
  `mmcky-timesheet-2026-04-v2`.
- The rendered PDF carries a **"REVISION — supersedes &lt;previous-id&gt;"**
  banner so the payment processor spots the correction in their inbox.
- The notification email subject is prefixed with **REVISION** for the
  same reason.
- Your contract's running ledger **replaces** the old entry with the
  new one (rather than adding a separate line).
- A cross-reference comment is posted on the previous, now-superseded
  PR linking forward to the revision.

The original PDF stays in `generated_pdfs/` permanently as the audit
trail of what the payment processor was originally sent. Cancellation
isn't a thing — supersession is.

!!! warning "Revisions are real submissions"
    A revision goes through the same PR + review + merge flow as the
    original. Your administrator reviews and merges it, and a fresh
    email goes to the payment processor. Don't file a revision for a
    typo in the description — only for changes that affect what should
    be paid.

## After the payment processor has paid

At this point a revision could create confusion: the original payment
has already gone out, and the corrected PDF would land in the payment
processor's inbox suggesting a *new* payment is owed. The engine
doesn't track payment state directly, so this is a judgment call.

**General guidance:**

- If the correction is **a smaller amount** (you over-billed and need
  to refund the difference): coordinate directly with your
  administrator. Don't file a revision — handle the reconciliation
  out-of-band.
- If the correction is **an additional amount** (you under-billed and
  the contract owes you more for that period): file a *new* invoice
  for the additional work. The engine assigns it a `-B` suffix (e.g.
  `mmcky-invoice-2026-04-B`) so it's a fresh claim, conceptually
  independent of the original. Your administrator will know what to do
  with it on the PSL side.
- If the original submission was **entirely wrong**: again, talk to
  your administrator. The fix is operational, not engine-driven.

When in doubt, ask. Your administrator monitors the payments inbox and
can tell you which path fits.

## Period mismatches and the parse-error path

A common mistake: opening the form with `Year: 2026 / Month: 05`, then
typing entry dates like `2025-05-10` (year typo). The parser will
reject:

```
Line 2: date `2025-05-10` is outside the selected period `2026-05`.
Either change the date or pick a different period.
```

This shows up as a `parse-error` label and an error comment on the
issue. The issue stays *open* (it didn't go anywhere yet), so just
edit the body and try `/submit` again. If you applied the `submit`
label, it was auto-removed; re-applying it (or commenting `/submit`)
re-triggers cleanly.

For *milestone invoices*, out-of-period dates are allowed — this is
intentional for catch-up submissions covering older milestones. See
[Submit a milestone invoice → Catch-up submissions](submit-invoice.md#catch-up-submissions).

## When the bot doesn't respond

If you `/validate` or `/submit` and nothing happens within a couple of
minutes, check:

1. **Did the workflow actually trigger?** Go to the Actions tab in
   your repo and look for a recent "Issue to PR" run.
2. **Was the comment exactly `/validate` or `/submit`?** The matcher
   checks that the comment *starts with* `/validate` or `/submit` —
   you can add notes after (e.g. `/submit thanks!`) but the command
   itself should be the first token.
3. **Are the right labels on the issue?** The workflow requires
   `timesheet` or `milestone-invoice`. The form applies these
   automatically, but if you opened a blank issue manually, they
   won't be there.

If everything looks right and nothing happens, contact your
administrator. They can re-run the workflow or escalate to the engine
maintainers.
