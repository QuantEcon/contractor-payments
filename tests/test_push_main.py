"""scripts/push_main.py against real git repositories: a bare "origin" and
two clones standing in for two approval runs racing to push to main."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.push_main import push_with_retry


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _clone(origin: Path, path: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(origin), str(path)], check=True)
    _git(path, "config", "user.name", "test")
    _git(path, "config", "user.email", "test@example.org")
    return path


def _commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"edit {name}")


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = _clone(origin, tmp_path / "seed")
    _git(seed, "checkout", "-q", "-b", "main")
    _commit(seed, "ledger.yml", "entries:\n- a\n")
    _git(seed, "push", "-q", "origin", "main")
    return origin, _clone(origin, tmp_path / "run1"), _clone(origin, tmp_path / "run2")


def test_plain_push(repos):
    origin, run1, _ = repos
    _commit(run1, "sub.yml", "x\n")
    assert push_with_retry(cwd=str(run1), sleep=lambda s: None)
    assert _git(run1, "rev-parse", "HEAD") == _git(origin, "rev-parse", "main")


def test_rebases_over_an_approval_that_landed_first(repos):
    origin, run1, run2 = repos
    _commit(run1, "a.yml", "first\n")
    _git(run1, "push", "-q", "origin", "HEAD:main")
    _commit(run2, "b.yml", "second\n")      # different file: replays cleanly
    assert push_with_retry(cwd=str(run2), sleep=lambda s: None)
    log = _git(origin, "log", "--format=%s", "main")
    assert log.splitlines()[:2] == ["edit b.yml", "edit a.yml"]


def test_conflict_aborts_without_pushing(repos):
    origin, run1, run2 = repos
    _commit(run1, "ledger.yml", "entries:\n- a\n- run1\n")
    _git(run1, "push", "-q", "origin", "HEAD:main")
    before = _git(origin, "rev-parse", "main")
    _commit(run2, "ledger.yml", "entries:\n- a\n- run2\n")
    assert not push_with_retry(cwd=str(run2), sleep=lambda s: None)
    assert _git(origin, "rev-parse", "main") == before
    # Rebase was aborted: run2 is back on its own commit, not mid-rebase.
    assert not (run2 / ".git" / "rebase-merge").exists()
    assert _git(run2, "log", "-1", "--format=%s") == "edit ledger.yml"


def test_gives_up_after_the_last_attempt(repos, tmp_path):
    _, run1, _ = repos
    _commit(run1, "x.yml", "x\n")
    _git(run1, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    sleeps = []
    assert not push_with_retry(cwd=str(run1), attempts=3, sleep=sleeps.append)
    assert len(sleeps) == 2     # no backoff after the final attempt


# ─── Rebuild on conflict (issue #10) ────────────────────────────────────────
# Two approvals on one contract both append to the same ledger. Text-merging
# it is wrong; replaying the (re-run safe) steps on the updated main is right.

def _append_run2():
    # Stands in for "stamp + update_ledger" on whatever main now holds.
    return "printf -- '- run2\\n' >> ledger.yml"


def test_conflict_is_rebuilt_on_the_updated_main(repos):
    origin, run1, run2 = repos
    _commit(run1, "ledger.yml", "entries:\n- a\n- run1\n")
    _git(run1, "push", "-q", "origin", "HEAD:main")
    _commit(run2, "ledger.yml", "entries:\n- a\n- run2\n")
    _git(run2, "commit", "-q", "--amend", "-m", "Process approved submission from PR #2")
    assert push_with_retry(cwd=str(run2), sleep=lambda s: None,
                           replay=_append_run2(), paths=["ledger.yml"])
    shown = subprocess.run(["git", "--git-dir", str(origin), "show", "main:ledger.yml"],
                           check=True, capture_output=True, text=True).stdout
    assert shown == "entries:\n- a\n- run1\n- run2\n"
    subject = subprocess.run(["git", "--git-dir", str(origin), "log", "-1", "--format=%s", "main"],
                             check=True, capture_output=True, text=True).stdout.strip()
    assert subject == "Process approved submission from PR #2"


def test_failed_rebuild_pushes_nothing(repos):
    origin, run1, run2 = repos
    _commit(run1, "ledger.yml", "entries:\n- a\n- run1\n")
    _git(run1, "push", "-q", "origin", "HEAD:main")
    before = _git(origin, "rev-parse", "main")
    _commit(run2, "ledger.yml", "entries:\n- a\n- run2\n")
    assert not push_with_retry(cwd=str(run2), sleep=lambda s: None,
                               replay="exit 3", paths=["ledger.yml"])
    assert _git(origin, "rev-parse", "main") == before


def test_rebuild_that_changes_nothing_means_already_on_main(repos):
    origin, run1, run2 = repos
    _commit(run1, "ledger.yml", "entries:\n- a\n- same\n")
    _git(run1, "push", "-q", "origin", "HEAD:main")
    before = _git(origin, "rev-parse", "main")
    _commit(run2, "ledger.yml", "entries:\n- a\n- other\n")
    assert push_with_retry(cwd=str(run2), sleep=lambda s: None,
                           replay="true", paths=["ledger.yml"])
    assert _git(origin, "rev-parse", "main") == before


def test_replay_requires_paths():
    with pytest.raises(ValueError):
        push_with_retry(replay="true")


def test_rebuild_with_a_bad_path_fails_instead_of_claiming_success(repos):
    """A failed `git add` stages nothing, which used to read as "already on
    main" and report success with nothing pushed."""
    origin, run1, run2 = repos
    _commit(run1, "ledger.yml", "entries:\n- a\n- run1\n")
    _git(run1, "push", "-q", "origin", "HEAD:main")
    before = _git(origin, "rev-parse", "main")
    _commit(run2, "ledger.yml", "entries:\n- a\n- run2\n")
    assert not push_with_retry(cwd=str(run2), sleep=lambda s: None,
                               replay=_append_run2(), paths=["no-such-dir/"])
    assert _git(origin, "rev-parse", "main") == before


def test_failed_reset_stops_the_rebuild_before_it_runs(monkeypatch, tmp_path):
    import scripts.push_main as pm
    ran = []

    def fake_git(*args, cwd=None):
        rc = 1 if args[:2] == ("reset", "--hard") else 0
        return subprocess.CompletedProcess(["git", *args], rc, "", "fatal: bad ref")

    monkeypatch.setattr(pm, "_git", fake_git)
    monkeypatch.setattr(pm.subprocess, "run", lambda *a, **k: ran.append(a) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    assert pm._replay("touch x", ["x"], "msg", str(tmp_path)) is False
    assert ran == []        # the replay command never ran on the wrong base
