"""Parse a GitHub Issue Form body into a structured submission.

Handles all three submission types:

- **Hourly Timesheet** — `.github/ISSUE_TEMPLATE/hourly-timesheet.yml`,
  with a `Time Entries` section in `YYYY-MM-DD | hours | description` rows.
- **Milestone Invoice** — `.github/ISSUE_TEMPLATE/milestone-invoice.yml`,
  with a `Milestone Entries` section in `ID | YYYY-MM-DD | amount | description`
  rows. Out-of-period dates are allowed for catch-up submissions.
- **Reimbursement Claim** — `.github/ISSUE_TEMPLATE/reimbursement-claim.yml`
  (Phase 5), with an `Expense Entries` section in
  `YYYY-MM-DD | amount | category | description` rows plus `Currency`,
  `Total` (cross-checked against the entry sum), and `Receipts` (attachment
  links) sections. Contractor-level — no Contract field. Duplicate dates are
  allowed; out-of-period dates warn rather than reject.

GitHub renders each form into the issue body as `### Heading\\n\\nValue` blocks.
This module turns that markdown back into a structured submission dict, or
returns a list of line-specific errors that the workflow surfaces back to the
contractor as a comment.

Submission type is auto-detected from which entries section is present
(`Time Entries` → hourly, `Milestone Entries` → milestone_invoice,
`Expense Entries` → reimbursement).

See PLAN.md §4.4 (hourly form), §4.6 (milestone form), §4.7 (reimbursement
form), §4.5 (validation strategy and failure handling), §4.8 (generic
submission YAML shape).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ─── Result types ───────────────────────────────────────────────────────────

@dataclass
class ParseError:
    """A line-specific or general parse error. `line` is 1-based within the
    entries section; None for errors that don't map to a single line."""
    message: str
    line: Optional[int] = None


@dataclass
class ParseWarning:
    """A non-fatal warning. Doesn't block the PR; surfaced to the contractor."""
    message: str


@dataclass
class ParseResult:
    submission: Optional[dict] = None
    errors: list[ParseError] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ─── Section extraction ─────────────────────────────────────────────────────

# GitHub Issue Form rendering: each field becomes "### Label\n\nValue\n\n".
# The section ends at the next "### " heading or end of body.
_SECTION_RE = re.compile(
    r"^###\s+(?P<heading>.+?)\s*\n\n(?P<content>.*?)(?=\n###\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _extract_sections(body: str) -> dict[str, str]:
    """Split a rendered Issue Form body into {heading: stripped_content}."""
    sections: dict[str, str] = {}
    for match in _SECTION_RE.finditer(body):
        heading = match.group("heading").strip()
        content = match.group("content").strip()
        sections[heading] = _strip_code_fence(content)
    return sections


def _strip_code_fence(text: str) -> str:
    """Remove a surrounding ``` code fence, as produced by `render: text`
    textareas. Leaves content unchanged if no fence is present."""
    lines = text.split("\n")
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].startswith("```"):
        return "\n".join(lines[1:-1])
    return text


# ─── Date parsing ───────────────────────────────────────────────────────────

_DATE_DASH_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")     # 2025-01-05
_DATE_SLASH_ISO = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")    # 2025/01/05
_DATE_DASH_DMY = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")     # 05-01-2025


def _parse_date(s: str) -> Optional[date]:
    """Parse a date string in one of the accepted formats. Returns None on
    failure. Accepts YYYY-MM-DD, YYYY/MM/DD, and DD-MM-YYYY (only when
    unambiguous)."""
    s = s.strip()
    for pattern in (_DATE_DASH_ISO, _DATE_SLASH_ISO):
        m = pattern.match(s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
    m = _DATE_DASH_DMY.match(s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # Only accept DD-MM-YYYY when the month is unambiguously 1-12.
        if 1 <= month <= 12:
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


# ─── Hours parsing ──────────────────────────────────────────────────────────

_HOURS_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours)?$",
    re.IGNORECASE,
)


def _parse_hours(s: str) -> Optional[float]:
    """Parse hours, stripping common unit suffixes (h, hr, hrs, hour, hours)."""
    m = _HOURS_RE.match(s.strip())
    return float(m.group(1)) if m else None


