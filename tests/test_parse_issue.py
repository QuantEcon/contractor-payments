"""Tests for scripts/parse_issue.py.

Organised to mirror the parser tolerances (§4.3) and reject rules (§4.4)
documented in PLAN.md. Each test names the rule it exercises.
"""
from __future__ import annotations

import pytest

from scripts.parse_issue import (
    ParseError,
    ParseResult,
    _detect_delimiter,
    _parse_date,
    _parse_hours,
    parse_issue,
)


# ─── Helpers ────────────────────────────────────────────────────────────────

_MONTH_NAMES = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}


def build_body(
    *,
    contract: str = "jane-doe-hourly-2025",
    year: str = "2025",
    month: str = "01 — January",
    entries: str = "2025-01-06 | 3.5 | NumPy lecture exercises review",
    notes: str = "_No response_",
    confirmation_checked: bool = True,
    period: str | None = None,
) -> str:
    """Build a body that mimics GitHub's Issue Form rendering for the Hourly
    Timesheet template.

    For convenience, `period="YYYY-MM"` can be passed instead of explicit
    `year` + `month` — it splits into the new two-dropdown shape automatically.
    """
    if period is not None:
        # Split a "YYYY-MM" shortcut into the Year/Month dropdown values.
        if "-" in period:
            y, m = period.split("-", 1)
            year = y
            mm = m.zfill(2)
            month = f"{mm} — {_MONTH_NAMES.get(mm, '?')}"
        else:
            # Malformed period — feed it through to Year so the error test sees it.
            year = period
            month = ""
    checkbox = "- [X]" if confirmation_checked else "- [ ]"
    return (
        f"### Contract\n\n"
        f"{contract}\n\n"
        f"### Year\n\n"
        f"{year}\n\n"
        f"### Month\n\n"
        f"{month}\n\n"
        f"### Time Entries\n\n"
        f"```\n{entries}\n```\n\n"
        f"### Additional notes (optional)\n\n"
        f"{notes}\n\n"
        f"### Confirmation\n\n"
        f"{checkbox} I confirm that the hours and descriptions above are accurate.\n"
    )


def build_milestone_body(
    *,
    contract: str = "QE-IUJ-2025-002",
    year: str = "2025",
    month: str = "11 — November",
    entries: str = "3 | 2025-11-15 | 77000 | Monthly Payment — November",
    notes: str = "_No response_",
    confirmation_checked: bool = True,
) -> str:
    """Build a body that mimics GitHub's Issue Form rendering for the
    Milestone Invoice template."""
    checkbox = "- [X]" if confirmation_checked else "- [ ]"
    return (
        f"### Contract\n\n"
        f"{contract}\n\n"
        f"### Year\n\n"
        f"{year}\n\n"
        f"### Month\n\n"
        f"{month}\n\n"
        f"### Milestone Entries\n\n"
        f"```\n{entries}\n```\n\n"
        f"### Additional notes (optional)\n\n"
        f"{notes}\n\n"
        f"### Confirmation\n\n"
        f"{checkbox} I confirm that the milestones listed above have been delivered and the amounts are correct per the contract.\n"
    )


def error_messages(result: ParseResult) -> list[str]:
    return [e.message for e in result.errors]


def error_on_line(result: ParseResult, line: int) -> ParseError | None:
    for e in result.errors:
        if e.line == line:
            return e
    return None


# ─── Happy path ─────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_single_entry_parses(self):
        result = parse_issue(build_body())
        assert result.ok, error_messages(result)
        assert result.submission is not None
        assert result.submission["contract_id"] == "jane-doe-hourly-2025"
        assert result.submission["period"] == "2025-01"
        assert result.submission["type"] == "timesheet"
        assert result.submission["status"] == "pending"
        assert result.submission["totals"]["hours"] == 3.5
        assert result.submission["entries"] == [
            {"date": "2025-01-06", "hours": 3.5, "description": "NumPy lecture exercises review"}
        ]

    def test_multiple_entries_sorted_by_date(self):
        body = build_body(entries=(
            "2025-01-20 | 4.0 | CI pipeline fixes\n"
            "2025-01-06 | 3.5 | NumPy review\n"
            "2025-01-13 | 5.0 | Plotting examples"
        ))
        result = parse_issue(body)
        assert result.ok
        dates = [e["date"] for e in result.submission["entries"]]
        assert dates == ["2025-01-06", "2025-01-13", "2025-01-20"]
        assert result.submission["totals"]["hours"] == 12.5

    def test_notes_captured_when_provided(self):
        body = build_body(notes="Travel time on the 15th not included.")
        result = parse_issue(body)
        assert result.ok
        assert result.submission["notes"] == "Travel time on the 15th not included."

    def test_no_response_notes_become_empty(self):
        result = parse_issue(build_body(notes="_No response_"))
        assert result.ok
        assert result.submission["notes"] == ""


