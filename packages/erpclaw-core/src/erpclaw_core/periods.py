"""Period phrases resolved by code, never by the model.

Every resolution returns from/to dates and a label that echoes them, so the
owner always sees which dates a figure covers.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from .common import Refusal

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    phrase: str

    @property
    def label(self) -> str:
        return f"{self.start:%d %b %Y} to {self.end:%d %b %Y}"


def _month_end(y: int, mth: int) -> date:
    return date(y, mth, calendar.monthrange(y, mth)[1])


def _add_months(y: int, mth: int, delta: int) -> tuple[int, int]:
    idx = y * 12 + (mth - 1) + delta
    return idx // 12, idx % 12 + 1


def fiscal_year_bounds(fy: int, start_month: int) -> tuple[date, date]:
    """FY N ends in calendar year N (FY 2025 with an April start runs Apr 2024 to Mar 2025)."""
    if start_month == 1:
        return date(fy, 1, 1), date(fy, 12, 31)
    sy, sm = fy - 1, start_month
    ey, em = _add_months(sy, sm, 11)
    return date(sy, sm, 1), _month_end(ey, em)


def fiscal_year_of(d: date, start_month: int) -> int:
    if start_month == 1:
        return d.year
    return d.year + 1 if d.month >= start_month else d.year


def resolve(phrase: str | None, today: date, fy_start_month: int = 1) -> Period:
    raw = (phrase or "").strip()
    p = raw.lower().strip(" .?!")
    p = re.sub(r"\s+", " ", p)
    if p in ("", "all", "all time", "ever", "to date", "as of today", "now", "today", "so far"):
        if p == "today":
            return Period(today, today, raw)
        return Period(date(1900, 1, 1), today, raw or "all time")

    m = re.fullmatch(r"(?:from )?(\d{4}-\d{2}-\d{2}) (?:to|until|-) (\d{4}-\d{2}-\d{2})", p)
    if m:
        a, b = date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        if b < a:
            raise Refusal("The end date is before the start date.", "bad_period")
        return Period(a, b, raw)

    if p in ("this month", "current month", "month to date", "mtd"):
        return Period(date(today.year, today.month, 1), today, raw)
    if p in ("last month", "previous month"):
        y, mth = _add_months(today.year, today.month, -1)
        return Period(date(y, mth, 1), _month_end(y, mth), raw)

    m = re.fullmatch(r"(?:the )?(?:last|past|previous) (\d+|\w+) months?", p)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else WORDS.get(m.group(1))
        if not n or n > 120:
            raise Refusal(f"I can't read '{raw}' as a number of months.", "bad_period")
        y, mth = _add_months(today.year, today.month, -(n - 1))
        return Period(date(y, mth, 1), today, raw)

    if p in ("this year", "year to date", "ytd", "this fiscal year", "this fy", "current year"):
        fy = fiscal_year_of(today, fy_start_month)
        start, _ = fiscal_year_bounds(fy, fy_start_month)
        return Period(start, today, raw)
    if p in ("last year", "previous year", "last fiscal year", "last fy"):
        fy = fiscal_year_of(today, fy_start_month) - 1
        return Period(*fiscal_year_bounds(fy, fy_start_month), raw)

    m = re.fullmatch(r"(?:fy|fiscal year|fiscal|year|calendar year|calendar)? ?'?(\d{4})", p)
    if m:
        fy = int(m.group(1))
        if p.startswith("calendar"):
            return Period(date(fy, 1, 1), date(fy, 12, 31), raw)
        return Period(*fiscal_year_bounds(fy, fy_start_month), raw)

    if p in ("this quarter", "current quarter", "quarter to date", "qtd"):
        q = (today.month - 1) // 3
        return Period(date(today.year, q * 3 + 1, 1), today, raw)
    if p in ("last quarter", "previous quarter"):
        y, mth = _add_months(today.year, ((today.month - 1) // 3) * 3 + 1, -3)
        return Period(date(y, mth, 1), _month_end(*_add_months(y, mth, 2)), raw)
    m = re.fullmatch(r"q([1-4]) (\d{4})|(\d{4}) q([1-4])", p)
    if m:
        q = int(m.group(1) or m.group(4))
        y = int(m.group(2) or m.group(3))
        sm = (q - 1) * 3 + 1
        return Period(date(y, sm, 1), _month_end(*_add_months(y, sm, 2)), raw)

    m = re.fullmatch(r"(?:in )?([a-z]+)(?: (\d{4}))?", p)
    if m and m.group(1) in MONTHS:
        mth = MONTHS[m.group(1)]
        y = int(m.group(2)) if m.group(2) else (today.year if mth <= today.month else today.year - 1)
        return Period(date(y, mth, 1), _month_end(y, mth), raw)

    raise Refusal(
        f"I can't tell which dates '{raw}' means. Try 'this month', 'last 6 months', 'FY 2025', "
        "'Q3 2026', 'October 2026' or 'from 2026-01-01 to 2026-03-31'.", "bad_period")