# ─── Amount parsing ─────────────────────────────────────────────────────────

_AMOUNT_RE = re.compile(r"^[-+]?(\d{1,3}(?:[,_]\d{3})*|\d+)(?:\.\d+)?$")


def _parse_amount(s: str) -> Optional[float]:
    """Parse a monetary amount. Accepts `77000`, `77,000`, `77_000`, `45.50`.
    Returns None on failure. Currency symbols and codes are not accepted —
    the currency is fixed by the contract."""
    s = s.strip()
    if not _AMOUNT_RE.match(s):
        return None
    cleaned = s.replace(",", "").replace("_", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ─── Year + Month → Period ──────────────────────────────────────────────────

_YEAR_RE = re.compile(r"^(\d{4})$")
# Month dropdown options have the form "01 — January". We only care about
# the leading 1- or 2-digit month number.
_MONTH_RE = re.compile(r"^\s*(\d{1,2})(?:\s*[—–-]\s*\w+)?\s*$")


def _combine_year_month(
    year: str,
    month: str,
) -> tuple[Optional[str], list[ParseError]]:
    """Combine the Year and Month dropdown values into a canonical `YYYY-MM`
    period string. Returns (period_or_none, errors)."""
    errors: list[ParseError] = []

    if not year or year == _NO_RESPONSE:
        errors.append(ParseError("Year field is required."))
        y: Optional[int] = None
    else:
        ym = _YEAR_RE.match(year.strip())
        if not ym:
            errors.append(ParseError(
                f"Year `{year}` is not a 4-digit year. Pick a value from the dropdown."
            ))
            y = None
        else:
            y = int(ym.group(1))

    if not month or month == _NO_RESPONSE:
        errors.append(ParseError("Month field is required."))
        m: Optional[int] = None
    else:
        mm = _MONTH_RE.match(month.strip())
        if not mm:
            errors.append(ParseError(
                f"Month `{month}` is not in the expected `MM — Name` format. "
                f"Pick a value from the dropdown."
            ))
            m = None
        else:
            m = int(mm.group(1))
            if not 1 <= m <= 12:
                errors.append(ParseError(
                    f"Month `{month}` must be between 1 and 12 (got `{m}`)."
                ))
                m = None

    if y is None or m is None:
        return None, errors
    return f"{y:04d}-{m:02d}", errors


# ─── Delimiter detection ────────────────────────────────────────────────────

_DELIMITER_CANDIDATES = ["|", "\t", ","]


def _detect_delimiter(
    text: str,
    *,
    expected_segments: int = 3,
) -> tuple[str, Optional[ParseWarning]]:
    """Pick the delimiter consistently used in the entries text. Prefers `|`;
    falls back to tab or comma with a warning. `expected_segments` is how many
    segments each row should split into (3 for hourly, 4 for milestone)."""
    needed = expected_segments - 1  # delimiter count = segments - 1
    lines = [ln for ln in text.strip().split("\n") if ln.strip()]
    if not lines:
        return "|", None
    for candidate in _DELIMITER_CANDIDATES:
        non_header = [ln for ln in lines if not _looks_like_header(ln, candidate)]
        if non_header and all(ln.count(candidate) >= needed for ln in non_header):
            if candidate == "|":
                return "|", None
            return candidate, ParseWarning(
                f"Entries used `{_label(candidate)}` as the column separator. "
                f"It worked this time — please use `|` next time so the form behaves consistently."
            )
    return "|", None  # default; entry-level errors will explain what went wrong


def _label(delim: str) -> str:
    return "tab" if delim == "\t" else delim


# Column labels that appear in the seeded header rows:
#   `Date | Hours | Description` (hourly)
#   `ID | Date | Amount | Description` (milestone)
#   `Date | Amount | Category | Description` (reimbursement)
# plus a few plausible hand-typed variants. A row is a header only when
# EVERY non-empty cell is one of these labels.
_HEADER_COLUMN_LABELS = {
    "date", "day", "hours", "description", "id", "amount", "category",
}


def _looks_like_header(line: str, delim: str) -> bool:
    """True only for the seeded header row (e.g. `Date | Hours | Description`):
    every non-empty cell must be a known column label. Data rows never qualify
    because their first cell is a date/ID/amount, not a label.

    Replaces the earlier keyword-substring heuristic, which silently skipped
    malformed data rows like `bad-date | 2 | oops` and produced false
    `/validate` successes (PLAN §10, E2E finding 2026-05-19)."""
    cells = [c.strip().lower() for c in line.split(delim)]
    non_empty = [c for c in cells if c]
    if len(non_empty) < 2:
        return False
    return all(c in _HEADER_COLUMN_LABELS for c in non_empty)


# ─── Hourly entries parsing (`Time Entries`) ────────────────────────────────

def _parse_time_entries(
    text: str,
    period: Optional[str],
) -> tuple[list[dict], list[ParseError], list[ParseWarning]]:
    """Parse the hourly Time Entries section. Returns (entries, errors,
    warnings). `period` may be None if Year/Month was missing/malformed; in
    that case we skip the out-of-period check and let the period-level error
    stand on its own."""
    errors: list[ParseError] = []
    warnings: list[ParseWarning] = []

    if not text or not text.strip():
        errors.append(ParseError("Time Entries section is empty — please add at least one row."))
        return [], errors, warnings

    delim, warning = _detect_delimiter(text, expected_segments=3)
    if warning is not None:
        warnings.append(warning)

    period_year_month: Optional[tuple[int, int]] = None
    if period and re.match(r"^\d{4}-\d{2}$", period):
        y, m = period.split("-")
        period_year_month = (int(y), int(m))

    entries: list[dict] = []
    seen_dates: dict[str, int] = {}  # ISO date -> line number it first appeared

    for line_no, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        if _looks_like_header(line, delim):
            continue

        parts = line.split(delim, 2)
        if len(parts) < 3:
            errors.append(ParseError(
                f"expected three fields separated by `{_label(delim)}` "
                f"(`YYYY-MM-DD {_label(delim)} hours {_label(delim)} description`) "
                f"but found {len(parts)}.",
                line=line_no,
            ))
            continue

        date_str, hours_str, desc = parts[0].strip(), parts[1].strip(), parts[2].strip()

        parsed_date = _parse_date(date_str)
        if parsed_date is None:
            errors.append(ParseError(
                f"couldn't read a date from `{date_str}` — please use `YYYY-MM-DD` "
                f"(e.g. `2025-01-05`).",
                line=line_no,
            ))
            continue

        parsed_hours = _parse_hours(hours_str)
        if parsed_hours is None:
            errors.append(ParseError(
                f"couldn't read hours from `{hours_str}` — please use a number "
                f"(e.g. `4.5`).",
                line=line_no,
            ))
            continue

        if parsed_hours <= 0:
            errors.append(ParseError(
                f"hours must be greater than 0 (got `{parsed_hours}`).",
                line=line_no,
            ))
            continue
        if parsed_hours > 24:
            errors.append(ParseError(
                f"hours must not exceed 24 in a single day (got `{parsed_hours}`). "
                f"If you meant to log across multiple days, split into separate rows.",
                line=line_no,
            ))
            continue

        if not desc:
            errors.append(ParseError(
                "description is empty — each row needs a brief description of the work.",
                line=line_no,
            ))
            continue

        date_iso = parsed_date.isoformat()

        if date_iso in seen_dates:
            errors.append(ParseError(
                f"duplicate date `{date_iso}` (also on line {seen_dates[date_iso]}). "
                f"Combine into one row per day.",
                line=line_no,
            ))
            continue

        if period_year_month is not None:
            if (parsed_date.year, parsed_date.month) != period_year_month:
                errors.append(ParseError(
                    f"date `{date_iso}` is outside the selected period `{period}`. "
                    f"Either change the date or pick a different period.",
                    line=line_no,
                ))
                continue

        seen_dates[date_iso] = line_no
        entries.append({
            "date": date_iso,
            "hours": parsed_hours,
            "description": desc,
        })

    if not entries and not errors:
        errors.append(ParseError("No valid time entries found in the Time Entries section."))

    entries.sort(key=lambda e: e["date"])
    return entries, errors, warnings


# ─── Milestone entries parsing (`Milestone Entries`) ────────────────────────

def _parse_milestone_entries(
    text: str,
) -> tuple[list[dict], list[ParseError], list[ParseWarning]]:
    """Parse the Milestone Entries section. Returns (entries, errors, warnings).

    Format: `ID | YYYY-MM-DD | amount | description` per row.
    Out-of-period dates are *allowed without warning* — catch-up submissions
    legitimately reference prior months (§4.6).
    Duplicate IDs within a single submission are rejected.
    """
    errors: list[ParseError] = []
    warnings: list[ParseWarning] = []

    if not text or not text.strip():
        errors.append(ParseError("Milestone Entries section is empty — please add at least one row."))
        return [], errors, warnings

    delim, warning = _detect_delimiter(text, expected_segments=4)
    if warning is not None:
        warnings.append(warning)

    entries: list[dict] = []
    seen_ids: dict[str, int] = {}  # milestone ID -> line number it first appeared

    for line_no, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        if _looks_like_header(line, delim):
            continue

        parts = line.split(delim, 3)
        if len(parts) < 4:
            errors.append(ParseError(
                f"expected four fields separated by `{_label(delim)}` "
                f"(`ID {_label(delim)} YYYY-MM-DD {_label(delim)} amount {_label(delim)} description`) "
                f"but found {len(parts)}.",
                line=line_no,
            ))
            continue

        id_str, date_str, amount_str, desc = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip(),
            parts[3].strip(),
        )

        if not id_str:
            errors.append(ParseError(
                "milestone ID is empty — please provide the milestone number from your contract.",
                line=line_no,
            ))
            continue

        parsed_date = _parse_date(date_str)
        if parsed_date is None:
            errors.append(ParseError(
                f"couldn't read a date from `{date_str}` — please use `YYYY-MM-DD` "
                f"(e.g. `2025-11-15`).",
                line=line_no,
            ))
            continue

        parsed_amount = _parse_amount(amount_str)
        if parsed_amount is None:
            errors.append(ParseError(
                f"couldn't read an amount from `{amount_str}` — please use a number "
                f"(e.g. `77000` or `77,000`).",
                line=line_no,
            ))
            continue

        if parsed_amount <= 0:
            errors.append(ParseError(
                f"amount must be greater than 0 (got `{parsed_amount}`).",
                line=line_no,
            ))
            continue

        if not desc:
            errors.append(ParseError(
                "description is empty — each row needs a brief description of the milestone.",
                line=line_no,
            ))
            continue

        if id_str in seen_ids:
            errors.append(ParseError(
                f"duplicate milestone ID `{id_str}` (also on line {seen_ids[id_str]}). "
                f"Each milestone may only be claimed once per submission.",
                line=line_no,
            ))
            continue

        seen_ids[id_str] = line_no
        entries.append({
            "id": id_str,
            "date": parsed_date.isoformat(),
            "amount": parsed_amount,
            "description": desc,
        })

    if not entries and not errors:
        errors.append(ParseError("No valid milestone entries found in the Milestone Entries section."))

    entries.sort(key=lambda e: e["date"])
    return entries, errors, warnings


