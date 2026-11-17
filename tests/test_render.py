"""Render the real Typst templates (skipped when `typst` isn't installed;
CI installs the version the pipeline pins).

The regression: about 20 one-sentence timesheet rows spill the PDF onto a
second page, and Typst refuses to export a multi-page document to a single
PNG. The preview render failed, so `/submit` failed for an ordinary month.
"""
from __future__ import annotations

import re
import shutil
import struct
from pathlib import Path

import pytest
import yaml

from scripts.generate_pdf import render_submission_pdf, render_submission_png

pytestmark = pytest.mark.skipif(
    shutil.which("typst") is None, reason="typst not installed"
)

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
SENTENCE = (
    "Refactored lecture notebooks, reviewed PRs, fixed CI and wrote exercise "
    "solutions for the dynamic programming lectures"
)


def _pdf_pages(path: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", path.read_bytes()))


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def _common(**over):
    base = {
        "contract_id": "QE-PSL-2026-001",
        "contract_start_date": "2026-01-01",
        "contract_end_date": "2026-12-31",
        "project": "CHOW",
        "submitted_date": "2026-06-01",
        "submitted_by": "jdoe",
        "issue_number": 1,
        "notes": "",
        "status": "pending",
        "approved_by": None,
        "approved_date": None,
    }
    base.update(over)
    return base


def timesheet(rows: int, description: str = SENTENCE) -> dict:
    entries = [
        {"date": f"2026-05-{d:02d}", "hours": 7.25, "description": description}
        for d in range(1, rows + 1)
    ]
    hours = 7.25 * rows
    return _common(
        submission_id="jdoe-timesheet-2026-05", type="timesheet", period="2026-05",
        entries=entries,
        totals={"hours": hours, "rate": 45.0, "amount": hours * 45.0,
                "currency": "AUD", "max_hours_per_month": None},
    )


def invoice(rows: int) -> dict:
    entries = [
        {"id": str(i), "date": "2026-05-15", "amount": 77000, "description": SENTENCE}
        for i in range(1, rows + 1)
    ]
    return _common(
        submission_id="jdoe-invoice-2026-05", type="milestone_invoice", period="2026-05",
        entries=entries, totals={"amount": 77000 * rows, "currency": "JPY"},
    )


def _render(tmp_path: Path, submission: dict) -> tuple[Path, Path]:
    sub = tmp_path / "sub.yml"
    settings = tmp_path / "settings.yml"
    sub.write_text(yaml.safe_dump(submission))
    settings.write_text(yaml.safe_dump(
        {"contractor": {"name": "Jane Doe", "github": "jdoe", "email": "j@example.com"}}
    ))
    pdf, png = tmp_path / "out.pdf", tmp_path / "out.png"
    render_submission_pdf(sub, settings, TEMPLATES, pdf, repo="QuantEcon/contractor-jdoe")
    render_submission_png(sub, settings, TEMPLATES, png, ppi=40, repo="QuantEcon/contractor-jdoe")
    return pdf, png


def test_short_month_is_one_page(tmp_path):
    pdf, png = _render(tmp_path, timesheet(5))
    assert _pdf_pages(pdf) == 1
    assert png.exists()


def test_full_month_of_sentences_renders_both_outputs(tmp_path):
    pdf, png = _render(tmp_path, timesheet(31))
    assert _pdf_pages(pdf) >= 2
    width, height = _png_size(png)
    # One tall preview image (taller than A4's 297/210 ratio), not an error.
    assert height / width > 297 / 210


def test_long_invoice_renders_both_outputs(tmp_path):
    pdf, png = _render(tmp_path, invoice(40))
    assert _pdf_pages(pdf) >= 2
    assert png.exists()


def test_approved_long_month_renders(tmp_path):
    sub = timesheet(31)
    sub.update(status="approved", approved_by="mmcky", approved_date="2026-06-03",
               supersedes="jdoe-timesheet-2026-05")
    pdf, png = _render(tmp_path, sub)
    assert pdf.exists() and png.exists()
