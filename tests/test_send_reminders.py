"""Tests for the pure helpers in scripts/send_reminders.py.

GitHub-API-touching functions (list issues, comment exists, post comment)
are integration territory — exercised against `test-contractor-payments`
during Phase 3c E2E. These tests cover period extraction, period-closed
arithmetic, and the rendered reminder body.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.send_reminders import (
    extract_period,
    is_period_closed,
    render_reminder_comment,
    sentinel_for,
    submission_type_from_labels,
)


# ─── Period extraction ──────────────────────────────────────────────────────

class TestExtractPeriod:
    def test_well_formed_body(self):
        body = (
            "### Contract\n\nQE-PSL-2026-001\n\n"
            "### Year\n\n2026\n\n"
            "### Month\n\n04 — April\n\n"
            "### Time Entries\n\n```text\nDate | Hours | Description\n```\n"
        )
        assert extract_period(body) == "2026-04"

    def test_missing_year(self):
        body = "### Month\n\n04 — April\n"
        assert extract_period(body) is None

    def test_missing_month(self):
        body = "### Year\n\n2026\n"
        assert extract_period(body) is None

    def test_blank_year(self):
        body = "### Year\n\n_No response_\n\n### Month\n\n04 — April\n"
        assert extract_period(body) is None

    def test_unparseable_year(self):
        body = "### Year\n\ntwenty-twenty-six\n\n### Month\n\n04 — April\n"
        assert extract_period(body) is None

    def test_month_with_em_dash(self):
        body = "### Year\n\n2025\n\n### Month\n\n12 — December\n"
        assert extract_period(body) == "2025-12"

    def test_empty_body(self):
        assert extract_period("") is None

    def test_none_body(self):
        assert extract_period(None) is None


# ─── Period-closed arithmetic ───────────────────────────────────────────────

class TestIsPeriodClosed:
    def _now(self, iso: str, tz: str = "UTC") -> datetime:
        # parse `2026-05-01T00:00:00` style strings
        dt = datetime.fromisoformat(iso)
        return dt.replace(tzinfo=ZoneInfo(tz))

    def test_open_when_inside_period(self):
        assert is_period_closed("2026-04", self._now("2026-04-15T12:00:00")) is False

    def test_closed_at_first_of_next_month(self):
        assert is_period_closed("2026-04", self._now("2026-05-01T00:00:00")) is True

    def test_closed_well_after(self):
        assert is_period_closed("2026-04", self._now("2026-07-15T00:00:00")) is True

    def test_year_boundary(self):
        # December 2025 closes at 2026-01-01.
        assert is_period_closed("2025-12", self._now("2025-12-31T23:00:00")) is False
        assert is_period_closed("2025-12", self._now("2026-01-01T00:00:00")) is True

    def test_open_at_last_second_of_period(self):
        # 2026-04-30 23:59:59 is still inside the period.
        assert is_period_closed("2026-04", self._now("2026-04-30T23:59:59")) is False

    def test_respects_timezone_of_now(self):
        # If now is May 1 00:00 in America/New_York, that's still April 30
        # in some Asian zones but closure is computed in the same tz as `now`.
        ny_now = datetime(2026, 5, 1, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        assert is_period_closed("2026-04", ny_now) is True


# ─── Reminder rendering ─────────────────────────────────────────────────────

class TestRenderReminderComment:
    def test_includes_sentinel(self):
        out = render_reminder_comment("2026-04", "timesheet")
        assert sentinel_for("2026-04") in out
        non_empty = [ln for ln in out.splitlines() if ln.strip()]
        assert non_empty[-1] == sentinel_for("2026-04")

    def test_period_in_body(self):
        out = render_reminder_comment("2026-04", "timesheet")
        assert "2026-04" in out

    def test_timesheet_label(self):
        out = render_reminder_comment("2026-04", "timesheet")
        assert "timesheet" in out
        assert "invoice" not in out

    def test_invoice_label(self):
        out = render_reminder_comment("2026-04", "milestone_invoice")
        assert "invoice" in out
        # The word "timesheet" should not appear when rendering an invoice reminder.
        assert "timesheet" not in out

    def test_call_to_action_present(self):
        out = render_reminder_comment("2026-04", "timesheet")
        assert "/submit" in out
        assert "/validate" in out

    def test_close_advice_present(self):
        out = render_reminder_comment("2026-04", "timesheet")
        assert "close this issue" in out.lower()

    def test_period_specific_sentinel_distinct(self):
        a = render_reminder_comment("2026-04", "timesheet")
        b = render_reminder_comment("2026-05", "timesheet")
        assert sentinel_for("2026-04") in a
        assert sentinel_for("2026-05") in b
        assert sentinel_for("2026-04") not in b
        assert sentinel_for("2026-05") not in a


# ─── Label routing ──────────────────────────────────────────────────────────

class TestSubmissionTypeFromLabels:
    def test_timesheet(self):
        assert submission_type_from_labels(["timesheet", "pending-review"]) == "timesheet"

    def test_milestone(self):
        assert submission_type_from_labels(["milestone-invoice", "pending-review"]) == "milestone_invoice"

    def test_neither(self):
        assert submission_type_from_labels(["pending-review", "wontfix"]) is None

    def test_empty(self):
        assert submission_type_from_labels([]) is None

    def test_both_prefers_timesheet(self):
        # Defensive — shouldn't happen, but be deterministic.
        assert submission_type_from_labels(["timesheet", "milestone-invoice"]) == "timesheet"


class TestReimbursementReminders:
    def test_reimbursement_label_routes(self):
        from scripts.send_reminders import submission_type_from_labels
        assert submission_type_from_labels(["reimbursement", "pending-review"]) == "reimbursement"

    def test_reimbursement_in_submission_labels(self):
        from scripts.send_reminders import SUBMISSION_LABELS
        assert "reimbursement" in SUBMISSION_LABELS

    def test_reminder_comment_names_claim(self):
        from scripts.send_reminders import render_reminder_comment, sentinel_for
        out = render_reminder_comment("2026-06", "reimbursement")
        assert "reimbursement claim" in out
        assert sentinel_for("2026-06") in out