# ─── Expense entries parsing (`Expense Entries`, reimbursements) ────────────

def _parse_expense_entries(
    text: str,
    period: Optional[str],
) -> tuple[list[dict], list[ParseError], list[ParseWarning]]:
    """Parse the reimbursement Expense Entries section. Returns (entries,
    errors, warnings).

    Format: `YYYY-MM-DD | amount | category | description` per row.
    Duplicate dates are allowed — flight + hotel on the same day is the
    normal case (contrast with `_parse_time_entries`, which rejects them).
    Out-of-period dates produce a *warning*, not an error — trips legitimately
    span month boundaries; the admin decides at PR review (§4.7).
    Category membership is checked later in `parse_issue` against the repo's
    `config/reimbursements.yml` allowed list.
    """
    errors: list[ParseError] = []
    warnings: list[ParseWarning] = []

    if not text or not text.strip():
        errors.append(ParseError("Expense Entries section is empty — please add at least one row."))
        return [], errors, warnings

    delim, warning = _detect_delimiter(text, expected_segments=4)
    if warning is not None:
        warnings.append(warning)

    period_year_month: Optional[tuple[int, int]] = None
    if period and re.match(r"^\d{4}-\d{2}$", period):
        y, m = period.split("-")
        period_year_month = (int(y), int(m))

    entries: list[dict] = []

    for line_no, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        if _looks_like_header(line, delim):
            continue

        parts = line.split(delim, 3)
        if len(parts) < 4:
            errors.append(ParseError(
                f"expected four fields separated by `{_label(delim)}` "
                f"(`YYYY-MM-DD {_label(delim)} amount {_label(delim)} category {_label(delim)} description`) "
                f"but found {len(parts)}.",
                line=line_no,
            ))
            continue

        date_str, amount_str, category_str, desc = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip(),
            parts[3].strip(),
        )

        parsed_date = _parse_date(date_str)
        if parsed_date is None:
            errors.append(ParseError(
                f"couldn't read a date from `{date_str}` — please use `YYYY-MM-DD` "
                f"(e.g. `2026-06-03`).",
                line=line_no,
            ))
            continue

        parsed_amount = _parse_amount(amount_str)
        if parsed_amount is None:
            errors.append(ParseError(
                f"couldn't read an amount from `{amount_str}` — please use a number "
                f"(e.g. `184.50` or `12,800`).",
                line=line_no,
            ))
            continue

        if parsed_amount <= 0:
            errors.append(ParseError(
                f"amount must be greater than 0 (got `{parsed_amount}`).",
                line=line_no,
            ))
            continue

        if not category_str:
            errors.append(ParseError(
                "category is empty — each row needs a category from your repo's allowed list.",
                line=line_no,
            ))
            continue

        if not desc:
            errors.append(ParseError(
                "description is empty — each row needs a brief description of the expense.",
                line=line_no,
            ))
            continue

        date_iso = parsed_date.isoformat()

        if period_year_month is not None:
            if (parsed_date.year, parsed_date.month) != period_year_month:
                warnings.append(ParseWarning(
                    f"line {line_no}: date `{date_iso}` is outside the claim period "
                    f"`{period}` — allowed for trips spanning a month boundary; "
                    f"the admin will review."
                ))

        entries.append({
            "date": date_iso,
            "amount": parsed_amount,
            "category": category_str.lower(),
            "description": desc,
        })

    if not entries and not errors:
        errors.append(ParseError("No valid expense entries found in the Expense Entries section."))

    entries.sort(key=lambda e: e["date"])
    return entries, errors, warnings