# ─── Parser tolerances (§4.3) ───────────────────────────────────────────────

class TestDateFormats:
    @pytest.mark.parametrize("date_str,expected", [
        ("2025-01-05", "2025-01-05"),
        ("2025/01/05", "2025-01-05"),
        ("05-01-2025", "2025-01-05"),  # DD-MM-YYYY unambiguous
        ("2025-1-5", "2025-01-05"),    # single-digit components
    ])
    def test_accepted_date_formats(self, date_str, expected):
        body = build_body(entries=f"{date_str} | 4.0 | work")
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert result.submission["entries"][0]["date"] == expected

    @pytest.mark.parametrize("bad", [
        "2025/13/01",   # invalid month
        "2025-02-30",   # invalid day for month
        "Jan 5 2025",   # unsupported format
        "5 January",    # missing year
        "garbage",
    ])
    def test_rejected_date_formats(self, bad):
        result = parse_issue(build_body(entries=f"{bad} | 4.0 | work"))
        assert not result.ok
        err = error_on_line(result, 1)
        assert err is not None
        assert "couldn't read a date" in err.message


class TestHoursParsing:
    @pytest.mark.parametrize("hours_str,expected", [
        ("4", 4.0),
        ("4.5", 4.5),
        ("4.5h", 4.5),
        ("4.5hr", 4.5),
        ("4.5 hrs", 4.5),
        ("4.5 hours", 4.5),
        ("4.5 HOUR", 4.5),
    ])
    def test_hour_unit_suffixes_stripped(self, hours_str, expected):
        body = build_body(entries=f"2025-01-06 | {hours_str} | work")
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert result.submission["entries"][0]["hours"] == expected

    def test_non_numeric_hours_rejected(self):
        result = parse_issue(build_body(entries="2025-01-06 | four | work"))
        assert not result.ok
        assert "couldn't read hours" in error_on_line(result, 1).message


class TestDelimiters:
    def test_pipe_delimiter_no_warning(self):
        result = parse_issue(build_body())
        assert result.ok
        assert result.warnings == []

    def test_comma_delimiter_accepted_with_warning(self):
        body = build_body(entries="2025-01-06, 3.5, NumPy review")
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert len(result.warnings) == 1
        assert "`,`" in result.warnings[0].message

    def test_tab_delimiter_accepted_with_warning(self):
        body = build_body(entries="2025-01-06\t3.5\tNumPy review")
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert len(result.warnings) == 1
        assert "tab" in result.warnings[0].message


class TestWhitespaceAndHeaders:
    def test_blank_lines_skipped(self):
        body = build_body(entries=(
            "2025-01-06 | 3.5 | NumPy review\n"
            "\n"
            "\n"
            "2025-01-13 | 5.0 | Plotting examples"
        ))
        result = parse_issue(body)
        assert result.ok
        assert len(result.submission["entries"]) == 2

    def test_header_row_skipped(self):
        body = build_body(entries=(
            "Date | Hours | Description\n"
            "2025-01-06 | 3.5 | NumPy review"
        ))
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert len(result.submission["entries"]) == 1

    def test_surrounding_whitespace_normalised(self):
        body = build_body(entries="  2025-01-06  |  3.5  |  Work with spaces  ")
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        entry = result.submission["entries"][0]
        assert entry["date"] == "2025-01-06"
        assert entry["hours"] == 3.5
        assert entry["description"] == "Work with spaces"


class TestDescriptionsCanContainPipes:
    def test_description_with_pipe_preserved(self):
        body = build_body(entries=(
            "2025-01-06 | 3.5 | Worked on the | character in regex"
        ))
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert result.submission["entries"][0]["description"] == (
            "Worked on the | character in regex"
        )

    def test_description_with_multiple_pipes(self):
        body = build_body(entries=(
            "2025-01-06 | 3.5 | One | two | three"
        ))
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert result.submission["entries"][0]["description"] == "One | two | three"


# ─── Reject rules (§4.3) ────────────────────────────────────────────────────

