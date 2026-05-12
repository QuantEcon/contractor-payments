"""Tests for the pure data-transformation helpers in create_submission_pr.py.

git/gh side effects are integration territory — exercised in the end-of-Phase-1
test against contractor-engine-test.
"""
from __future__ import annotations

import pytest

from scripts.create_submission_pr import (
    branch_name_for_issue,
    enrich_submission,
    format_currency_amount,
    generate_submission_id,
    render_pr_body,
)


# ─── Helpers ────────────────────────────────────────────────────────────────

PARSED_SAMPLE = {
    "type": "timesheet",
    "contract_id": "jane-doe-hourly-2025",
    "period": "2025-01",
    "entries": [
        {"date": "2025-01-06", "hours": 3.5, "description": "NumPy review"},
        {"date": "2025-01-13", "hours": 5.0, "description": "Plotting examples"},
    ],
    "totals": {"hours": 8.5},
    "notes": "",
    "status": "pending",
}

CONTRACT_HOURLY_AUD = {
    "contract_id": "jane-doe-hourly-2025",
    "type": "hourly",
    "status": "active",
    "start_date": "2025-01-01",
    "end_date": "2025-12-31",
    "terms": {
        "hourly_rate": 45.00,
        "currency": "AUD",
        "max_hours_per_month": 40,
    },
}


# ─── Submission ID ──────────────────────────────────────────────────────────

class TestGenerateSubmissionId:
    def test_combines_handle_and_issue(self):
        assert generate_submission_id("janedoe", 42) == "janedoe-timesheet-42"

    def test_handle_with_dashes_preserved(self):
        assert generate_submission_id("jane-doe", 7) == "jane-doe-timesheet-7"


# ─── Currency formatting ────────────────────────────────────────────────────

class TestCurrencyFormatting:
    def test_aud_rounds_to_two_decimals(self):
        assert format_currency_amount(45.5555, "AUD") == 45.56

    def test_usd_rounds_to_two_decimals(self):
        assert format_currency_amount(100.001, "USD") == 100.00

    def test_jpy_rounds_to_integer(self):
        assert format_currency_amount(5000.7, "JPY") == 5001
        assert isinstance(format_currency_amount(5000.7, "JPY"), int)

    def test_currency_case_insensitive(self):
        assert format_currency_amount(5000.0, "jpy") == 5000
        assert isinstance(format_currency_amount(5000.0, "jpy"), int)


# ─── Enrichment ─────────────────────────────────────────────────────────────

