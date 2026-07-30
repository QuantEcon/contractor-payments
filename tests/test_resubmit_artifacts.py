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

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from scripts import create_submission_pr as cspr
import scripts.create_submission_pr as cspr
from scripts.create_submission_pr import (
    place_receipts,
    purge_branch_artifacts,
    stage_and_commit,
    update_pr_body,
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


# ─── PR body refresh on the update path ─────────────────────────────────────

# Stand-in for the `gh` CLI, so the real `gh pr edit --body-file` invocation
# runs (temp file and all) rather than being asserted on as a mock call.
_FAKE_GH = '''#!/usr/bin/env python3
import os
import shutil
import sys

args = sys.argv[1:]
with open(os.environ["GH_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")

if args[:2] == ["pr", "list"]:
    print(os.environ.get("GH_PR_NUMBER", ""))
elif args[:2] == ["pr", "edit"]:
    if "--body-file" in args:
        shutil.copyfile(args[args.index("--body-file") + 1], os.environ["GH_BODY_OUT"])
    sys.exit(int(os.environ.get("GH_EDIT_EXIT", "0")))
elif args[:2] == ["repo", "view"]:
    print("QuantEcon/contractor-janedoe")
sys.exit(0)
'''


class FakeGh:
    def __init__(self, log: Path, body: Path):
        self._log = log
        self.body_path = body

    @property
    def calls(self) -> list[str]:
        return self._log.read_text(encoding="utf-8").splitlines() if self._log.exists() else []

    @property
    def body(self) -> str:
        return self.body_path.read_text(encoding="utf-8") if self.body_path.exists() else ""


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("GH_BODY_OUT", str(tmp_path / "pr-body.md"))
    monkeypatch.setenv("GH_PR_NUMBER", "7")
    return FakeGh(tmp_path / "gh.log", tmp_path / "pr-body.md")


class TestUpdatePrBody:
    def test_writes_the_body_through_gh_pr_edit(self, fake_gh, tmp_path):
        assert update_pr_body(7, "fresh body\n", cwd=tmp_path) is True
        assert len(fake_gh.calls) == 1
        assert fake_gh.calls[0].startswith("pr edit 7 --body-file ")
        assert fake_gh.body == "fresh body\n"

    def test_returns_false_instead_of_raising_when_gh_fails(
        self, fake_gh, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("GH_EDIT_EXIT", "1")
        assert update_pr_body(7, "fresh body\n", cwd=tmp_path) is False


@pytest.fixture
def cloned_repo(tmp_path):
    """Work tree with a real `origin`, so `git ls-remote`, the branch fetch and
    the force-push behave the way they do in the workflow."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True,
    )
    work = _init_repo(tmp_path / "contractor-repo")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


class TestPrBodyRefreshedOnResubmit:
    """The push updates a PR's commits; it never touches the description. So a
    resubmit used to leave the body — total, period, line items, receipt sizes,
    the email-size warnings — frozen on the first submission's content while the
    YAML and PDF moved on underneath it. That body is what the admin approves
    against."""

    ISSUE = 12
    BRANCH = "submission/issue-12"

    def _first_submission_pushed(self, repo):
        _git(repo, "checkout", "-q", "-b", self.BRANCH)
        _write(
            repo / "submissions/2026-06/janedoe-reimbursement-2026-06.yml",
            "totals:\n  amount: 100\n  currency: JPY\n",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "first submit")
        _git(repo, "push", "-q", "-u", "origin", self.BRANCH)
        _git(repo, "checkout", "-q", "main")
        # The workflow starts from a fresh clone, so the branch is remote-only.
        _git(repo, "branch", "-qD", self.BRANCH)

    def _resubmit(self, repo, tmp_path):
        (repo / "config").mkdir(parents=True, exist_ok=True)
        (repo / "config" / "reimbursements.yml").write_text(
            "project: CHOW\nallowed_categories: [meals, accommodation]\n",
            encoding="utf-8",
        )
        parsed = {
            "type": "reimbursement",
            "period": "2026-06",
            "entries": [
                {"date": "2026-06-03", "amount": 9000, "category": "accommodation",
                 "description": "Hotel"},
                {"date": "2026-06-05", "amount": 3000, "category": "meals",
                 "description": "Dinner"},
            ],
            "totals": {"amount": 12000, "currency": "JPY"},
            "trip_context": "PyCon JP",
            "receipts": [],
            "notes": "",
            "status": "pending",
        }
        submission_file = tmp_path / "submission.json"
        submission_file.write_text(json.dumps(parsed), encoding="utf-8")
        return cspr.main([
            "--submission-file", str(submission_file),
            "--issue-number", str(self.ISSUE),
            "--issue-author", "janedoe",
            "--issue-title", "Reimbursement 2026-06",
            "--repo-root", str(repo),
            "--submitted-date", "2026-06-11",
            "--skip-pdf",
        ])

    def test_body_is_rewritten_with_this_run_content(
        self, cloned_repo, tmp_path, fake_gh,
    ):
        self._first_submission_pushed(cloned_repo)

        assert self._resubmit(cloned_repo, tmp_path) == 0

        assert any(c.startswith("pr edit 7 ") for c in fake_gh.calls)
        body = fake_gh.body
        assert "**Total amount:** 12000 JPY" in body
        assert "**Line items:** 2" in body
        assert f"Closes #{self.ISSUE}" in body
        assert "100" not in body.replace("12000", "")   # no trace of the old total

    def test_failed_body_update_warns_but_keeps_the_submission(
        self, cloned_repo, tmp_path, fake_gh, monkeypatch, capsys,
    ):
        """The submission is committed and pushed before the body update runs;
        a failed `gh` call must not throw that away."""
        monkeypatch.setenv("GH_EDIT_EXIT", "1")
        self._first_submission_pushed(cloned_repo)

        assert self._resubmit(cloned_repo, tmp_path) == 0

        assert "could not refresh PR #7" in capsys.readouterr().err
        # ...and the new submission is on the branch, both locally and on origin.
        assert _tracked(cloned_repo) >= {
            "submissions/2026-06/janedoe-reimbursement-2026-06.yml"
        }
        pushed = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", f"origin/{self.BRANCH}"],
            cwd=cloned_repo, capture_output=True, text=True, check=True,
        ).stdout.split()
        assert "submissions/2026-06/janedoe-reimbursement-2026-06.yml" in pushed


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


class TestContractorNotifiedMarker:
    """`report_submit_error` leaves a marker so the workflow's catch-all
    failure() comment stands down — otherwise the contractor gets two comments,
    the generic one asserting the submission text is fine and thereby
    contradicting the specific one above it."""

    def test_marker_is_written(self, tmp_path):
        marker = tmp_path / "notified"
        cspr._mark_contractor_notified(str(marker))
        assert marker.read_text(encoding="utf-8") == "1"

    def test_unwritable_marker_is_not_fatal(self, tmp_path):
        # Already on the failure path; worst case is a second generic comment.
        cspr._mark_contractor_notified(str(tmp_path / "nope" / "notified"))
