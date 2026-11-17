"""Decimal money arithmetic (scripts/money.py) and the call sites that use it.

The regressions pinned here are real values the float code got wrong on the
payment PDF: 1.5 h x 33.33 billed as 49.99 (should be 50.00), 10.5 h x 40.05
as 420.52 (should be 420.53), and quarter hours printed as `7.2`.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.create_submission_pr import enrich_submission, format_currency_amount
from scripts.generate_pdf import _add_display_strings
from scripts.money import (
    amount_for_hours,
    format_hours,
    format_money,
    has_excess_precision,
    hours_for_yaml,
    money_for_yaml,
    sum_decimal,
    to_decimal,
)
from scripts.parse_issue import parse_issue
from scripts.update_ledger import _recompute_totals


class TestToDecimal:
    def test_float_uses_shortest_repr(self):
        assert to_decimal(0.1) == Decimal("0.1")
        assert to_decimal(420.53) == Decimal("420.53")

    def test_int_and_str(self):
        assert to_decimal(77000) == Decimal("77000")
        assert to_decimal("77,000") == Decimal("77000")
        assert to_decimal(" 45.50 ") == Decimal("45.50")

    @pytest.mark.parametrize("bad", [True, None, "abc", float("nan"), float("inf"), [1]])
    def test_rejects_non_numbers(self, bad):
        with pytest.raises(ValueError):
            to_decimal(bad)


class TestHalfUpRounding:
    @pytest.mark.parametrize("hours,rate,expected", [
        (1.5, 33.33, "50.00"),     # float gave 49.99
        (10.5, 40.05, "420.53"),   # float gave 420.52
        (37.25, 45.10, "1679.98"),
        (7.5, 45.1, "338.25"),
    ])
    def test_hours_times_rate(self, hours, rate, expected):
        assert amount_for_hours(hours, rate, "AUD") == Decimal(expected)

    def test_jpy_rounds_to_whole_yen_half_up(self):
        assert amount_for_hours(2.5, 1001, "JPY") == Decimal("2503")  # 2502.5
        assert money_for_yaml(2502.5, "JPY") == 2503
        assert isinstance(money_for_yaml(2502.5, "JPY"), int)

    def test_stored_float_has_exact_repr(self):
        value = money_for_yaml(Decimal("10.5") * Decimal("40.05"), "USD")
        assert repr(value) == "420.53"

    def test_format_currency_amount_is_half_up(self):
        # 0.125 is exact in binary; round() would give 0.12 (half-even).
        assert format_currency_amount(0.125, "AUD") == 0.13


class TestSums:
    def test_no_float_artefacts(self):
        assert sum_decimal([100.10, 100.10, 105.02]) == Decimal("305.22")

    def test_hours_for_yaml(self):
        assert hours_for_yaml(sum_decimal([4.1, 4.2])) == 8.3


class TestFormatting:
    def test_money(self):
        assert format_money(1234.5, "AUD") == "1,234.50"
        assert format_money(163300, "JPY") == "163,300"
        assert format_money(49.995, "USD") == "50.00"

    @pytest.mark.parametrize("hours,expected", [
        (5, "5.0"), (7.5, "7.5"), (7.25, "7.25"), (12.75, "12.75"), (0.1, "0.1"),
    ])
    def test_hours_keep_quarter_hours(self, hours, expected):
        # `f"{7.25:.1f}"` printed `7.2` on the PDF.
        assert format_hours(hours) == expected

    def test_excess_precision(self):
        assert has_excess_precision(77000.5, "JPY")
        assert not has_excess_precision(77000, "JPY")
        assert has_excess_precision(45.555, "AUD")
        assert not has_excess_precision(45.5, "AUD")


# ─── Call sites ─────────────────────────────────────────────────────────────

def _hourly_contract(rate):
    return {
        "contract_id": "QE-PSL-2026-001",
        "type": "hourly",
        "terms": {"hourly_rate": rate, "currency": "AUD", "max_hours_per_month": None},
    }


def test_enrich_submission_bills_half_up():
    parsed = {
        "type": "timesheet",
        "contract_id": "QE-PSL-2026-001",
        "period": "2026-05",
        "entries": [{"date": "2026-05-04", "hours": 1.5, "description": "x"}],
        "totals": {"hours": 1.5},
    }
    out = enrich_submission(
        parsed, _hourly_contract(33.33),
        submitter="jdoe", submission_id="jdoe-timesheet-2026-05",
        issue_number=1, submitted_date="2026-06-01",
    )
    assert out["totals"]["amount"] == 50.0
    assert repr(out["totals"]["amount"]) == "50.0"


def test_ledger_totals_use_ledger_currency():
    ledger = {"type": "hourly", "currency": "JPY", "totals": {}}
    _recompute_totals(ledger, [
        {"hours": 1.25, "amount": 1250, "status": "approved"},
        {"hours": 2.5, "amount": 2500, "status": "approved"},
    ])
    assert ledger["totals"]["amount_to_date"] == 3750
    assert isinstance(ledger["totals"]["amount_to_date"], int)
    assert ledger["totals"]["hours_to_date"] == 3.75


def test_pdf_display_strings_keep_quarter_hours():
    data = _add_display_strings({
        "entries": [{"date": "2026-05-04", "hours": 7.25, "description": "x"}],
        "totals": {"hours": 12.75, "rate": 45.0, "amount": 573.75, "currency": "AUD"},
    })
    assert data["entries"][0]["hours_display"] == "7.25"
    assert data["totals"]["hours_display"] == "12.75"
    assert data["totals"]["amount_amount_display"] == "573.75"


def _timesheet_body(entries: str) -> str:
    return (
        "### Contract\n\nQE-PSL-2026-001\n\n"
        "### Year\n\n2026\n\n"
        "### Month\n\n05\n\n"
        f"### Time Entries\n\n```text\n{entries}\n```\n\n"
        "### Additional notes (optional)\n\n_No response_\n\n"
        "### Confirmation\n\n- [x] I confirm\n"
    )


def test_parser_rejects_hours_beyond_hundredths():
    result = parse_issue(_timesheet_body("2026-05-04 | 1.333 | thing"))
    assert not result.ok
    assert any("at most 2 decimal places" in e.message for e in result.errors)


def test_parser_accepts_quarter_hours_and_sums_exactly():
    result = parse_issue(_timesheet_body(
        "2026-05-04 | 4.1 | a\n2026-05-05 | 4.2 | b\n2026-05-06 | 0.25 | c"
    ))
    assert result.ok, result.errors
    assert result.submission["totals"]["hours"] == 8.55
