"""Tests for the superseded-claim lookup in scripts/finalize_approval.py.

The PDF render and the git side effects are integration territory. What is
covered here is `mark_superseded_yaml`, which resolved the predecessor's path
by assuming it lived under the *revision's* period. A revision that also
corrects the period leaves the predecessor under the old one, so that
assumption raised FileNotFoundError in the first post-merge step — and because
neither workflow gates on failure, that silently skipped the ledger update, the
approval email and the audit comment, while the revision had already been
stamped `approved` on disk.
"""
from __future__ import annotations

import pytest
import yaml

from scripts.finalize_approval import mark_superseded_yaml


def _write_submission(repo_root, period, submission_id, **extra):
    path = repo_root / "submissions" / period / f"{submission_id}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "submission_id": submission_id,
        "period": period,
        "status": "approved",
        "totals": {"amount": 1250.0, "currency": "AUD"},
        **extra,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestMarkSupersededYaml:
    def test_same_period_predecessor(self, tmp_path):
        original = _write_submission(tmp_path, "2026-06", "jane-2026-06")

        updated = mark_superseded_yaml(
            "jane-2026-06", "jane-2026-06-v2", "2026-06", tmp_path,
        )

        assert updated == original
        data = _load(original)
        assert data["status"] == "superseded"
        assert data["superseded_by"] == "jane-2026-06-v2"

    def test_predecessor_in_a_different_period_is_found(self, tmp_path):
        """The regression: claim filed for 2026-05, revised to 2026-06."""
        original = _write_submission(tmp_path, "2026-05", "jane-2026-05")
        _write_submission(tmp_path, "2026-06", "jane-2026-06-v2", status="approved")

        updated = mark_superseded_yaml(
            "jane-2026-05", "jane-2026-06-v2", "2026-06", tmp_path,
        )

        assert updated == original
        assert _load(original)["status"] == "superseded"

    def test_same_period_wins_over_a_scan_match(self, tmp_path):
        """The hint is still preferred, so an id that legitimately appears in
        two periods resolves to the expected one without scanning."""
        target = _write_submission(tmp_path, "2026-06", "jane-claim")
        other = _write_submission(tmp_path, "2026-05", "jane-claim")

        updated = mark_superseded_yaml(
            "jane-claim", "jane-claim-v2", "2026-06", tmp_path,
        )

        assert updated == target
        assert _load(target)["status"] == "superseded"
        assert _load(other)["status"] == "approved"

    def test_ambiguous_match_refuses_to_guess(self, tmp_path):
        """Same id under two periods and neither is the hinted one: stamping
        the wrong claim superseded would silently retire a live submission."""
        _write_submission(tmp_path, "2026-04", "jane-claim")
        _write_submission(tmp_path, "2026-05", "jane-claim")

        with pytest.raises(FileNotFoundError, match="ambiguous"):
            mark_superseded_yaml(
                "jane-claim", "jane-claim-v2", "2026-06", tmp_path,
            )

    def test_missing_everywhere_still_raises(self, tmp_path):
        _write_submission(tmp_path, "2026-06", "someone-else-2026-06")

        with pytest.raises(FileNotFoundError) as excinfo:
            mark_superseded_yaml(
                "jane-2026-05", "jane-2026-06-v2", "2026-06", tmp_path,
            )

        assert "every other period" in str(excinfo.value)

    def test_no_submissions_directory_at_all(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            mark_superseded_yaml("x", "x-v2", "2026-06", tmp_path)
