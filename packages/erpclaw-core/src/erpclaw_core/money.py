"""Exact money and quantity handling. Floats are refused everywhere."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MINOR_UNITS = {"USD": 2, "EUR": 2, "GBP": 2, "INR": 2, "JPY": 0, "KWD": 3}


class MoneyError(ValueError):
    pass


def to_decimal(value) -> Decimal:
    """Exact Decimal from str/int/Decimal. A float is a bug, never a value."""
    if isinstance(value, bool):
        raise MoneyError("a true/false value is not an amount")
    if isinstance(value, float):
        raise MoneyError("floating-point amounts are refused; pass the amount as text")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip().replace(",", ""))
        except InvalidOperation:
            raise MoneyError(f"'{value}' is not a number") from None
    else:
        raise MoneyError(f"unsupported amount type {type(value).__name__}")
    if not d.is_finite():
        raise MoneyError("amount must be finite")
    return d


MAX_INTEGER_DIGITS = 15  # amounts below 10^15 units
PLAIN = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")  # plain notation only


def to_amount(value, currency: str, *, allow_negative: bool = False) -> Decimal:
    """A posted amount at the currency's scale: Decimal, int or
    plain decimal text; floats, bools, None, NaN, Infinity, exponents, separators,
    extra non-zero places and 10^15 or more refused. Negative zero becomes zero."""
    if isinstance(value, (bool, float)) or value is None:
        raise MoneyError(f"{type(value).__name__} is not an amount")
    if isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, Decimal):
        d = value
    elif isinstance(value, str):
        if not PLAIN.match(value.strip()):
            raise MoneyError(f"'{value}' is not a plain decimal amount")
        d = Decimal(value.strip())
    else:
        raise MoneyError(f"unsupported amount type {type(value).__name__}")
    if not d.is_finite():
        raise MoneyError("amount must be finite")
    if currency not in MINOR_UNITS:
        raise MoneyError(f"unknown currency {currency}")
    if d < 0 and not allow_negative:
        raise MoneyError("negative amount")
    # Size first: quantizing a value beyond the context's precision raises
    # InvalidOperation rather than a refusal.
    if d != 0 and d.adjusted() >= MAX_INTEGER_DIGITS:
        raise MoneyError("amount too large")
    exp = Decimal(1).scaleb(-MINOR_UNITS[currency])
    if d != d.quantize(exp, rounding=ROUND_HALF_UP):
        raise MoneyError(f"more than {MINOR_UNITS[currency]} decimal places for {currency}")
    q = d.quantize(exp)
    return q.copy_abs() if q == 0 else q


def canonical(amount: Decimal, currency: str) -> str:
    """The one stored spelling: the currency's scale, plain notation, no -0."""
    q = to_decimal(amount).quantize(Decimal(1).scaleb(-MINOR_UNITS[currency]))
    s = format(q, "f")
    return s[1:] if s.startswith("-") and q == 0 else s


def quantize(amount: Decimal, currency: str) -> Decimal:
    places = MINOR_UNITS.get(currency, 2)
    exp = Decimal(1).scaleb(-places)
    return to_decimal(amount).quantize(exp, rounding=ROUND_HALF_UP)


def fmt(amount: Decimal | None, currency: str = "USD") -> str:
    if amount is None:
        return "no data"
    q = quantize(amount, currency)
    places = MINOR_UNITS.get(currency, 2)
    return f"{q:,.{places}f}"


def fmt_rate(rate: Decimal, currency: str = "USD") -> str:
    """A unit rate as stored, never rounded: at least the currency's places, and every place the rate has
    (a rate holds at most four, by the contract input and the database check). An owner approving a card sees
    the rate that will be posted (displaying 0.12 for a stored 0.1234 would hide the posted rate)."""
    d = to_decimal(rate)
    places = max(MINOR_UNITS.get(currency, 2), -d.normalize().as_tuple().exponent)
    return f"{d:,.{places}f}"


def fmt_qty(qty: Decimal) -> str:
    q = to_decimal(qty).normalize()
    s = format(q, "f")
    return s
