"""Send the approval-notification email with the approved PDF attached.

Called by `.github/workflows/process-approved.yml` after
`scripts.finalize_approval` re-renders the PDF and `scripts.update_ledger`
appends the ledger entry. The companion `scripts.notify_comment` posts an
internal GitHub comment that confirms this email was sent.

Recipients policy (PLAN §6, §9):
  - `testing_mode: false` → To: $PSL_EMAIL, Cc: $QUANTECON_EMAIL_REVIEWER
  - `testing_mode: true`  → To: $QUANTECON_EMAIL_REVIEWER, no Cc (PSL is
    NEVER contacted while testing_mode is on).

`testing_mode` resolves per-repo, most specific wins (see
`_effective_testing_mode`):
  1. contractor `config/settings.yml` → notifications.testing_mode
  2. engine `templates/fiscal-host.yml` → notifications.testing_mode
  3. True (fail-safe — "don't email PSL" if nothing is configured)

Reply-To header is set to $SMTP_FROM (the payments@ alias). PSL's
"Reply" lands at payments@ → routed to the underlying admin mailbox
where the conversation labels under the existing "payments" filter;
"Reply All" additionally reaches $QUANTECON_EMAIL_REVIEWER (the human
approver Cc'd on the original send).

Required environment:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM  (org secrets)
  PSL_EMAIL, QUANTECON_EMAIL_REVIEWER                         (org variables)

CLI:
  python -m scripts.notify_email \\
      --submission submissions/2025-11/mmcky-invoice-2025-11.yml \\
      --settings   config/settings.yml \\
      --pdf        generated_pdfs/2025-11/mmcky-invoice-2025-11.pdf \\
      --issue-url  https://github.com/QuantEcon/test-contractor-payments/issues/13 \\
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
    value = notifications.get("testing_mode", True)
    # Present-but-null (`testing_mode:` with no value) parses to None; treat it
    # as unset and fail safe to True rather than letting bool(None) == False
    # silently enable production emailing.
    return True if value is None else bool(value)


def _effective_testing_mode(settings: dict, fiscal_host_path: Path) -> tuple[bool, str]:
    """Resolve testing_mode with the per-repo override taking precedence over
    the engine-wide default. Order (most specific wins):

      1. contractor `config/settings.yml` → notifications.testing_mode
      2. engine `templates/fiscal-host.yml` → notifications.testing_mode
      3. True (fail-safe — never email PSL if nothing is configured)

    The per-repo layer lets production and test repos coexist: a real
    contractor opts into PSL delivery with `testing_mode: false` in their own
    settings, while a test repo inherits the safe default with no config.
    Returns (testing_mode, source) — `source` names the deciding layer for
    operational logging and the audit comment."""
    repo_notifications = settings.get("notifications") or {}
    repo_value = repo_notifications.get("testing_mode")
    # `is not None` (not `in`) so a present-but-null override counts as unset and
    # falls through to the engine default — a blank `testing_mode:` must never
    # silently flip a repo into production emailing.
    if repo_value is not None:
        return bool(repo_value), "repo settings.yml"
    return _read_testing_mode(fiscal_host_path), "engine fiscal-host.yml default"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable `{name}`. See notes/EMAIL_SETUP.md."
        )
    return value


# Receipt attachment MIME types by extension (fetch_receipts.py allowlist).
# fetch_receipts canonicalises `.jpeg` to `.jpg` and so never stages one; the
# entry stays for hand-added or legacy committed receipts.
_RECEIPT_MIME = {
    ".pdf": ("application", "pdf"),
    ".png": ("image", "png"),
    ".jpg": ("image", "jpeg"),
    ".jpeg": ("image", "jpeg"),
}


def _receipt_mime(path: Path) -> tuple[str, str]:
    """MIME type for a receipt attachment, from its extension.

    Trusting the extension is sound only because `fetch_receipts` stores
    receipts under the extension implied by their *magic bytes*, so the
    suffix reflects the content rather than the name the contractor uploaded.
    An unrecognised suffix is therefore unexpected: fall back to the generic
    binary type and say so, rather than guessing (mimetypes.guess_type would
    cheerfully label anything) — this email goes to the fiscal host, and a
    file must never arrive labelled as something it isn't.
    """
    mime = _RECEIPT_MIME.get(path.suffix.lower())
    if mime is None:
        print(
            f"WARNING: receipt {path.name} has an unexpected extension "
            f"`{path.suffix}` — attaching it as application/octet-stream.",
            file=sys.stderr,
        )
        return ("application", "octet-stream")
    return mime


def select_receipt_paths(
    submission: dict,
    receipts_dir: Optional[Path],
) -> tuple[list[Path], list[str]]:
    """Resolve which receipt files to attach, from the submission's own list.

    Returns `(paths, warnings)`. The submission YAML is the authority: it is
    what the admin approved and what the claim PDF renders from, so driving
    the attachments off it keeps the email, the PDF and the approved record in
    agreement by construction.

    Globbing the directory instead does not hold that property. The receipts
    directory is keyed by submission id, so a re-submitted claim reuses it,
    and anything an earlier run left behind — a receipt the contractor
    withdrew, or a kept receipt duplicated under its old index prefix — would
    be attached and sent to the fiscal host without appearing in the PDF.
    Stray and missing files are both reported rather than silently resolved.
    """
    warnings: list[str] = []
    if not receipts_dir or not receipts_dir.is_dir():
        return [], warnings

    paths: list[Path] = []
    for entry in submission.get("receipts") or []:
        filename = entry.get("filename") if isinstance(entry, dict) else entry
        if not filename:
            continue
        # Basename only — a YAML value must never escape the receipts dir.
        path = receipts_dir / Path(str(filename)).name
        if path.is_file():
            paths.append(path)
        else:
            warnings.append(
                f"submission lists receipt `{filename}` but {path} is "
                f"missing — it will NOT be attached."
            )

    listed = {p.name for p in paths}
    for stray in sorted(receipts_dir.iterdir()):
        if stray.is_file() and stray.name not in listed:
            warnings.append(
                f"{stray} is present but not listed in the submission — "
                f"not attached."
            )
    return paths, warnings


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
    receipt_paths: Optional[list[Path]] = None,
) -> EmailMessage:
    """Build the email message. Pure function — takes the already-resolved
    recipients (testing_mode logic happens in main() before calling us).

    `receipt_paths` (reimbursement claims) are attached after the claim PDF
    so PSL receives the evidence alongside the paperwork."""
    receipt_paths = receipt_paths or []
    submission_type = submission.get("type", "timesheet")
    type_label = _TYPE_LABEL.get(submission_type, "Submission")
    period = submission["period"]
    totals = submission["totals"]
    currency = totals.get("currency", "")
    amount_display = f"{_fmt_amount(totals['amount'], currency)} {currency}".strip()

    real_name = contractor.get("name", submission.get("submitted_by", "Unknown"))
    github_handle = contractor.get("github", submission.get("submitted_by", ""))

    # Revisions get a REVISION marker in the subject so PSL spots the
    # correction at a glance in their inbox. PDF banner carries the full
    # "supersedes X" context; the subject just flags the type. Phase 2.5.
    revision_marker = "REVISION " if submission.get("supersedes") else ""
    subject = (
        f"[QuantEcon] {type_label} {revision_marker}approved — "
        f"{real_name} — {period} — {amount_display}"
    )

    approver_handle = submission.get("approved_by") or "admin"
    body_lines = [
        f"Approved by QuantEcon @{approver_handle} for processing.",
        "",
        f"Contractor:    {real_name} (@{github_handle})",
    ]
    if submission_type == "reimbursement":
        # Contractor-level claim: the PSL funding code replaces the contract
        # reference so PSL knows the cost centre to bill.
        body_lines.append(f"Project:       {submission.get('project', '—')}")
    else:
        body_lines.append(f"Contract:      {submission['contract_id']}")
    body_lines.extend([
        f"Type:          {type_label}",
        f"Period:        {period}",
        f"Amount:        {amount_display}",
        f"Approved:      {submission.get('approved_date', '—')} "
        f"by @{submission.get('approved_by', '—')}",
        "",
    ])
    if submission_type == "reimbursement":
        count = len(receipt_paths)
        body_lines.append(
            f"Attached: the approved claim PDF and {count} receipt file(s)."
        )
    else:
        body_lines.append("Attached: the approved invoice PDF.")
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

    # Receipt attachments (reimbursement claims).
    for receipt_path in receipt_paths:
        maintype, subtype = _receipt_mime(receipt_path)
        with open(receipt_path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype=maintype, subtype=subtype,
                filename=receipt_path.name,
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
    p.add_argument("--receipts-dir", type=Path, default=None,
                   help="Directory of committed receipt files to attach "
                        "(reimbursement claims: receipts/{period}/{submission_id}). "
                        "Missing or empty directory is tolerated (no receipts "
                        "attached).")
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
    testing_mode, testing_mode_source = _effective_testing_mode(settings, fiscal_host_path)

    psl_email = os.environ.get("PSL_EMAIL", "").strip()
    reviewer_email = os.environ.get("QUANTECON_EMAIL_REVIEWER", "").strip()
    if not reviewer_email:
        raise RuntimeError(
            "QUANTECON_EMAIL_REVIEWER env var is required "
            "(used as both Cc and testing-mode To)."
        )

    if testing_mode:
        to_addr = reviewer_email
        cc_addr = None
        print(f"testing_mode=true ({testing_mode_source}) — sending to "
              f"{reviewer_email} only (PSL will not be contacted).",
              file=sys.stderr)
    else:
        if not psl_email:
            raise RuntimeError(
                "PSL_EMAIL env var is required when testing_mode=false."
            )
        to_addr = psl_email
        cc_addr = reviewer_email
        print(f"testing_mode=false ({testing_mode_source}) — sending to "
              f"{psl_email} (Cc {reviewer_email}).", file=sys.stderr)

    receipt_paths, receipt_warnings = select_receipt_paths(
        submission, args.receipts_dir
    )
    for warning in receipt_warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    sender = _require_env("SMTP_FROM") if not args.dry_run else os.environ.get("SMTP_FROM", "<SMTP_FROM>")
    msg = compose_message(
        submission=submission,
        contractor=contractor,
        pdf_path=args.pdf,
        issue_url=args.issue_url,
        sender=sender,
        to=to_addr,
        cc=cc_addr,
        reply_to=sender,
        receipt_paths=receipt_paths,
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
        attachment_names = [args.pdf.name, *(p.name for p in receipt_paths)]
        print(f"--- (with attachments: {', '.join(attachment_names)}) ---")
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
        "testing_mode_source": testing_mode_source,
        "dry_run": args.dry_run,
        "attachments": 1 + len(receipt_paths),  # claim/invoice PDF + receipts
    }

    if args.output_summary:
        args.output_summary.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_summary, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
