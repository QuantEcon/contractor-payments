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

    Adds:
      - entries[].hours_display    e.g. "3.5", "5.0"
      - totals.hours_display       e.g. "12.5"
      - totals.rate_display        e.g. "50.00 AUD", "5000 JPY"
      - totals.amount_display      e.g. "625.00 AUD", "42500 JPY"
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
        totals_out["rate_display"] = f"{fmt_money(totals['rate'])} {currency}"
    if "amount" in totals:
        totals_out["amount_display"] = f"{fmt_money(totals['amount'])} {currency}"
    out["totals"] = totals_out

    # Entry hours
    entries_out = []
    for e in data.get("entries", []):
        eo = dict(e)
        if "hours" in eo:
            eo["hours_display"] = f"{eo['hours']:.1f}"
        entries_out.append(eo)
    out["entries"] = entries_out

    return out


def _stage_working_dir(template_dir: Path, data: dict) -> Path:
    """Copy template + assets into a temp dir and write data.yml beside them.

    Returns the path to the staged timesheet.typ file."""
    work_dir = Path(tempfile.mkdtemp(prefix="timesheet-pdf-"))
    # Template file
    shutil.copy(template_dir / "timesheet.typ", work_dir / "timesheet.typ")
    # Assets directory
    assets_src = template_dir / "assets"
    if assets_src.exists():
        shutil.copytree(assets_src, work_dir / "assets")
    # Data
    with open(work_dir / "data.yml", "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return work_dir / "timesheet.typ"


def render_submission_pdf(
    submission_path: Path,
    settings_path: Path,
    template_dir: Path,
    output_path: Path,
) -> None:
    """Render `submission_path` into a PDF at `output_path`.

    Pure-ish: reads files, invokes Typst, writes the PDF. The temp working
    directory is cleaned up after a successful render.
    """
    with open(submission_path, encoding="utf-8") as f:
        submission = yaml.safe_load(f)
    with open(settings_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    data = _merge_contractor(submission, settings)
    data = _add_display_strings(data)

    staged_typ = _stage_working_dir(template_dir, data)
    work_dir = staged_typ.parent

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["typst", "compile", str(staged_typ), str(output_path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stdout)
            sys.stderr.write(result.stderr)
            raise RuntimeError(
                f"typst compile failed with exit code {result.returncode}. "
                f"Working dir kept for inspection: {work_dir}"
            )
    finally:
        # On success we clean up; on failure we keep the dir (see above).
        if output_path.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--submission", required=True, type=Path,
                   help="Path to submission YAML (submissions/<period>/<id>.yml).")
    p.add_argument("--settings", required=True, type=Path,
                   help="Path to config/settings.yml.")
    p.add_argument("--templates", required=True, type=Path,
                   help="Templates directory (contains timesheet.typ + assets/).")
    p.add_argument("--output", required=True, type=Path,
                   help="Output PDF path.")
    args = p.parse_args(argv)

    render_submission_pdf(
        submission_path=args.submission,
        settings_path=args.settings,
        template_dir=args.templates,
        output_path=args.output,
    )
    print(f"Rendered: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
