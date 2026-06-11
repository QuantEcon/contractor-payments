"""Tests for the renderers in scripts/post_validate_result.py.

GitHub-API-touching functions (find/create/update/delete comment) are
integration territory — exercised against `test-contractor-payments` during
Phase 3c E2E. These tests cover the pure rendering paths only.
"""
from __future__ import annotations

from scripts.post_validate_result import (
    SENTINEL,
    render_error_comment,
    render_success_comment,
)


# ─── Success renderer ───────────────────────────────────────────────────────

class TestRenderSuccessComment:
    def _enriched_timesheet(self) -> dict:
        return {
            "type": "timesheet",
            "contract_id": "QE-PSL-2026-001",
            "period": "2026-04",
            "entries": [
                {"date": "2026-04-06", "hours": 3.5, "description": "x"},
                {"date": "2026-04-13", "hours": 5.0, "description": "y"},
                {"date": "2026-04-20", "hours": 4.0, "description": "z"},
            ],
            "totals": {
                "hours": 12.5,
                "rate": 45.00,
                "amount": 562.50,
                "currency": "AUD",
            },
        }

    def _enriched_milestone(self) -> dict:
        return {
            "type": "milestone_invoice",
            "contract_id": "QE-IUJ-2025-002",
            "period": "2026-07",
            "entries": [
                {"id": 3, "date": "2026-07-15", "amount": 77000, "description": "July"},
            ],
            "totals": {"amount": 77000, "currency": "JPY"},
        }

    def test_includes_sentinel(self):
        out = render_success_comment(self._enriched_timesheet())
        assert SENTINEL in out
        non_empty = [ln for ln in out.splitlines() if ln.strip()]
        assert non_empty[-1] == SENTINEL

    def test_header_signals_success(self):
        out = render_success_comment(self._enriched_timesheet())
        assert "Validation passed" in out
        assert "✅" in out

    def test_timesheet_shows_hours_rate_total(self):
        out = render_success_comment(self._enriched_timesheet())
        assert "12.5" in out         # hours
        assert "45.0" in out          # rate
        assert "562.5" in out         # total
        assert "AUD" in out

    def test_timesheet_shows_entry_count_pluralised(self):
        out = render_success_comment(self._enriched_timesheet())
        assert "3 days" in out

    def test_timesheet_single_entry_singular(self):
        enriched = self._enriched_timesheet()
        enriched["entries"] = enriched["entries"][:1]
        out = render_success_comment(enriched)
        assert "1 day" in out
        assert "1 days" not in out

    def test_milestone_shows_total_currency(self):
        out = render_success_comment(self._enriched_milestone())
        assert "77000" in out
        assert "JPY" in out

    def test_milestone_does_not_show_rate(self):
        out = render_success_comment(self._enriched_milestone())
        # Milestone contracts have no `rate` — make sure we don't try to render one.
        assert "Rate" not in out
        assert "/hour" not in out

    def test_includes_call_to_action(self):
        out = render_success_comment(self._enriched_timesheet())
        assert "/submit" in out
        assert "submit" in out.lower()

    def test_contract_id_in_output(self):
        out = render_success_comment(self._enriched_timesheet())
        assert "QE-PSL-2026-001" in out

    def test_period_in_output(self):
        out = render_success_comment(self._enriched_timesheet())
        assert "2026-04" in out


# ─── Error renderer ─────────────────────────────────────────────────────────

class TestRenderErrorComment:
    def test_includes_sentinel(self):
        out = render_error_comment([{"message": "x"}])
        assert SENTINEL in out
        non_empty = [ln for ln in out.splitlines() if ln.strip()]
        assert non_empty[-1] == SENTINEL

    def test_header_signals_failure(self):
        out = render_error_comment([{"message": "x"}])
        assert "Validation failed" in out
        assert "❌" in out

    def test_line_specific_error_formatted(self):
        out = render_error_comment([
            {"line": 3, "message": "couldn't read a date from `2025/01/05`"},
        ])
        assert "**Line 3:**" in out
        assert "2025/01/05" in out

    def test_general_error_no_line_prefix(self):
        out = render_error_comment([
            {"message": "Period field is required.", "line": None},
        ])
        assert "Line " not in out
        assert "- Period field is required." in out

    def test_multiple_errors_each_appear(self):
        out = render_error_comment([
            {"line": 1, "message": "bad date"},
            {"line": 3, "message": "bad hours"},
            {"message": "missing confirmation"},
        ])
        assert "**Line 1:**" in out
        assert "**Line 3:**" in out
        assert "missing confirmation" in out

    def test_warnings_section_only_when_present(self):
        without = render_error_comment([{"message": "x"}])
        assert "Notes" not in without

        with_warn = render_error_comment(
            [{"message": "x"}],
            warnings=[{"message": "Used `,` as separator — please use `|`."}],
        )
        assert "Notes" in with_warn

    def test_includes_revalidate_instruction(self):
        out = render_error_comment([{"message": "x"}])
        assert "/validate" in out


# ─── Reimbursement success renderer (§4.7, Phase 5) ─────────────────────────

class TestRenderSuccessCommentReimbursement:
    def _enriched_reimbursement(self) -> dict:
        return {
            "type": "reimbursement",
            "project": "CHOW",
            "period": "2026-06",
            "entries": [
                {"date": "2026-06-03", "amount": 12000, "category": "accommodation",
                 "description": "Hotel"},
                {"date": "2026-06-05", "amount": 300, "category": "meals",
                 "description": "Dinner"},
            ],
            "receipts": [
                {"filename": "taxi-receipt.png", "source_url": "https://github.com/user-attachments/assets/aaa"},
                {"filename": "hotel-invoice.pdf", "source_url": "https://github.com/user-attachments/files/1/h.pdf"},
            ],
            "totals": {"amount": 12300, "currency": "JPY"},
        }

    def test_project_row_replaces_contract_row(self):
        from scripts.post_validate_result import render_success_comment
        out = render_success_comment(self._enriched_reimbursement())
        assert "| Project | `CHOW` |" in out
        assert "| Contract |" not in out

    def test_line_items_receipts_and_total(self):
        from scripts.post_validate_result import render_success_comment
        out = render_success_comment(self._enriched_reimbursement())
        assert "| Line items | 2 |" in out
        assert "| Receipts found | 2 |" in out
        assert "**12300 JPY**" in out

    def test_sentinel_present(self):
        from scripts.post_validate_result import render_success_comment, SENTINEL
        out = render_success_comment(self._enriched_reimbursement())
        assert SENTINEL in out
