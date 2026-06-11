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
    cross_check_milestone_ids,
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
        assert any(
            "Multiple entries sections" in m and "Milestone Entries" in m
            for m in error_messages(result)
        )

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


# ─── Milestone ID cross-check against contract schedule (§4.6) ──────────────

class TestCrossCheckMilestoneIds:
    """Non-blocking cross-check that submitted milestone IDs appear in the
    contract's structured `milestones[]` schedule. Engine stays permissive:
    unknown IDs surface as warnings on the PR body, not parse errors.
    """

    def _contract(self, milestones=None, contract_id="QE-FOO-2025-001"):
        c = {"contract_id": contract_id}
        if milestones is not None:
            c["milestones"] = milestones
        return c

    def test_hourly_submission_skipped(self):
        # Wrong submission type → function is a no-op, even if the contract
        # has a milestones[] list. Hourly timesheets don't carry milestone IDs.
        submission = {"type": "timesheet", "entries": [{"id": "99"}]}
        contract = self._contract(milestones=[{"id": 1}])
        assert cross_check_milestone_ids(submission, contract) == []

    def test_all_ids_known_no_warning(self):
        submission = {
            "type": "milestone_invoice",
            "entries": [{"id": "1"}, {"id": "2"}],
        }
        contract = self._contract(milestones=[
            {"id": 1, "description": "kick-off"},
            {"id": 2, "description": "draft"},
            {"id": 3, "description": "final"},
        ])
        assert cross_check_milestone_ids(submission, contract) == []

    def test_one_unknown_id_warns_once(self):
        submission = {
            "type": "milestone_invoice",
            "entries": [{"id": "1"}, {"id": "9"}],
        }
        contract = self._contract(milestones=[{"id": 1}, {"id": 2}])
        warnings = cross_check_milestone_ids(submission, contract)
        assert len(warnings) == 1
        assert "9" in warnings[0].message
        assert "QE-FOO-2025-001" in warnings[0].message

    def test_multiple_unknown_ids_warn_per_entry(self):
        submission = {
            "type": "milestone_invoice",
            "entries": [{"id": "7"}, {"id": "8"}, {"id": "1"}],
        }
        contract = self._contract(milestones=[{"id": 1}, {"id": 2}])
        warnings = cross_check_milestone_ids(submission, contract)
        assert len(warnings) == 2
        messages = " | ".join(w.message for w in warnings)
        assert "7" in messages and "8" in messages

    def test_legacy_contract_without_milestones_field_no_warning(self):
        # Pre-structured-schema contracts: parser stays silent and the admin
        # verifies the row against contract.notes during PR review.
        submission = {
            "type": "milestone_invoice",
            "entries": [{"id": "1"}, {"id": "42"}],
        }
        contract = self._contract(milestones=None)
        assert cross_check_milestone_ids(submission, contract) == []
        # Empty list behaves the same as missing field.
        contract_empty = self._contract(milestones=[])
        assert cross_check_milestone_ids(submission, contract_empty) == []

    def test_id_type_coercion_int_vs_string(self):
        # YAML loads bare `1` as int; the form submission always carries
        # strings. Both shapes should match without warning.
        submission = {
            "type": "milestone_invoice",
            "entries": [{"id": "1"}],
        }
        contract = self._contract(milestones=[{"id": 1}])
        assert cross_check_milestone_ids(submission, contract) == []


# ─── Header heuristic tightened (PLAN §10 fix, 2026-06-11) ──────────────────

