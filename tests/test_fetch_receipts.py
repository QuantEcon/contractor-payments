"""Tests for the pure helpers in scripts/fetch_receipts.py.

The network download function is injected into `stage_receipts`, so
everything here runs offline. The real token-authenticated fetch (and the
redirect / auth-header-stripping behaviour) is exercised by the Phase 5
E2E against the private test repo.
"""
from __future__ import annotations

import email.message
import hashlib
import json
import pathlib
import urllib.error
import urllib.request

import pytest

import scripts.fetch_receipts as fr
from scripts.fetch_receipts import (
    _CANONICAL_EXT,
    _DOWNLOAD_CHUNK_BYTES,
    _MAGIC_TYPES,
    MAX_RECEIPT_BYTES,
    ReceiptTooLargeError,
    _read_capped,
    detect_extension,
    download,
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

    def test_extension_allowlist_agrees_with_the_magic_allowlist(self):
        # The two allowlists must not drift: every extension we are willing to
        # read off a filename has to canonicalise to a type we can actually
        # detect, or a name could claim a type the sniffer never produces.
        detected = {ext for _, ext in _MAGIC_TYPES}
        assert set(_CANONICAL_EXT.values()) == detected
        assert detected <= set(_CANONICAL_EXT)


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

    def test_long_extension_capped(self):
        # A hostile name can put the length in the extension instead of the
        # stem; unbounded it would only surface as ENAMETOOLONG at write time.
        out = sanitize_filename("a." + "x" * 5000)
        assert len(out) <= 80
        staged, _ = staged_filename(1, "a." + "x" * 5000, ".pdf")
        assert len(staged) < 255


class TestStagedFilename:
    def test_allowed_extension_kept(self):
        assert staged_filename(1, "hotel-invoice.pdf", ".pdf") == (
            "01-hotel-invoice.pdf", None,
        )

    def test_jpeg_normalised_to_jpg(self):
        assert staged_filename(2, "photo.JPEG", ".jpg") == ("02-photo.jpg", None)

    def test_uuid_asset_gains_magic_extension(self):
        # user-attachments/assets/<uuid> URLs carry no filename.
        name, warning = staged_filename(
            3, "0f1e2d3c-4b5a-6789-abcd-ef0123456789", ".png",
        )
        assert name == "03-0f1e2d3c-4b5a-6789-abcd-ef0123456789.png"
        assert warning is None

    def test_mismatched_extension_appends_magic(self):
        # Name claims .txt but the bytes are a PDF — nothing silently lost.
        # Not a warning: .txt was never a type we'd have believed anyway.
        assert staged_filename(1, "scan.txt", ".pdf") == ("01-scan.txt.pdf", None)

    def test_filename_cannot_override_the_sniffed_type(self):
        # The outward consequence: notify_email derives the attachment MIME
        # type from this suffix, so a PNG named `.pdf` would otherwise reach
        # the fiscal host labelled application/pdf.
        name, warning = staged_filename(1, "invoice.pdf", ".png")
        assert name == "01-invoice.pdf.png"
        assert not name.endswith(".pdf")
        assert warning is not None
        assert "invoice.pdf" in warning and ".png" in warning

    def test_wrong_image_extension_also_corrected(self):
        name, warning = staged_filename(2, "receipt.jpg", ".png")
        assert name == "02-receipt.jpg.png"
        assert warning is not None


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
        manifest, errors, warnings = stage_receipts(receipts, tmp_path, fetch)
        assert errors == []
        assert warnings == []
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
        manifest, errors, _ = stage_receipts(receipts, tmp_path, fetch)
        assert manifest == []
        assert len(errors) == 1
        assert "receipt.docx" in errors[0]
        assert "unsupported" in errors[0]

    def test_download_failure_is_error(self, tmp_path):
        receipts = [{"name": "gone.pdf", "url": "https://github.com/user-attachments/files/3/gone.pdf"}]
        fetch = _fetch_from({receipts[0]["url"]: RuntimeError("HTTP 404")})
        manifest, errors, _ = stage_receipts(receipts, tmp_path, fetch)
        assert manifest == []
        assert len(errors) == 1
        assert "download failed" in errors[0]

    def test_oversize_is_error(self, tmp_path):
        receipts = [{"name": "huge.pdf", "url": "https://github.com/user-attachments/files/4/huge.pdf"}]
        fetch = _fetch_from({receipts[0]["url"]: b"%PDF" + b"\x00" * (MAX_RECEIPT_BYTES + 1)})
        manifest, errors, _ = stage_receipts(receipts, tmp_path, fetch)
        assert manifest == []
        assert len(errors) == 1
        assert "huge.pdf" in errors[0]

    def test_aborted_oversize_download_reports_size_not_network(self, tmp_path):
        # `download` now bails out mid-stream, so the oversize case arrives as
        # an exception; it must still read as a size problem, not a 404.
        receipts = [{"name": "huge.pdf", "url": "https://github.com/user-attachments/files/4/huge.pdf"}]
        fetch = _fetch_from({receipts[0]["url"]: ReceiptTooLargeError()})
        manifest, errors, _ = stage_receipts(receipts, tmp_path, fetch)
        assert manifest == []
        assert "huge.pdf" in errors[0]
        assert "Split or compress the receipt." in errors[0]
        assert "download failed" not in errors[0]

    def test_declared_size_is_reported_when_known(self, tmp_path):
        receipts = [{"name": "huge.pdf", "url": "https://github.com/user-attachments/files/4/huge.pdf"}]
        fetch = _fetch_from({receipts[0]["url"]: ReceiptTooLargeError(41_000_000)})
        _, errors, _ = stage_receipts(receipts, tmp_path, fetch)
        assert "41.0 MB" in errors[0]

    def test_type_mismatch_warns_and_stores_the_sniffed_extension(self, tmp_path):
        # PNG bytes uploaded as `invoice.pdf`: staged as .png (so the email
        # can't mislabel it) and the disagreement is surfaced, not swallowed.
        receipts = [{"name": "invoice.pdf", "url": "https://github.com/user-attachments/assets/x"}]
        fetch = _fetch_from({receipts[0]["url"]: PNG_BYTES})
        manifest, errors, warnings = stage_receipts(receipts, tmp_path, fetch)
        assert errors == []
        assert manifest[0]["filename"] == "01-invoice.pdf.png"
        assert (tmp_path / "01-invoice.pdf.png").read_bytes() == PNG_BYTES
        assert len(warnings) == 1
        assert "invoice.pdf" in warnings[0]

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
        manifest, errors, _ = stage_receipts(receipts, tmp_path, fetch)
        assert len(manifest) == 1
        assert len(errors) == 1


# ─── Download: bounded read, redirects ──────────────────────────────────────

class _FakeResponse:
    """Minimal urlopen stand-in. `body=None` streams endlessly, which is how
    we prove the read stops instead of draining whatever the server sends."""

    def __init__(self, body: bytes | None = b"", *, content_length=None):
        self._body = body
        self._pos = 0
        self.served = 0
        self.headers = {} if content_length is None else {
            "Content-Length": str(content_length)
        }

    def read(self, size: int = -1) -> bytes:
        if self._body is None:          # endless stream
            self.served += size
            return b"\x00" * size
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        self.served += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Replays a script of responses / raised errors, recording each request
    so the tests can inspect the headers and URL of every hop."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _redirect(location: str, code: int = 302) -> urllib.error.HTTPError:
    headers = email.message.Message()
    headers["Location"] = location
    return urllib.error.HTTPError("https://github.com/x", code, "Found", headers, None)


def _install(monkeypatch, opener) -> None:
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: opener)


