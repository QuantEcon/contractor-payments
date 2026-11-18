"""Push the current HEAD to main, rebasing over approvals that landed first.

Used by `.github/workflows/process-approved.yml` for both of its commits to
main: the stamped submission + ledger, and the record of the approval email.

Two approvals merging close together both push to main. Without a retry the
loser is simply rejected. On rejection this fetches main, rebases the local
commit onto it and tries again, up to `--attempts` times with a short backoff.
A rebase conflict means another run changed the same lines; the rebase is
aborted and nothing is pushed, so a re-run starts from the settled main.

CLI:
  python -m scripts.push_main [--attempts 5] [--remote origin] [--branch main]

Exit status 0 once pushed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from typing import Callable, Optional


def _git(*args: str, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def push_with_retry(
    *,
    attempts: int = 5,
    remote: str = "origin",
    branch: str = "main",
    cwd: Optional[str] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Push HEAD to `remote/branch`, rebasing and retrying on rejection.
    Returns True once pushed; False on a rebase conflict or after the last
    attempt. Reasons go to stderr."""
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
            print(f"ERROR: rebasing onto the updated {branch} conflicted: another "
                  f"run changed the same lines. Nothing was pushed.",
                  file=sys.stderr)
            return False
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
    args = p.parse_args(argv)
    ok = push_with_retry(attempts=args.attempts, remote=args.remote, branch=args.branch)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
