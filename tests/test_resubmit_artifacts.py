"""Regression tests for the resubmit path — the case where an issue is
`/submit`ted a second time while its PR is still open.

The engine used to only ever *add* on that path: `place_receipts` merged into
the existing receipts directory and `stage_and_commit` was handed just the
paths it had written, so the previous run's submission YAML, PDF/PNG and
receipts all survived alongside the new ones. Three separate failures came out
of that one gap — a withdrawn receipt still emailed to the fiscal host, an
abandoned claim approved after a period correction, and an unmergeable PR when
a second same-month claim was filed before the first merged.

These use real git repositories in tmp_path because the defect lived in the
interaction between the filesystem writes and what got staged; asserting on
mocked `git add` calls would have missed it (a plain `git add <path>` of a
deleted file leaves the deletion unstaged).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.create_submission_pr import (
    place_receipts,
    purge_branch_artifacts,
    stage_and_commit,
)


# ─── git helpers ────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "engine@example.org")
    _git(path, "config", "user.name", "Engine")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-qm", "seed")
    return path


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tracked(repo: Path) -> set[str]:
    return set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD").split())


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path / "contractor-repo")


# ─── purge_branch_artifacts ─────────────────────────────────────────────────

class TestPurgeBranchArtifacts:
    def test_removes_what_the_branch_added(self, repo):
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", "submission/issue-12")
        _write(repo / "submissions/2026-05/x-2026-05.yml", "period: 2026-05\n")
        _write(repo / "generated_pdfs/2026-05/x-2026-05.pdf")
        _write(repo / "receipts/2026-05/x-2026-05/01-taxi.png")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first submit")

        removed = purge_branch_artifacts(base, cwd=repo)

        assert {p.relative_to(repo).as_posix() for p in removed} == {
            "submissions/2026-05/x-2026-05.yml",
            "generated_pdfs/2026-05/x-2026-05.pdf",
            "receipts/2026-05/x-2026-05/01-taxi.png",
        }
        for path in removed:
            assert not path.exists()

    def test_leaves_artifacts_already_merged_on_the_base_branch(self, repo):
        """A -vN revision must not delete the original claim it supersedes."""
        _write(repo / "submissions/2026-05/x-2026-05.yml", "status: approved\n")
        _write(repo / "generated_pdfs/2026-05/x-2026-05.pdf")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "approved original")
        base = _git(repo, "rev-parse", "HEAD").strip()

        _git(repo, "checkout", "-q", "-b", "submission/issue-12")
        _write(repo / "submissions/2026-05/x-2026-05-v2.yml", "period: 2026-05\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "revision")

        removed = purge_branch_artifacts(base, cwd=repo)

        assert [p.name for p in removed] == ["x-2026-05-v2.yml"]
        assert (repo / "submissions/2026-05/x-2026-05.yml").exists()
        assert (repo / "generated_pdfs/2026-05/x-2026-05.pdf").exists()

    def test_ignores_non_artifact_paths(self, repo):
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", "submission/issue-12")
        _write(repo / "submissions/2026-05/x.yml")
        _write(repo / "ledger/reimbursements.yml", "totals: {}\n")
        _write(repo / "config/settings.yml", "a: 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "submit + unrelated")

        purge_branch_artifacts(base, cwd=repo)

        assert (repo / "ledger/reimbursements.yml").exists()
        assert (repo / "config/settings.yml").exists()

    def test_no_op_when_branch_added_nothing(self, repo):
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", "submission/issue-12")
        assert purge_branch_artifacts(base, cwd=repo) == []


# ─── stage_and_commit records removals ──────────────────────────────────────

class TestStageAndCommitRecordsRemovals:
    def test_deleted_paths_leave_the_commit(self, repo):
        """The core of the fix: purging is worthless if the deletion never
        reaches HEAD. `git add <deleted-file>` alone did not do it."""
        stale = _write(repo / "receipts/2026-06/x/01-taxi.png")
        keep = _write(repo / "receipts/2026-06/x/02-hotel.pdf")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first submit")
        assert "receipts/2026-06/x/01-taxi.png" in _tracked(repo)

        stale.unlink()
        committed = stage_and_commit(
            [stale, keep], 12, cwd=repo, submission_type="reimbursement",
            update=True,
        )

        assert committed is True
        tracked = _tracked(repo)
        assert "receipts/2026-06/x/01-taxi.png" not in tracked
        assert "receipts/2026-06/x/02-hotel.pdf" in tracked

    def test_still_reports_no_op_when_nothing_changed(self, repo):
        path = _write(repo / "submissions/2026-06/x.yml", "a: 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first")
        assert stage_and_commit([path], 12, cwd=repo, update=True) is False


# ─── End-to-end: the three symptoms ─────────────────────────────────────────

class TestResubmitSymptoms:
    """Each test drives the real purge + place + stage sequence the update
    path now runs, and asserts on the committed tree."""

    def _first_submit(self, repo, period="2026-06", sub_id="x-2026-06",
                      receipts=("01-taxi.png", "02-hotel.pdf")):
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", "submission/issue-12")
        _write(repo / f"submissions/{period}/{sub_id}.yml", f"period: {period}\n")
        for name in receipts:
            _write(repo / f"receipts/{period}/{sub_id}/{name}")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first submit")
        return base

    def _resubmit(self, repo, base, staging, period, sub_id, manifest):
        purged = purge_branch_artifacts(base, cwd=repo)
        yaml_path = _write(
            repo / f"submissions/{period}/{sub_id}.yml", f"period: {period}\n"
        )
        placed = place_receipts(
            staging, manifest, {"period": period, "submission_id": sub_id}, repo,
        )
        stage_and_commit(
            [*purged, yaml_path, *placed], 12, cwd=repo,
            submission_type="reimbursement", update=True,
        )
        return placed

    def test_withdrawn_receipt_does_not_survive(self, repo, tmp_path):
        """Symptom 1 (F1): receipts are index-numbered from 01- in issue
        order, so withdrawing the first one renumbers the second. The old
        code left the withdrawn file AND a duplicate of the kept one."""
        base = self._first_submit(repo)
        staging = tmp_path / "staging"
        _write(staging / "01-hotel.pdf")

        self._resubmit(
            repo, base, staging, "2026-06", "x-2026-06",
            [{"filename": "01-hotel.pdf", "source_url": "u2"}],
        )

        receipts = sorted(
            p.name for p in (repo / "receipts/2026-06/x-2026-06").iterdir()
        )
        assert receipts == ["01-hotel.pdf"]
        assert {t for t in _tracked(repo) if t.startswith("receipts/")} == {
            "receipts/2026-06/x-2026-06/01-hotel.pdf"
        }

    def test_period_correction_leaves_exactly_one_submission(self, repo, tmp_path):
        """Symptom 2, the worst one: correcting the period wrote a *second*
        YAML, and process-approved.yml then approved whichever sorted first —
        for a forward correction, the abandoned claim."""
        base = self._first_submit(
            repo, period="2026-05", sub_id="x-2026-05", receipts=("01-flight.pdf",)
        )
        staging = tmp_path / "staging"
        _write(staging / "01-flight.pdf")

        self._resubmit(
            repo, base, staging, "2026-06", "x-2026-06",
            [{"filename": "01-flight.pdf", "source_url": "u1"}],
        )

        submissions = sorted(
            t for t in _tracked(repo) if t.startswith("submissions/")
        )
        assert submissions == ["submissions/2026-06/x-2026-06.yml"]
        assert not (repo / "submissions/2026-05").exists()
        # and the abandoned claim's receipts went with it
        assert sorted(t for t in _tracked(repo) if t.startswith("receipts/")) == [
            "receipts/2026-06/x-2026-06/01-flight.pdf"
        ]

    def test_uniqueness_suffix_change_leaves_no_collision(self, repo, tmp_path):
        """Symptom 3: a second same-month claim re-filed under a -B suffix
        left the un-suffixed set committed, so the PR stayed unmergeable."""
        base = self._first_submit(
            repo, sub_id="x-2026-06", receipts=("01-receipt.pdf",)
        )
        staging = tmp_path / "staging"
        _write(staging / "01-receipt.pdf")

        self._resubmit(
            repo, base, staging, "2026-06", "x-2026-06-B",
            [{"filename": "01-receipt.pdf", "source_url": "u1"}],
        )

        assert sorted(t for t in _tracked(repo) if t.startswith("submissions/")) == [
            "submissions/2026-06/x-2026-06-B.yml"
        ]
        assert not (repo / "receipts/2026-06/x-2026-06").exists()


class TestPurgeRespectsSkipPdf:
    def test_skip_pdf_keeps_the_committed_pdf(self, repo):
        """Nothing regenerates PDFs under --skip-pdf, so purging them would
        drop the committed artifact rather than replace it."""
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", "submission/issue-12")
        _write(repo / "submissions/2026-06/x.yml")
        _write(repo / "generated_pdfs/2026-06/x.pdf")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first submit")

        removed = purge_branch_artifacts(
            base, cwd=repo, dirs=("submissions", "receipts"),
        )

        assert [p.name for p in removed] == ["x.yml"]
        assert (repo / "generated_pdfs/2026-06/x.pdf").exists()