class TestHeaderHeuristicTightened:
    """The old heuristic skipped any row whose first cell contained a keyword
    substring (`date`, `id`, ...), silently dropping malformed data rows and
    producing false `/validate` successes. A header row is now only one where
    EVERY non-empty cell is a known column label.
    """

    def test_bad_date_row_now_errors_instead_of_silent_skip(self):
        # The E2E finding from 2026-05-19: previously skipped as a "header".
        body = build_body(entries=(
            "bad-date | 2 | oops\n"
            "2025-01-06 | 3.5 | Real work"
        ))
        result = parse_issue(body)
        assert not result.ok
        assert any("bad-date" in m for m in error_messages(result))

    def test_id_like_first_cell_no_longer_silently_dropped(self):
        # "identify" contains "id" — the old heuristic dropped this row as a
        # "header". Milestone IDs are free-form strings, so the correct
        # behaviour is to keep it as a data row (the milestone-ID cross-check
        # warns later if it isn't in the contract schedule).
        body = build_milestone_body(entries=(
            "identify | 2025-11-15 | 77000 | work\n"
            "3 | 2025-11-15 | 77000 | Monthly Payment — November"
        ))
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert [e["id"] for e in result.submission["entries"]] == ["identify", "3"]

    def test_seeded_headers_still_skipped(self):
        hourly = build_body(entries=(
            "Date | Hours | Description\n"
            "2025-01-06 | 3.5 | NumPy review"
        ))
        milestone = build_milestone_body(entries=(
            "ID | Date | Amount | Description\n"
            "3 | 2025-11-15 | 77000 | Monthly Payment — November"
        ))
        for body in (hourly, milestone):
            result = parse_issue(body)
            assert result.ok, error_messages(result)
            assert len(result.submission["entries"]) == 1


# ─── Reimbursement Claim parser (§4.7, Phase 5) ─────────────────────────────

_RECEIPT_PNG = "https://github.com/user-attachments/assets/0f1e2d3c-4b5a-6789-abcd-ef0123456789"
_RECEIPT_PDF = "https://github.com/user-attachments/files/12345678/hotel-invoice.pdf"
_DEFAULT_RECEIPTS = (
    f"![taxi-receipt.png]({_RECEIPT_PNG})\n"
    f"[hotel-invoice.pdf]({_RECEIPT_PDF})"
)


def build_reimbursement_body(
    *,
    year: str = "2026",
    month: str = "06 — June",
    entries: str = "2026-06-03 | 184.50 | travel | Taxi airport to hotel",
    currency: str = "JPY",
    total: str = "184.50",
    trip_context: str = "_No response_",
    receipts: str = _DEFAULT_RECEIPTS,
    confirmation_checked: bool = True,
) -> str:
    """Build a body that mimics GitHub's Issue Form rendering for the
    Reimbursement Claim template. No Contract field (contractor-level);
    the Receipts textarea is unfenced (no `render: text`) so drag-and-drop
    attachments preview in the issue."""
    checkbox = "- [X]" if confirmation_checked else "- [ ]"
    return (
        f"### Year\n\n{year}\n\n"
        f"### Month\n\n{month}\n\n"
        f"### Expense Entries\n\n```\n{entries}\n```\n\n"
        f"### Currency\n\n{currency}\n\n"
        f"### Total\n\n{total}\n\n"
        f"### Trip / project context (optional)\n\n{trip_context}\n\n"
        f"### Receipts\n\n{receipts}\n\n"
        f"### Confirmation\n\n{checkbox} I confirm these expenses were incurred by me for QuantEcon work.\n"
    )


