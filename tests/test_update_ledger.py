"""Tests for scripts/update_ledger.py.

Covers the pure-data helpers (empty-ledger construction, entry building,
append-with-totals, cross-checks). The CLI wrapper just reads/writes
YAML and isn't tested separately — the helpers carry the logic.
"""
from __future__ import annotations

import pytest

from scripts.update_ledger import (
    _recompute_totals,
    _build_entry,
    _empty_ledger,
    append_submission,
    is_already_applied,
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


# ─── Revisions (Phase 2.5) ──────────────────────────────────────────────────

class TestRevisionHourly:
    """Hourly ledger: revision marks superseded entry, excludes from totals."""

    def test_revision_supersedes_original_in_hourly_ledger(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        ledger = append_submission(HOURLY_SUBMISSION, ledger)

        # Original had 14.5h × $50 = $725. Revision corrects to 16h × $50 = $800.
        revision = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-04-v2",
            "supersedes": "janedoe-timesheet-2026-04",
            "revision_of": "janedoe-timesheet-2026-04",
            "totals": {"hours": 16.0, "rate": 50.00, "amount": 800.00, "currency": "AUD"},
        }
        out = append_submission(revision, ledger)

        # Original entry kept but marked superseded.
        assert len(out["submissions"]) == 2
        superseded = [s for s in out["submissions"] if s.get("status") == "superseded"]
        assert len(superseded) == 1
        assert superseded[0]["submission_id"] == "janedoe-timesheet-2026-04"
        assert superseded[0]["superseded_by"] == "janedoe-timesheet-2026-04-v2"

        # Totals reflect only the revision; original is excluded.
        assert out["totals"]["hours_to_date"] == 16.0
        assert out["totals"]["amount_to_date"] == 800.00
        # Count drops the superseded entry (one active submission, not two).
        assert out["totals"]["submissions_count"] == 1

    def test_revision_chain_only_counts_latest(self):
        """Two consecutive revisions: only the most recent counts."""
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        ledger = append_submission(HOURLY_SUBMISSION, ledger)
        v2 = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-04-v2",
            "supersedes": "janedoe-timesheet-2026-04",
            "totals": {"hours": 16.0, "rate": 50.00, "amount": 800.00, "currency": "AUD"},
        }
        ledger = append_submission(v2, ledger)
        v3 = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-04-v3",
            "supersedes": "janedoe-timesheet-2026-04-v2",
            "totals": {"hours": 17.5, "rate": 50.00, "amount": 875.00, "currency": "AUD"},
        }
        out = append_submission(v3, ledger)

        # All three are present, two superseded, one active.
        assert len(out["submissions"]) == 3
        statuses = [s.get("status") for s in out["submissions"]]
        assert statuses.count("superseded") == 2
        # Only the active v3 counts toward totals.
        assert out["totals"]["amount_to_date"] == 875.00
        assert out["totals"]["hours_to_date"] == 17.5
        assert out["totals"]["submissions_count"] == 1


class TestRevisionMilestone:
    """Milestone ledger: revision marks superseded claim, excludes from totals."""

    def test_revision_supersedes_original_in_milestone_ledger(self):
        ledger = _empty_ledger(MILESTONE_SUBMISSION)
        ledger = append_submission(MILESTONE_SUBMISSION, ledger)

        # Original 77000 JPY → revision corrects to 80000 JPY.
        revision = {
            **MILESTONE_SUBMISSION,
            "submission_id": "mmcky-invoice-2025-11-v2",
            "supersedes": "mmcky-invoice-2025-11",
            "revision_of": "mmcky-invoice-2025-11",
            "totals": {"amount": 80000, "currency": "JPY"},
        }
        out = append_submission(revision, ledger)

        # Both claims present, original marked superseded.
        assert len(out["claims"]) == 2
        superseded = [c for c in out["claims"] if c.get("status") == "superseded"]
        assert len(superseded) == 1
        assert superseded[0]["superseded_by"] == "mmcky-invoice-2025-11-v2"

        # Totals reflect only the revision.
        assert out["totals"]["amount_to_date"] == 80000
        assert out["totals"]["claims_count"] == 1


class TestRevisionEdgeCases:
    def test_supersedes_unknown_id_raises(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        ledger = append_submission(HOURLY_SUBMISSION, ledger)
        bad = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-04-v2",
            "supersedes": "janedoe-timesheet-2026-04-ghost",  # not in ledger
        }
        with pytest.raises(ValueError, match="no entry with that submission_id"):
            append_submission(bad, ledger)

    def test_independent_second_invoice_b_appends_normally(self):
        """Independent invoice (-B): no `supersedes`, both entries count.
        This is the same-period collision case where there's no relationship
        between the two submissions."""
        ledger = _empty_ledger(MILESTONE_SUBMISSION)
        ledger = append_submission(MILESTONE_SUBMISSION, ledger)
        independent = {
            **MILESTONE_SUBMISSION,
            "submission_id": "mmcky-invoice-2025-11-B",
            # no `supersedes` field — this is an independent invoice
            "entries": [
                {"id": "4", "date": "2025-11-20", "amount": 12000,
                 "description": "Bonus milestone"},
            ],
            "totals": {"amount": 12000, "currency": "JPY"},
        }
        out = append_submission(independent, ledger)

        # Both claims active and counted.
        assert len(out["claims"]) == 2
        assert all(c.get("status") != "superseded" for c in out["claims"])
        assert out["totals"]["amount_to_date"] == 89000  # 77000 + 12000
        assert out["totals"]["claims_count"] == 2

    def test_currency_mismatch_raises(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        bad = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-05",
            "totals": {"hours": 5.0, "rate": 30.00, "amount": 150.00, "currency": "USD"},
        }
        with pytest.raises(ValueError, match="Currency mismatch"):
            append_submission(bad, ledger)

    def test_duplicate_submission_id_with_different_content_raises(self):
        ledger = _empty_ledger(HOURLY_SUBMISSION)
        ledger = append_submission(HOURLY_SUBMISSION, ledger)
        changed = {**HOURLY_SUBMISSION,
                   "totals": {**HOURLY_SUBMISSION["totals"], "amount": 800.0}}
        with pytest.raises(ValueError, match=r"already in the ledger, but with different content \(amount\)"):
            append_submission(changed, ledger)


