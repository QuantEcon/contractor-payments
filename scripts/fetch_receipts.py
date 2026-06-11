"""Fetch reimbursement receipt attachments into a staging directory.

Phase 5: reimbursement claims carry receipts as GitHub issue attachments
(drag-and-dropped into the form's Receipts textarea). There is no official
API for issue attachments, so this script downloads each attachment URL with
a token-authenticated request — the standard workaround, E2E-verified
against the private test repo (PLAN §8 Phase 5 stage 0b).

Network I/O is isolated here (with an injectable fetch function) so
`create_submission_pr.py` stays free of download concerns and the pure
helpers stay unit-testable. Files are staged to a temp directory and named
`NN-<sanitized-name>.<ext>`; `create_submission_pr.place_receipts` later
copies them to `receipts/{period}/{submission_id}/` once the final
submission ID (with any -B / -vN suffix) is known.

Type policy: receipts must be PDF, PNG, or JPEG — detected from magic
bytes, not the filename. Anything else is a hard error (the contractor
re-uploads in a supported format). Redirects are followed manually so the
Authorization header is NOT forwarded to the signed storage URL GitHub
redirects to (S3 rejects requests carrying both auth mechanisms).

CLI:
    python -m scripts.fetch_receipts \
        --submission-file /tmp/submission.json \
        --staging-dir /tmp/receipts \
        --output-manifest /tmp/receipts_manifest.json \
        [--output-errors-json /tmp/errors.json]

No-op (empty manifest, exit 0) for non-reimbursement submissions, so the
workflow may call it unconditionally.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# GitHub caps issue attachments at 25 MB; anything bigger means the URL
# isn't a normal attachment. Hard error rather than warn — the warn-only
# size posture (PR-body warnings vs Gmail's ~25 MB cap) applies to email
# delivery, not to pulling arbitrary blobs into the repo.
MAX_RECEIPT_BYTES = 30 * 1024 * 1024

_MAGIC_TYPES = (
    (b"%PDF", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)

_ALLOWED_EXTS = {".pdf", ".png", ".jpg", ".jpeg"}


# ─── Pure helpers (testable) ────────────────────────────────────────────────

def detect_extension(data: bytes) -> Optional[str]:
    """Canonical extension from file magic bytes; None for disallowed types."""
    for magic, ext in _MAGIC_TYPES:
        if data.startswith(magic):
            return ext
    return None


def sanitize_filename(name: str) -> str:
    """Make an attachment name safe to commit: ASCII, no path separators or
    spaces, collapsed separator runs, lowercased extension, bounded length.
    Falls back to `receipt` when nothing survives."""
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # drop any path part
    name = name.replace(" ", "-")
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)
    name = re.sub(r"-{2,}", "-", name)
    name = re.sub(r"\.{2,}", ".", name)
    name = name.strip("._-")
    stem, dot, ext = name.rpartition(".")
    if dot:
        name = f"{stem[:60]}.{ext.lower()}" if stem else ext.lower()
    else:
        name = name[:60]
    return name or "receipt"


def staged_filename(index: int, source_name: str, detected_ext: str) -> str:
    """`NN-<sanitized>` with the magic-bytes extension as the source of
    truth: a sanitized name already carrying a matching allowed extension
    keeps it; otherwise the detected extension is appended (covers
    `user-attachments/assets/<uuid>` URLs, which have no filename)."""
    base = sanitize_filename(source_name)
    stem, dot, ext = base.rpartition(".")
    if dot and f".{ext}" in _ALLOWED_EXTS:
        keep = f".{ext}"
        if keep == ".jpeg":
            keep = ".jpg"
        base = stem
    else:
        keep = detected_ext
        if not dot:
            base = base
        # name had a non-allowed extension — keep it as part of the stem
        # so nothing is silently lost (e.g. `scan.heic` → `scan.heic.pdf`
        # never happens: disallowed magic already errored; `scan.txt` with
        # PDF magic → `scan.txt.pdf`).
    return f"{index:02d}-{base}{keep}"


def stage_receipts(
    receipts: list[dict],
    staging_dir: Path,
    fetch: Callable[[str], bytes],
) -> tuple[list[dict], list[str]]:
    """Download each receipt `{"name", "url"}` into `staging_dir`.

    Returns (manifest_entries, errors). Manifest entries are
    `{"filename", "source_url", "bytes", "sha256"}` in input order. Any
    error (download failure, disallowed type, oversize) is collected per
    receipt; on any error the caller should treat the whole fetch as
    failed — partial receipt sets must not reach the PR.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    errors: list[str] = []

    for index, receipt in enumerate(receipts, start=1):
        name = receipt.get("name") or "receipt"
        url = receipt["url"]
        try:
            data = fetch(url)
        except Exception as exc:  # urllib raises several flavours
            errors.append(
                f"receipt `{name}`: download failed ({exc}). "
                f"Re-attach the file and `/submit` again."
            )
            continue

        if len(data) > MAX_RECEIPT_BYTES:
            errors.append(
                f"receipt `{name}`: file is {len(data) / 1_000_000:.1f} MB, "
                f"larger than GitHub's attachment cap — not committing it. "
                f"Split or compress the receipt."
            )
            continue

        ext = detect_extension(data)
        if ext is None:
            errors.append(
                f"receipt `{name}`: unsupported file type (not PDF, PNG, or "
                f"JPEG by content). Re-export the receipt in a supported "
                f"format and re-attach it."
            )
            continue

        filename = staged_filename(index, name, ext)
        (staging_dir / filename).write_bytes(data)
        manifest.append({
            "filename": filename,
            "source_url": url,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    return manifest, errors


# ─── Download (network) ─────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable automatic redirects: urllib would forward the Authorization
    header to the redirect target, and the signed storage URL GitHub points
    at rejects requests carrying a second auth mechanism."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def download(url: str, token: str, *, timeout: int = 60, max_redirects: int = 5) -> bytes:
    """GET an attachment URL with token auth, following redirects manually
    and dropping the Authorization header once redirected off github.com."""
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    use_auth = True
    for _ in range(max_redirects + 1):
        headers = {"User-Agent": "contractor-payments-engine"}
        if use_auth:
            headers["Authorization"] = f"token {token}"
        request = urllib.request.Request(current, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as err:
            if err.code in (301, 302, 303, 307, 308):
                location = err.headers.get("Location")
                if not location:
                    raise
                current = location
                use_auth = False  # signed URL — never forward the token
                continue
            raise
    raise RuntimeError(f"too many redirects fetching {url}")


# ─── CLI ────────────────────────────────────────────────────────────────────

def _write_errors_json(path: str, errors: list[str]) -> None:
    """Same shape as parse_issue's errors JSON so the workflow's existing
    error-comment step can post fetch failures unchanged."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "ok": False,
                "errors": [{"line": None, "message": m} for m in errors],
                "warnings": [],
            },
            f,
            indent=2,
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--submission-file", required=True,
                        help="JSON from parse_issue.py --output-json.")
    parser.add_argument("--staging-dir", required=True,
                        help="Directory to download receipt files into.")
    parser.add_argument("--output-manifest", required=True,
                        help="Where to write the receipts manifest JSON.")
    parser.add_argument("--output-errors-json",
                        help="On failure, write parse_issue-shaped errors JSON "
                             "here for the workflow's error-comment step.")
    args = parser.parse_args(argv)

    with open(args.submission_file, encoding="utf-8") as f:
        submission = json.load(f)

    def write_manifest(entries: list[dict]) -> None:
        with open(args.output_manifest, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "receipts": entries,
                    "total_bytes": sum(e["bytes"] for e in entries),
                },
                f,
                indent=2,
            )

    if submission.get("type") != "reimbursement":
        write_manifest([])
        print("Not a reimbursement submission — no receipts to fetch.")
        return 0

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        message = ("GH_TOKEN / GITHUB_TOKEN is not set — cannot fetch "
                   "private-repo attachments.")
        print(f"ERROR: {message}", file=sys.stderr)
        if args.output_errors_json:
            _write_errors_json(args.output_errors_json, [message])
        return 1

    manifest, errors = stage_receipts(
        submission.get("receipts", []),
        Path(args.staging_dir),
        fetch=lambda url: download(url, token),
    )

    if errors:
        for message in errors:
            print(f"ERROR: {message}", file=sys.stderr)
        if args.output_errors_json:
            _write_errors_json(args.output_errors_json, errors)
        return 1

    write_manifest(manifest)
    for entry in manifest:
        print(f"Fetched {entry['filename']} ({entry['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
