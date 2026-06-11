"""Tests for scripts/update_ledger_issue.py.

Focus: markdown body rendering. The `gh issue edit` side effect isn't
unit-tested — it's exercised by the end-of-Phase-2 E2E on
contractor-engine-test.

Phase 2.5 coverage: superseded entries should render with strikethrough,
forward-link to the revision, and not surface in `_last_approval`.
"""
from __future__ import annotations

from scripts.update_ledger_issue import (
    _last_approval,
    _submission_cell,
    render_hourly_body,
    render_milestone_body,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────

CONTRACT_HOURLY = {
    "contract_id": "QE-PSL-2026-001",
    "type": "hourly",
    "status": "active",
    "start_date": "2026-01-01",
    "end_date": "2026-12-31",
}

CONTRACT_MILESTONE = {
    "contract_id": "QE-IUJ-2025-002",
    "type": "milestone",
    "status": "active",
    "start_date": "2025-09-01",
    "end_date": "2026-02-28",
}


def _hourly_ledger_with(*submissions):
    return {
        "contract_id": "QE-PSL-2026-001",
        "type": "hourly",
        "currency": "AUD",
        "submissions": list(submissions),
        "totals": {
            "hours_to_date": sum(
                s["hours"] for s in submissions if s.get("status") != "superseded"
            ),
            "amount_to_date": sum(
                s["amount"] for s in submissions if s.get("status") != "superseded"
            ),
            "submissions_count": sum(
                1 for s in submissions if s.get("status") != "superseded"
            ),
        },
    }


def _milestone_ledger_with(*claims):
    return {
        "contract_id": "QE-IUJ-2025-002",
        "type": "milestone",
        "currency": "JPY",
        "claims": list(claims),
        "totals": {
            "amount_to_date": sum(
                c["amount"] for c in claims if c.get("status") != "superseded"
            ),
            "claims_count": sum(
                1 for c in claims if c.get("status") != "superseded"
            ),
        },
    }


# ─── _submission_cell ───────────────────────────────────────────────────────

class TestSubmissionCell:
    def test_active_entry_just_returns_link(self):
        cell = _submission_cell({
            "submission_id": "mmcky-invoice-2026-02",
            "period": "2026-02",
        })
        assert cell == "[`mmcky-invoice-2026-02`](submissions/2026-02/mmcky-invoice-2026-02.yml)"
        assert "~~" not in cell

    def test_superseded_entry_strikes_link_and_appends_arrow_to_successor(self):
        cell = _submission_cell({
            "submission_id": "mmcky-invoice-2026-02",
            "period": "2026-02",
            "status": "superseded",
            "superseded_by": "mmcky-invoice-2026-02-v2",
        })
        # Original is struck through; arrow + revision link follows.
        assert cell.startswith("~~[`mmcky-invoice-2026-02`]")
        assert "→" in cell
        assert "[`mmcky-invoice-2026-02-v2`]" in cell
        # The successor link itself is NOT struck through.
        assert "~~[`mmcky-invoice-2026-02-v2`]~~" not in cell

    def test_superseded_without_successor_just_strikes(self):
        """Edge case: superseded but no `superseded_by` field. Still strikes,
        but no arrow."""
        cell = _submission_cell({
            "submission_id": "mmcky-invoice-2026-02",
            "period": "2026-02",
            "status": "superseded",
        })
        assert cell == "~~[`mmcky-invoice-2026-02`](submissions/2026-02/mmcky-invoice-2026-02.yml)~~"
        assert "→" not in cell


# ─── _last_approval ─────────────────────────────────────────────────────────

class TestLastApproval:
    def test_returns_last_active_entry(self):
        items = [
            {"submission_id": "a", "approved_date": "2026-01-01", "approved_by": "alice"},
            {"submission_id": "b", "approved_date": "2026-02-01", "approved_by": "bob"},
        ]
        assert _last_approval(items) == ("2026-02-01", "bob")

    def test_skips_superseded_entries(self):
        """The most recent ACTIVE entry's approval is what's shown — a more
        recent superseded entry shouldn't surface as 'last approved'."""
        items = [
            {"submission_id": "a", "approved_date": "2026-01-01", "approved_by": "alice"},
            {"submission_id": "b", "approved_date": "2026-02-01", "approved_by": "bob",
             "status": "superseded"},
        ]
        assert _last_approval(items) == ("2026-01-01", "alice")

    def test_empty_returns_none(self):
        assert _last_approval([]) == (None, None)

    def test_all_superseded_returns_none(self):
        items = [
            {"submission_id": "a", "approved_date": "2026-01-01", "approved_by": "alice",
             "status": "superseded"},
        ]
        assert _last_approval(items) == (None, None)


# ─── Body rendering — hourly ────────────────────────────────────────────────

class TestRenderHourlyBody:
    def test_clean_ledger_no_strikethrough(self):
        ledger = _hourly_ledger_with({
            "submission_id": "mmcky-timesheet-2026-04", "period": "2026-04",
            "hours": 10.0, "rate": 50.00, "amount": 500.00,
            "approved_date": "2026-05-01", "approved_by": "mmcky",
        })
        body = render_hourly_body(ledger, CONTRACT_HOURLY)
        assert "~~" not in body
        # The supersession arrow specifically — guard against false positives
        # from the contract period range ("start → end") in the header.
        assert "→ [`" not in body
        assert "500.00 AUD" in body
        assert "Submissions" in body
        # Summary count matches the single active submission.
        assert "| 1 |" in body  # submissions_count column

    def test_revision_strikes_superseded_and_links_forward(self):
        ledger = _hourly_ledger_with(
            {
                "submission_id": "mmcky-timesheet-2026-04",
                "period": "2026-04",
                "hours": 10.0, "rate": 50.00, "amount": 500.00,
                "approved_date": "2026-05-01", "approved_by": "mmcky",
                "status": "superseded",
                "superseded_by": "mmcky-timesheet-2026-04-v2",
            },
            {
                "submission_id": "mmcky-timesheet-2026-04-v2",
                "period": "2026-04",
                "hours": 12.0, "rate": 50.00, "amount": 600.00,
                "approved_date": "2026-05-10", "approved_by": "mmcky",
            },
        )
        body = render_hourly_body(ledger, CONTRACT_HOURLY)

        # Superseded line: struck-through hours/rate/amount/approved
        assert "~~10.0~~" in body
        assert "~~50.00 AUD~~" in body
        assert "~~500.00 AUD~~" in body
        assert "~~2026-05-01 by @mmcky~~" in body
        # Forward link to revision
        assert "→ [`mmcky-timesheet-2026-04-v2`]" in body

        # Active line: NOT struck-through
        assert "| 12.0 |" in body
        assert "| 600.00 AUD |" in body
        assert "| 2026-05-10 by @mmcky |" in body

        # Totals reflect only the active entry
        assert "600.00 AUD" in body  # in the summary too
        # Last-approved cell shows the active (most recent non-superseded)
        assert "2026-05-10 by @mmcky |" in body


# ─── Body rendering — milestone ─────────────────────────────────────────────

class TestRenderMilestoneBody:
    def test_clean_ledger_no_strikethrough(self):
        ledger = _milestone_ledger_with({
            "submission_id": "mmcky-invoice-2026-02", "period": "2026-02",
            "entries": [{"id": "5", "date": "2026-02-15", "amount": 77000,
                         "description": "Feb"}],
            "amount": 77000,
            "approved_date": "2026-05-13", "approved_by": "mmcky",
        })
        body = render_milestone_body(ledger, CONTRACT_MILESTONE)
        assert "~~" not in body
        assert "→ [`" not in body
        assert "77,000 JPY" in body

    def test_revision_strikes_superseded_milestone_claim(self):
        ledger = _milestone_ledger_with(
            {
                "submission_id": "mmcky-invoice-2026-02", "period": "2026-02",
                "entries": [{"id": "5", "amount": 77000, "description": "Feb"}],
                "amount": 77000,
                "approved_date": "2026-05-13", "approved_by": "mmcky",
                "status": "superseded",
                "superseded_by": "mmcky-invoice-2026-02-v2",
            },
            {
                "submission_id": "mmcky-invoice-2026-02-v2", "period": "2026-02",
                "entries": [{"id": "5", "amount": 80000, "description": "Feb (corrected)"}],
                "amount": 80000,
                "approved_date": "2026-05-18", "approved_by": "mmcky",
            },
        )
        body = render_milestone_body(ledger, CONTRACT_MILESTONE)

        # Superseded claim: struck-through period/milestones/amount/approved.
        assert "~~2026-02~~" in body
        assert "~~#5~~" in body
        assert "~~77,000 JPY~~" in body
        assert "~~2026-05-13 by @mmcky~~" in body

        # Forward link to revision.
        assert "→ [`mmcky-invoice-2026-02-v2`]" in body

        # Active claim renders clean.
        assert "| 2026-02 | [`mmcky-invoice-2026-02-v2`]" in body
        assert "| 80,000 JPY |" in body

        # Summary count is 1 (the active claim only); amount_to_date matches.
        # claims_count is the third column in the summary table.
        assert "| 1 | 80,000 JPY |" in body

    def test_independent_b_invoice_appears_normally(self):
        """A -B independent invoice has no `status`, no strikethrough,
        no arrow — just another row in the table."""
        ledger = _milestone_ledger_with(
            {
                "submission_id": "mmcky-invoice-2026-02", "period": "2026-02",
                "entries": [{"id": "5", "amount": 77000, "description": "Feb"}],
                "amount": 77000,
                "approved_date": "2026-05-13", "approved_by": "mmcky",
            },
            {
                "submission_id": "mmcky-invoice-2026-02-B", "period": "2026-02",
                "entries": [{"id": "7", "amount": 12000, "description": "Bonus"}],
                "amount": 12000,
                "approved_date": "2026-05-20", "approved_by": "mmcky",
            },
        )
        body = render_milestone_body(ledger, CONTRACT_MILESTONE)

        assert "~~" not in body
        assert "→ [`" not in body
        assert "77,000 JPY" in body
        assert "12,000 JPY" in body
        # Summary counts both as active.
        assert "| 2 | 89,000 JPY |" in body


# ─── Reimbursement body (Phase 5) ───────────────────────────────────────────

def _reimbursement_ledger_with(*claims):
    totals = {}
    for c in claims:
        if c.get("status") == "superseded":
            continue
        bucket = totals.setdefault(c["currency"], {"amount_to_date": 0, "claims_count": 0})
        bucket["amount_to_date"] += c["amount"]
        bucket["claims_count"] += 1
    return {
        "type": "reimbursement",
        "claims": list(claims),
        "totals": dict(sorted(totals.items())),
    }


def _claim(submission_id="janedoe-reimbursement-2026-06", period="2026-06",
           amount=12300, currency="JPY", project="CHOW", **extra):
    return {
        "submission_id": submission_id,
        "period": period,
        "approved_date": "2026-06-12",
        "approved_by": "mmcky",
        "amount": amount,
        "currency": currency,
        "project": project,
        **extra,
    }


class TestRenderReimbursementBody:
    def test_per_currency_summary_rows(self):
        from scripts.update_ledger_issue import render_reimbursement_body
        ledger = _reimbursement_ledger_with(
            _claim(),
            _claim(submission_id="janedoe-reimbursement-2026-06-B",
                   amount=800.25, currency="AUD"),
        )
        body = render_reimbursement_body(ledger, {})
        assert "# 📒 Running ledger — Reimbursements" in body
        assert "| JPY | 1 | 12,300 JPY |" in body
        assert "| AUD | 1 | 800.25 AUD |" in body

    def test_claims_table_has_project_column(self):
        from scripts.update_ledger_issue import render_reimbursement_body
        body = render_reimbursement_body(_reimbursement_ledger_with(_claim()), {})
        assert "| Period | Submission | Project | Amount | Approved |" in body
        assert "`CHOW`" in body

    def test_superseded_row_struck_through(self):
        from scripts.update_ledger_issue import render_reimbursement_body
        ledger = _reimbursement_ledger_with(
            _claim(status="superseded",
                   superseded_by="janedoe-reimbursement-2026-06-v2"),
            _claim(submission_id="janedoe-reimbursement-2026-06-v2", amount=11800),
        )
        body = render_reimbursement_body(ledger, {})
        assert "~~" in body
        assert "→" in body
        # Only the active claim counts.
        assert "| JPY | 1 | 11,800 JPY |" in body

    def test_empty_state(self):
        from scripts.update_ledger_issue import render_reimbursement_body
        body = render_reimbursement_body(
            {"type": "reimbursement", "claims": [], "totals": {}}, {},
        )
        assert "_No claims approved yet._" in body

    def test_marker_present(self):
        from scripts.update_ledger_issue import render_reimbursement_body
        body = render_reimbursement_body(_reimbursement_ledger_with(_claim()), {})
        assert "<!-- ledger-issue-marker:reimbursements -->" in body

    def test_render_body_dispatches(self):
        from scripts.update_ledger_issue import render_body
        body = render_body(_reimbursement_ledger_with(_claim()), {})
        assert "Reimbursements" in body
