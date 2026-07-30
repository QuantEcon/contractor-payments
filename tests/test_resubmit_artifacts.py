"""Regression tests for the resubmit path — the case where an issue is
`/submit`ted a second time while its PR is still open.

The engine used to only ever *add* on that path: `stage_and_commit` was handed
just the paths it had written, so the previous run's submission YAML and
PDF/PNG survived alongside the new ones. The damaging case is a corrected
period — both submissions end up on the branch, and `process-approved.yml`
then approves whichever one sorts first, which for a forward correction is the
one the contractor abandoned.

These use real git repositories in tmp_path because the defect lived in the
interaction between the filesystem writes and what got staged; asserting on
mocked `git add` calls would have missed it (a plain `git add <path>` of a
deleted file leaves the deletion unstaged).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import scripts.create_submission_pr as cspr
from scripts.create_submission_pr import purge_branch_artifacts, stage_and_commit


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
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first submit")

        removed = purge_branch_artifacts(base, cwd=repo)

        assert {p.relative_to(repo).as_posix() for p in removed} == {
            "submissions/2026-05/x-2026-05.yml",
            "generated_pdfs/2026-05/x-2026-05.pdf",
        }
        for path in removed:
            assert not path.exists()

    def test_leaves_artifacts_already_merged_on_the_base_branch(self, repo):
        """A -vN revision must not delete the original it supersedes."""
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
        _write(repo / "ledger/QE-PSL-2026-001.yml", "items: []\n")
        _write(repo / "config/settings.yml", "a: 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "submit + unrelated")

        purge_branch_artifacts(base, cwd=repo)

        assert (repo / "ledger/QE-PSL-2026-001.yml").exists()
        assert (repo / "config/settings.yml").exists()

    def test_no_op_when_branch_added_nothing(self, repo):
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", "submission/issue-12")
        assert purge_branch_artifacts(base, cwd=repo) == []

    def test_skip_pdf_keeps_the_committed_pdf(self, repo):
        """Nothing regenerates PDFs under --skip-pdf, so purging them would
        drop the committed artifact rather than replace it."""
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", "submission/issue-12")
        _write(repo / "submissions/2026-06/x.yml")
        _write(repo / "generated_pdfs/2026-06/x.pdf")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first submit")

        removed = purge_branch_artifacts(base, cwd=repo, dirs=("submissions",))

        assert [p.name for p in removed] == ["x.yml"]
        assert (repo / "generated_pdfs/2026-06/x.pdf").exists()


# ─── stage_and_commit records removals ──────────────────────────────────────

class TestStageAndCommitRecordsRemovals:
    def test_deleted_paths_leave_the_commit(self, repo):
        """The core of the fix: purging is worthless if the deletion never
        reaches HEAD. `git add <deleted-file>` alone did not do it."""
        stale = _write(repo / "submissions/2026-05/x-2026-05.yml")
        keep = _write(repo / "submissions/2026-06/x-2026-06.yml")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first submit")
        assert "submissions/2026-05/x-2026-05.yml" in _tracked(repo)

        stale.unlink()
        committed = stage_and_commit([stale, keep], 12, cwd=repo, update=True)

        assert committed is True
        tracked = _tracked(repo)
        assert "submissions/2026-05/x-2026-05.yml" not in tracked
        assert "submissions/2026-06/x-2026-06.yml" in tracked

    def test_still_reports_no_op_when_nothing_changed(self, repo):
        path = _write(repo / "submissions/2026-06/x.yml", "a: 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first")
        assert stage_and_commit([path], 12, cwd=repo, update=True) is False


# ─── The symptom that matters ───────────────────────────────────────────────

class TestPeriodCorrectionLeavesOneSubmission:
    """Reproduces the worst case end to end: a contractor files for the wrong
    month, corrects it before merge, and the branch must carry only the
    corrected submission. Pre-fix it carried both, and `head -1` approved the
    abandoned one — coherently, so nothing looked wrong."""

    def test_only_the_corrected_submission_survives(self, repo):
        base = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "checkout", "-q", "-b", "submission/issue-12")
        _write(repo / "submissions/2026-05/x-2026-05.yml", "period: 2026-05\n")
        _write(repo / "generated_pdfs/2026-05/x-2026-05.pdf")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first submit (wrong month)")

        purged = purge_branch_artifacts(base, cwd=repo)
        yaml_path = _write(
            repo / "submissions/2026-06/x-2026-06.yml", "period: 2026-06\n"
        )
        pdf_path = _write(repo / "generated_pdfs/2026-06/x-2026-06.pdf")
        stage_and_commit(
            [*purged, yaml_path, pdf_path], 12, cwd=repo, update=True,
        )

        assert sorted(t for t in _tracked(repo) if t.startswith("submissions/")) == [
            "submissions/2026-06/x-2026-06.yml"
        ]
        assert not (repo / "submissions/2026-05").exists()
        assert sorted(t for t in _tracked(repo) if t.startswith("generated_pdfs/")) == [
            "generated_pdfs/2026-06/x-2026-06.pdf"
        ]


class TestStalePrBodyIsFlaggedOnThePr:
    """`update_pr_body` failing leaves the run green and the submission pushed,
    so the only signal used to be a stderr line in a workflow log. The PR body
    is the approval-decision surface, so the caveat has to land on the PR.
    """

    def test_warns_on_the_pr_when_the_body_could_not_be_refreshed(
        self, repo, monkeypatch,
    ):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cspr, "_run", fake_run)
        assert cspr.warn_stale_pr_body(7, cwd=repo) is True

        assert calls[0][:4] == ["gh", "pr", "comment", "7"]
        body = calls[0][calls[0].index("--body") + 1]
        assert "out of date" in body
        assert "Files changed" in body

    def test_returns_false_when_even_the_comment_fails(self, repo, monkeypatch):
        monkeypatch.setattr(
            cspr, "_run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no auth"),
        )
        assert cspr.warn_stale_pr_body(7, cwd=repo) is False
