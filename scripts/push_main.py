"""Push the current HEAD to main, rebasing over approvals that landed first.

Used by `.github/workflows/process-approved.yml` for both of its commits to
main: the stamped submission + ledger, and the record of the approval email.

Two approvals merging close together both push to main. Without a retry the
loser is simply rejected. On rejection this fetches main, rebases the local
commit onto it and tries again, up to `--attempts` times with a short backoff.

A rebase conflict means another run changed the same lines: two approvals on
one contract both append to `ledger/<contract>.yml`. Text-merging a ledger is
wrong, but rebuilding it is easy, because the pipeline's steps are re-run safe.
With `--replay CMD`, a conflict resets to the updated main, runs CMD (the
stamp and ledger steps again) and commits the `--paths` it rewrote with the
original message, then pushes that. Without `--replay`, the rebase is aborted
and nothing is pushed.

CLI:
  python -m scripts.push_main [--attempts 5] [--remote origin] [--branch main]
                              [--replay CMD --paths PATH...]

Exit status 0 once pushed (or when a replay finds the change already on main),
1 otherwise.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from typing import Callable, Optional, Sequence


def _git(*args: str, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _replay(replay: str, paths: Sequence[str], message: str,
            cwd: Optional[str]) -> Optional[bool]:
    """Rebuild the change on FETCH_HEAD. Returns None to carry on pushing,
    True if the change turns out to be on main already, False on failure."""
    print("Rebase conflicted: another run changed the same files. Rebuilding "
          "this change on the updated main instead.", file=sys.stderr)
    # Every git step is checked: running the rebuild on the wrong base, or
    # staging nothing and reading that as "already on main", would report a
    # push that never happened.
    reset = _git("reset", "--hard", "FETCH_HEAD", cwd=cwd)
    if reset.returncode != 0:
        print(f"ERROR: could not reset to the updated branch: "
              f"{reset.stderr.strip()}. Nothing was pushed.", file=sys.stderr)
        return False
    rebuilt = subprocess.run(["bash", "-ec", replay], cwd=cwd)
    if rebuilt.returncode != 0:
        print(f"ERROR: the rebuild command failed (exit {rebuilt.returncode}). "
              f"Nothing was pushed.", file=sys.stderr)
        return False
    added = _git("add", "-A", "--", *paths, cwd=cwd)
    if added.returncode != 0:
        print(f"ERROR: could not stage the rebuilt files: "
              f"{added.stderr.strip()}. Nothing was pushed.", file=sys.stderr)
        return False
    staged = _git("diff", "--cached", "--quiet", cwd=cwd)
    if staged.returncode == 0:
        print("Nothing left to commit after rebuilding: the change is already "
              "on main.")
        return True
    if staged.returncode != 1:
        print(f"ERROR: could not inspect the rebuilt changes: "
              f"{staged.stderr.strip()}. Nothing was pushed.", file=sys.stderr)
        return False
    commit = _git("commit", "-m", message, cwd=cwd)
    if commit.returncode != 0:
        print(f"ERROR: commit after rebuild failed: {commit.stderr.strip()}",
              file=sys.stderr)
        return False
    return None


def push_with_retry(
    *,
    attempts: int = 5,
    remote: str = "origin",
    branch: str = "main",
    cwd: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
    replay: Optional[str] = None,
    paths: Sequence[str] = (),
) -> bool:
    """Push HEAD to `remote/branch`, rebasing and retrying on rejection.
    On a rebase conflict, rebuild with `replay` if given (see module doc).
    Returns True once pushed; False on an unrecoverable conflict or after the
    last attempt. Reasons go to stderr."""
    if replay and not paths:
        raise ValueError("--replay needs the --paths it rewrites")
    message = _git("log", "-1", "--format=%B", cwd=cwd).stdout.strip()
    for attempt in range(1, attempts + 1):
        push = _git("push", remote, f"HEAD:{branch}", cwd=cwd)
        if push.returncode == 0:
            print(f"Pushed to {branch} on attempt {attempt}.")
            return True
        print(f"Push rejected (attempt {attempt}/{attempts}); another approval "
              f"likely landed first.\n{push.stderr.strip()}", file=sys.stderr)
        if attempt == attempts:
            break
        fetch = _git("fetch", remote, branch, cwd=cwd)
        if fetch.returncode != 0:
            print(f"git fetch failed: {fetch.stderr.strip()}", file=sys.stderr)
            sleep(attempt * 3)
            continue
        rebase = _git("rebase", "FETCH_HEAD", cwd=cwd)
        if rebase.returncode != 0:
            _git("rebase", "--abort", cwd=cwd)
            if not replay:
                print(f"ERROR: rebasing onto the updated {branch} conflicted: "
                      f"another run changed the same lines. Nothing was pushed.",
                      file=sys.stderr)
                return False
            done = _replay(replay, paths, message, cwd)
            if done is not None:
                return done
        sleep(attempt * 3)
    print(f"ERROR: could not push to {branch} after {attempts} attempts "
          f"(persistent contention, or a rejected push such as branch "
          f"protection). Nothing was pushed.", file=sys.stderr)
    return False


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--attempts", type=int, default=5)
    p.add_argument("--remote", default="origin")
    p.add_argument("--branch", default="main")
    p.add_argument("--replay", default=None,
                   help="Shell command that regenerates this change on the updated "
                        "branch after a rebase conflict.")
    p.add_argument("--paths", nargs="+", default=[],
                   help="Paths the --replay command rewrites (committed after it runs).")
    args = p.parse_args(argv)
    ok = push_with_retry(attempts=args.attempts, remote=args.remote, branch=args.branch,
                         replay=args.replay, paths=args.paths)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