# ─── Receipt link extraction (`Receipts`, reimbursements) ───────────────────

# Markdown image `![name](url)` or link `[name](url)` as GitHub leaves them
# in the body after a drag-and-drop upload.
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)\)")
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")

# GitHub-hosted attachment URL shapes (current `user-attachments` and the
# legacy per-repo `/files/` + image-CDN forms).
_ATTACHMENT_URL_RE = re.compile(
    r"^https://(?:"
    r"github\.com/user-attachments/(?:assets|files)/"
    r"|github\.com/[^/]+/[^/]+/files/\d+/"
    r"|(?:private-)?user-images\.githubusercontent\.com/"
    r")"
)


def _extract_receipt_links(
    text: str,
) -> tuple[list[dict], list[ParseError], list[ParseWarning]]:
    """Extract receipt attachment links from the Receipts section.

    Returns (receipts, errors, warnings) where each receipt is
    `{"name": <link text or URL tail>, "url": <attachment URL>}`, deduplicated
    by URL preserving order. GitHub-hosted attachment URLs only; anything else
    is skipped with a warning. Zero attachments is an error — receipts are the
    point of a reimbursement claim.
    """
    errors: list[ParseError] = []
    warnings: list[ParseWarning] = []
    receipts: list[dict] = []
    seen_urls: set[str] = set()
    named: dict[str, str] = {}

    if text.strip() in ("", _NO_RESPONSE):
        errors.append(ParseError(
            "Receipts section has no attachments — drag and drop your receipt "
            "files (PDF, PNG, JPG) into the Receipts box."
        ))
        return receipts, errors, warnings

    for m in _MD_LINK_RE.finditer(text):
        name, url = m.group(1).strip(), m.group(2).strip()
        if name:
            named.setdefault(url, name)  # first name wins, matching URL dedupe

    for url in _URL_RE.findall(text):
        url = url.rstrip(".,;")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        if _ATTACHMENT_URL_RE.match(url):
            name = named.get(url) or url.rstrip("/").rsplit("/", 1)[-1]
            receipts.append({"name": name, "url": url})
        else:
            warnings.append(ParseWarning(
                f"Receipts: skipped `{url}` — not a GitHub attachment. Drag the "
                f"file into the Receipts box rather than pasting an external link."
            ))

    if not receipts and not errors:
        errors.append(ParseError(
            "Receipts section has no attachments — drag and drop your receipt "
            "files (PDF, PNG, JPG) into the Receipts box."
        ))

    return receipts, errors, warnings


