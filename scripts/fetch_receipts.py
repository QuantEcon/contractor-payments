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
re-uploads in a supported format). The sniffed type also decides the stored
extension: the bytes are the file, the contractor-supplied filename is only
a label, and downstream (`notify_email`) derives the attachment MIME type
from that extension. Redirects are followed manually so the Authorization
header is NOT forwarded to the signed storage URL GitHub redirects to (S3
rejects requests carrying both auth mechanisms).

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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# GitHub caps issue attachments at 25 MB; anything bigger means the URL
# isn't a normal attachment. Hard error rather than warn — the warn-only
# size posture (PR-body warnings vs Gmail's ~25 MB cap) applies to email
# delivery, not to pulling arbitrary blobs into the repo.
MAX_RECEIPT_BYTES = 30 * 1024 * 1024

# Body is read in bounded chunks (see _read_capped) so an oversize URL is
# abandoned mid-stream instead of after a full read into memory.
_DOWNLOAD_CHUNK_BYTES = 256 * 1024

# Redirect targets are restricted to HTTP(S): following redirects by hand
# means we no longer get urllib's own scheme guard, and the opener would
# happily open `file:///...` and stage a local file as a "receipt".
_ALLOWED_REDIRECT_SCHEMES = ("http", "https")

_MAGIC_TYPES = (
    (b"%PDF", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)

# Filename extensions worth trusting *when they agree with the sniffed
# content*, mapped to the canonical extension `detect_extension` returns
# (`.jpeg` is the only alias). Keeping the mapping explicit — rather than a
# bare set alongside _MAGIC_TYPES — means the two allowlists cannot drift
# apart unnoticed; a test asserts every key canonicalises to a detected type.
_CANONICAL_EXT = {
    ".pdf": ".pdf",
    ".png": ".png",
    ".jpg": ".jpg",
    ".jpeg": ".jpg",
}


class ReceiptTooLargeError(Exception):
    """Body exceeded MAX_RECEIPT_BYTES. Distinct from a download failure so
    `stage_receipts` can report the size problem rather than blaming the
    network. `size_bytes` is the declared size when the server sent a
    Content-Length, else None (a capped read never learns the real size)."""

    def __init__(self, size_bytes: Optional[int] = None):
        self.size_bytes = size_bytes
        super().__init__(
            f"body exceeds the {MAX_RECEIPT_BYTES // (1024 * 1024)} MB cap"
        )


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
        # Bound the extension too, not just the stem: a hostile name like
        # `a.` + 5000 chars would otherwise sail past the stem cap and only
        # blow up as ENAMETOOLONG when the staged file is written.
        name = f"{stem[:60]}.{ext.lower()[:10]}" if stem else ext.lower()[:60]
    else:
        name = name[:60]
    return name or "receipt"


def staged_filename(
    index: int, source_name: str, detected_ext: str,
) -> tuple[str, Optional[str]]:
    """`NN-<sanitized>.<detected_ext>` — the sniffed content type always wins.

    Returns `(filename, warning)`; `warning` is set when the attachment
    filename claimed a supported type the bytes contradict.

    The extension is never taken from the filename, because the filename is
    contractor-supplied and the extension is load-bearing downstream:
    `notify_email` maps it to the attachment's MIME type, so a PNG named
    `.pdf` would reach the fiscal host labelled `application/pdf`. Only the
    human-meaningful basename is preserved. A claimed extension that agrees
    with the content is absorbed (`hotel.pdf` → `01-hotel.pdf`); anything
    else stays in the stem so nothing is silently lost and the disagreement
    is visible in the committed name (`scan.txt` with PDF bytes →
    `01-scan.txt.pdf`; likewise `shot.png` with PDF bytes →
    `01-shot.png.pdf`). A name with no extension at all — the
    `user-attachments/assets/<uuid>` shape — simply gains the detected one.
    """
    base = sanitize_filename(source_name)
    stem, dot, ext = base.rpartition(".")
    claimed = _CANONICAL_EXT.get(f".{ext.lower()}") if dot else None
    warning = None
    if claimed == detected_ext:
        base = stem  # filename agrees with the bytes — don't duplicate it
    elif claimed is not None:
        warning = (
            f"receipt `{source_name}` is named as {claimed} but its content "
            f"is {detected_ext} — stored as {detected_ext} (the file's own "
            f"bytes decide the type, not its name)."
        )
    return f"{index:02d}-{base}{detected_ext}", warning


def _oversize_error(name: str, size_bytes: Optional[int]) -> str:
    """Oversize message, with the measured size when we know it. A capped
    read only knows the body is *at least* over the cap, so the wording has
    to work without an exact figure."""
    measured = (
        f"is {size_bytes / 1_000_000:.1f} MB" if size_bytes is not None
        else f"is over {MAX_RECEIPT_BYTES // (1024 * 1024)} MB"
    )
    return (
        f"receipt `{name}`: file {measured}, larger than GitHub's attachment "
        f"cap — not committing it. Split or compress the receipt."
    )


def stage_receipts(
    receipts: list[dict],
    staging_dir: Path,
    fetch: Callable[[str], bytes],
) -> tuple[list[dict], list[str], list[str]]:
    """Download each receipt `{"name", "url"}` into `staging_dir`.

    Returns (manifest_entries, errors, warnings). Manifest entries are
    `{"filename", "source_url", "bytes", "sha256"}` in input order. Any
    error (download failure, disallowed type, oversize) is collected per
    receipt; on any error the caller should treat the whole fetch as
    failed — partial receipt sets must not reach the PR. Warnings are
    non-blocking (currently: filename/content type disagreements).
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []

    for index, receipt in enumerate(receipts, start=1):
        name = receipt.get("name") or "receipt"
        url = receipt["url"]
        try:
            data = fetch(url)
        except ReceiptTooLargeError as exc:
            errors.append(_oversize_error(name, exc.size_bytes))
            continue
        except Exception as exc:  # urllib raises several flavours
            errors.append(
                f"receipt `{name}`: download failed ({exc}). "
                f"Re-attach the file and `/submit` again."
            )
            continue

        # Belt and braces: `download` aborts an oversize body mid-stream, but
        # `fetch` is injectable and a caller could hand us anything.
        if len(data) > MAX_RECEIPT_BYTES:
            errors.append(_oversize_error(name, len(data)))
            continue

        ext = detect_extension(data)
        if ext is None:
            errors.append(
                f"receipt `{name}`: unsupported file type (not PDF, PNG, or "
                f"JPEG by content). Re-export the receipt in a supported "
                f"format and re-attach it."
            )
            continue

        filename, warning = staged_filename(index, name, ext)
        if warning:
            warnings.append(warning)
        (staging_dir / filename).write_bytes(data)
        manifest.append({
            "filename": filename,
            "source_url": url,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    return manifest, errors, warnings


# ─── Download (network) ─────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable automatic redirects: urllib would forward the Authorization
    header to the redirect target, and the signed storage URL GitHub points
    at rejects requests carrying a second auth mechanism."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _read_capped(response, limit: int = MAX_RECEIPT_BYTES) -> bytes:
    """Read the body, aborting as soon as it is known to exceed `limit`.

    `response.read()` followed by a size check would materialise the whole
    body first — a 40 MiB URL peaks at ~80 MiB (the buffer plus the joined
    result) before being rejected. Reading in chunks keeps the peak at
    `limit` + one chunk, whatever the server sends.
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.strip().isdigit() and int(declared) > limit:
        # Trusted only to fail fast — an honest server saves us the transfer.
        raise ReceiptTooLargeError(int(declared))

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ReceiptTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)


