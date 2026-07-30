"""Tests for scripts/notify_comment.py — the pure comment renderer, and the
locked-issue handling in `post_comment`.

The renderer cases cover both ledger shapes and the "email not sent" branch.
`TestPostCommentOnLockedIssue` covers the bug that made the audit comment
impossible to post at all on the normal happy path: submission issues are
locked when filed, and GitHub rejects comments on a locked issue.
"""
from __future__ import annotations

import subprocess
import pytest
import scripts.notify_comment as nc

from scripts.notify_comment import compose_comment


HOURLY_SUBMISSION = {
    "submission_id": "janedoe-timesheet-2026-04",
    "type": "timesheet",
    "contract_id": "QE-PSL-2026-001",
    "period": "2026-04",
    "approved_by": "mmcky",
    "approved_date": "2026-05-13",
    "totals": {"hours": 14.5, "rate": 50.0, "amount": 725.0, "currency": "AUD"},
}

HOURLY_LEDGER = {
    "contract_id": "QE-PSL-2026-001",
    "type": "hourly",
    "currency": "AUD",
    "totals": {"hours_to_date": 14.5, "amount_to_date": 725.0, "submissions_count": 1},
}



EMAIL_SUMMARY = {
    "to": "reviewer@example.org",
    "cc": None,
    "subject": "x",
    "sent_at": "2026-06-12 01:00:00 UTC",
    "testing_mode": True,
    "testing_mode_source": "engine fiscal-host.yml default",
    "dry_run": False,
}


class TestComposeCommentExistingTypes:
    def test_hourly_renders_contract_and_ledger(self):
        out = compose_comment(
            submission=HOURLY_SUBMISSION, ledger=HOURLY_LEDGER,
            email_summary=EMAIL_SUMMARY, issue_number=42,
        )
        assert "**Timesheet approved** by @mmcky" in out
        assert "**Contract:** `QE-PSL-2026-001`" in out
        assert "725.00 AUD" in out
        assert "14.5 hours" in out
        assert "sent to reviewer@example.org" in out

    def test_missing_email_summary_warns(self):
        out = compose_comment(
            submission=HOURLY_SUBMISSION, ledger=HOURLY_LEDGER,
            email_summary=None, issue_number=42,
        )
        assert "not sent" in out


class TestPostCommentOnLockedIssue:
    """Submission issues are locked when filed, and GitHub rejects comments on
    a locked issue. That made the post-merge audit comment impossible on the
    happy path — and since nothing gated on failure, it silently skipped the
    `processed` label too. Found by E2E: merged PRs #32/#36 carry no
    `processed` label and their issues have no audit comment.
    """

    LOCKED_ERR = "GraphQL: Unable to create comment because issue is locked (addComment)"

    def _fake_gh(self, calls, *, comment_fails_until_unlocked=True,
                 unlock_rc=0, relock_rc=0):
        def run(cmd, **kwargs):
            calls.append(cmd[:3])
            verb = cmd[2]
            if verb == "comment":
                locked = comment_fails_until_unlocked and "unlock" not in [
                    c[2] for c in calls[:-1]
                ]
                if locked:
                    return subprocess.CompletedProcess(cmd, 1, "", self.LOCKED_ERR)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if verb == "unlock":
                return subprocess.CompletedProcess(cmd, unlock_rc, "", "nope")
            if verb == "lock":
                return subprocess.CompletedProcess(cmd, relock_rc, "", "nope")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return run

    def test_unlocks_comments_then_relocks(self, monkeypatch):
        calls = []
        monkeypatch.setattr(subprocess, "run", self._fake_gh(calls))
        nc.post_comment(39, "audit body", repo="Q/r")
        assert [c[2] for c in calls] == ["comment", "unlock", "comment", "lock"]

    def test_relocks_even_when_the_retry_fails(self, monkeypatch):
        """Leaving a submission issue unlocked is worse than a missing
        comment, so the re-lock is in a finally."""
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd[:3])
            if cmd[2] == "comment":
                return subprocess.CompletedProcess(cmd, 1, "", self.LOCKED_ERR)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", run)
        with pytest.raises(RuntimeError):
            nc.post_comment(39, "audit body", repo="Q/r")
        assert [c[2] for c in calls] == ["comment", "unlock", "comment", "lock"]

    def test_no_unlock_attempted_when_the_issue_is_not_locked(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            subprocess, "run",
            self._fake_gh(calls, comment_fails_until_unlocked=False),
        )
        nc.post_comment(39, "audit body", repo="Q/r")
        assert [c[2] for c in calls] == ["comment"]

    def test_unrelated_failure_still_raises_without_unlocking(self, monkeypatch):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd[:3])
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 403: forbidden")

        monkeypatch.setattr(subprocess, "run", run)
        with pytest.raises(RuntimeError, match="403"):
            nc.post_comment(39, "audit body", repo="Q/r")
        assert [c[2] for c in calls] == ["comment"]

    def test_unlock_failure_is_reported_clearly(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            subprocess, "run", self._fake_gh(calls, unlock_rc=1),
        )
        with pytest.raises(RuntimeError, match="unlock` failed"):
            nc.post_comment(39, "audit body", repo="Q/r")
