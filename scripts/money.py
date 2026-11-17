"""Decimal arithmetic and formatting for money and hours.

Every amount the engine computes, stores or displays goes through here.

Why Decimal: binary floats can't represent most cent values exactly, so
`round(1.5 * 33.33, 2)` gives 49.99 where any accountant (and PSL) would
write 50.00, and `round(10.5 * 40.05, 2)` gives 420.52 instead of 420.53.
Python's `round` is also round-half-even, not the half-up rule used on
invoices. Both errors land on the PDF that triggers payment.

Storage: the committed YAML keeps plain numbers (float for 2-decimal
currencies, int for JPY) so existing contractor repos need no migration.
That is safe because every stored value is first quantized here: a float
built from `Decimal("420.53")` has the shortest repr `420.53`, so it reads
back as exactly that. Floats are converted with `repr`, never `Decimal(x)`
(which would expand 420.53 to 420.529999...).

The rule the rest of the engine follows: never do arithmetic on the float
values read from YAML. Convert with `to_decimal`, compute, then quantize
with `money_for_yaml` / `hours_for_yaml` before writing.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Union

Number = Union[int, float, str, Decimal]

# ISO 4217 currencies with no minor unit. Only JPY is in use today; add others
# here (e.g. KRW) if a contract ever needs one.
_ZERO_DECIMAL_CURRENCIES = frozenset({"JPY"})

_HOURS_QUANTUM = Decimal("0.01")


def minor_units(currency: str) -> int:
    """Decimal places the currency uses: 0 for JPY, 2 otherwise."""
    return 0 if (currency or "").upper() in _ZERO_DECIMAL_CURRENCIES else 2


def _quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-minor_units(currency))


def to_decimal(value: Number) -> Decimal:
    """Exact Decimal for a number read from YAML, JSON or a form.

    Floats go through `repr` so `0.1` becomes `Decimal("0.1")`, not the
    55-digit binary expansion. Strings may carry `,` or `_` thousands
    separators. Raises ValueError for anything that isn't a finite number
    (bools included: YAML `yes` must never become an amount).
    """
    if isinstance(value, bool):
        raise ValueError(f"expected a number, got boolean {value!r}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(repr(value))
    elif isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("_", "")
        try:
            result = Decimal(cleaned)
        except InvalidOperation as e:
            raise ValueError(f"not a number: {value!r}") from e
    else:
        raise ValueError(f"expected a number, got {type(value).__name__} {value!r}")
    if not result.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return result


def sum_decimal(values: Iterable[Number]) -> Decimal:
    """Exact sum of numbers read from YAML."""
    return sum((to_decimal(v) for v in values), Decimal(0))


def quantize_money(value: Number, currency: str) -> Decimal:
    """Round to the currency's minor unit, half-up (invoice rounding)."""
    return to_decimal(value).quantize(_quantum(currency), rounding=ROUND_HALF_UP)


def money_for_yaml(value: Number, currency: str) -> Union[int, float]:
    """Quantized amount as the plain number stored in YAML: int for JPY,
    float (with an exact 2-decimal repr) otherwise."""
    q = quantize_money(value, currency)
    return int(q) if minor_units(currency) == 0 else float(q)


def amount_for_hours(hours: Number, rate: Number, currency: str) -> Decimal:
    """hours x rate, rounded once at the end, half-up."""
    return quantize_money(to_decimal(hours) * to_decimal(rate), currency)


def format_money(value: Number, currency: str) -> str:
    """Display string with thousands separators and the currency's decimals:
    `1,234.50` (AUD/USD), `77,000` (JPY). No currency code."""
    q = quantize_money(value, currency)
    return f"{q:,}" if minor_units(currency) == 0 else f"{q:,.2f}"


def has_excess_precision(value: Number, currency: str) -> bool:
    """True if `value` has more decimal places than the currency allows,
    e.g. `77000.5` JPY or `45.555` AUD. Used to reject amounts that would
    otherwise be silently rounded."""
    d = to_decimal(value)
    return d != d.quantize(_quantum(currency))


def quantize_hours(value: Number) -> Decimal:
    """Hours to two decimal places, half-up."""
    return to_decimal(value).quantize(_HOURS_QUANTUM, rounding=ROUND_HALF_UP)


def hours_for_yaml(value: Number) -> float:
    """Quantized hours as the float stored in YAML."""
    return float(quantize_hours(value))


def format_hours(value: Number) -> str:
    """Hours with one or two decimals: `5.0`, `7.5`, `7.25`.

    The previous `f"{hours:.1f}"` printed quarter hours wrongly (7.25 as
    `7.2`, 12.75 as `12.8`) on the PDF next to an amount computed from the
    real value.
    """
    s = f"{quantize_hours(value):.2f}"
    return s[:-1] if s.endswith("0") else s