# ─── Re-runs (issue #11) ────────────────────────────────────────────────────

class TestRerun:
    """`Re-run failed jobs` replays the ledger step on a main that may already
    hold this submission's entry. Re-applying the same entry is a no-op;
    anything else is a real inconsistency and still raises."""

    def test_reapplying_the_same_hourly_entry_is_a_no_op(self):
        once = append_submission(HOURLY_SUBMISSION, _empty_ledger(HOURLY_SUBMISSION))
        assert is_already_applied(HOURLY_SUBMISSION, once)
        assert append_submission(HOURLY_SUBMISSION, once) == once

    def test_reapplying_the_same_milestone_claim_is_a_no_op(self):
        once = append_submission(MILESTONE_SUBMISSION, _empty_ledger(MILESTONE_SUBMISSION))
        assert append_submission(MILESTONE_SUBMISSION, once) == once
        assert once["totals"]["claims_count"] == 1

    def test_not_applied_when_absent(self):
        assert not is_already_applied(HOURLY_SUBMISSION, _empty_ledger(HOURLY_SUBMISSION))

    def test_different_approval_date_is_not_a_rerun(self):
        once = append_submission(HOURLY_SUBMISSION, _empty_ledger(HOURLY_SUBMISSION))
        restamped = {**HOURLY_SUBMISSION, "approved_date": "2026-05-14"}
        with pytest.raises(ValueError, match="approved_date"):
            is_already_applied(restamped, once)

    def test_reapplying_a_revision_is_a_no_op(self):
        original = append_submission(HOURLY_SUBMISSION, _empty_ledger(HOURLY_SUBMISSION))
        revision = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-04-v2",
            "supersedes": "janedoe-timesheet-2026-04",
            "totals": {**HOURLY_SUBMISSION["totals"], "hours": 16.0, "amount": 800.0},
        }
        revised = append_submission(revision, original)
        assert append_submission(revision, revised) == revised
        assert revised["totals"]["amount_to_date"] == 800.0

    def test_revision_present_but_target_not_marked_raises(self):
        original = append_submission(HOURLY_SUBMISSION, _empty_ledger(HOURLY_SUBMISSION))
        revision = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-04-v2",
            "supersedes": "janedoe-timesheet-2026-04",
        }
        revised = append_submission(revision, original)
        revised["submissions"][0].pop("superseded_by")
        with pytest.raises(ValueError, match="superseded entry"):
            is_already_applied(revision, revised)

    def test_an_entry_later_superseded_still_counts_as_applied(self):
        """The original's ledger entry gains `status: superseded` when a
        revision lands; re-driving the original must not read that as a
        content change."""
        original = append_submission(HOURLY_SUBMISSION, _empty_ledger(HOURLY_SUBMISSION))
        revision = {
            **HOURLY_SUBMISSION,
            "submission_id": "janedoe-timesheet-2026-04-v2",
            "supersedes": "janedoe-timesheet-2026-04",
        }
        revised = append_submission(revision, original)
        assert is_already_applied(HOURLY_SUBMISSION, revised)


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


class TestTotalsAreRounded:
    """Float summation left artefacts like `305.21999999999997` in the
    committed ledger, while every surface that displays it formatted on the
    way out — so the stored audit record and the numbers people read
    disagreed."""

    def test_float_artefact_does_not_reach_the_ledger(self):
        ledger = {"type": "hourly", "currency": "AUD", "totals": {}}
        items = [
            {"hours": 1, "amount": 100.10, "status": "approved"},
            {"hours": 1, "amount": 100.10, "status": "approved"},
            {"hours": 1, "amount": 105.02, "status": "approved"},
        ]
        _recompute_totals(ledger, items)

        total = ledger["totals"]["amount_to_date"]
        assert total == 305.22
        # repr, not ==: the artefact is invisible to equality but is exactly
        # what `yaml.safe_dump` writes into the committed file.
        assert repr(total) == "305.22"

    def test_jpy_integer_totals_are_unaffected(self):
        ledger = {"type": "milestone", "currency": "JPY", "totals": {}}
        _recompute_totals(ledger, [{"amount": 77000, "status": "approved"}])
        assert repr(ledger["totals"]["amount_to_date"]) == "77000"

    def test_superseded_entries_still_excluded(self):
        ledger = {"type": "milestone", "currency": "AUD", "totals": {}}
        _recompute_totals(ledger, [
            {"amount": 100.10, "status": "approved"},
            {"amount": 999.99, "status": "superseded"},
        ])
        assert ledger["totals"]["amount_to_date"] == 100.10
        assert ledger["totals"]["claims_count"] == 1
