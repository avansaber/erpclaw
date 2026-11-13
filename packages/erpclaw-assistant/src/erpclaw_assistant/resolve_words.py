"""Resolution words."""
from __future__ import annotations

import re
from erpclaw_core.masters import STOP, _distance, _norm, same_word
from .resolve_text import table
from .resolve_types import Found, Occurrence


def _occurrence_ranges(text: str, occurrences: list[Occurrence]) -> list[list[int]]:
    """Stable character positions survive word spans blanked by request supersession."""
    tokens = [mt for mt in re.finditer(r"\S+", text) if words(mt.group())]
    return [[tokens[o.start].start(), tokens[o.end - 1].end()] for o in occurrences]



def words(text: str) -> list[str]:
    """The request's words, normalised as stored names are (`masters._norm`): punctuation is removed and never
    bounds; the word itself decides."""
    return _norm(text).split()



def punct_after(text: str) -> frozenset[int]:
    """The positions in `words(text)` that a punctuation mark follows ("tomorrow;" or "tomorrow ;"): a mark never
    bounds a name, but a day word before one ends it (table version 3)."""
    out, n = set(), -1
    for token in text.split():
        if not _norm(token):  # a mark alone
            if n >= 0:
                out.add(n)
            continue
        if re.match(r"[^\w\s]", token) and n >= 0:
            out.add(n)
        n += 1
        if re.search(r"[^\w\s]$", token):
            out.add(n)
    return frozenset(out)



def bounded_names(ws: list[str], names: list[str], *, item: bool, lang: str | None = None,
                  bounded: bool = True, breaks: frozenset[int] = frozenset(),
                  extra_bounds: frozenset[str] = frozenset(),
                  figure_bounds: frozenset[int] = frozenset()) -> list[Found]:
    """Each stored name whose whole normalised words occur in the request as consecutive words, bounded on both sides
    ; a name whose run lies inside another found name's run is set aside. For items the final word may be the
    generated singular or plural of the stored one (guard 2). `breaks`: the positions a punctuation mark follows
    (`punct_after`).

    The continuation guard: a bounding word ends a name only if no stored name of the kind continues past it with
    that same word ("in" after Acme does not bound when "Acme in Leeds Ltd" is stored), on either side, an item's
    words compared as the name is found (Copper Wire continues into "Copper Wires Monday Pack"). A day word ends a
    name only before the end, a mark, a figure or a bounding word (never a company suffix), and not where a stored
    name starts with it or goes on from the found one with the word after it. It only ever turns a silent match into
    a question, never the reverse."""
    t = table(lang)
    stored = [_norm(n).split() for n in names]

    def same(run: list[str], nw: list[str]) -> bool:  # an item's final word as it is found: singular or plural
        return run[:-1] == nw[:-1] and (run[-1] == nw[-1] or (item and same_word(run[-1], nw[-1])))

    def continues(nw: list[str], w: str, before: bool) -> bool:
        k = len(nw)
        return any(len(s) > k and ((s[-k - 1] == w and same(s[-k:], nw)) if before else (same(s[:k], nw) and s[k] == w))
                   for s in stored)

    def bounds(nw: list[str], pos: int, before: bool) -> bool:
        w = ws[pos] if 0 <= pos < len(ws) else None
        if not bounded or w is None or w.isdigit() or pos in figure_bounds:
            return True
        if not before and w in t["weekday_ahead"] | t["weekday_behind"] and \
                pos + 1 < len(ws) and ws[pos + 1] in t["weekdays"]:
            # A qualified weekday has the same name guards as a bare day, including its qualifier.
            if continues(nw, w, before) or any(s and s[0] == w for s in stored):
                return False
            pos += 1
            w = ws[pos]
        after = ws[pos + 1] if pos + 1 < len(ws) else None
        then = ws[pos + 2] if pos + 2 < len(ws) else None
        marked = pos in breaks
        if (w in t["quantity"] or w in t["scales"]) and ((item and before) or (not before and w not in t["articles"])):
            edge = nw[0] if before else nw[-1]  # a number word after a name, or before an item: a quantity
            return not (edge in t["quantity"] or edge in t["scales"]) and not continues(nw, w, before)
        if w in t["bound"] | extra_bounds:
            return not continues(nw, w, before)
        if not before and w in t["bound_after"]:  # table version 3, with the word after the day word
            k = len(nw)
            # the next word (marks between or not) is never a company suffix, nor the word after a figure ("Monday,
            # Inc.", "Monday 2000 Ltd"); then the end, a mark, a figure or a bounding word ends the name
            suffix = t["company_suffix"]
            ends = after not in suffix and not (after is not None and after.isdigit() and then in suffix) and \
                (after is None or marked or after.isdigit() or after in t["bound"])
            if not ends or any(s and s[0] == w for s in stored) or \
                    any(len(s) > k and same(s[:k], nw) and s[k] == after for s in stored):
                return False
            return not continues(nw, w, before)
        return False

    found = []
    for name in names:
        nw = _norm(name).split()
        k = len(nw)
        if not k:
            continue
        for i in range(len(ws) - k + 1):
            run = ws[i:i + k]
            if run[:-1] != nw[:-1] or not (run[-1] == nw[-1] or (item and same_word(run[-1], nw[-1]))):
                continue
            if bounds(nw, i - 1, True) and bounds(nw, i + k, False):
                found.append(Found(name, i, i + k))
    return [f for f in found if not any(g.start <= f.start and f.end <= g.end and (g.end - g.start) > (f.end - f.start)
                                        for g in found)]



