"""Post or clear a parse-error comment on a timesheet submission issue.

The comment carries an HTML sentinel marker so the workflow can find and
update it in place on re-run, rather than spamming a new comment for every
failed parse. Applying / removing the `parse-error` label is part of the
same operation, so the state on the issue is self-consistent.

Two subcommands:

    post    --errors-file errs.json --issue 42
    clear   --issue 42

Both read the repo from --repo or the GITHUB_REPOSITORY env var, and use the
`gh` CLI under the hood (no Python GitHub library). See PLAN §4.4.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


SENTINEL = "<!-- timesheet-parse-error -->"
DEFAULT_LABEL = "parse-error"


# ─── Pure rendering (testable) ──────────────────────────────────────────────

def render_error_comment(
    errors: list[dict],
    warnings: Optional[list[dict]] = None,
) -> str:
    """Render a list of parser errors (and optional warnings) into a markdown
    comment that ends with the sentinel marker.

    Each error is a dict with `message` (str) and optional `line` (int).
    Each warning is a dict with `message` (str).
    """
    warnings = warnings or []

    lines: list[str] = []
    lines.append("🤖 **Submission needs a fix**")
    lines.append("")
    lines.append("I couldn't parse this submission. Here's what I found:")
    lines.append("")

    for err in errors:
        msg = err["message"]
        line_no = err.get("line")
        if line_no is not None:
            lines.append(f"- **Line {line_no}:** {msg}")
        else:
            lines.append(f"- {msg}")

    lines.append("")
    lines.append("To fix, **edit this issue** (click the ⋯ menu → Edit) and")
    lines.append("update those lines. I'll re-check automatically when you save.")

    if warnings:
        lines.append("")
        lines.append("_Notes (non-blocking):_")
        for w in warnings:
            lines.append(f"- {w['message']}")

    lines.append("")
    lines.append(SENTINEL)
    return "\n".join(lines) + "\n"


# ─── GitHub API via gh ──────────────────────────────────────────────────────

def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a gh command, raising on non-zero exit unless check=False is passed."""
    return subprocess.run(
        args, capture_output=True, text=True, check=True, **kwargs
    )


def find_existing_comment_id(repo: str, issue: int) -> Optional[int]:
    """Return the ID of the existing parse-error comment, or None if there
    isn't one. Uses --paginate so issues with many comments are handled."""
    result = _run([
        "gh", "api", "--paginate",
        f"repos/{repo}/issues/{issue}/comments",
    ])
    comments = json.loads(result.stdout)
    # --paginate concatenates JSON arrays into one stream; gh handles flattening.
    if isinstance(comments, dict):
        comments = [comments]
    for c in comments:
        if SENTINEL in c.get("body", ""):
            return int(c["id"])
    return None


def _write_body_tempfile(body: str) -> Path:
    """Write the body to a temp file. Avoids shell-escaping issues for
    multi-line content."""
    fd, name = tempfile.mkstemp(suffix=".md", prefix="parse-error-")
    path = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def update_comment(repo: str, comment_id: int, body: str) -> None:
    body_path = _write_body_tempfile(body)
    try:
        _run([
            "gh", "api", "--method", "PATCH",
            f"repos/{repo}/issues/comments/{comment_id}",
            "-F", f"body=@{body_path}",
        ])
    finally:
        body_path.unlink(missing_ok=True)


def create_comment(repo: str, issue: int, body: str) -> None:
    body_path = _write_body_tempfile(body)
    try:
        _run([
            "gh", "issue", "comment", str(issue),
            "--repo", repo,
            "--body-file", str(body_path),
        ])
    finally:
        body_path.unlink(missing_ok=True)


def delete_comment(repo: str, comment_id: int) -> None:
    _run([
        "gh", "api", "--method", "DELETE",
        f"repos/{repo}/issues/comments/{comment_id}",
    ])


def add_label(repo: str, issue: int, label: str) -> None:
    # `gh issue edit --add-label` creates the label on the repo if it doesn't
    # already exist (in recent gh versions). Falls back gracefully if not.
    _run([
        "gh", "issue", "edit", str(issue),
        "--repo", repo,
        "--add-label", label,
    ])


def remove_label(repo: str, issue: int, label: str) -> None:
    # Don't fail if the label isn't applied — clearing should be idempotent.
    subprocess.run(
        [
            "gh", "issue", "edit", str(issue),
            "--repo", repo,
            "--remove-label", label,
        ],
        capture_output=True, text=True, check=False,
    )


# ─── Orchestration ──────────────────────────────────────────────────────────

def post_or_update(
    repo: str,
    issue: int,
    errors: list[dict],
    warnings: list[dict],
    label: str,
) -> None:
    if not errors:
        raise ValueError("post_or_update called with no errors — nothing to report")
    body = render_error_comment(errors, warnings)
    existing = find_existing_comment_id(repo, issue)
    if existing is not None:
        update_comment(repo, existing, body)
        print(f"Updated existing parse-error comment ({existing}).")
    else:
        create_comment(repo, issue, body)
        print("Posted new parse-error comment.")
    add_label(repo, issue, label)


def clear(repo: str, issue: int, label: str) -> None:
    existing = find_existing_comment_id(repo, issue)
    if existing is not None:
        delete_comment(repo, existing)
        print(f"Deleted parse-error comment ({existing}).")
    else:
        print("No parse-error comment to clear.")
    remove_label(repo, issue, label)


# ─── CLI ────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    p_post = sub.add_parser("post", help="Post or update the parse-error comment.")
    p_post.add_argument("--errors-file", required=True,
                        help="JSON output from parse_issue.py (--output-errors-json).")
    p_post.add_argument("--issue", type=int, required=True)
    p_post.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    p_post.add_argument("--label", default=DEFAULT_LABEL)

    p_clear = sub.add_parser("clear", help="Delete the parse-error comment and remove the label.")
    p_clear.add_argument("--issue", type=int, required=True)
    p_clear.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    p_clear.add_argument("--label", default=DEFAULT_LABEL)

    args = parser.parse_args(argv)

    if not args.repo:
        print("ERROR: --repo or GITHUB_REPOSITORY env var is required.", file=sys.stderr)
        return 2

    if args.mode == "post":
        with open(args.errors_file, encoding="utf-8") as f:
            data = json.load(f)
        errors = data.get("errors", [])
        warnings = data.get("warnings", [])
        if not errors:
            print("No errors in errors-file — nothing to post.", file=sys.stderr)
            return 0
        post_or_update(args.repo, args.issue, errors, warnings, args.label)
    elif args.mode == "clear":
        clear(args.repo, args.issue, args.label)

    return 0


if __name__ == "__main__":
    sys.exit(main())