def cross_check_milestone_ids(
    submission: dict, contract: dict,
) -> list[ParseWarning]:
    """Non-blocking cross-check: each submitted milestone `id` should appear
    in the contract's `milestones[]` list (PLAN §4.2 + §4.6).

    Returns a list of warnings — one per entry whose ID isn't in the
    contract's schedule. Engine stays permissive: the admin sees the
    warning in the PR body alongside parse warnings and decides.

    No-op for non-milestone submissions, or when the contract has no
    `milestones[]` field (legacy contracts pre-dating the structured
    schema; admin verifies against `contract.notes` in that case).
    """
    if submission.get("type") != "milestone_invoice":
        return []
    contract_milestones = contract.get("milestones") or []
    if not contract_milestones:
        return []
    # Normalise IDs to strings so 1 (yaml int) and "1" (form string) match.
    known_ids = {str(m["id"]) for m in contract_milestones if "id" in m}
    warnings: list[ParseWarning] = []
    for entry in submission.get("entries", []):
        entry_id = str(entry.get("id", ""))
        if entry_id and entry_id not in known_ids:
            warnings.append(ParseWarning(
                f"milestone ID `{entry_id}` is not in contract "
                f"`{contract.get('contract_id', '?')}`'s schedule. "
                f"Known IDs: {sorted(known_ids, key=lambda s: (len(s), s))}. "
                f"Admin: please verify this row during PR review."
            ))
    return warnings


