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
    resolve_collision_suffix,
    resolve_payer_today,
)


# ─── Helpers ────────────────────────────────────────────────────────────────

PARSED_SAMPLE = {
    "type": "timesheet",
    "contract_id": "QE-PSL-2025-001",
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
    "contract_id": "QE-PSL-2025-001",
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
    def test_period_based(self):
        assert generate_submission_id("janedoe", "2026-06") == "janedoe-timesheet-2026-06"

    def test_handle_with_dashes_preserved(self):
        assert generate_submission_id("jane-doe", "2025-12") == "jane-doe-timesheet-2025-12"


class TestResolveCollisionSuffix:
    def test_no_collision_returns_base(self, tmp_path):
        result = resolve_collision_suffix(tmp_path, "janedoe-timesheet-2026-06", "2026-06")
        assert result == "janedoe-timesheet-2026-06"

    def test_first_collision_yields_v2(self, tmp_path):
        period_dir = tmp_path / "submissions" / "2026-06"
        period_dir.mkdir(parents=True)
        (period_dir / "janedoe-timesheet-2026-06.yml").touch()
        result = resolve_collision_suffix(tmp_path, "janedoe-timesheet-2026-06", "2026-06")
        assert result == "janedoe-timesheet-2026-06-v2"

    def test_second_collision_yields_v3(self, tmp_path):
        period_dir = tmp_path / "submissions" / "2026-06"
        period_dir.mkdir(parents=True)
        (period_dir / "janedoe-timesheet-2026-06.yml").touch()
        (period_dir / "janedoe-timesheet-2026-06-v2.yml").touch()
        result = resolve_collision_suffix(tmp_path, "janedoe-timesheet-2026-06", "2026-06")
        assert result == "janedoe-timesheet-2026-06-v3"

    def test_different_handle_no_collision(self, tmp_path):
        """Collisions are per-handle; another contractor's submission in the
        same period doesn't trigger a suffix."""
        period_dir = tmp_path / "submissions" / "2026-06"
        period_dir.mkdir(parents=True)
        (period_dir / "alice-timesheet-2026-06.yml").touch()
        result = resolve_collision_suffix(tmp_path, "bob-timesheet-2026-06", "2026-06")
        assert result == "bob-timesheet-2026-06"


# ─── Payer-timezone date ────────────────────────────────────────────────────

class TestResolvePayerToday:
    def test_reads_timezone_from_fiscal_host(self, tmp_path):
        fiscal_host = tmp_path / "fiscal-host.yml"
        fiscal_host.write_text(
            "psl_foundation:\n  timezone: America/New_York\n",
            encoding="utf-8",
        )
        result = resolve_payer_today(fiscal_host)
        # ISO date format; specific value is wall-clock-dependent, so just check shape.
        assert len(result) == 10 and result[4] == "-" and result[7] == "-"

    def test_falls_back_to_utc_when_fiscal_host_missing(self, tmp_path):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        result = resolve_payer_today(tmp_path / "nonexistent.yml")
        assert result == datetime.now(ZoneInfo("UTC")).date().isoformat()

    def test_falls_back_to_utc_when_timezone_field_missing(self, tmp_path):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        fiscal_host = tmp_path / "fiscal-host.yml"
        fiscal_host.write_text("psl_foundation:\n  name: PSL Foundation\n", encoding="utf-8")
        result = resolve_payer_today(fiscal_host)
        assert result == datetime.now(ZoneInfo("UTC")).date().isoformat()


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

def _enrich(parsed=PARSED_SAMPLE, contract=CONTRACT_HOURLY_AUD, **overrides):
    """Helper: enrich with sensible defaults so individual tests stay focused."""
    kwargs = dict(
        submitter="janedoe",
        submission_id="janedoe-timesheet-2025-01",
        issue_number=42,
        submitted_date="2025-02-01",
    )
    kwargs.update(overrides)
    return enrich_submission(parsed, contract, **kwargs)


class TestEnrichSubmission:
    def test_basic_enrichment(self):
        result = _enrich()
        assert result["submission_id"] == "janedoe-timesheet-2025-01"
        assert result["contract_id"] == "QE-PSL-2025-001"
        assert result["type"] == "timesheet"
        assert result["period"] == "2025-01"
        assert result["submitted_date"] == "2025-02-01"
        assert result["submitted_by"] == "janedoe"
        assert result["issue_number"] == 42
        assert result["status"] == "pending"
        assert result["approved_by"] is None
        assert result["approved_date"] is None

    def test_contract_dates_included(self):
        result = _enrich()
        assert result["contract_start_date"] == "2025-01-01"
        assert result["contract_end_date"] == "2025-12-31"

    def test_totals_computed_correctly(self):
        result = _enrich()
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
        result = _enrich(contract=contract)
        # 8.5 * 5000 = 42500
        assert result["totals"]["amount"] == 42500
        assert isinstance(result["totals"]["amount"], int)
        assert result["totals"]["currency"] == "JPY"

    def test_entries_preserved(self):
        result = _enrich()
        assert result["entries"] == PARSED_SAMPLE["entries"]

    def test_entries_sorted_by_date(self):
        unsorted = {
            **PARSED_SAMPLE,
            "entries": [
                {"date": "2025-01-13", "hours": 5.0, "description": "Plotting examples"},
                {"date": "2025-01-06", "hours": 3.5, "description": "NumPy review"},
            ],
        }
        result = _enrich(parsed=unsorted)
        assert [e["date"] for e in result["entries"]] == ["2025-01-06", "2025-01-13"]

    def test_notes_preserved(self):
        parsed = {**PARSED_SAMPLE, "notes": "Travel time excluded."}
        result = _enrich(parsed=parsed)
        assert result["notes"] == "Travel time excluded."

    def test_contract_id_mismatch_raises(self):
        bad_contract = {**CONTRACT_HOURLY_AUD, "contract_id": "different-id"}
        with pytest.raises(ValueError, match="mismatch"):
            _enrich(contract=bad_contract)

    def test_non_hourly_contract_raises_for_timesheet(self):
        bad_contract = {**CONTRACT_HOURLY_AUD, "type": "milestone"}
        with pytest.raises(ValueError, match="requires a `hourly` contract"):
            _enrich(contract=bad_contract)

    def test_missing_terms_raises(self):
        bad_contract = {**CONTRACT_HOURLY_AUD, "terms": {"hourly_rate": 45.00}}
        with pytest.raises(ValueError, match="missing required terms"):
            _enrich(contract=bad_contract)


# ─── Enrichment: milestone invoice ──────────────────────────────────────────

CONTRACT_MILESTONE_JPY = {
    "contract_id": "QE-IUJ-2025-002",
    "type": "milestone",
    "status": "active",
    "start_date": "2025-09-01",
    "end_date": "2026-02-28",
    "currency": "JPY",
    "project": "iuj-visit",
    "notes": "Six monthly payments of 77000 JPY.",
}

PARSED_MILESTONE_SAMPLE = {
    "type": "milestone_invoice",
    "contract_id": "QE-IUJ-2025-002",
    "period": "2025-11",
    "entries": [
        {"id": "3", "date": "2025-11-15", "amount": 77000.0,
         "description": "Monthly Payment — November"},
    ],
    "totals": {"amount": 77000.0},
    "notes": "",
    "status": "pending",
}


def _enrich_milestone(parsed=PARSED_MILESTONE_SAMPLE, contract=CONTRACT_MILESTONE_JPY, **overrides):
    kwargs = dict(
        submitter="mmcky",
        submission_id="mmcky-invoice-2025-11",
        issue_number=99,
        submitted_date="2025-11-20",
    )
    kwargs.update(overrides)
    return enrich_submission(parsed, contract, **kwargs)


class TestEnrichMilestoneSubmission:
    def test_basic_milestone_enrichment(self):
        result = _enrich_milestone()
        assert result["type"] == "milestone_invoice"
        assert result["submission_id"] == "mmcky-invoice-2025-11"
        assert result["contract_id"] == "QE-IUJ-2025-002"
        assert result["period"] == "2025-11"
        assert result["totals"]["amount"] == 77000   # JPY rounds to int
        assert result["totals"]["currency"] == "JPY"
        assert "hours" not in result["totals"]
        assert "rate" not in result["totals"]

    def test_milestone_entries_amounts_formatted_for_currency(self):
        # JPY should produce int amounts (no decimals).
        result = _enrich_milestone()
        assert result["entries"][0]["amount"] == 77000
        assert isinstance(result["entries"][0]["amount"], int)

    def test_multiple_milestone_entries_summed(self):
        parsed = {
            **PARSED_MILESTONE_SAMPLE,
            "entries": [
                {"id": "1", "date": "2025-09-15", "amount": 77000.0, "description": "Sep"},
                {"id": "2", "date": "2025-10-15", "amount": 77000.0, "description": "Oct"},
                {"id": "3", "date": "2025-11-15", "amount": 77000.0, "description": "Nov"},
            ],
        }
        result = _enrich_milestone(parsed=parsed)
        assert result["totals"]["amount"] == 231000

    def test_non_milestone_contract_raises_for_invoice(self):
        bad_contract = {**CONTRACT_MILESTONE_JPY, "type": "hourly"}
        with pytest.raises(ValueError, match="requires a `milestone` contract"):
            _enrich_milestone(contract=bad_contract)

    def test_milestone_contract_missing_currency_raises(self):
        bad_contract = {k: v for k, v in CONTRACT_MILESTONE_JPY.items() if k != "currency"}
        with pytest.raises(ValueError, match="missing a top-level `currency`"):
            _enrich_milestone(contract=bad_contract)

    def test_contract_id_mismatch_still_caught(self):
        bad_contract = {**CONTRACT_MILESTONE_JPY, "contract_id": "different-id"}
        with pytest.raises(ValueError, match="mismatch"):
            _enrich_milestone(contract=bad_contract)


# ─── PR body ────────────────────────────────────────────────────────────────

class TestRenderPrBody:
    def _sample_submission(self) -> dict:
        return _enrich()

    def _path(self) -> str:
        return "submissions/2025-01/janedoe-timesheet-2025-01.yml"

    def test_closes_issue(self):
        body = render_pr_body(
            issue_number=42, submitter="janedoe",
            submission=self._sample_submission(),
            submission_path_rel=self._path(),
        )
        assert "Closes #42" in body

    def test_includes_totals(self):
        body = render_pr_body(
            issue_number=42, submitter="janedoe",
            submission=self._sample_submission(),
            submission_path_rel=self._path(),
        )
        assert "382.5" in body or "382.50" in body
        assert "AUD" in body
        assert "8.5" in body

    def test_warnings_appear_when_present(self):
        body = render_pr_body(
            issue_number=42, submitter="janedoe",
            submission=self._sample_submission(),
            submission_path_rel=self._path(),
            warnings=[{"message": "Used `,` as separator — please use `|` next time."}],
        )
        assert "Parse warnings" in body
        assert "`,`" in body

    def test_no_warnings_section_when_empty(self):
        body = render_pr_body(
            issue_number=42, submitter="janedoe",
            submission=self._sample_submission(),
            submission_path_rel=self._path(),
            warnings=[],
        )
        assert "Parse warnings" not in body


# ─── Branch naming ──────────────────────────────────────────────────────────

class TestBranchNaming:
    def test_branch_name_format(self):
        assert branch_name_for_issue(42) == "submission/issue-42"
