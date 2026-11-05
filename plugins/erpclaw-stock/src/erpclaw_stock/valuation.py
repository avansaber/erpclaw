"""Stock valuation, the arithmetic alone (tests/expected_books/CONVENTIONS.md): every figure is an exact Decimal,
and every rounding is half-up at the company currency's scale (0.01 for USD, 1 for JPY, 0.001 for KWD). Nothing here
reads or writes the books."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from erpclaw_core.money import quantize

# Digits every product and quotient below is carried to before its one rounding to the currency. The stock rows hold
# quantities of at most 14 whole digits and 4 places and values below 10^15, so a value times a quantity has at most
# 36 digits: far beyond Decimal's default 28, which would round it first.
PRECISION = 60


def _rounded(a: Decimal, b: Decimal, divisor: Decimal | None, currency: str) -> Decimal:
    """round(a x b / divisor) half-up at the currency's scale, the product and the quotient exact before it."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        exact = a * b if divisor is None else a * b / divisor
        return quantize(exact, currency)


def _exact(*values: object) -> None:
    for v in values:
        if not isinstance(v, Decimal):
            raise TypeError(f"stock figures are exact decimals, never {type(v).__name__}")


def receipt_value(qty: Decimal, rate: Decimal, currency: str) -> Decimal:
    """A receipt's value: qty x rate, rounded. It can round to zero; the caller refuses such a receipt."""
    _exact(qty, rate)
    return _rounded(qty, rate, None, currency)


def moving_average_cost(qty_on_hand: Decimal, value_on_hand: Decimal, qty_out: Decimal, currency: str) -> Decimal:
    """An issue's cost under moving average: round(value x qty out / qty on hand), never a rounded rate times the
    quantity; the last units take the whole remaining value, so the value left never drifts."""
    _exact(qty_on_hand, value_on_hand, qty_out)
    if not 0 < qty_out <= qty_on_hand:
        raise ValueError(f"cannot take {qty_out} of {qty_on_hand}")
    if qty_out == qty_on_hand:
        return value_on_hand
    return _rounded(value_on_hand, qty_out, qty_on_hand, currency)


@dataclass(frozen=True)
class Group:
    """A FIFO layer group as it stands: its id, and the quantity and value it still holds."""
    id: str
    qty: Decimal
    value: Decimal


def fifo_take(groups: list[Group], qty_out: Decimal, currency: str) -> list[tuple[str, Decimal, Decimal]]:
    """An issue under FIFO, from the oldest group first: (group, qty, value) per group taken from. A whole group gives
    its whole remaining value; part of one gives round(remaining value x qty / remaining qty)."""
    _exact(qty_out, *(x for g in groups for x in (g.qty, g.value)))
    if qty_out <= 0:
        raise ValueError(f"cannot take {qty_out}")
    takes, left = [], qty_out
    for g in groups:
        if left == 0:
            break
        if g.qty <= 0:
            continue
        if left >= g.qty:
            takes.append((g.id, g.qty, g.value))
            left -= g.qty
        else:
            takes.append((g.id, left, _rounded(g.value, left, g.qty, currency)))
            left = Decimal(0)
    if left > 0:
        raise ValueError(f"cannot take {qty_out}: {qty_out - left} on hand")
    return takes


def return_cost(issued_qty: Decimal, issued_value: Decimal, returned_qty: Decimal, returned_value: Decimal,
                qty: Decimal, currency: str) -> Decimal:
    """A return's cost against its original issue, allocated cumulatively: with the issue's qty Q and value V, and R
    returned against it so far with this one, round(V x R / Q) less the earlier returns' cost; everything back gives
    V exactly. Never the selling rate, never the current cost."""
    _exact(issued_qty, issued_value, returned_qty, returned_value, qty)
    total = returned_qty + qty
    if not 0 < qty or total > issued_qty:
        raise ValueError(f"cannot return {qty}: {issued_qty - returned_qty} of {issued_qty} left to return")
    cumulative = issued_value if total == issued_qty else _rounded(issued_value, total, issued_qty, currency)
    return cumulative - returned_value