# ─── Top-level parse ────────────────────────────────────────────────────────

# Field labels as they appear in the rendered issue body. The IDs in the form
# YAML are different (e.g. `entries`) but GitHub renders by label.
_LABEL_CONTRACT = "Contract"
_LABEL_YEAR = "Year"
_LABEL_MONTH = "Month"
_LABEL_TIME_ENTRIES = "Time Entries"
_LABEL_MILESTONE_ENTRIES = "Milestone Entries"
_LABEL_EXPENSE_ENTRIES = "Expense Entries"
_LABEL_CURRENCY = "Currency"
_LABEL_TOTAL = "Total"
_LABEL_TRIP_CONTEXT = "Trip / project context (optional)"
_LABEL_RECEIPTS = "Receipts"
_LABEL_NOTES = "Additional notes (optional)"
_LABEL_CONFIRMATION = "Confirmation"

_NO_RESPONSE = "_No response_"

# Mirrors the v1 supported list in contracts and the form's Currency dropdown.
# Re-validated here because the issue body is editable after creation.
_SUPPORTED_CURRENCIES = {"AUD", "USD", "JPY"}


def _fmt_num(v: float) -> str:
    """Render a float without a trailing `.0` for whole numbers (3300, 184.5)."""
    return f"{v:g}"


def parse_issue(
    body: str,
    *,
    allowed_categories: Optional[list[str]] = None,
) -> ParseResult:
    """Parse a rendered GitHub Issue Form body into a structured submission.

    Auto-detects submission type from which entries section is present:
    - `Time Entries` present → `type: timesheet` (hourly)
    - `Milestone Entries` present → `type: milestone_invoice`
    - `Expense Entries` present → `type: reimbursement`

    `allowed_categories` is the reimbursement category allowlist from the
    repo's `config/reimbursements.yml`; None skips the membership check
    (non-reimbursement repos, and callers that pre-date the option).

    Returns a ParseResult. On success, `result.submission` is a dict ready to
    be written as a submission YAML; `result.errors` is empty. On failure,
    `result.errors` lists the problems with line numbers where applicable and
    `result.submission` is None.
    """
    result = ParseResult()
    sections = _extract_sections(body)

    contract = sections.get(_LABEL_CONTRACT, "").strip()
    year = sections.get(_LABEL_YEAR, "").strip()
    month = sections.get(_LABEL_MONTH, "").strip()
    notes = sections.get(_LABEL_NOTES, "").strip()
    confirmation = sections.get(_LABEL_CONFIRMATION, "")

    # Submission type — auto-detect from which entries section is present.
    present = [
        label for label in
        (_LABEL_TIME_ENTRIES, _LABEL_MILESTONE_ENTRIES, _LABEL_EXPENSE_ENTRIES)
        if label in sections
    ]

    if len(present) > 1:
        result.errors.append(ParseError(
            f"Multiple entries sections found in the issue body "
            f"({', '.join(f'`{p}`' for p in present)}). "
            f"An issue must use exactly one submission template."
        ))
        return result
    if not present:
        result.errors.append(ParseError(
            "No entries section found. Expected `Time Entries` (Hourly Timesheet), "
            "`Milestone Entries` (Milestone Invoice), or `Expense Entries` "
            "(Reimbursement Claim)."
        ))
        return result

    submission_type = {
        _LABEL_TIME_ENTRIES: "timesheet",
        _LABEL_MILESTONE_ENTRIES: "milestone_invoice",
        _LABEL_EXPENSE_ENTRIES: "reimbursement",
    }[present[0]]

    # Contract — required for contract-backed types only. Reimbursements are
    # contractor-level (§4.7); their form has no Contract field.
    if submission_type != "reimbursement":
        if not contract or contract == _NO_RESPONSE:
            result.errors.append(ParseError("Contract field is required."))

    # Year + Month → Period
    valid_period, period_errors = _combine_year_month(year, month)
    result.errors.extend(period_errors)

    # Confirmation
    if "- [x]" not in confirmation.lower():
        result.errors.append(ParseError(
            "Confirmation checkbox must be ticked before submission."
        ))

    # Entries — always parse so the contractor sees all problems at once.
    if submission_type == "timesheet":
        entries_text = sections.get(_LABEL_TIME_ENTRIES, "")
        entries, entry_errors, entry_warnings = _parse_time_entries(entries_text, valid_period)
    elif submission_type == "milestone_invoice":
        entries_text = sections.get(_LABEL_MILESTONE_ENTRIES, "")
        entries, entry_errors, entry_warnings = _parse_milestone_entries(entries_text)
    else:
        entries_text = sections.get(_LABEL_EXPENSE_ENTRIES, "")
        entries, entry_errors, entry_warnings = _parse_expense_entries(entries_text, valid_period)
    result.errors.extend(entry_errors)
    result.warnings.extend(entry_warnings)

    if submission_type == "reimbursement":
        # Currency — dropdown-constrained in the form, but the body is editable.
        currency = sections.get(_LABEL_CURRENCY, "").strip().upper()
        if not currency or currency == _NO_RESPONSE.upper():
            result.errors.append(ParseError("Currency field is required."))
            currency = None
        elif currency not in _SUPPORTED_CURRENCIES:
            result.errors.append(ParseError(
                f"Currency `{currency}` is not supported. "
                f"Supported: {', '.join(sorted(_SUPPORTED_CURRENCIES))}."
            ))
            currency = None

        # Stated total must match the sum of line items (sanity cross-check).
        total_text = sections.get(_LABEL_TOTAL, "").strip()
        if not total_text or total_text == _NO_RESPONSE:
            result.errors.append(ParseError("Total field is required."))
        else:
            stated_total = _parse_amount(total_text)
            if stated_total is None:
                result.errors.append(ParseError(
                    f"couldn't read an amount from Total `{total_text}` — "
                    f"please use a number (e.g. `184.50` or `12,800`)."
                ))
            elif entries and not entry_errors:
                computed = round(sum(e["amount"] for e in entries), 2)
                if round(stated_total, 2) != computed:
                    result.errors.append(ParseError(
                        f"Total `{_fmt_num(stated_total)}` doesn't match the sum of "
                        f"the expense entries `{_fmt_num(computed)}` — please "
                        f"correct the Total or the line items."
                    ))

        # Category allowlist (config/reimbursements.yml), case-insensitive.
        if allowed_categories:
            allowed = {c.strip().lower() for c in allowed_categories}
            for entry in entries:
                if entry["category"] not in allowed:
                    result.errors.append(ParseError(
                        f"category `{entry['category']}` (entry dated {entry['date']}) "
                        f"is not in this repo's allowed list: "
                        f"{', '.join(sorted(allowed))}."
                    ))

        receipts, receipt_errors, receipt_warnings = _extract_receipt_links(
            sections.get(_LABEL_RECEIPTS, "")
        )
        result.errors.extend(receipt_errors)
        result.warnings.extend(receipt_warnings)

    if result.errors:
        return result

    if submission_type == "timesheet":
        total_hours = round(sum(e["hours"] for e in entries), 2)
        result.submission = {
            "type": "timesheet",
            "contract_id": contract,
            "period": valid_period,
            "entries": entries,
            "totals": {"hours": total_hours},
            "notes": "" if notes in ("", _NO_RESPONSE) else notes,
            "status": "pending",
        }
    elif submission_type == "milestone_invoice":
        total_amount = round(sum(e["amount"] for e in entries), 2)
        result.submission = {
            "type": "milestone_invoice",
            "contract_id": contract,
            "period": valid_period,
            "entries": entries,
            "totals": {"amount": total_amount},
            "notes": "" if notes in ("", _NO_RESPONSE) else notes,
            "status": "pending",
        }
    else:
        trip_context = sections.get(_LABEL_TRIP_CONTEXT, "").strip()
        total_amount = round(sum(e["amount"] for e in entries), 2)
        result.submission = {
            "type": "reimbursement",
            "period": valid_period,
            "entries": entries,
            "totals": {"amount": total_amount, "currency": currency},
            "trip_context": "" if trip_context in ("", _NO_RESPONSE) else trip_context,
            "receipts": receipts,
            "notes": "" if notes in ("", _NO_RESPONSE) else notes,
            "status": "pending",
        }
    return result