class TestReadCapped:
    def test_returns_whole_body_under_the_cap(self):
        assert _read_capped(_FakeResponse(PDF_BYTES), limit=1024) == PDF_BYTES

    def test_stops_reading_once_over_the_cap(self):
        # The point of the chunked read: an oversize (here: endless) body is
        # abandoned after one chunk instead of being materialised in full.
        response = _FakeResponse(None)
        with pytest.raises(ReceiptTooLargeError):
            _read_capped(response, limit=1024)
        assert response.served == _DOWNLOAD_CHUNK_BYTES

    def test_declared_content_length_fails_before_transfer(self):
        response = _FakeResponse(None, content_length=MAX_RECEIPT_BYTES + 1)
        with pytest.raises(ReceiptTooLargeError) as exc:
            _read_capped(response)
        assert exc.value.size_bytes == MAX_RECEIPT_BYTES + 1
        assert response.served == 0

    def test_honest_content_length_under_the_cap_is_fine(self):
        response = _FakeResponse(PDF_BYTES, content_length=len(PDF_BYTES))
        assert _read_capped(response, limit=1024) == PDF_BYTES


class TestDownload:
    def test_token_sent_then_dropped_on_redirect(self, monkeypatch):
        opener = _FakeOpener([
            _redirect("https://objects.githubusercontent.com/signed?sig=1"),
            _FakeResponse(PDF_BYTES),
        ])
        _install(monkeypatch, opener)

        assert download("https://github.com/user-attachments/files/1/a.pdf", "t0k") == PDF_BYTES
        assert opener.requests[0].get_header("Authorization") == "token t0k"
        # The signed storage URL must never carry the token (S3 rejects it,
        # and the token has no business leaving github.com).
        assert opener.requests[1].get_header("Authorization") is None

    def test_relative_location_is_resolved_against_the_current_url(self, monkeypatch):
        opener = _FakeOpener([_redirect("/signed/blob"), _FakeResponse(PDF_BYTES)])
        _install(monkeypatch, opener)

        download("https://github.com/user-attachments/files/1/a.pdf", "t0k")
        assert opener.requests[1].full_url == "https://github.com/signed/blob"

    def test_non_http_redirect_is_refused(self, monkeypatch):
        # Manual redirect handling loses urllib's scheme guard; without this
        # check the opener would read a local file and stage it as a receipt.
        opener = _FakeOpener([_redirect("file:///etc/passwd")])
        _install(monkeypatch, opener)

        with pytest.raises(RuntimeError, match="non-HTTP"):
            download("https://github.com/user-attachments/files/1/a.pdf", "t0k")
        assert len(opener.requests) == 1  # never opened the file:// URL

    def test_redirect_chain_is_bounded(self, monkeypatch):
        opener = _FakeOpener([_redirect(f"https://example.org/{i}") for i in range(4)])
        _install(monkeypatch, opener)

        with pytest.raises(RuntimeError, match="too many redirects"):
            download("https://github.com/user-attachments/files/1/a.pdf", "t0k",
                     max_redirects=2)
        assert len(opener.requests) == 3  # initial + max_redirects hops

    def test_non_redirect_http_error_propagates(self, monkeypatch):
        headers = email.message.Message()
        error = urllib.error.HTTPError("https://github.com/x", 404, "Not Found", headers, None)
        _install(monkeypatch, _FakeOpener([error]))

        with pytest.raises(urllib.error.HTTPError):
            download("https://github.com/user-attachments/files/1/a.pdf", "t0k")

    def test_oversize_body_raises_receipt_too_large(self, monkeypatch):
        _install(monkeypatch, _FakeOpener([
            _FakeResponse(None, content_length=MAX_RECEIPT_BYTES * 2),
        ]))
        with pytest.raises(ReceiptTooLargeError):
            download("https://github.com/user-attachments/files/1/a.pdf", "t0k")


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


