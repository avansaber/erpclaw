"""Resolution dates."""
from __future__ import annotations

import re
from datetime import date, timedelta
from erpclaw_core.common import Refusal
from .resolve_text import _say, table
from .resolve_words import words
from .resolve_amounts import number_words
from .resolve_state import Session


def _unit(ws: list[str], i: int) -> bool:
    """A calendar word used as a unit or a rate, not a date: "two months of retainer", "per day", "150 a day"."""
    t = table()
    if i + 1 < len(ws) and ws[i + 1] in t["unit_before"]:
        return True
    if i and ws[i - 1] in t["unit_after"]:
        return True
    return i >= 2 and ws[i - 1] in t["articles"] and bool(re.fullmatch(r"[\d.,]+", ws[i - 2]))



def _calendar(ws: list[str], i: int) -> bool:
    """A calendar word at position i: "May" and "March" only when qualified or beside a day."""
    t, w = table(), ws[i]
    if w in t["calendar_not"]:
        return (i > 0 and ws[i - 1] in t["month_qualifiers"]) or (i + 1 < len(ws) and ws[i + 1].isdigit())
    return w in t["calendar_words"] or w in t["months"] or w in t["weekdays"]



def _ago() -> str:
    """'<number> days ago' or '<number> weeks ago', the number a whole run of number words or digits."""
    t = table()
    num = "|".join(sorted(set(t["quantity"]) | set(t["scales"]), key=len, reverse=True))
    return rf"\b((?:(?:{num})[- ](?:and[- ])?)*(?:{num})|\d+)\s+(day|week)s?\s+ago\b"



def _touched(ws: list[str], before: int, after: int) -> bool:
    """A word left beside a date reading that belongs to it: a number word (never digits: an amount precedes
    dates), a fraction, a qualifier or a calendar word. Then the reading is not the owner's whole phrase."""
    t = table()
    near = [k for k in (before, after) if 0 <= k < len(ws)]
    numbers = set(t["quantity"]) - t["articles"] | set(t["scales"])
    return any(ws[k] in t["date_touch"] or ws[k] in numbers or _calendar(ws, k) for k in near)



def _touched_text(rest: str, a: int, b: int) -> bool:
    """`_touched` for a reading matched in the text from character a to b: the words beside it, and a hyphen joined to
    it ("twenty-" before "one days ago")."""
    if (a and rest[a - 1] == "-") or (b < len(rest) and rest[b] == "-"):
        return True
    left, right = words(rest[:a])[-1:], words(rest[b:])[:1]
    ws = left + ["\x00"] + right
    return _touched(ws, len(left) - 1 if left else -1, len(left) + 1 if right else len(ws))



def _recent(today: date, month: int, day: int, year: int | None) -> date | None:
    try:
        if year is not None:
            return date(year, month, day)
        d = date(today.year, month, day)
        return d if d <= today else date(today.year - 1, month, day)
    except ValueError:
        return None



