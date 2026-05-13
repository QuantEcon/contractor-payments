"""Finalise an approved submission: stamp the YAML with approval metadata
and re-render the PDF + PNG.

Called by `.github/workflows/process-approved.yml` on PR merge. The merged
PR's branch holds a submission YAML in `pending` state with `approved_by`
and `approved_date` both null. This script updates those three fields and
re-renders the PDF + PNG so the approval block flips from the amber
"PENDING REVIEW" state to the green "APPROVED BY @... ON ..." state.

The template at templates/timesheet.typ + templates/invoice.typ already
handles the rendering branch based on whether `approved_by` is null — see
the approval block at the bottom of each template. No template change is
needed.

CLI:
  python -m scripts.finalize_approval \\
      --submission submissions/2025-11/mmcky-invoice-2025-11.yml \\
      --settings   config/settings.yml \\
      --templates  templates \\
      --approver   mmcky \\
      [--approved-date 2025-11-20]   # defaults to today in payer timezone
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import yaml

from scripts.create_submission_pr import (
    resolve_payer_today,
    submission_pdf_path,
    submission_png_path,
)
from scripts.generate_pdf import (
    DEFAULT_PNG_PPI,
    render_submission_pdf,
    render_submission_png,
)


def stamp_approval(
    submission: dict,
    *,
    approver: str,
    approved_date: str,
) -> dict:
    """Pure transform: return a copy of `submission` with the three approval
    fields set. Idempotent — calling it twice with the same args is a no-op.
    """
    out = dict(submission)
    out["status"] = "approved"
    out["approved_by"] = approver
    out["approved_date"] = approved_date
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--submission", required=True, type=Path,
                   help="Path to the submission YAML to finalise (mutated in place).")
    p.add_argument("--settings", required=True, type=Path,
                   help="Path to config/settings.yml.")
    p.add_argument("--templates", required=True, type=Path,
                   help="Templates directory (contains timesheet.typ + invoice.typ + assets/).")
    p.add_argument("--approver", required=True,
                   help="GitHub handle of the admin who merged the PR.")
    p.add_argument("--approved-date", default=None,
                   help="ISO date for `approved_date`. Default: today in the fiscal "
                        "host's timezone (read from templates/fiscal-host.yml: "
                        "psl_foundation.timezone).")
    p.add_argument("--repo-root", default=".",
                   help="Working tree root (default: current directory). "
                        "Used to locate generated_pdfs/<period>/ for the re-render outputs.")
    p.add_argument("--repo", default=None,
                   help="GitHub repo slug (owner/name) for the footer issue link. "
                        "Defaults to the GITHUB_REPOSITORY env var.")
    p.add_argument("--png-ppi", type=int, default=DEFAULT_PNG_PPI,
                   help=f"PNG resolution in pixels per inch (default: {DEFAULT_PNG_PPI}).")
    args = p.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    templates_dir = Path(args.templates).resolve()
    settings_path = Path(args.settings).resolve()
    submission_path = Path(args.submission).resolve()

    # Resolve approval date.
    if args.approved_date is None:
        args.approved_date = resolve_payer_today(templates_dir / "fiscal-host.yml")

    # Load + stamp + persist.
    with open(submission_path, encoding="utf-8") as f:
        submission = yaml.safe_load(f)

    stamped = stamp_approval(
        submission,
        approver=args.approver,
        approved_date=args.approved_date,
    )

    with open(submission_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            stamped, f,
            default_flow_style=False, sort_keys=False, allow_unicode=True, width=100,
        )
    print(f"Stamped: {submission_path.relative_to(repo_root)}")

    # Re-render PDF + PNG, overwriting the pending versions.
    pdf_path = submission_pdf_path(stamped, repo_root)
    png_path = submission_png_path(stamped, repo_root)

    render_submission_pdf(
        submission_path=submission_path,
        settings_path=settings_path,
        template_dir=templates_dir,
        output_path=pdf_path,
        repo=args.repo,
    )
    render_submission_png(
        submission_path=submission_path,
        settings_path=settings_path,
        template_dir=templates_dir,
        output_path=png_path,
        ppi=args.png_ppi,
        repo=args.repo,
    )
    print(f"Re-rendered: {pdf_path.relative_to(repo_root)}")
    print(f"Re-rendered: {png_path.relative_to(repo_root)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