class TestWarningsReachThePrBodyOnSuccess:
    """Warnings raised while staging used to reach only stderr: the errors JSON
    was written solely on the failure path, so a receipt whose declared type
    contradicted its bytes staged silently and the admin never saw the note.
    """

    def test_warnings_are_recorded_on_the_success_path(self, tmp_path):
        path = tmp_path / "errors.json"
        fr._append_warnings_json(str(path), ["01-a.pdf is actually a PNG"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["errors"] == []
        assert payload["warnings"] == [{"message": "01-a.pdf is actually a PNG"}]

    def test_existing_parse_warnings_are_preserved(self, tmp_path):
        """The workflow points fetch_receipts and parse_issue at the same
        /tmp/errors.json, so overwriting would trade one silent warning for
        another."""
        path = tmp_path / "errors.json"
        path.write_text(json.dumps({
            "ok": True,
            "errors": [],
            "warnings": [{"message": "date outside the claim period"}],
        }), encoding="utf-8")

        fr._append_warnings_json(str(path), ["01-a.pdf is actually a PNG"])

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert [w["message"] for w in payload["warnings"]] == [
            "date outside the claim period",
            "01-a.pdf is actually a PNG",
        ]

    def test_no_warnings_leaves_the_file_untouched(self, tmp_path):
        path = tmp_path / "errors.json"
        fr._append_warnings_json(str(path), [])
        assert not path.exists()

    def test_unreadable_existing_file_does_not_lose_our_warnings(self, tmp_path):
        path = tmp_path / "errors.json"
        path.write_text("{not json", encoding="utf-8")
        fr._append_warnings_json(str(path), ["still reported"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["warnings"] == [{"message": "still reported"}]


# ─── Real files, not crafted headers ────────────────────────────────────────

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "receipts"
_REAL_RECEIPTS = [
    ("receipt-hotel.pdf", ".pdf"),
    ("receipt-taxi.png", ".png"),
    ("receipt-meal.jpg", ".jpg"),
]


class TestRealEncoderOutput:
    """The magic-byte tests above assert against hand-written byte prefixes,
    which cannot catch a detector that works on a synthetic 8-byte header but
    not on what a real encoder emits. These read actual files — see
    tests/fixtures/receipts/README.md, which also documents their second job as
    the drag-into-an-issue assets for the manual E2E.
    """

    def test_fixtures_are_present(self):
        """A glob that quietly matched nothing would make the rest pass."""
        missing = [n for n, _ in _REAL_RECEIPTS if not (_FIXTURES / n).is_file()]
        assert not missing, f"missing receipt fixtures: {missing}"

    @pytest.mark.parametrize("name,expected", _REAL_RECEIPTS)
    def test_detected_from_real_bytes(self, name, expected):
        assert detect_extension((_FIXTURES / name).read_bytes()) == expected

    @pytest.mark.parametrize("name,expected", _REAL_RECEIPTS)
    def test_agreeing_name_is_not_duplicated(self, name, expected):
        """`hotel.pdf` carrying PDF bytes stages as `01-hotel.pdf`, not
        `01-hotel.pdf.pdf`."""
        detected = detect_extension((_FIXTURES / name).read_bytes())
        filename, warning = staged_filename(1, name, detected)
        assert filename == f"01-{name}"
        assert warning is None

    @pytest.mark.parametrize("name,_expected", _REAL_RECEIPTS)
    def test_content_beats_a_lying_filename(self, name, _expected):
        """The rule that keeps a PNG from reaching the fiscal host labelled
        `application/pdf`. Exercised here with real bytes: whatever the file
        actually is, a contradicting name must not decide the stored type."""
        data = (_FIXTURES / name).read_bytes()
        detected = detect_extension(data)
        liar = ".png" if detected != ".png" else ".pdf"
        filename, warning = staged_filename(1, f"scan{liar}", detected)

        assert filename.endswith(detected), (
            f"stored as {filename!r}; the {detected} content must decide the "
            f"extension, not the {liar} in the name"
        )
        assert warning is not None and "bytes decide" in warning

    def test_every_allowed_type_has_a_fixture(self):
        """If a type is added to the allowlist without a sample file, the
        detector's behaviour on real bytes of that type goes untested."""
        covered = {ext for _, ext in _REAL_RECEIPTS}
        canonical = set(_CANONICAL_EXT.values())
        assert canonical <= covered, (
            f"allowlist covers {sorted(canonical)} but fixtures only cover "
            f"{sorted(covered)} — add a sample file for the difference"
        )