def _collides(a: str, b: str, item: bool) -> bool:
    x, y = _norm(a).split(), _norm(b).split()
    if x == y:
        return True
    return item and len(x) == len(y) and x[:-1] == y[:-1] and same_word(x[-1], y[-1])



def _raw_in(text: str, name: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None



def _candidates(rows: list[dict], ws: list[str], *, item: bool, limit: int = 5) -> list[dict]:
    """The records sharing a word with the owner's words, or near one (by edit distance), nearest first; words of
    the bounding and quantity tables, stop words and figures are not compared."""
    t = table()
    said = [w for w in ws if w not in t["bound"] and w not in t["quantity"] and w not in STOP and not w.isdigit()]
    scored = []
    for r in rows:
        have = [w for w in _norm(r["name"]).split() if w not in STOP]
        shared = sum(1 for h in have if any(h == w or (item and same_word(w, h)) for w in said))
        near = min((_distance(w, h) for w in said for h in have
                    if len(w) >= 3 and _distance(w, h) <= max(len(w), len(h)) // 3), default=None)
        if shared or near is not None:
            scored.append((-shared, near if near is not None else 0, r["name"], r))
    return [r for *_, r in sorted(scored, key=lambda x: x[:3])[:limit]]



def _leaves(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj



def _raw_words(text: str) -> list[str]:
    """The request's words as the owner wrote them, one per entry of `words(text)`."""
    return [tok for tok in text.split() if _norm(tok)]



def _as_written(text: str, span: tuple[int, int]) -> str:
    """The owner's own words over a span, as written, without a closing mark."""
    a, b = span
    return re.sub(r"[,.;:!?]+$", "", " ".join(_raw_words(text)[a:b]))



def _written_span(ws: list[str], value) -> tuple[int, int] | None:
    """Where the value's words stand together in the owner's words, or None."""
    vw = _norm(str(value or "")).split()
    for i in range(len(ws) - len(vw) + 1):
        if vw and ws[i:i + len(vw)] == vw:
            return i, i + len(vw)
    return None



def without_names(text: str, spans: list) -> str:
    """The owner's words without the stored names found in them, so a calendar word inside a name
    ("Weekend Support", "September Labs") is never read as a date."""
    if not spans:
        return text
    drop = {k for f in spans for k in range(f.start, f.end)}
    return " ".join(tok for k, tok in enumerate(_raw_words(text)) if k not in drop)



def _alternatives(ws: list[str], rows: list[dict], item: bool) -> list[str]:
    """The records whose mentions "or" stands between, or "either" stands before ("either Acme Corporation or Hooli", "5
    copper wire or circuit board"): the owner has not chosen, so the field always asks (one record per call).
    Mentions here need no bounds; a word inside a mention never counts."""
    t = table()
    found = bounded_names(ws, [r["name"] for r in rows], item=item, bounded=False)
    if len({f.name for f in found}) < 2:
        return []
    inside = {k for f in found for k in range(f.start, f.end)}
    first, last = min(f.start for f in found), max(f.start for f in found)
    for k, w in enumerate(ws):
        if k not in inside and ((w in t["or"] and first < k < last) or (w in t["either"] and k < last)):
            return sorted({f.name for f in found})
    return []



def _outside(ws: list[str], spans: list[Found]) -> set[str]:
    return {w for k, w in enumerate(ws) if not any(f.start <= k < f.end for f in spans)}



def _has_run(ws: list[str], run: tuple[str, ...]) -> bool:
    return any(tuple(ws[i:i + len(run)]) == run for i in range(len(ws) - len(run) + 1))



def consumed_ranges(text: str, name: str, *, item: bool = False, bounded: bool = False,
                    runs: list[Found] | None = None) -> list[tuple[int, int]]:
    """Project the resolver's matched word runs onto their original code points."""
    found = runs if runs is not None else bounded_names(words(text), [name], item=item, bounded=bounded,
                                                       breaks=punct_after(text))
    tokens = [(mt.start(), mt.end()) for mt in re.finditer(r"\S+", text) if words(mt.group())]
    raw = list(re.finditer(re.escape(name), text, re.I))
    out = []
    for f in found:
        exact = next((mt for mt in raw if len(words(text[:mt.start()])) == f.start and
                      len(words(text[:mt.end()])) == f.end), None)
        if exact:
            out.append((exact.start(), exact.end()))
        elif f.end <= len(tokens):
            a, b = tokens[f.start][0], tokens[f.end - 1][1]
            while a < b and not text[a].isalnum():
                a += 1
            while b > a and not text[b - 1].isalnum():
                b -= 1
            out.append((a, b))
    return out



def _core(name: str) -> list[str]:
    """A stored name's core words: its words without stop words, bounding words and number words."""
    t = table()
    skip = STOP | t["bound"] | set(t["quantity"]) | set(t["scales"]) | t["number_and"]
    return [w for w in _norm(name).split() if w not in skip]
