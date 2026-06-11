"""Tests for the pure comment renderer in scripts/notify_comment.py.

Added in Phase 5 alongside the reimbursement branch; the hourly/milestone
cases double as regression guards for the contract_id refactor (the
renderer previously KeyError'd on submissions without a contract_id).
"""
from __future__ import annotations

from scripts.notify_comment import compose_comment


HOURLY_SUBMISSION = {
    "submission_id": "janedoe-timesheet-2026-04",
    "type": "timesheet",
    "contract_id": "QE-PSL-2026-001",
    "period": "2026-04",
    "approved_by": "mmcky",
    "approved_date": "2026-05-13",
    "totals": {"hours": 14.5, "rate": 50.0, "amount": 725.0, "currency": "AUD"},
}

HOURLY_LEDGER = {
    "contract_id": "QE-PSL-2026-001",
    "type": "hourly",
    "currency": "AUD",
    "totals": {"hours_to_date": 14.5, "amount_to_date": 725.0, "submissions_count": 1},
}

REIMBURSEMENT_SUBMISSION = {
    "submission_id": "janedoe-reimbursement-2026-06",
    "type": "reimbursement",
    "project": "CHOW",
    "period": "2026-06",
    "approved_by": "mmcky",
    "approved_date": "2026-06-12",
    "totals": {"amount": 12300, "currency": "JPY"},
}

REIMBURSEMENT_LEDGER = {
    "type": "reimbursement",
    "claims": [],
    "totals": {
        "JPY": {"amount_to_date": 24300, "claims_count": 2},
        "AUD": {"amount_to_date": 800.25, "claims_count": 1},
    },
}

EMAIL_SUMMARY = {
    "to": "reviewer@example.org",
    "cc": None,
    "subject": "x",
    "sent_at": "2026-06-12 01:00:00 UTC",
    "testing_mode": True,
    "testing_mode_source": "engine fiscal-host.yml default",
    "dry_run": False,
}


class TestComposeCommentExistingTypes:
    def test_hourly_renders_contract_and_ledger(self):
        out = compose_comment(
            submission=HOURLY_SUBMISSION, ledger=HOURLY_LEDGER,
            email_summary=EMAIL_SUMMARY, issue_number=42,
        )
        assert "**Timesheet approved** by @mmcky" in out
        assert "**Contract:** `QE-PSL-2026-001`" in out
        assert "725.00 AUD" in out
        assert "14.5 hours" in out
        assert "sent to reviewer@example.org" in out

    def test_missing_email_summary_warns(self):
        out = compose_comment(
            submission=HOURLY_SUBMISSION, ledger=HOURLY_LEDGER,
            email_summary=None, issue_number=42,
        )
        assert "not sent" in out


class TestComposeCommentReimbursement:
    def _compose(self):
        return compose_comment(
            submission=REIMBURSEMENT_SUBMISSION, ledger=REIMBURSEMENT_LEDGER,
            email_summary=EMAIL_SUMMARY, issue_number=42,
        )

    def test_project_line_replaces_contract_line(self):
        out = self._compose()
        assert "**Project:** `CHOW`" in out
        assert "**Contract:**" not in out

    def test_ledger_line_uses_currency_bucket(self):
        out = self._compose()
        # This claim's currency bucket only — not the AUD bucket.
        assert "running total: 24,300 JPY across 2 JPY claim(s)" in out
        assert "800.25" not in out

    def test_no_keyerror_without_contract_id(self):
        # The pre-Phase-5 renderer did submission["contract_id"] — this is
        # the regression guard for that latent KeyError.
        out = self._compose()
        assert "Reimbursement Claim approved" in out
