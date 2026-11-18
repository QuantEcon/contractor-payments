#!/usr/bin/env python3
"""Write the engine caller workflows into contractor repo checkouts.

Every contractor repo carries three thin callers (`.github/workflows/`) that
invoke this engine's reusable workflows at a pinned ref: the `@ref` on each
`uses:` line and the matching `engine_ref:` input. This copies the current
`contractor-template/` callers into each checkout, pinned at `--ref`, so that

  - a caller change (a new trigger, explicit secrets) reaches existing repos,
    not only repos onboarded after it; and
  - moving a repo between pins is one command: `v1` for live contractor
    repos, `main` (or a PR branch) for the test repo.

It only writes files. Review the diff, then commit and push in each repo.

Usage:
  python onboarding/sync_callers.py contractors/contractor-jdoe [more repos...]
  python onboarding/sync_callers.py --ref main ../test-contractor-payments
  python onboarding/sync_callers.py --check contractors/*   # exit 1 on drift

`--ref` defaults to the ref the template pins (the current release tag).

See notes/ADMIN_RUNBOOK.md, "Engine releases".
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

ENGINE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_WORKFLOWS = ENGINE_ROOT / "contractor-template" / ".github" / "workflows"

_USES = re.compile(
    r"(QuantEcon/contractor-payments/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml)@([^\s#]+)"
)
_ENGINE_REF = re.compile(r"^(\s*engine_ref:\s*)(\S+)\s*$", re.MULTILINE)
# A git ref name, conservatively: no spaces, quotes or YAML-significant chars.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def template_ref() -> str:
    """The ref the template callers pin (they must all agree)."""
    refs = {
        m.group(2)
        for path in sorted(TEMPLATE_WORKFLOWS.glob("*.yml"))
        for m in _USES.finditer(path.read_text(encoding="utf-8"))
    }
    if len(refs) != 1:
        raise SystemExit(f"template callers disagree on the engine ref: {sorted(refs)}")
    return refs.pop()


def pin(text: str, ref: str) -> str:
    """Rewrite every engine `uses:` ref and `engine_ref:` value to `ref`."""
    if not _SAFE_REF.match(ref):
        raise ValueError(f"not a usable git ref: {ref!r}")
    text = _USES.sub(lambda m: f"{m.group(1)}@{ref}", text)
    return _ENGINE_REF.sub(lambda m: f"{m.group(1)}{ref}", text)


def rendered_callers(ref: str) -> dict[str, str]:
    """Template caller filename -> its content pinned at `ref`."""
    return {
        path.name: pin(path.read_text(encoding="utf-8"), ref)
        for path in sorted(TEMPLATE_WORKFLOWS.glob("*.yml"))
    }


def sync_repo(repo: Path, ref: str, *, check: bool = False) -> list[str]:
    """Bring `repo`'s callers in line with the template at `ref`. Returns the
    names of files that differed (and, unless `check`, were rewritten)."""
    workflows = repo / ".github" / "workflows"
    if not (repo / ".git").exists() or not workflows.is_dir():
        raise SystemExit(f"{repo}: not a contractor repo checkout (no .git or .github/workflows)")
    changed = []
    for name, content in rendered_callers(ref).items():
        target = workflows / name
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == content:
            continue
        changed.append(name)
        if not check:
            target.write_text(content, encoding="utf-8")
    return changed


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("repos", nargs="+", type=Path, help="Contractor repo checkouts.")
    p.add_argument("--ref", default=None,
                   help="Engine ref to pin (tag or branch). Default: the template's.")
    p.add_argument("--check", action="store_true",
                   help="Report drift without writing; exit 1 if any repo differs.")
    args = p.parse_args(argv)

    ref = args.ref or template_ref()
    drift = False
    for repo in args.repos:
        changed = sync_repo(repo, ref, check=args.check)
        if not changed:
            print(f"{repo}: callers already match the template at @{ref}")
            continue
        drift = True
        verb = "differ from" if args.check else "updated to"
        print(f"{repo}: {', '.join(changed)} {verb} the template at @{ref}")
    if args.check and drift:
        return 1
    if drift and not args.check:
        print("\nReview with `git diff`, then commit and push in each repo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