def date_phrases(text: str, today: date, date_order: str | None = None) -> list[list[date]]:
    """Each date phrase in the request, as its readings (one when clear, two for an unclear figure date); a phrase the
    engine cannot read as one day raises `date_unknown`. Digits and ISO dates read in any language; words only in a
    shipped table."""
    t, low = table(), text.lower()
    out: list[list[date]] = []
    qualifiers = "|".join(sorted(t["day_qualifiers"], key=len, reverse=True))
    months = "|".join(sorted(t["months"], key=len, reverse=True))
    if re.search(t["vague_dates"], low) or re.search(
            rf"\b(?:{qualifiers})\s+(?:the\s+)?(?:[1-9]|[12]\d|3[01])(?:st|nd|rd|th)\b|"
            rf"\b(?:{qualifiers})\s+(?:early|mid|late)[-\s]+(?:{months})\b", low):
        raise Refusal(_say("date_unknown"), "date_unknown")
    for mt in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", low):
        try:
            out.append([date(int(mt[1]), int(mt[2]), int(mt[3]))])
        except ValueError:
            raise Refusal(_say("date_unknown"), "date_unknown") from None
    clear = None  # the order of the owner's own earlier figure date whose order was clear
    for mt in re.finditer(r"(?<![\w./])(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?(?![\w/])|"
                          r"(?<![\w./])(\d{1,2})\.(\d{1,2})\.(\d{4})(?![\w.])", low):
        a, b, y = (int(mt[1]), int(mt[2]), mt[3]) if mt[1] else (int(mt[4]), int(mt[5]), mt[6])
        year = None if y is None else (int(y) + 2000 if len(y) == 2 else int(y))
        dmy, mdy = _recent(today, b, a, year), _recent(today, a, b, year)
        if a > 12 and dmy:
            out.append([dmy])
            clear = clear or "DMY"
        elif b > 12 and mdy:
            out.append([mdy])
            clear = clear or "MDY"
        elif dmy and mdy:
            order = clear or date_order
            out.append([dmy] if order == "DMY" or dmy == mdy else [mdy] if order == "MDY" else [dmy, mdy])
        else:
            raise Refusal(_say("date_unknown"), "date_unknown")
    rest = low
    for pattern, delta in t["relative_days"]:
        for mt in re.finditer(pattern, rest):
            if _touched_text(rest, mt.start(), mt.end()):
                raise Refusal(_say("date_unknown"), "date_unknown")
            out.append([today + timedelta(days=delta)])
        rest = re.sub(pattern, lambda mt: " " * len(mt.group(0)), rest)
    for mt in re.finditer(t["month_end"], rest):
        if _touched_text(rest, mt.start(), mt.end()):
            raise Refusal(_say("date_unknown"), "date_unknown")
        nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        out.append([nxt - timedelta(days=1)])
    rest = re.sub(t["month_end"], lambda mt: " " * len(mt.group(0)), rest)
    if set(words(rest)) & t["date_vocab"]:
        raise Refusal(_say("date_unknown"), "date_unknown")
    for mt in re.finditer(_ago(), rest):
        n = int(mt[1]) if mt[1].isdigit() else number_words(re.split(r"[- ]+", mt[1]))
        if n is None or _touched_text(rest, mt.start(), mt.end()):
            raise Refusal(_say("date_unknown"), "date_unknown")  # not one number as written, or a part of a phrase
        out.append([today - timedelta(days=int(n) * (7 if mt[2] == "week" else 1))])
    rest = re.sub(_ago(), lambda mt: " " * len(mt.group(0)), rest)
    ws = words(rest)
    read: set[int] = set()
    for i, w in enumerate(ws):
        if w in t["weekdays"] and i + 1 < len(ws) and ws[i + 1] in t["weekday_relative"]:
            k, after = t["weekdays"].index(w), tuple(ws[i + 1:i + 3])
            if after == t["weekday_back"]:
                out.append([today - timedelta(days=((today.weekday() - k) % 7 or 7) + 7)])
            elif after == t["weekday_on"]:
                out.append([today + timedelta(days=((k - today.weekday()) % 7 or 7) + 7)])
            else:
                raise Refusal(_say("date_unknown"), "date_unknown")
            read.update(range(i, i + 3))
            continue
        if w in t["today"] or w in t["yesterday"] or w in t["tomorrow"] or w in t["weekdays"]:
            read.add(i)
            own = i - 1 if (w in t["weekdays"] and i and ws[i - 1] in t["weekday_ahead"] | t["weekday_behind"]) else i
            read.add(own)
            if _touched(ws, own - 1, i + 1):
                raise Refusal(_say("date_unknown"), "date_unknown")
        if w in t["today"]:
            out.append([today])
        elif w in t["yesterday"]:
            out.append([today - timedelta(days=1)])
        elif w in t["tomorrow"]:
            out.append([today + timedelta(days=1)])
        elif w in t["weekdays"]:
            k = t["weekdays"].index(w)
            if i and ws[i - 1] in t["ahead"]:
                out.append([today + timedelta(days=(k - today.weekday()) % 7 or 7)])
            else:
                out.append([today - timedelta(days=(today.weekday() - k) % 7 or 7)])
        elif w in t["months"]:  # a month name counts only with a day beside it ("May Smith" is not a date)
            mon, nxt, prv = t["months"][w], ws[i + 1] if i + 1 < len(ws) else "", ws[i - 1] if i else ""
            y_after = ws[i + 2] if i + 2 < len(ws) and len(ws[i + 2]) == 4 and ws[i + 2].isdigit() else None
            y_before = nxt if len(nxt) == 4 and nxt.isdigit() else None
            after = _recent(today, mon, int(nxt), int(y_after) if y_after else None) \
                if nxt.isdigit() and len(nxt) <= 2 else None
            before = _recent(today, mon, int(prv), int(y_before) if y_before else None) \
                if prv.isdigit() and len(prv) <= 2 else None
            if after or before:
                a = i - 1 if before else i
                b = (i + 3 if y_after else i + 2) if after else (i + 2 if y_before else i + 1)
                if (a and ws[a - 1] in t["day_qualifiers"]) or (b < len(ws) and ws[b] in t["day_qualifiers"]):
                    raise Refusal(_say("date_unknown"), "date_unknown")  # "next November 3": asked, never read
            if after and before and after != before:
                out.append([before, after])
            elif after or before:
                out.append([after or before])
            if after:
                read.update({i, i + 1} | ({i + 2} if y_after else set()))
            if before:
                read.update({i - 1, i} | ({i + 1} if y_before else set()))
    if any(_calendar(ws, i) and i not in read and not _unit(ws, i) for i in range(len(ws))):
        raise Refusal(_say("date_unknown"), "date_unknown")  # a calendar word no rule read: asked, never dropped
    return out



