# Email setup — Google Workspace + GitHub Secrets

Operational runbook for wiring up the approval-email pipeline (Phase 2 of
[PLAN.md](../PLAN.md#8-build-phases)). One-time setup; everything below
sticks until credentials rotate.

> **Audience:** QuantEcon admin with Google Workspace admin access **and**
> GitHub org-admin on `QuantEcon`. Should take ~15 minutes end to end.

## What you're setting up

A Google Workspace mailbox that the GitHub Actions workflow uses to send
the approval-PDF email when a submission PR merges. The mailbox sends only
— we never read replies from it inside the workflow. (Forwarding inbound
mail to a real inbox is a nice-to-have; see Step 6.)

End state:

```
Contractor's PR merges
        │
        ▼
.github/workflows/process-approved.yml fires
        │
        ▼
scripts/notify_email.py uses smtplib + GitHub Secrets to send mail via
smtp.gmail.com:587 from <service-account-mailbox>
        │
        ▼
Recipient (vars.PSL_EMAIL, Cc vars.QUANTECON_EMAIL) lands the approved PDF
```

## What you'll set

By the end you'll have these GitHub **org-level Secrets** (scope: Private
repositories — same as the others):

| Name | Value | Set in this runbook? |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | Already done |
| `SMTP_PORT` | `587` | Already done |
| `SMTP_USER` | service-account mailbox address (the address you choose in Step 1) | Step 4 |
| `SMTP_FROM` | usually same as `SMTP_USER` | Step 4 |
| `SMTP_PASSWORD` | Google app password (16-char string) | Step 4 |

And these org-level Variables (already done):

| Name | Value |
|---|---|
| `PSL_EMAIL` | PSL Foundation recipient |
| `QUANTECON_EMAIL` | QuantEcon admin Cc |

---

## Step 1 — Pick or provision the service-account mailbox

Decide on the sending mailbox address. Conventions to choose from (using
`<your-domain>` as a placeholder for your Workspace domain):

- `services@<your-domain>` — neutral, suggests an automated service
- `automation@<your-domain>` — explicit about what it is
- `noreply@<your-domain>` — clearest "don't reply here"; can pair with a
  forwarding rule that quietly drops or redirects replies
- `payments@<your-domain>` — describes the function

**Recommendation:** `services@<your-domain>`. It's not jurisdiction-specific
(unlike `payments@`) and reads naturally on the recipient's side.

If the mailbox doesn't exist yet:

1. Go to <https://admin.google.com/> → **Directory** → **Users** → **Add new user**.
2. Create the account. Note: this counts as a Google Workspace seat ($6/month
   on Business Starter). Or, if QuantEcon has a Google Group set up as an
   address, you can use that instead (no extra seat needed — but the app
   password approach below requires a real account, not a group).
3. Set a strong, randomly-generated password (you won't need to remember it
   after Step 3, but lock the account against takeover regardless).

If it already exists, just confirm you have access to log in as that user.

## Step 2 — Enable 2-Step Verification on the mailbox

App passwords require 2-Step Verification to be turned on for the account.

1. Sign in to <https://myaccount.google.com/> *as the service-account user*
   (not as your own admin account).
2. **Security** → **How you sign in to Google** → **2-Step Verification**.
3. Turn it on. Use a phone number you have access to (a Google Voice number
   or admin's personal phone for the recovery code is fine — this account
   won't be doing day-to-day logins).
4. After enabling, you'll see "App passwords" appear in the Security menu.

## Step 3 — Generate an app password

Still signed in as the service-account user:

1. **Security** → **2-Step Verification** → scroll to **App passwords**
   (or go directly to <https://myaccount.google.com/apppasswords>).
2. Enter a label that identifies the use, e.g.
   `QuantEcon contractor-payments workflow`.
3. Click **Create**. Google generates a 16-character password (shown as
   four 4-char groups separated by spaces, e.g. `abcd efgh ijkl mnop`).
4. **Copy this immediately — it won't be shown again.**

When you paste it into GitHub Secrets (Step 4), strip the spaces:
`abcdefghijklmnop`.

> **Rotation note:** app passwords don't expire automatically, but
> you can revoke and regenerate from the same page anytime. Rotate if
> the password ever leaks; the SMTP runner will start failing on the
> next workflow run, which is a useful sentinel.

## Step 4 — Set the three remaining GitHub org Secrets

UI: <https://github.com/organizations/QuantEcon/settings/secrets/actions>

Add three new secrets (scope: **Private repositories**):

| Name | Value |
|---|---|
| `SMTP_USER` | the mailbox address from Step 1 |
| `SMTP_FROM` | same as `SMTP_USER` |
| `SMTP_PASSWORD` | the 16-char app password from Step 3, no spaces |

Or via `gh` CLI (needs `admin:org` scope — refresh with
`gh auth refresh -h github.com -s admin:org` if needed):

```bash
gh secret set SMTP_USER --org QuantEcon --visibility private \
  --body '<service-account-mailbox>'

gh secret set SMTP_FROM --org QuantEcon --visibility private \
  --body '<service-account-mailbox>'

gh secret set SMTP_PASSWORD --org QuantEcon --visibility private \
  --body '<16-char-app-password>'
```

`--visibility private` matches the existing scope choice for `SMTP_HOST` /
`SMTP_PORT` — all current and future private repos in the org get access
automatically.

## Step 5 — Smoke test from a local Python session

Before relying on this in CI, send one test email from your laptop. This
proves the credentials work outside of any workflow plumbing.

```python
import smtplib
from email.message import EmailMessage

# Fill in the values you just configured (or pull from your password manager)
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "<service-account-mailbox>"         # the mailbox you set up
SMTP_FROM = "<service-account-mailbox>"
SMTP_PASSWORD = "<16-char-app-password>"        # no spaces
TO = "<your-personal-test-address>"             # NEVER PSL during testing

msg = EmailMessage()
msg["Subject"] = "[QuantEcon] Email pipeline smoke test"
msg["From"] = SMTP_FROM
msg["To"] = TO
msg.set_content(
    "If you can read this, the QuantEcon contractor-payments email "
    "pipeline credentials are working. Safe to delete."
)

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
    smtp.starttls()
    smtp.login(SMTP_USER, SMTP_PASSWORD)
    smtp.send_message(msg)
print("Sent.")
```

Run it:

```bash
python smoke_test_smtp.py
```

You should see a message in your test address within a minute. If it
doesn't arrive, check the troubleshooting section below.

## Step 6 — (Optional) Forward inbound mail to a real inbox

If anyone replies to an approval email, the reply will land in the
service-account mailbox by default. Two ways to handle:

- **Auto-forward** — In the service-account's Gmail settings →
  **Forwarding and POP/IMAP** → forward to the QuantEcon admin mailbox
  (the same one referenced by `vars.QUANTECON_EMAIL`). Replies surface
  to a real human.
- **Set a "do-not-reply" reply-to** — Add `Reply-To: <vars.QUANTECON_EMAIL>`
  in the outgoing email's headers (`scripts/notify_email.py` will support
  this). PSL replies go to admin directly; the service-account inbox stays
  empty.

Both are fine. The Reply-To approach is more correct semantically (PSL
replies don't bounce off a no-touch mailbox) but auto-forward works without
code changes if you want a quick fix later.

---

## Troubleshooting

**`SMTPAuthenticationError: 535 Username and Password not accepted`**
- 2-Step Verification not enabled on the service-account user (Step 2).
- You used the account's regular password instead of the app password.
- The app password has spaces — strip them: `abcdefghijklmnop`, not
  `abcd efgh ijkl mnop`.
- The account is on a Workspace edition with Less Secure Apps disabled,
  but the **app password** path should always work regardless.

**`SMTPSenderRefused: 530 5.7.0 Authentication Required`**
- `starttls()` was not called before `login()`. Don't use the bare SMTP
  port (25); use submission port 587 with STARTTLS.

**Mail goes to the recipient's spam folder**
- Set up SPF for your sending domain (TXT record allowing
  `_spf.google.com`). Google's SMTP relays its own SPF for Workspace
  domains, but having the TXT record on your DNS is what makes inbound
  checks pass on the recipient's side.
- DKIM signing is enabled automatically by Workspace for outbound mail.
- DMARC is optional but recommended once SPF + DKIM are in place.

**Workflow runs report `secrets.SMTP_PASSWORD` is empty**
- The secret is set but the calling repo doesn't have access. Verify the
  secret's "Repository access" scope on
  <https://github.com/organizations/QuantEcon/settings/secrets/actions>
  includes the contractor repo where the workflow ran.
- The thin caller workflow is missing `secrets: inherit` — the reusable
  workflow can't see secrets the caller didn't pass through.

**Rate limits hit**
- Gmail SMTP allows ~2,000 messages/day per account. We're nowhere near
  that. If it ever bites, the right move is a transactional service
  (Postmark / Mailgun), not raising Gmail's limit.

---

## Once Phase 2 ships

1. End-to-end test runs with `notifications.testing_mode: true` in
   `templates/fiscal-host.yml`. PSL is never contacted — all mail goes to
   `vars.QUANTECON_EMAIL`.
2. When you're satisfied (likely after a month of internal-only runs),
   open `templates/fiscal-host.yml`, set `testing_mode: false`, commit
   and push. Next merge fires email to PSL.

The flip is a one-line change. No code change needed, no credential
shuffle. By design.
