"""The metrics layer: named business measures with written definitions.

Every result carries its value, the resolved period, the basis, the row count
and the method. No source rows means "no data", never zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable

from .common import Ctx
from .contracts import Shown
from .periods import Period


@dataclass
class MetricRow:
    label: str
    value: Decimal
    extra: str = ""


@dataclass
class MetricResult:
    metric: str
    title: str
    period: Period | None
    basis: str
    value: Decimal | None            # None means no data
    unit: str = "money"              # "money" | "count"
    rows: list[MetricRow] = field(default_factory=list)
    row_count: int = 0
    method: str = ""
    as_of: bool = False              # balances are "as of" the period end
    asked_end: date | None = None    # set when an as-of period was capped at today


@dataclass
class Metric:
    name: str
    title: str
    definition: str
    run: Callable[[Ctx, Period, str | None, str | None], MetricResult]
    default_period: str = "this year"
    as_of: bool = False   # a balance: never stated as of a date after today
    synonyms: tuple[str, ...] = ()  # the owner's words for it, for the capability catalogue
    planned: str | None = None  # the release it is planned for; until then it is off (as Contract.planned)


def _v(value: Decimal | None, unit: str, sh: Shown, what: str, of: str | None = None) -> str:
    """A measure's figure as the answer shows it; a money figure is recorded as the answer's amount."""
    if value is None:
        return "no data"
    if unit == "count":
        return str(int(value))
    return f"{sh.amount(value, what, of)} {sh.currency}"


def render(r: MetricResult, sh: Shown) -> tuple[str, list[str]]:
    figures: list[str] = []
    when = ""
    if r.period is not None:
        when = f" as of {r.period.end:%d %b %Y}" if r.as_of else f", {r.period.label}"
        if r.as_of and r.asked_end:
            when += f" (today; the period you asked for runs to {r.asked_end:%d %b %Y})"
    if r.value is None:
        text = f"{r.title}{when}: no data. Nothing in the books matches ({r.basis})."
        return text, figures
    value = _v(r.value, r.unit, sh, "value")
    head = f"{r.title}{when}: {value}"
    figures.append(value)
    lines = [head, f"Basis: {r.basis}."]
    if r.method:
        lines.append(f"Method: {r.method}.")
    if r.rows:
        width = max(len(x.label) for x in r.rows)
        for x in r.rows:
            val = _v(x.value, r.unit, sh, "row", x.label)
            figures.append(val)
            lines.append(f"  {x.label.ljust(width)}  {val}{('  ' + x.extra) if x.extra else ''}")
    return "\n".join(lines), figures
