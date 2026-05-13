"""Tests for scripts/update_ledger.py.

Covers the pure-data helpers (empty-ledger construction, entry building,
append-with-totals, cross-checks). The CLI wrapper just reads/writes
YAML and isn't tested separately — the helpers carry the logic.
"""
from __future__ import annotations

import pytest

from scripts.update_ledger import (
    _build_entry,
    _empty_ledger,
    append_submission,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────

HOURLY_SUBMISSION = {
    "submission_id": "janedoe-timesheet-2026-04",
    "type": "timesheet",
    "contract_id": "QE-PSL-2026-001",
    "period": "2026-04",
    "approved_date": "2026-05-13",
    "approved_by": "mmcky",
    "entries": [
        {"date": "2026-04-06", "hours": 3.5, "description": "NumPy review"},
    ],
    "totals": {
        "hours": 14.5,
        "rate": 50.00,
        "amount": 725.00,
        "currency": "AUD",
    },
    "status": "approved",
}

MILESTONE_SUBMISSION = {
    "submission_id": "mmcky-invoice-2025-11",
    "type": "milestone_invoice",
    "contract_id": "QE-IUJ-2025-002",
    "period": "2025-11",
    "approved_date": "2025-11-20",
    "approved_by": "mmcky",
    "entries": [
        {"id": "3", "date": "2025-11-15", "amount": 77000,
         "description": "Monthly Payment — November"},
    ],
    "totals": {"amount": 77000, "currency": "JPY"},
    "status": "approved",
}


# ─── Empty ledger shape ─────────────────────────────────────────────────────

class TestEmptyLedger:
    def test_hourly_shape(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        assert ledger["contract_id"] == "QE-PSL-2026-001"
        assert ledger["type"] == "hourly"
        assert ledger["currency"] == "AUD"
        assert ledger["submissions"] == []
        assert ledger["totals"]["hours_to_date"] == 0
        assert ledger["totals"]["amount_to_date"] == 0
        assert ledger["totals"]["submissions_count"] == 0
        # No milestone-shaped keys leak in
        assert "claims" not in ledger

    def test_milestone_shape(self):
        ledger = _empty_ledger(MILESTONE_SUBMISSION)
        assert ledger["contract_id"] == "QE-IUJ-2025-002"
        assert ledger["type"] == "milestone"
        assert ledger["currency"] == "JPY"
        assert ledger["claims"] == []
        assert ledger["totals"]["amount_to_date"] == 0
        assert ledger["totals"]["claims_count"] == 0
        assert "submissions" not in ledger
        assert "hours_to_date" not in ledger["totals"]

    def test_unknown_type_raises(self):
        bad = {**HOURLY_SUBMISSION, "type": "reimbursement"}  # Phase 5; not yet
        with pytest.raises(ValueError, match="Unknown submission type"):
            _empty_ledger(bad)


# ─── Entry building ────────────────────────────────────────────────────────

class TestBuildEntry:
    def test_hourly_entry_has_hours_rate_amount(self):
        entry = _build_entry(HOURLY_SUBMISSION)
        assert entry["submission_id"] == "janedoe-timesheet-2026-04"
        assert entry["hours"] == 14.5
        assert entry["rate"] == 50.00
        assert entry["amount"] == 725.00
        assert entry["approved_date"] == "2026-05-13"
        assert entry["approved_by"] == "mmcky"
        # No milestone-shaped fields
        assert "entries" not in entry

    def test_milestone_entry_preserves_per_row_entries(self):
        entry = _build_entry(MILESTONE_SUBMISSION)
        assert entry["submission_id"] == "mmcky-invoice-2025-11"
        assert entry["amount"] == 77000
        # Per-milestone breakdown is preserved for audit
        assert entry["entries"] == MILESTONE_SUBMISSION["entries"]
        assert "hours" not in entry
        assert "rate" not in entry

    def test_milestone_entry_with_multiple_rows(self):
        catchup = {
            **MILESTONE_SUBMISSION,
            "entries": [
                {"id": "1", "date": "2025-09-15", "amount": 77000, "description": "Sep"},
                {"id": "2", "date": "2025-10-15", "amount": 77000, "description": "Oct"},
                {"id": "3", "date": "2025-11-15", "amount": 77000, "description": "Nov"},
            ],
            "totals": {"amount": 231000, "currency": "JPY"},
        }
        entry = _build_entry(catchup)
        assert len(entry["entries"]) == 3
        assert entry["amount"] == 231000


# ─── Append (hourly) ────────────────────────────────────────────────────────

class TestAppendHourly:
    def test_first_submission_into_empty_ledger(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        out = append_submission(HOURLY_SUBMISSION, ledger)
        assert len(out["submissions"]) == 1
        assert out["totals"]["hours_to_date"] == 14.5
        assert out["totals"]["amount_to_date"] == 725.00
        assert out["totals"]["submissions_count"] == 1

    def test_second_submission_sums_totals(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        ledger = append_submission(HOURLY_SUBMISSION, ledger)
        second = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-05",
            "period": "2026-05",
            "totals": {"hours": 8.0, "rate": 50.00, "amount": 400.00, "currency": "AUD"},
        }
        out = append_submission(second, ledger)
        assert out["totals"]["hours_to_date"] == 22.5      # 14.5 + 8.0
        assert out["totals"]["amount_to_date"] == 1125.00  # 725 + 400
        assert out["totals"]["submissions_count"] == 2


# ─── Append (milestone) ────────────────────────────────────────────────────

class TestAppendMilestone:
    def test_first_claim_into_empty_ledger(self):
        ledger = _empty_ledger(MILESTONE_SUBMISSION)
        out = append_submission(MILESTONE_SUBMISSION, ledger)
        assert len(out["claims"]) == 1
        assert out["totals"]["amount_to_date"] == 77000
        assert out["totals"]["claims_count"] == 1

    def test_second_claim_sums_totals(self):
        ledger = _empty_ledger(MILESTONE_SUBMISSION)
        ledger = append_submission(MILESTONE_SUBMISSION, ledger)
        second = {
            **MILESTONE_SUBMISSION,
            "submission_id": "mmcky-invoice-2025-12",
            "period": "2025-12",
            "entries": [
                {"id": "4", "date": "2025-12-15", "amount": 77000, "description": "Dec"},
            ],
            "totals": {"amount": 77000, "currency": "JPY"},
        }
        out = append_submission(second, ledger)
        assert out["totals"]["amount_to_date"] == 154000
        assert out["totals"]["claims_count"] == 2

    def test_catchup_submission_treated_as_single_claim(self):
        # A multi-row catch-up submission is one ledger claim (not three).
        catchup = {
            **MILESTONE_SUBMISSION,
            "entries": [
                {"id": "1", "date": "2025-09-15", "amount": 77000, "description": "Sep"},
                {"id": "2", "date": "2025-10-15", "amount": 77000, "description": "Oct"},
                {"id": "3", "date": "2025-11-15", "amount": 77000, "description": "Nov"},
            ],
            "totals": {"amount": 231000, "currency": "JPY"},
        }
        ledger = _empty_ledger(catchup)
        out = append_submission(catchup, ledger)
        assert out["totals"]["claims_count"] == 1   # one submission → one claim
        assert out["totals"]["amount_to_date"] == 231000
        # but the per-milestone breakdown is preserved inside that claim:
        assert len(out["claims"][0]["entries"]) == 3


# ─── Cross-checks ──────────────────────────────────────────────────────────

class TestCrossChecks:
    def test_contract_id_mismatch_raises(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        bad = {**HOURLY_SUBMISSION, "contract_id": "QE-PSL-9999-999"}
        with pytest.raises(ValueError, match="Contract ID mismatch"):
            append_submission(bad, ledger)

    def test_type_mismatch_raises(self):
        # A milestone submission against an hourly ledger.
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        bad = {
            **MILESTONE_SUBMISSION,
            "contract_id": HOURLY_SUBMISSION["contract_id"],  # match contract to isolate the type check
        }
        with pytest.raises(ValueError, match="Type mismatch"):
            append_submission(bad, ledger)

    def test_currency_mismatch_raises(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        bad = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-05",
            "totals": {"hours": 5.0, "rate": 30.00, "amount": 150.00, "currency": "USD"},
        }
        with pytest.raises(ValueError, match="Currency mismatch"):
            append_submission(bad, ledger)

    def test_duplicate_submission_id_raises(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        ledger = append_submission(HOURLY_SUBMISSION, ledger)
        with pytest.raises(ValueError, match="already in the ledger"):
            append_submission(HOURLY_SUBMISSION, ledger)


# ─── Currency-aware rounding ───────────────────────────────────────────────

class TestRounding:
    def test_jpy_amounts_stay_int(self):
        ledger = _empty_ledger(MILESTONE_SUBMISSION)
        out = append_submission(MILESTONE_SUBMISSION, ledger)
        assert isinstance(out["totals"]["amount_to_date"], int)

    def test_hours_to_date_rounded_to_two_places(self):
        # Slight float imprecision shouldn't propagate as 14.499999999.
        sub1 = {
            **HOURLY_SUBMISSION,
            "totals": {"hours": 4.1, "rate": 50.00, "amount": 205.00, "currency": "AUD"},
        }
        sub2 = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-05",
            "totals": {"hours": 4.2, "rate": 50.00, "amount": 210.00, "currency": "AUD"},
        }
        ledger = _empty_ledger(sub1)
        ledger = append_submission(sub1, ledger)
        out = append_submission(sub2, ledger)
        # 4.1 + 4.2 = 8.299999... in float — should round cleanly
        assert out["totals"]["hours_to_date"] == 8.3