# ─── CLI entry point ────────────────────────────────────────────────────────

def _format_error(err: ParseError) -> str:
    prefix = f"Line {err.line}: " if err.line is not None else ""
    return f"{prefix}{err.message}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse a GitHub Issue Form body into a structured submission."
    )
    parser.add_argument(
        "--body-file",
        help="File containing the issue body. Defaults to $ISSUE_BODY env var, then stdin.",
    )
    parser.add_argument(
        "--output-json",
        help="On success, write the submission dict here as JSON.",
    )
    parser.add_argument(
        "--output-errors-json",
        help="Write a JSON report of errors and warnings here regardless of outcome.",
    )
    parser.add_argument(
        "--reimbursements",
        help="Path to the repo's config/reimbursements.yml. Optional; when the "
        "file exists, its allowed_categories list is enforced for "
        "reimbursement submissions. Missing file = check skipped (repos "
        "without reimbursements enabled).",
    )
    args = parser.parse_args(argv)

    if args.body_file:
        with open(args.body_file, encoding="utf-8") as f:
            body = f.read()
    elif "ISSUE_BODY" in os.environ:
        body = os.environ["ISSUE_BODY"]
    else:
        body = sys.stdin.read()

    allowed_categories: Optional[list[str]] = None
    if args.reimbursements and os.path.exists(args.reimbursements):
        import yaml  # deferred: only the CLI path needs it

        with open(args.reimbursements, encoding="utf-8") as f:
            reimbursements_config = yaml.safe_load(f) or {}
        allowed_categories = reimbursements_config.get("allowed_categories")

    result = parse_issue(body, allowed_categories=allowed_categories)

    if args.output_errors_json:
        with open(args.output_errors_json, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ok": result.ok,
                    "errors": [
                        {"line": e.line, "message": e.message} for e in result.errors
                    ],
                    "warnings": [{"message": w.message} for w in result.warnings],
                },
                f,
                indent=2,
            )

    if result.ok and args.output_json and result.submission is not None:
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(result.submission, f, indent=2)

    if not result.ok:
        for err in result.errors:
            print(f"ERROR: {_format_error(err)}", file=sys.stderr)
        return 1

    for w in result.warnings:
        print(f"WARNING: {w.message}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