class TestEnrichSubmission:
    def test_basic_enrichment(self):
        result = enrich_submission(
            PARSED_SAMPLE, CONTRACT_HOURLY_AUD,
            submitter="janedoe", issue_number=42, submitted_date="2025-02-01",
        )
        assert result["submission_id"] == "janedoe-timesheet-42"
        assert result["contract_id"] == "jane-doe-hourly-2025"
        assert result["type"] == "timesheet"
        assert result["period"] == "2025-01"
        assert result["submitted_date"] == "2025-02-01"
        assert result["submitted_by"] == "janedoe"
        assert result["issue_number"] == 42
        assert result["status"] == "pending"
        assert result["approved_by"] is None
        assert result["approved_date"] is None

    def test_totals_computed_correctly(self):
        result = enrich_submission(
            PARSED_SAMPLE, CONTRACT_HOURLY_AUD,
            submitter="janedoe", issue_number=42, submitted_date="2025-02-01",
        )
        # 8.5 hours * 45.00 AUD = 382.50
        assert result["totals"]["hours"] == 8.5
        assert result["totals"]["rate"] == 45.00
        assert result["totals"]["amount"] == 382.50
        assert result["totals"]["currency"] == "AUD"

    def test_jpy_totals_are_integers(self):
        contract = {
            **CONTRACT_HOURLY_AUD,
            "terms": {"hourly_rate": 5000, "currency": "JPY", "max_hours_per_month": 40},
        }
        result = enrich_submission(
            PARSED_SAMPLE, contract,
            submitter="janedoe", issue_number=1, submitted_date="2025-02-01",
        )
        # 8.5 * 5000 = 42500
        assert result["totals"]["amount"] == 42500
        assert isinstance(result["totals"]["amount"], int)
        assert result["totals"]["currency"] == "JPY"

    def test_entries_preserved(self):
        result = enrich_submission(
            PARSED_SAMPLE, CONTRACT_HOURLY_AUD,
            submitter="janedoe", issue_number=42, submitted_date="2025-02-01",
        )
        assert result["entries"] == PARSED_SAMPLE["entries"]

    def test_notes_preserved(self):
        parsed = {**PARSED_SAMPLE, "notes": "Travel time excluded."}
        result = enrich_submission(
            parsed, CONTRACT_HOURLY_AUD,
            submitter="x", issue_number=1, submitted_date="2025-02-01",
        )
        assert result["notes"] == "Travel time excluded."

    def test_contract_id_mismatch_raises(self):
        bad_contract = {**CONTRACT_HOURLY_AUD, "contract_id": "different-id"}
        with pytest.raises(ValueError, match="mismatch"):
            enrich_submission(
                PARSED_SAMPLE, bad_contract,
                submitter="x", issue_number=1, submitted_date="2025-02-01",
            )

    def test_non_hourly_contract_raises(self):
        bad_contract = {**CONTRACT_HOURLY_AUD, "type": "milestone"}
        with pytest.raises(ValueError, match="only `hourly` is supported"):
            enrich_submission(
                PARSED_SAMPLE, bad_contract,
                submitter="x", issue_number=1, submitted_date="2025-02-01",
            )

    def test_missing_terms_raises(self):
        bad_contract = {**CONTRACT_HOURLY_AUD, "terms": {"hourly_rate": 45.00}}
        with pytest.raises(ValueError, match="missing required terms"):
            enrich_submission(
                PARSED_SAMPLE, bad_contract,
                submitter="x", issue_number=1, submitted_date="2025-02-01",
            )


# ─── PR body ────────────────────────────────────────────────────────────────

class TestRenderPrBody:
    def _sample_submission(self) -> dict:
        return enrich_submission(
            PARSED_SAMPLE, CONTRACT_HOURLY_AUD,
            submitter="janedoe", issue_number=42, submitted_date="2025-02-01",
        )

    def test_closes_issue(self):
        body = render_pr_body(
            issue_number=42, submitter="janedoe",
            submission=self._sample_submission(),
            submission_path_rel="submissions/2025-01/janedoe-timesheet-42.yml",
        )
        assert "Closes #42" in body

    def test_includes_totals(self):
        body = render_pr_body(
            issue_number=42, submitter="janedoe",
            submission=self._sample_submission(),
            submission_path_rel="submissions/2025-01/janedoe-timesheet-42.yml",
        )
        assert "382.5" in body or "382.50" in body
        assert "AUD" in body
        assert "8.5" in body

    def test_warnings_appear_when_present(self):
        body = render_pr_body(
            issue_number=42, submitter="janedoe",
            submission=self._sample_submission(),
            submission_path_rel="submissions/2025-01/janedoe-timesheet-42.yml",
            warnings=[{"message": "Used `,` as separator — please use `|` next time."}],
        )
        assert "Parse warnings" in body
        assert "`,`" in body

    def test_no_warnings_section_when_empty(self):
        body = render_pr_body(
            issue_number=42, submitter="janedoe",
            submission=self._sample_submission(),
            submission_path_rel="submissions/2025-01/janedoe-timesheet-42.yml",
            warnings=[],
        )
        assert "Parse warnings" not in body


# ─── Branch naming ──────────────────────────────────────────────────────────

class TestBranchNaming:
    def test_branch_name_format(self):
        assert branch_name_for_issue(42) == "submission/issue-42"