def _iso(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return date.min  # a date the engine cannot read is still a date the model claimed



def resolve_date(text: str, today: date, session: Session, path: str, model_value=None,
                 date_order: str | None = None) -> date | None:
    """The one date the owner wrote, or None when they wrote none; several, or an unclear one, is a question."""
    if session.bindings.get(path):
        return date.fromisoformat(session.bindings[path])
    try:
        phrases = date_phrases(text, today, date_order)
    except Refusal as rf:
        raise session.ask(path, rf.message, rf.code, [], []) from None
    readings = sorted({d for p in phrases for d in p}) if len(phrases) != 1 else list(dict.fromkeys(phrases[0]))
    if not phrases:
        t = table()
        documents = "|".join(re.escape(w) for w in sorted(t["document_nouns"] | {"order", "delivery"}))
        determiners = "|".join(re.escape(w) for w in sorted(t["determiners"] - {"last"}))
        document = rf"on\s+(?:(?:{determiners})\s+)?last\s+(?:{documents})\b"
        # "on its last invoice" names a document; keep asking about any other unread "on ..." phrase.
        if any(not re.match(document, text[mt.start():], re.I)
               for mt in re.finditer(r"\bon\s+[^\s.;!?]+", text, re.I)):
            raise session.ask(path, _say("date_unknown"), "date_unknown", [], [])
        mv = _iso(model_value)
        if mv is not None and mv != today:  # the model read a date the engine cannot: ask, never guess
            raise session.ask(path, _say("date_unknown"), "date_unknown", [], [])
        return None
    if len(readings) == 1:
        return readings[0]
    one = len(phrases) == 1 and len(readings) == 2
    labels = [f"{d.day} {d:%B}" if one else f"{d.day} {d:%b %Y}" for d in readings]
    ask = _say("date_ambiguous", a=labels[0], b=labels[1]) if one else _say("date_which", dates=" or ".join(labels))
    raise session.ask(path, ask, "date_ambiguous", labels, [d.isoformat() for d in readings])