def download(url: str, token: str, *, timeout: int = 60, max_redirects: int = 5) -> bytes:
    """GET an attachment URL with token auth, following redirects manually.

    The Authorization header is dropped on the *first* redirect, whatever
    host the target is on — not just "once redirected off github.com", which
    is what this docstring used to claim. Dropping unconditionally is the
    safer rule and the one to keep: the token must never reach the signed
    storage URL, and "is this still github.com?" is precisely the test an
    open redirect defeats. The consequence to be aware of: a
    github.com→github.com hop would continue unauthenticated and 404 on a
    private-repo attachment. GitHub sends a single hop straight to storage,
    so that stays theoretical — and if it ever changes the fetch fails
    loudly instead of leaking the token.
    """
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
                return _read_capped(response)
        except urllib.error.HTTPError as err:
            if err.code in (301, 302, 303, 307, 308):
                location = err.headers.get("Location")
                if not location:
                    raise
                # Resolve relative Locations against the current URL, and
                # refuse anything that isn't HTTP(S): following redirects by
                # hand loses urllib's scheme guard, and this opener would
                # otherwise read a `file:///` target off the runner.
                current = urllib.parse.urljoin(current, location)
                scheme = urllib.parse.urlsplit(current).scheme.lower()
                if scheme not in _ALLOWED_REDIRECT_SCHEMES:
                    raise RuntimeError(
                        f"refusing redirect to non-HTTP(S) URL `{current}` "
                        f"while fetching {url}"
                    ) from err
                use_auth = False  # signed URL — never forward the token
                continue
            raise
    raise RuntimeError(f"too many redirects fetching {url}")