class TestReimbursementHappyPath:
    def test_basic_claim_parses(self):
        result = parse_issue(build_reimbursement_body())
        assert result.ok, error_messages(result)
        sub = result.submission
        assert sub["type"] == "reimbursement"
        assert "contract_id" not in sub
        assert sub["period"] == "2026-06"
        assert sub["status"] == "pending"
        assert sub["totals"] == {"amount": 184.50, "currency": "JPY"}
        assert sub["entries"] == [{
            "date": "2026-06-03",
            "amount": 184.50,
            "category": "travel",
            "description": "Taxi airport to hotel",
        }]
        assert sub["trip_context"] == ""
        assert sub["receipts"] == [
            {"name": "taxi-receipt.png", "url": _RECEIPT_PNG},
            {"name": "hotel-invoice.pdf", "url": _RECEIPT_PDF},
        ]

    def test_multiple_entries_summed_and_sorted(self):
        body = build_reimbursement_body(
            entries=(
                "2026-06-05 | 300 | meals | Team dinner\n"
                "2026-06-03 | 184.5 | travel | Taxi\n"
                "2026-06-04 | 12,000 | accommodation | Hotel one night"
            ),
            total="12,484.50",
        )
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        dates = [e["date"] for e in result.submission["entries"]]
        assert dates == ["2026-06-03", "2026-06-04", "2026-06-05"]
        assert result.submission["totals"]["amount"] == 12484.50

    def test_duplicate_dates_allowed(self):
        # Flight + hotel on the same day is the normal case — contrast with
        # timesheets, which reject duplicate dates.
        body = build_reimbursement_body(
            entries=(
                "2026-06-03 | 500 | travel | Flight\n"
                "2026-06-03 | 200 | accommodation | Hotel"
            ),
            total="700",
        )
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert len(result.submission["entries"]) == 2

    def test_seeded_header_row_skipped(self):
        body = build_reimbursement_body(
            entries=(
                "Date | Amount | Category | Description\n"
                "2026-06-03 | 184.50 | travel | Taxi"
            ),
        )
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert len(result.submission["entries"]) == 1

    def test_receipt_url_shapes_and_dedupe(self):
        legacy = "https://github.com/QuantEcon/contractor-x/files/999/old.pdf"
        bare = "https://github.com/user-attachments/assets/aaaabbbb-cccc-dddd-eeee-ffff00001111"
        body = build_reimbursement_body(receipts=(
            f"![a.png]({_RECEIPT_PNG})\n"
            f"[old.pdf]({legacy})\n"
            f"{bare}\n"
            f"![a-again.png]({_RECEIPT_PNG})"  # duplicate URL — dropped
        ))
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        receipts = result.submission["receipts"]
        assert [r["url"] for r in receipts] == [_RECEIPT_PNG, legacy, bare]
        # Names: link text where present, URL tail for bare URLs.
        assert receipts[0]["name"] == "a.png"
        assert receipts[1]["name"] == "old.pdf"
        assert receipts[2]["name"] == bare.rsplit("/", 1)[-1]

    def test_trip_context_captured(self):
        body = build_reimbursement_body(
            trip_context="PyCon JP — invited talk on QuantEcon lectures."
        )
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert result.submission["trip_context"].startswith("PyCon JP")

    def test_categories_match_case_insensitively(self):
        body = build_reimbursement_body(
            entries="2026-06-03 | 184.50 | TRAVEL | Taxi",
        )
        result = parse_issue(body, allowed_categories=["Travel", "Meals"])
        assert result.ok, error_messages(result)
        assert result.submission["entries"][0]["category"] == "travel"

    def test_allowed_categories_none_skips_check(self):
        body = build_reimbursement_body(
            entries="2026-06-03 | 184.50 | anything-goes | Taxi",
        )
        result = parse_issue(body)  # no allowlist passed
        assert result.ok, error_messages(result)