class TestRejectRules:
    def test_duplicate_date(self):
        body = build_body(entries=(
            "2025-01-06 | 3.5 | first\n"
            "2025-01-06 | 2.0 | second"
        ))
        result = parse_issue(body)
        assert not result.ok
        err = error_on_line(result, 2)
        assert err is not None
        assert "duplicate" in err.message.lower()
        assert "line 1" in err.message

    def test_date_outside_period(self):
        body = build_body(
            period="2025-01",
            entries="2025-02-03 | 3.5 | wrong month",
        )
        result = parse_issue(body)
        assert not result.ok
        err = error_on_line(result, 1)
        assert err is not None
        assert "outside the selected period" in err.message
        assert "2025-01" in err.message

    def test_zero_hours_rejected(self):
        result = parse_issue(build_body(entries="2025-01-06 | 0 | nothing"))
        assert not result.ok
        assert "greater than 0" in error_on_line(result, 1).message

    def test_excessive_hours_rejected(self):
        result = parse_issue(build_body(entries="2025-01-06 | 25 | too much"))
        assert not result.ok
        assert "24" in error_on_line(result, 1).message

    def test_missing_description(self):
        result = parse_issue(build_body(entries="2025-01-06 | 3.5 | "))
        assert not result.ok
        assert "description is empty" in error_on_line(result, 1).message

    def test_missing_fields_too_few_delimiters(self):
        result = parse_issue(build_body(entries="2025-01-06 | 3.5"))
        assert not result.ok
        assert "expected three fields" in error_on_line(result, 1).message

    def test_empty_entries_section(self):
        body = build_body(entries="")
        result = parse_issue(body)
        assert not result.ok
        assert any("empty" in m for m in error_messages(result))


# ─── Top-level field validation ─────────────────────────────────────────────

class TestFieldValidation:
    def test_missing_contract(self):
        body = build_body(contract="_No response_")
        result = parse_issue(body)
        assert not result.ok
        assert any("Contract" in m for m in error_messages(result))

    def test_missing_year(self):
        body = build_body(year="_No response_")
        result = parse_issue(body)
        assert not result.ok
        assert any("Year" in m for m in error_messages(result))

    def test_missing_month(self):
        body = build_body(month="_No response_")
        result = parse_issue(body)
        assert not result.ok
        assert any("Month" in m for m in error_messages(result))

    def test_malformed_year(self):
        body = build_body(year="twenty-five")
        result = parse_issue(body)
        assert not result.ok
        assert any("4-digit year" in m for m in error_messages(result))

    def test_malformed_month(self):
        body = build_body(month="Janvier")
        result = parse_issue(body)
        assert not result.ok
        assert any("Month `Janvier`" in m for m in error_messages(result))

    def test_unchecked_confirmation(self):
        body = build_body(confirmation_checked=False)
        result = parse_issue(body)
        assert not result.ok
        assert any("Confirmation" in m for m in error_messages(result))

    def test_lowercase_x_confirmation_accepted(self):
        body = build_body().replace("[X]", "[x]")
        result = parse_issue(body)
        assert result.ok, error_messages(result)


# ─── Errors are collected, not short-circuited ──────────────────────────────

class TestMultipleErrorsReported:
    def test_field_errors_and_entry_errors_both_surfaced(self):
        # Bad period AND bad entries — both should appear.
        body = build_body(
            period="not-a-period",
            entries=(
                "garbage | 4 | work\n"
                "2025-01-06 | bad | also work"
            ),
        )
        result = parse_issue(body)
        assert not result.ok
        msgs = error_messages(result)
        assert any("YYYY-MM" in m for m in msgs)
        assert any("couldn't read a date" in m for m in msgs)
        assert any("couldn't read hours" in m for m in msgs)


# ─── Unit-level coverage of helpers ─────────────────────────────────────────

class TestHelpers:
    def test_parse_date_iso(self):
        from datetime import date
        assert _parse_date("2025-01-05") == date(2025, 1, 5)

    def test_parse_date_slash(self):
        from datetime import date
        assert _parse_date("2025/01/05") == date(2025, 1, 5)

    def test_parse_date_dmy(self):
        from datetime import date
        assert _parse_date("05-01-2025") == date(2025, 1, 5)

    def test_parse_date_invalid_returns_none(self):
        assert _parse_date("not a date") is None
        assert _parse_date("2025-13-01") is None
        assert _parse_date("2025-02-30") is None

    def test_parse_hours(self):
        assert _parse_hours("4.5") == 4.5
        assert _parse_hours("4.5h") == 4.5
        assert _parse_hours("4.5 hours") == 4.5
        assert _parse_hours("four") is None

    def test_detect_delimiter_pipe(self):
        delim, warning = _detect_delimiter("2025-01-06 | 3.5 | work")
        assert delim == "|"
        assert warning is None

    def test_detect_delimiter_comma(self):
        delim, warning = _detect_delimiter("2025-01-06, 3.5, work")
        assert delim == ","
        assert warning is not None


# ─── Milestone Invoice parser (§4.6) ────────────────────────────────────────

