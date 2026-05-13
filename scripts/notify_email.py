"""Send the approval-notification email with the approved PDF attached.

Called by `.github/workflows/process-approved.yml` after
`scripts.finalize_approval` re-renders the PDF and `scripts.update_ledger`
appends the ledger entry. The companion `scripts.notify_comment` posts an
internal GitHub comment that confirms this email was sent.

Recipients policy (PLAN §6, §9):
  - `testing_mode: false` → To: $PSL_EMAIL, Cc: $QUANTECON_EMAIL
  - `testing_mode: true`  → To: $QUANTECON_EMAIL, no Cc (PSL is NEVER
    contacted while testing_mode is on).

The flag lives in `templates/fiscal-host.yml` under
`notifications.testing_mode`. Defaults to true (testing) if missing —
fail-safe to "don't email PSL".

Reply-To header is set to $QUANTECON_EMAIL so any reply from the
recipient goes to a real human admin, not back to the no-touch SMTP
account.

Required environment:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM  (org secrets)
  PSL_EMAIL, QUANTECON_EMAIL                                  (org variables)

CLI:
  python -m scripts.notify_email \\
      --submission submissions/2025-11/mmcky-invoice-2025-11.yml \\
      --settings   config/settings.yml \\
      --pdf        generated_pdfs/2025-11/mmcky-invoice-2025-11.pdf \\
      --issue-url  https://github.com/QuantEcon/contractor-engine-test/issues/13 \\
      --output-summary /tmp/email_summary.json  # for notify_comment to read
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import yaml


# Map submission.type → human-readable label for the subject line.
_TYPE_LABEL = {
    "timesheet": "Timesheet",
    "milestone_invoice": "Milestone Invoice",
    "reimbursement": "Reimbursement Claim",   # Phase 5
}


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        raise RuntimeError(f"{path} is empty.")
    return data


def _fmt_amount(amount: float, currency: str) -> str:
    """Currency-aware display. JPY: no decimals. AUD/USD: 2 decimals."""
    if currency.upper() == "JPY":
        return f"{int(round(amount)):,}"
    return f"{amount:,.2f}"


def _read_testing_mode(fiscal_host_path: Path) -> bool:
    """Read `notifications.testing_mode` from fiscal-host.yml. Defaults to
    True (testing) if the file or field is missing — fail-safe."""
    if not fiscal_host_path.exists():
        return True
    fiscal_host = _load_yaml(fiscal_host_path)
    notifications = fiscal_host.get("notifications", {}) or {}
    return bool(notifications.get("testing_mode", True))


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable `{name}`. See docs/EMAIL_SETUP.md."
        )
    return value


def compose_message(
    *,
    submission: dict,
    contractor: dict,
    pdf_path: Path,
    issue_url: Optional[str],
    sender: str,
    to: str,
    cc: Optional[str],
    reply_to: Optional[str],
) -> EmailMessage:
    """Build the email message. Pure function — takes the already-resolved
    recipients (testing_mode logic happens in main() before calling us)."""
    submission_type = submission.get("type", "timesheet")
    type_label = _TYPE_LABEL.get(submission_type, "Submission")
    period = submission["period"]
    totals = submission["totals"]
    currency = totals.get("currency", "")
    amount_display = f"{_fmt_amount(totals['amount'], currency)} {currency}".strip()

    real_name = contractor.get("name", submission.get("submitted_by", "Unknown"))
    github_handle = contractor.get("github", submission.get("submitted_by", ""))

    subject = (
        f"[QuantEcon] {type_label} approved — "
        f"{real_name} — {period} — {amount_display}"
    )

    body_lines = [
        "Approved by QuantEcon admin for processing.",
        "",
        f"Contractor:    {real_name} (@{github_handle})",
        f"Contract:      {submission['contract_id']}",
        f"Type:          {type_label}",
        f"Period:        {period}",
        f"Amount:        {amount_display}",
        f"Approved:      {submission.get('approved_date', '—')} "
        f"by @{submission.get('approved_by', '—')}",
        "",
        "Attached: the approved invoice PDF.",
    ]
    if issue_url:
        body_lines.extend(["", f"Issue: {issue_url}"])
    body = "\n".join(body_lines) + "\n"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    # PDF attachment
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    msg.add_attachment(
        pdf_bytes,
        maintype="application", subtype="pdf",
        filename=pdf_path.name,
    )
    return msg


def send_message(
    msg: EmailMessage,
    *,
    host: str,
    port: int,
    user: str,
    password: str,
) -> None:
    """Submit the message via SMTP with STARTTLS."""
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--submission", required=True, type=Path,
                   help="Path to the approved submission YAML.")
    p.add_argument("--settings", required=True, type=Path,
                   help="Path to config/settings.yml (for contractor real name).")
    p.add_argument("--pdf", required=True, type=Path,
                   help="Path to the approved PDF to attach.")
    p.add_argument("--fiscal-host", type=Path, default=None,
                   help="Path to templates/fiscal-host.yml. Default: derived "
                        "from --engine-templates (engine/templates/fiscal-host.yml).")
    p.add_argument("--engine-templates", type=Path, default=Path("engine/templates"),
                   help="Engine templates directory (default: engine/templates, "
                        "matching the reusable-workflow layout).")
    p.add_argument("--issue-url", default=None,
                   help="URL to the original submission issue (for the email body).")
    p.add_argument("--output-summary", type=Path, default=None,
                   help="If set, write a JSON summary of the send to this path "
                        "(used by notify_comment.py to confirm the send in-band).")
    p.add_argument("--dry-run", action="store_true",
                   help="Compose the message and print it; don't actually send. "
                        "Useful for local development without SMTP credentials.")
    args = p.parse_args(argv)

    submission = _load_yaml(args.submission)
    settings = _load_yaml(args.settings)
    contractor = settings.get("contractor", {})

    fiscal_host_path = args.fiscal_host or (args.engine_templates / "fiscal-host.yml")
    testing_mode = _read_testing_mode(fiscal_host_path)

    psl_email = os.environ.get("PSL_EMAIL", "").strip()
    quantecon_email = os.environ.get("QUANTECON_EMAIL", "").strip()
    if not quantecon_email:
        raise RuntimeError(
            "QUANTECON_EMAIL env var is required (used as both Cc and testing-mode To)."
        )

    if testing_mode:
        to_addr = quantecon_email
        cc_addr = None
        print(f"testing_mode=true — sending to {quantecon_email} only "
              f"(PSL will not be contacted).", file=sys.stderr)
    else:
        if not psl_email:
            raise RuntimeError(
                "PSL_EMAIL env var is required when testing_mode=false."
            )
        to_addr = psl_email
        cc_addr = quantecon_email
        print(f"testing_mode=false — sending to {psl_email} (Cc {quantecon_email}).",
              file=sys.stderr)

    sender = _require_env("SMTP_FROM") if not args.dry_run else os.environ.get("SMTP_FROM", "<SMTP_FROM>")
    msg = compose_message(
        submission=submission,
        contractor=contractor,
        pdf_path=args.pdf,
        issue_url=args.issue_url,
        sender=sender,
        to=to_addr,
        cc=cc_addr,
        reply_to=quantecon_email,
    )

    if args.dry_run:
        print("--- email (dry-run, not sent) ---")
        # Print headers + body, omit attachment bytes for readability.
        for header in ("Subject", "From", "To", "Cc", "Reply-To"):
            val = msg.get(header)
            if val:
                print(f"{header}: {val}")
        print()
        print(msg.get_body(preferencelist=("plain",)).get_content())
        print(f"--- (with attachment: {args.pdf.name}) ---")
    else:
        send_message(
            msg,
            host=_require_env("SMTP_HOST"),
            port=int(_require_env("SMTP_PORT")),
            user=_require_env("SMTP_USER"),
            password=_require_env("SMTP_PASSWORD"),
        )

    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = {
        "to": to_addr,
        "cc": cc_addr,
        "subject": msg["Subject"],
        "sent_at": sent_at,
        "testing_mode": testing_mode,
        "dry_run": args.dry_run,
    }

    if args.output_summary:
        args.output_summary.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_summary, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
