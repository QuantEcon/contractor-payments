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

from scripts.finalize_approval import existing_approval, main, mark_superseded_yaml


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


# ─── Re-runs (issue #11) ────────────────────────────────────────────────────

class TestExistingApproval:
    """On `Re-run failed jobs` after the commit-back succeeded, main already
    holds the stamped YAML and the approved PDF, which may already have been
    emailed. The stamp must be kept, not redone with today's date."""

    def test_pending_submission_is_stamped_normally(self):
        assert existing_approval({"status": "pending"}, approver="mmcky") is None

    def test_already_approved_by_the_same_approver_keeps_the_date(self):
        sub = {"status": "approved", "approved_by": "mmcky", "approved_date": "2026-05-13"}
        assert existing_approval(sub, approver="mmcky") == "2026-05-13"

    def test_approved_by_someone_else_refuses(self):
        sub = {"submission_id": "x", "status": "approved",
               "approved_by": "someone", "approved_date": "2026-05-13"}
        with pytest.raises(ValueError, match="@someone"):
            existing_approval(sub, approver="mmcky")

    def test_superseded_submission_refuses(self):
        sub = {"submission_id": "x", "status": "superseded", "superseded_by": "x-v2"}
        with pytest.raises(ValueError, match="superseded by `x-v2`"):
            existing_approval(sub, approver="mmcky")


class TestMainOnRerun:
    def _setup(self, tmp_path, **sub):
        path = _write_submission(tmp_path, "2026-05", "jdoe-timesheet-2026-05", **sub)
        pdf_dir = tmp_path / "generated_pdfs" / "2026-05"
        pdf_dir.mkdir(parents=True)
        for ext in ("pdf", "png"):
            (pdf_dir / f"jdoe-timesheet-2026-05.{ext}").write_bytes(b"as sent")
        return path

    def _main(self, tmp_path, path):
        return main([
            "--submission", str(path), "--settings", str(tmp_path / "settings.yml"),
            "--templates", str(tmp_path / "templates"), "--approver", "mmcky",
            "--approved-date", "2026-06-30", "--repo-root", str(tmp_path),
        ])

    def test_keeps_stamp_and_rendered_files(self, tmp_path):
        path = self._setup(tmp_path, approved_by="mmcky", approved_date="2026-05-13")
        before = path.read_bytes()
        assert self._main(tmp_path, path) == 0
        assert path.read_bytes() == before
        assert (tmp_path / "generated_pdfs/2026-05/jdoe-timesheet-2026-05.pdf").read_bytes() == b"as sent"

    def test_conflicting_approval_fails_without_writing(self, tmp_path):
        path = self._setup(tmp_path, approved_by="someone", approved_date="2026-05-13")
        before = path.read_bytes()
        assert self._main(tmp_path, path) == 1
        assert path.read_bytes() == before