class TestMilestoneHappyPath:
    def test_single_milestone_parses(self):
        result = parse_issue(build_milestone_body())
        assert result.ok, error_messages(result)
        assert result.submission is not None
        assert result.submission["type"] == "milestone_invoice"
        assert result.submission["contract_id"] == "QE-IUJ-2025-002"
        assert result.submission["period"] == "2025-11"
        assert result.submission["totals"]["amount"] == 77000
        assert result.submission["entries"] == [
            {"id": "3", "date": "2025-11-15", "amount": 77000.0,
             "description": "Monthly Payment — November"},
        ]
        assert "hours" not in result.submission["totals"]

    def test_multiple_milestones_catch_up(self):
        body = build_milestone_body(
            month="11 — November",
            entries=(
                "3 | 2025-11-15 | 77000 | Monthly Payment — November\n"
                "2 | 2025-10-15 | 77000 | Monthly Payment — October (catch-up)\n"
                "1 | 2025-09-15 | 77000 | Monthly Payment — September (catch-up)"
            ),
        )
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        # Entries sorted chronologically (matches hourly behaviour).
        ids_in_order = [e["id"] for e in result.submission["entries"]]
        assert ids_in_order == ["1", "2", "3"]
        assert result.submission["totals"]["amount"] == 231000

    def test_out_of_period_date_allowed_for_catch_up(self):
        # Period is November, but milestone date is September (legitimate catch-up).
        body = build_milestone_body(
            month="11 — November",
            entries="1 | 2025-09-15 | 77000 | Late September claim",
        )
        result = parse_issue(body)
        assert result.ok, error_messages(result)

    def test_amount_with_comma_separator(self):
        body = build_milestone_body(
            entries="3 | 2025-11-15 | 77,000 | Monthly Payment",
        )
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert result.submission["totals"]["amount"] == 77000


class TestMilestoneRejectRules:
    def test_duplicate_id_rejected(self):
        body = build_milestone_body(entries=(
            "3 | 2025-11-15 | 77000 | November\n"
            "3 | 2025-12-15 | 77000 | December"
        ))
        result = parse_issue(body)
        assert not result.ok
        assert any("duplicate milestone ID" in m.lower() or "duplicate" in m.lower()
                   for m in error_messages(result))

    def test_negative_amount_rejected(self):
        body = build_milestone_body(entries="3 | 2025-11-15 | -100 | bogus")
        result = parse_issue(body)
        assert not result.ok
        assert any("amount" in m.lower() for m in error_messages(result))

    def test_zero_amount_rejected(self):
        body = build_milestone_body(entries="3 | 2025-11-15 | 0 | nothing")
        result = parse_issue(body)
        assert not result.ok
        assert any("greater than 0" in m for m in error_messages(result))

    def test_empty_id_rejected(self):
        body = build_milestone_body(entries=" | 2025-11-15 | 77000 | description")
        result = parse_issue(body)
        assert not result.ok
        assert any("milestone ID" in m for m in error_messages(result))

    def test_empty_description_rejected(self):
        body = build_milestone_body(entries="3 | 2025-11-15 | 77000 | ")
        result = parse_issue(body)
        assert not result.ok
        assert any("description" in m.lower() for m in error_messages(result))

    def test_missing_fields_rejected(self):
        body = build_milestone_body(entries="3 | 2025-11-15 | 77000")
        result = parse_issue(body)
        assert not result.ok
        assert any("four fields" in m for m in error_messages(result))

    def test_invalid_date_rejected(self):
        body = build_milestone_body(entries="3 | not-a-date | 77000 | description")
        result = parse_issue(body)
        assert not result.ok
        assert any("date" in m.lower() for m in error_messages(result))


class TestMilestoneTypeDetection:
    def test_milestone_section_routes_to_milestone_parser(self):
        result = parse_issue(build_milestone_body())
        assert result.submission["type"] == "milestone_invoice"

    def test_time_section_still_routes_to_hourly_parser(self):
        result = parse_issue(build_body())
        assert result.submission["type"] == "timesheet"

    def test_both_sections_present_is_rejected(self):
        # Synthesise a body with both Time Entries and Milestone Entries.
        body = (
            "### Contract\n\nQE-FOO-2025-001\n\n"
            "### Year\n\n2025\n\n"
            "### Month\n\n11 — November\n\n"
            "### Time Entries\n\n```\n2025-11-01 | 3 | work\n```\n\n"
            "### Milestone Entries\n\n```\n3 | 2025-11-15 | 77000 | work\n```\n\n"
            "### Confirmation\n\n- [X] confirm\n"
        )
        result = parse_issue(body)
        assert not result.ok
        assert any("Both" in m and "Milestone" in m for m in error_messages(result))

    def test_no_entries_section_is_rejected(self):
        body = (
            "### Contract\n\nQE-FOO-2025-001\n\n"
            "### Year\n\n2025\n\n"
            "### Month\n\n11 — November\n\n"
            "### Confirmation\n\n- [X] confirm\n"
        )
        result = parse_issue(body)
        assert not result.ok
        assert any("No entries section" in m for m in error_messages(result))
