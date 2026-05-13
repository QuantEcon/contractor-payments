"""Render a timesheet submission YAML into a PDF via Typst.

The Typst template at templates/timesheet.typ reads from `data.yml` in its
own directory. We set up an isolated working directory containing:

  working/
  ├── timesheet.typ          (copy of the template)
  ├── data.yml               (the submission + contractor data)
  └── assets/
      ├── quantecon-logo.png
      └── psl-foundation-logo.png

then run `typst compile working/timesheet.typ <output.pdf>`. Isolating the
working dir avoids polluting the repo with intermediate files and keeps the
template's `image("assets/...")` calls resolvable.

CLI:
  python -m scripts.generate_pdf \\
      --submission submissions/2026-04/mmcky-timesheet-1.yml \\
      --settings   config/settings.yml \\
      --templates  templates \\
      --output     generated_pdfs/2026-04/mmcky-timesheet-1.pdf
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import yaml


def _merge_contractor(submission: dict, settings: dict) -> dict:
    """Fold the contractor identity from settings.yml into the submission dict.

    The template expects `data.contractor.{name, github, email}` to be present;
    submissions don't carry that themselves because it lives in the repo's
    config/settings.yml.
    """
    out = dict(submission)
    out["contractor"] = settings.get("contractor", {})
    return out


def _add_display_strings(data: dict) -> dict:
    """Add pre-formatted display strings for the template.

    Typst loses trailing zeros on integer-valued floats (5.0 -> "5") and
    has no built-in currency-aware formatter. We do the formatting here
    while we still have Python's nice format specifiers.

    Adds (hourly):
      - entries[].hours_display          e.g. "3.5", "5.0"
      - totals.hours_display             e.g. "12.5"
      - totals.rate_amount_display       e.g. "50.00", "5000"   (no currency)
      - totals.amount_amount_display     e.g. "625.00", "42500" (no currency)

    Adds (milestone invoice):
      - entries[].amount_display         e.g. "77,000", "45.50"
      - totals.amount_amount_display     e.g. "231,000", "925.00"

    The currency code lives in `totals.currency` and is rendered as a
    separate column by the template.
    """
    totals = data.get("totals", {})
    currency = totals.get("currency", "")

    def fmt_money(value: float) -> str:
        if currency.upper() == "JPY":
            return str(int(round(value)))
        return f"{value:,.2f}"

    out = dict(data)

    # Totals
    totals_out = dict(totals)
    if "hours" in totals:
        totals_out["hours_display"] = f"{totals['hours']:.1f}"
    if "rate" in totals:
        totals_out["rate_amount_display"] = fmt_money(totals["rate"])
    if "amount" in totals:
        totals_out["amount_amount_display"] = fmt_money(totals["amount"])
    out["totals"] = totals_out

    # Per-entry display strings — branch on which fields are present.
    entries_out = []
    for e in data.get("entries", []):
        eo = dict(e)
        if "hours" in eo:
            eo["hours_display"] = f"{eo['hours']:.1f}"
        if "amount" in eo:
            eo["amount_display"] = fmt_money(eo["amount"])
        entries_out.append(eo)
    out["entries"] = entries_out

    return out


# Map submission type → template filename in the templates directory.
_TEMPLATE_FOR_TYPE = {
    "timesheet": "timesheet.typ",
    "milestone_invoice": "invoice.typ",
}


def _template_filename_for(data: dict) -> str:
    """Pick the right .typ template based on `data["type"]`. Defaults to the
    hourly timesheet template if the type is missing (backwards-compat with
    earlier Phase 1 fixtures that didn't carry a type field)."""
    submission_type = data.get("type", "timesheet")
    template = _TEMPLATE_FOR_TYPE.get(submission_type)
    if template is None:
        raise ValueError(
            f"Unknown submission type `{submission_type}` — no template registered. "
            f"Known types: {sorted(_TEMPLATE_FOR_TYPE)}."
        )
    return template


def _stage_working_dir(template_dir: Path, data: dict) -> Path:
    """Copy template + assets into a temp dir and write data.yml beside them.

    Picks the template file by `data["type"]`. Returns the path to the staged
    .typ file (which is what `typst compile` is invoked on)."""
    work_dir = Path(tempfile.mkdtemp(prefix="submission-pdf-"))
    template_filename = _template_filename_for(data)
    shutil.copy(template_dir / template_filename, work_dir / template_filename)
    # Assets directory
    assets_src = template_dir / "assets"
    if assets_src.exists():
        shutil.copytree(assets_src, work_dir / "assets")
    # Data
    with open(work_dir / "data.yml", "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return work_dir / template_filename


DEFAULT_PNG_PPI = 200  # Higher than Typst's 144 default for crisp inline preview.


def _load_data(
    submission_path: Path,
    settings_path: Path,
    template_dir: Path,
    repo: Optional[str] = None,
) -> dict:
    """Load submission + settings + branding into the data dict the template wants.

    `repo` (e.g. "QuantEcon/contractor-mmcky") is environmental, not part of
    the submission record — passed in at render time so the template can build
    a link to the issue. Defaults to the `GITHUB_REPOSITORY` env var if unset.
    """
    with open(submission_path, encoding="utf-8") as f:
        submission = yaml.safe_load(f)
    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    branding = _load_branding(template_dir)
    data = _merge_contractor(submission, settings)
    data["branding"] = branding
    data["repo"] = repo or os.environ.get("GITHUB_REPOSITORY") or None
    data = _add_display_strings(data)
    return data


def _load_branding(template_dir: Path) -> dict:
    """Load templates/branding.yml. Returns an empty dict if the file is missing
    (so the template can render with logo-only headers in dev environments)."""
    branding_path = template_dir / "branding.yml"
    if not branding_path.exists():
        return {}
    with open(branding_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _run_typst(staged_typ: Path, output_path: Path, fmt: str, ppi: Optional[int]) -> None:
    """Invoke `typst compile` with the requested format. Raises on failure."""
    work_dir = staged_typ.parent
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["typst", "compile", "--format", fmt]
    if ppi is not None:
        cmd.extend(["--ppi", str(ppi)])
    cmd.extend([str(staged_typ), str(output_path)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)
            raise RuntimeError(
                f"typst compile failed with exit code {result.returncode}. "
                f"Working dir kept for inspection: {work_dir}"
            )
    finally:
        if output_path.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def render_submission_pdf(
    submission_path: Path,
    settings_path: Path,
    template_dir: Path,
    output_path: Path,
    repo: Optional[str] = None,
) -> None:
    """Render `submission_path` into a PDF at `output_path`."""
    data = _load_data(submission_path, settings_path, template_dir, repo=repo)
    staged_typ = _stage_working_dir(template_dir, data)
    _run_typst(staged_typ, output_path, fmt="pdf", ppi=None)


def render_submission_png(
    submission_path: Path,
    settings_path: Path,
    template_dir: Path,
    output_path: Path,
    ppi: int = DEFAULT_PNG_PPI,
    repo: Optional[str] = None,
) -> None:
    """Render `submission_path` into a PNG at `output_path` (single page)."""
    data = _load_data(submission_path, settings_path, template_dir, repo=repo)
    staged_typ = _stage_working_dir(template_dir, data)
    _run_typst(staged_typ, output_path, fmt="png", ppi=ppi)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--submission", required=True, type=Path,
                   help="Path to submission YAML (submissions/<period>/<id>.yml).")
    p.add_argument("--settings", required=True, type=Path,
                   help="Path to config/settings.yml.")
    p.add_argument("--templates", required=True, type=Path,
                   help="Templates directory (contains timesheet.typ + assets/).")
    p.add_argument("--output", required=True, type=Path,
                   help="Output path. Format inferred from extension (.pdf or .png).")
    p.add_argument("--ppi", type=int, default=DEFAULT_PNG_PPI,
                   help=f"PNG resolution in pixels per inch (default: {DEFAULT_PNG_PPI}). Ignored for PDF output.")
    p.add_argument("--repo", default=None,
                   help="GitHub repo slug (owner/name) for the issue link in the footer. "
                        "Defaults to the GITHUB_REPOSITORY env var; omits the link if neither is set.")
    args = p.parse_args(argv)

    suffix = args.output.suffix.lower()
    if suffix == ".pdf":
        render_submission_pdf(
            submission_path=args.submission,
            settings_path=args.settings,
            template_dir=args.templates,
            output_path=args.output,
            repo=args.repo,
        )
    elif suffix == ".png":
        render_submission_png(
            submission_path=args.submission,
            settings_path=args.settings,
            template_dir=args.templates,
            output_path=args.output,
            ppi=args.ppi,
            repo=args.repo,
        )
    else:
        print(f"ERROR: --output must end in .pdf or .png (got {suffix})", file=sys.stderr)
        return 2

    print(f"Rendered: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