class TestReimbursementRejects:
    def test_unparseable_date(self):
        body = build_reimbursement_body(entries="not-a-date | 184.50 | travel | Taxi")
        result = parse_issue(body)
        assert not result.ok
        assert any("date" in m.lower() for m in error_messages(result))

    def test_amount_zero_or_negative(self):
        body = build_reimbursement_body(
            entries="2026-06-03 | 0 | travel | Taxi", total="0",
        )
        result = parse_issue(body)
        assert not result.ok
        assert any("greater than 0" in m for m in error_messages(result))

    def test_too_few_fields(self):
        body = build_reimbursement_body(entries="2026-06-03 | 184.50 | Taxi")
        result = parse_issue(body)
        assert not result.ok
        assert any("four fields" in m for m in error_messages(result))

    def test_empty_category(self):
        body = build_reimbursement_body(entries="2026-06-03 | 184.50 |  | Taxi")
        result = parse_issue(body)
        assert not result.ok
        assert any("category is empty" in m for m in error_messages(result))

    def test_total_mismatch_shows_both_numbers(self):
        body = build_reimbursement_body(
            entries="2026-06-03 | 184.50 | travel | Taxi", total="200",
        )
        result = parse_issue(body)
        assert not result.ok
        msg = " | ".join(error_messages(result))
        assert "200" in msg and "184.5" in msg

    def test_total_unparseable(self):
        body = build_reimbursement_body(total="about 200")
        result = parse_issue(body)
        assert not result.ok
        assert any("Total" in m for m in error_messages(result))

    def test_total_missing(self):
        body = build_reimbursement_body(total="_No response_")
        result = parse_issue(body)
        assert not result.ok
        assert any("Total field is required" in m for m in error_messages(result))

    def test_unsupported_currency(self):
        body = build_reimbursement_body(currency="EUR")
        result = parse_issue(body)
        assert not result.ok
        assert any("EUR" in m and "not supported" in m for m in error_messages(result))

    def test_category_not_in_allowed_list(self):
        body = build_reimbursement_body(
            entries="2026-06-03 | 184.50 | bribes | Taxi",
        )
        result = parse_issue(body, allowed_categories=["travel", "meals"])
        assert not result.ok
        msg = " | ".join(error_messages(result))
        assert "bribes" in msg and "travel" in msg

    def test_zero_entries(self):
        body = build_reimbursement_body(entries="", total="0")
        result = parse_issue(body)
        assert not result.ok
        assert any("Expense Entries section is empty" in m for m in error_messages(result))

    def test_no_receipts(self):
        body = build_reimbursement_body(receipts="_No response_")
        result = parse_issue(body)
        assert not result.ok
        assert any("Receipts" in m for m in error_messages(result))

    def test_external_links_only_is_rejected_with_warning(self):
        body = build_reimbursement_body(
            receipts="[receipt](https://www.dropbox.com/s/abc/receipt.pdf)"
        )
        result = parse_issue(body)
        assert not result.ok
        assert any("Receipts" in m for m in error_messages(result))
        assert any("dropbox" in w.message for w in result.warnings)

    def test_confirmation_unchecked(self):
        body = build_reimbursement_body(confirmation_checked=False)
        result = parse_issue(body)
        assert not result.ok
        assert any("Confirmation" in m for m in error_messages(result))


class TestReimbursementWarnings:
    def test_out_of_period_date_warns_not_rejects(self):
        # Trips legitimately span month boundaries (§4.7) — contrast with
        # timesheets, which hard-reject out-of-period dates.
        body = build_reimbursement_body(
            entries=(
                "2026-05-31 | 500 | travel | Flight (departed prior month)\n"
                "2026-06-01 | 200 | accommodation | Hotel"
            ),
            total="700",
        )
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert len(result.submission["entries"]) == 2
        assert any("2026-05-31" in w.message and "outside" in w.message
                   for w in result.warnings)

    def test_external_url_alongside_attachments_warns_but_passes(self):
        body = build_reimbursement_body(receipts=(
            f"![a.png]({_RECEIPT_PNG})\n"
            f"[extra](https://example.com/x.pdf)"
        ))
        result = parse_issue(body)
        assert result.ok, error_messages(result)
        assert len(result.submission["receipts"]) == 1
        assert any("example.com" in w.message for w in result.warnings)


class TestReimbursementTypeDetection:
    def test_expense_section_routes_to_reimbursement(self):
        result = parse_issue(build_reimbursement_body())
        assert result.submission["type"] == "reimbursement"

    def test_expense_plus_time_sections_rejected(self):
        body = (
            "### Year\n\n2026\n\n"
            "### Month\n\n06 — June\n\n"
            "### Time Entries\n\n```\n2026-06-01 | 3 | work\n```\n\n"
            "### Expense Entries\n\n```\n2026-06-03 | 184.50 | travel | Taxi\n```\n\n"
            "### Confirmation\n\n- [X] confirm\n"
        )
        result = parse_issue(body)
        assert not result.ok
        assert any("Multiple entries sections" in m for m in error_messages(result))

    def test_no_contract_field_required(self):
        # The reimbursement form has no Contract field; its absence must not
        # produce a "Contract field is required" error.
        result = parse_issue(build_reimbursement_body())
        assert result.ok, error_messages(result)
        assert not any("Contract" in m for m in error_messages(result))
