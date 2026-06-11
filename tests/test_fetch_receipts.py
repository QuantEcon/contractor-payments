"""Tests for the pure helpers in scripts/fetch_receipts.py.

The network download function is injected into `stage_receipts`, so
everything here runs offline. The real token-authenticated fetch (and the
redirect / auth-header-stripping behaviour) is exercised by the Phase 5
E2E against the private test repo.
"""
from __future__ import annotations

import hashlib
import json

from scripts.fetch_receipts import (
    MAX_RECEIPT_BYTES,
    detect_extension,
    sanitize_filename,
    stage_receipts,
    staged_filename,
)


PDF_BYTES = b"%PDF-1.4\n%fake minimal pdf body\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


# ─── Type detection ─────────────────────────────────────────────────────────

class TestDetectExtension:
    def test_pdf(self):
        assert detect_extension(PDF_BYTES) == ".pdf"

    def test_png(self):
        assert detect_extension(PNG_BYTES) == ".png"

    def test_jpeg(self):
        assert detect_extension(JPG_BYTES) == ".jpg"

    def test_disallowed_types_are_none(self):
        # HEIC / zip / text — anything that isn't PDF/PNG/JPEG by content.
        assert detect_extension(b"PK\x03\x04 zip archive") is None
        assert detect_extension(b"plain text receipt") is None
        assert detect_extension(b"") is None


# ─── Filename sanitisation ──────────────────────────────────────────────────

class TestSanitizeFilename:
    def test_spaces_become_dashes(self):
        assert sanitize_filename("hotel invoice june.pdf") == "hotel-invoice-june.pdf"

    def test_path_components_stripped(self):
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("C:\\receipts\\taxi.png") == "taxi.png"

    def test_unicode_normalised_to_ascii(self):
        # NFKD decomposition keeps the base letters; the em-dash drops out.
        assert sanitize_filename("hôtel—reçu.PDF") == "hotelrecu.pdf"

    def test_extension_lowercased(self):
        assert sanitize_filename("SCAN.JPG") == "SCAN.jpg"

    def test_empty_falls_back(self):
        assert sanitize_filename("☃☃☃") == "receipt"

    def test_long_stem_capped(self):
        long = "x" * 200 + ".pdf"
        out = sanitize_filename(long)
        assert out.endswith(".pdf")
        assert len(out) <= 64


class TestStagedFilename:
    def test_allowed_extension_kept(self):
        assert staged_filename(1, "hotel-invoice.pdf", ".pdf") == "01-hotel-invoice.pdf"

    def test_jpeg_normalised_to_jpg(self):
        assert staged_filename(2, "photo.JPEG", ".jpg") == "02-photo.jpg"

    def test_uuid_asset_gains_magic_extension(self):
        # user-attachments/assets/<uuid> URLs carry no filename.
        name = staged_filename(3, "0f1e2d3c-4b5a-6789-abcd-ef0123456789", ".png")
        assert name == "03-0f1e2d3c-4b5a-6789-abcd-ef0123456789.png"

    def test_mismatched_extension_appends_magic(self):
        # Name claims .txt but the bytes are a PDF — nothing silently lost.
        assert staged_filename(1, "scan.txt", ".pdf") == "01-scan.txt.pdf"


# ─── Staging ────────────────────────────────────────────────────────────────

def _fetch_from(mapping):
    def fetch(url):
        value = mapping[url]
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


class TestStageReceipts:
    def test_happy_path_manifest_and_files(self, tmp_path):
        receipts = [
            {"name": "taxi-receipt.png", "url": "https://github.com/user-attachments/assets/aaa"},
            {"name": "hotel-invoice.pdf", "url": "https://github.com/user-attachments/files/1/hotel-invoice.pdf"},
        ]
        fetch = _fetch_from({
            receipts[0]["url"]: PNG_BYTES,
            receipts[1]["url"]: PDF_BYTES,
        })
        manifest, errors = stage_receipts(receipts, tmp_path, fetch)
        assert errors == []
        assert [m["filename"] for m in manifest] == [
            "01-taxi-receipt.png",
            "02-hotel-invoice.pdf",
        ]
        assert manifest[0]["source_url"] == receipts[0]["url"]
        assert manifest[0]["bytes"] == len(PNG_BYTES)
        assert manifest[0]["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
        for m in manifest:
            assert (tmp_path / m["filename"]).read_bytes() in (PNG_BYTES, PDF_BYTES)

    def test_disallowed_type_is_error(self, tmp_path):
        receipts = [{"name": "receipt.docx", "url": "https://github.com/user-attachments/files/2/receipt.docx"}]
        fetch = _fetch_from({receipts[0]["url"]: b"PK\x03\x04 not a supported type"})
        manifest, errors = stage_receipts(receipts, tmp_path, fetch)
        assert manifest == []
        assert len(errors) == 1
        assert "receipt.docx" in errors[0]
        assert "unsupported" in errors[0]

    def test_download_failure_is_error(self, tmp_path):
        receipts = [{"name": "gone.pdf", "url": "https://github.com/user-attachments/files/3/gone.pdf"}]
        fetch = _fetch_from({receipts[0]["url"]: RuntimeError("HTTP 404")})
        manifest, errors = stage_receipts(receipts, tmp_path, fetch)
        assert manifest == []
        assert len(errors) == 1
        assert "download failed" in errors[0]

    def test_oversize_is_error(self, tmp_path):
        receipts = [{"name": "huge.pdf", "url": "https://github.com/user-attachments/files/4/huge.pdf"}]
        fetch = _fetch_from({receipts[0]["url"]: b"%PDF" + b"\x00" * (MAX_RECEIPT_BYTES + 1)})
        manifest, errors = stage_receipts(receipts, tmp_path, fetch)
        assert manifest == []
        assert len(errors) == 1
        assert "huge.pdf" in errors[0]

    def test_partial_failure_reports_both(self, tmp_path):
        # One good + one bad: the good file stages, but the error means the
        # caller treats the whole fetch as failed (no partial receipt sets).
        receipts = [
            {"name": "ok.png", "url": "https://github.com/user-attachments/assets/ok"},
            {"name": "bad.bin", "url": "https://github.com/user-attachments/assets/bad"},
        ]
        fetch = _fetch_from({
            receipts[0]["url"]: PNG_BYTES,
            receipts[1]["url"]: b"\x00\x01\x02",
        })
        manifest, errors = stage_receipts(receipts, tmp_path, fetch)
        assert len(manifest) == 1
        assert len(errors) == 1


# ─── CLI no-op for other types ──────────────────────────────────────────────

class TestMainNonReimbursement:
    def test_writes_empty_manifest_and_exits_zero(self, tmp_path):
        from scripts.fetch_receipts import main

        submission_file = tmp_path / "submission.json"
        submission_file.write_text(json.dumps({"type": "timesheet"}))
        manifest_file = tmp_path / "manifest.json"
        rc = main([
            "--submission-file", str(submission_file),
            "--staging-dir", str(tmp_path / "staging"),
            "--output-manifest", str(manifest_file),
        ])
        assert rc == 0
        manifest = json.loads(manifest_file.read_text())
        assert manifest == {"receipts": [], "total_bytes": 0}
