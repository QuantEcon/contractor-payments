"""Tests for scripts/find_previous_submission.py.

Verifies the filesystem-scan logic that the workflow uses on
`issues.reopened` to identify the supersedes target for a revision.
"""
from __future__ import annotations

import yaml

from scripts.find_previous_submission import find_latest_approved_for_issue


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


class TestFindLatestApprovedForIssue:
    def test_no_submissions_returns_none(self, tmp_path):
        assert find_latest_approved_for_issue(tmp_path, 42) is None

    def test_no_match_returns_none(self, tmp_path):
        _write(tmp_path / "submissions" / "2026-01" / "alice-timesheet-2026-01.yml", {
            "submission_id": "alice-timesheet-2026-01",
            "issue_number": 7,  # different issue
            "status": "approved",
            "approved_date": "2026-01-15",
        })
        assert find_latest_approved_for_issue(tmp_path, 42) is None

    def test_single_match_returned(self, tmp_path):
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02.yml", {
            "submission_id": "mmcky-invoice-2026-02",
            "issue_number": 18,
            "status": "approved",
            "approved_date": "2026-05-17",
        })
        assert find_latest_approved_for_issue(tmp_path, 18) == "mmcky-invoice-2026-02"

    def test_skips_pending_submissions(self, tmp_path):
        """A pending (unapproved) submission for this issue shouldn't
        register as the supersedes target — only approved entries do."""
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02.yml", {
            "submission_id": "mmcky-invoice-2026-02",
            "issue_number": 18,
            "status": "pending",
            "approved_date": None,
        })
        assert find_latest_approved_for_issue(tmp_path, 18) is None

    def test_skips_superseded_submissions(self, tmp_path):
        """The whole point: superseded entries are no longer the active
        version, so they shouldn't be the supersedes target for the next
        revision. The revision chain walks forward through ACTIVE entries."""
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02.yml", {
            "submission_id": "mmcky-invoice-2026-02",
            "issue_number": 18,
            "status": "superseded",
            "approved_date": "2026-05-17",
            "superseded_by": "mmcky-invoice-2026-02-v2",
        })
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02-v2.yml", {
            "submission_id": "mmcky-invoice-2026-02-v2",
            "issue_number": 18,
            "status": "approved",
            "approved_date": "2026-05-18",
        })
        # Should return v2, not the superseded original.
        assert (
            find_latest_approved_for_issue(tmp_path, 18) == "mmcky-invoice-2026-02-v2"
        )

    def test_latest_revision_wins_via_approved_date(self, tmp_path):
        """If there's an active original AND an active revision (shouldn't
        happen in practice — revision should have superseded the original —
        but defensive: sort by approved_date descending picks the right one)."""
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02.yml", {
            "submission_id": "mmcky-invoice-2026-02",
            "issue_number": 18,
            "status": "approved",
            "approved_date": "2026-05-17",
        })
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02-v2.yml", {
            "submission_id": "mmcky-invoice-2026-02-v2",
            "issue_number": 18,
            "status": "approved",
            "approved_date": "2026-05-18",
        })
        assert (
            find_latest_approved_for_issue(tmp_path, 18) == "mmcky-invoice-2026-02-v2"
        )

    def test_filename_tiebreaker_when_approved_date_matches(self, tmp_path):
        """Identical approved_date (e.g. testing): lexically greater
        submission_id wins, which prefers -v3 over -v2 over base."""
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02-v2.yml", {
            "submission_id": "mmcky-invoice-2026-02-v2",
            "issue_number": 18,
            "status": "approved",
            "approved_date": "2026-05-18",
        })
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02-v3.yml", {
            "submission_id": "mmcky-invoice-2026-02-v3",
            "issue_number": 18,
            "status": "approved",
            "approved_date": "2026-05-18",
        })
        assert (
            find_latest_approved_for_issue(tmp_path, 18) == "mmcky-invoice-2026-02-v3"
        )

    def test_independent_b_invoice_not_picked_as_supersedes_target(self, tmp_path):
        """Independent -B invoices come from DIFFERENT issues, so they won't
        match the issue_number filter. Sanity check."""
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02.yml", {
            "submission_id": "mmcky-invoice-2026-02",
            "issue_number": 18,
            "status": "approved",
            "approved_date": "2026-05-17",
        })
        _write(tmp_path / "submissions" / "2026-02" / "mmcky-invoice-2026-02-B.yml", {
            "submission_id": "mmcky-invoice-2026-02-B",
            "issue_number": 25,  # different issue
            "status": "approved",
            "approved_date": "2026-05-19",
        })
        # Reopening issue 18 should target the original, not the unrelated -B.
        assert find_latest_approved_for_issue(tmp_path, 18) == "mmcky-invoice-2026-02"