# ─── CLI ────────────────────────────────────────────────────────────────────

def _write_errors_json(
    path: str, errors: list[str], warnings: Optional[list[str]] = None,
) -> None:
    """Same shape as parse_issue's errors JSON so the workflow's existing
    error-comment step can post fetch failures unchanged."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "ok": False,
                "errors": [{"line": None, "message": m} for m in errors],
                "warnings": [{"message": m} for m in (warnings or [])],
            },
            f,
            indent=2,
        )


def _append_warnings_json(path: str, warnings: list[str]) -> None:
    """Add warnings to the shared errors JSON on the *success* path.

    Without this a warning raised while staging — a receipt whose declared
    extension contradicts its magic bytes, say — only ever reached stderr in a
    workflow log, because the errors JSON was written solely on failure. The
    file is how non-fatal notes reach the PR body (`create_submission_pr
    --errors-file`), which is where the admin actually looks.

    Merges rather than overwrites: the workflow points this at the same
    `/tmp/errors.json` that `parse_issue` already wrote its own warnings to,
    and clobbering those would trade one silent warning class for another.
    """
    if not warnings:
        return
    payload = {"ok": True, "errors": [], "warnings": []}
    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
        if isinstance(existing, dict):
            payload["errors"] = existing.get("errors") or []
            payload["warnings"] = list(existing.get("warnings") or [])
            payload["ok"] = existing.get("ok", True)
    except (OSError, json.JSONDecodeError):
        # No prior file, or unreadable — start clean rather than lose ours.
        pass
    payload["warnings"].extend({"message": m} for m in warnings)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as exc:
        print(f"WARNING: could not record warnings in {path}: {exc}",
              file=sys.stderr)


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

    manifest, errors, warnings = stage_receipts(
        submission.get("receipts", []),
        Path(args.staging_dir),
        fetch=lambda url: download(url, token),
    )

    for message in warnings:
        print(f"WARNING: {message}", file=sys.stderr)

    if errors:
        for message in errors:
            print(f"ERROR: {message}", file=sys.stderr)
        if args.output_errors_json:
            _write_errors_json(args.output_errors_json, errors, warnings)
        return 1

    if args.output_errors_json:
        # Success, but any warnings still need a route to the PR body.
        _append_warnings_json(args.output_errors_json, warnings)

    write_manifest(manifest)
    for entry in manifest:
        print(f"Fetched {entry['filename']} ({entry['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
