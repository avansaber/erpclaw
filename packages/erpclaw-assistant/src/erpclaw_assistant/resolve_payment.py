"""Resolution payment."""
from __future__ import annotations

import re
from datetime import date
from sqlalchemy import select
from erpclaw_core import models as m
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.masters import _norm
from .resolve_text import MESSAGE_BREAK, _say, table
from .resolve_words import _as_written, _raw_words, _written_span, bounded_names, words
from .resolve_types import Found, Occurrence, OutgoingAccountWords
from .resolve_amounts import _amount_marker_words, _decimal, _has_amount_marker, _marks_at, _word_at, figures
from .resolve_state import Session
from .resolve_dates import _ago, _calendar, date_phrases
from .resolve_records import document_mentions


def _phrase_stops() -> frozenset:
    t = table()
    return t["bound"] | t["phrase_stop"] | t["determiners"] | t["pronouns"] | t["methods"] | t["write_verbs"] | \
        t["document_nouns"] | t["today"] | t["yesterday"] | frozenset(t["weekdays"]) | frozenset(t["months"]) | \
        t["coordinators"]



def _run(ws: list[str], raw: list[str], i: int, step: int, stops: frozenset) -> tuple[int, int] | None:
    """The run of phrase words from word i in direction `step` (1 right, -1 left), up to a stopping word, a word with
    a figure in it, or a mark that ends a clause; its span (start, end), or None when it is empty."""
    got: list[int] = []
    while 0 <= i < len(ws) and len(got) < 6:
        w = ws[i]
        if w in stops or (any(ch.isdigit() for ch in w) and figures(raw[i])):
            break
        if step < 0 and got and re.search(r"[,.;:!?]$", raw[i]):
            break  # leftwards, a word closing an earlier clause is not the payer's
        got.append(i)
        if step > 0 and re.search(r"[,.;:!?]$", raw[i]):
            break
        i += step
    if not got:
        return None
    return min(got), max(got) + 1



def payment_display_purpose(ctx: Ctx, text: str) -> str | None:
    """A trailing purpose, as the owner wrote it, when it names a non-bank chart entry."""
    names = {"sales", "the sales"}
    names.update(_norm(row) for row in ctx.conn.execute(select(m.account.c.name).where(
        m.account.c.company_id == ctx.company_id,
        (m.account.c.account_type != "bank") | m.account.c.account_type.is_(None))).scalars())
    for part in reversed(text.split(MESSAGE_BREAK)):
        match = re.search(r"\bfor\s+(.+?)\s*[.;!?]*\s*$", part, re.I)
        if match and _norm(match.group(1).rstrip(".;!? ")) in names:
            return match.group(0).rstrip(".;!? ")
    return None


def payer_phrases(text: str) -> list[str]:
    """The payers the owner wrote, as written, in order: "<payer> paid", "paid by <payer>", "from <payer>" and
    "<payer>'s payment". A passive "was paid" names none; a pronoun, a payment method or a document is no payer."""
    t, ws, raw = table(), words(text), _raw_words(text)
    stops = _phrase_stops()
    spans: list[tuple[int, int]] = []

    def coordinated(span: tuple[int, int], step: int) -> None:
        """The payers joined to this one by "and" or "or", in the same direction."""
        a, b = span
        while True:
            k = a - 1 if step < 0 else b
            if not (0 <= k < len(ws)) or ws[k] not in t["coordinators"]:
                return
            nxt = _run(ws, raw, k + step, step, stops)
            if not nxt or (nxt[1] != k if step < 0 else nxt[0] != k + 1):
                return
            spans.append(nxt)
            a, b = nxt

    for i, w in enumerate(ws):
        if w == t["payer_paid"]:
            j = i - 1
            while j >= 0 and ws[j] in t["payer_skip"]:
                j -= 1
            if j >= 0 and ws[j] not in t["passive"]:
                span = _run(ws, raw, j, -1, stops)
                if span and span[1] == j + 1:
                    spans.append(span)
                    coordinated(span, -1)
            if i + 1 < len(ws) and ws[i + 1] == t["payer_by"]:
                span = _run(ws, raw, i + 2, 1, stops)
                if span:
                    spans.append(span)
                    coordinated(span, 1)
        elif w in t["payer_from"]:
            span = _run(ws, raw, i + 1, 1, stops)
            if span:
                spans.append(span)
                coordinated(span, 1)
    for mt in re.finditer(t["payer_possessive"], text, re.I):
        j = len(words(text[:mt.start()])) - 1
        span = _run(ws, raw, j, -1, stops) if j >= 0 else None
        if span and span[1] == j + 1:
            spans.append(span)
    out: list[str] = []
    for a, b in sorted(set(spans)):
        said = re.sub(rf"(?:{t['possessive_suffix']})?[,.;:!?]*$", "", " ".join(raw[a:b]))
        if said and said not in out:
            out.append(said)
    return out



def account_phrases(text: str) -> list[tuple[int, int]]:
    """The spans of the accounts the owner named the money going into: "into <account>", "<account> account" and
    "account <number>". A payment method ("by bank transfer") names none."""
    t, ws, raw = table(), words(text), _raw_words(text)
    stops = t["bound"] | t["phrase_stop"] | t["determiners"] | t["pronouns"] | t["coordinators"] | t["method_cues"]
    out: list[tuple[int, int]] = []
    for i, w in enumerate(ws):
        if w in t["account_into"]:
            j = i + 1
            while j < len(ws) and (ws[j] in t["determiners"] or ws[j] == "either"):
                j += 1
            if j < len(ws) and ws[j] in t["account_nouns"]:  # "into account Savings"
                j += 1
            if j < len(ws) and ws[j].isdigit():
                out.append((j, j + 1))
            elif j < len(ws) and not figures(raw[j]):
                span = _run(ws, raw, j, 1, stops)
                if span and span[0] == j:
                    out.append(span)
                    while span[1] + 1 < len(ws) and ws[span[1]] in t["coordinators"]:  # "into Bank and Savings"
                        nxt = _run(ws, raw, span[1] + 1, 1, stops)
                        if not nxt or nxt[0] != span[1] + 1:
                            break
                        out.append(nxt)
                        span = nxt
        elif w in t["account_nouns"]:
            after = _run(ws, raw, i + 1, 1, stops) if i + 1 < len(ws) else None
            if i + 1 < len(ws) and any(ch.isdigit() for ch in ws[i + 1]):
                out.append((i + 1, i + 2))
            elif after and after[0] == i + 1:  # "to account Missing", "account Savings"
                out.append(after)
            elif i:
                span = _run(ws, raw, i - 1, -1, stops)
                if span and span[1] == i:
                    out.append(span)
    return sorted(set(out))



def payment_name_spans(text: str, rows: list[dict], *, item: bool = False,
                       bounded: bool = True) -> list[Found]:
    """Full numeric names with adjacent amount marks; marks never give the inner figure ownership."""
    ws = words(text)
    clean = list(ws)
    skip = set()
    for _, start, end in figures(text):
        k = _word_at(text, start)
        if k >= len(clean):
            continue
        marks_ = _marks_at(text, start, end)
        if not _has_amount_marker(text, start, end):
            continue
        clean[k] = text[start:end].lower()
        for tok, _ in marks_:
            for j in (k - 1, k + 1):
                if 0 <= j < len(ws) and ws[j] == _norm(tok):
                    skip.add(j)
    mapping = [i for i in range(len(clean)) if i not in skip]
    projected = [clean[i] for i in mapping]
    names = [r["name"] for r in rows]
    bounds = table()["account_into"] | table()["method_cues"]
    # Keep every literal full name before considering spellings with an adjacent marker inserted.
    found = bounded_names(ws, names, item=item, extra_bounds=bounds, bounded=bounded)
    # A comma before a bank mention starts a new clause; its right edge still uses the usual whole-name guard.
    if bounded and rows and all(r.get("account_type") == "bank" for r in rows):
        tokens = [match for match in re.finditer(r"\S+", text) if _norm(match.group())]
        for i in range(1, min(len(ws), len(tokens))):
            if re.search(r",\s*$", text[tokens[i - 1].start():tokens[i].start()]):
                found += [Found(f.name, f.start + i, f.end + i) for f in
                          bounded_names(ws[i:], names, item=item, extra_bounds=bounds) if f.start == 0]
    found += [Found(f.name, mapping[f.start], mapping[f.end - 1] + 1)
              for f in bounded_names(projected, names, item=item, extra_bounds=bounds, bounded=bounded)]
    return list(dict.fromkeys(found))



def guard_payment_names(text: str, occurrences: list[Occurrence], session: Session, path: str) -> None:
    """An amount mark on a name's number asks before any amount or record can bind."""
    for _, start, end in figures(text):
        name = next((o for o in occurrences if o.kind in {"account", "paid_from", "item", "customer", "supplier", "warehouse"}
                     and o.start <= _word_at(text, start) < o.end), None)
        if name and any(o.kind == "figure" for o in occurrences) and _has_amount_marker(text, start, end):
            # Reuse the existing template, showing the independent amount and the complete marked name.
            raise session.ask(path, _say("figure_unused", figure=text), "money_name_ambiguous", [], [])



def payment_accounts(ctx: Ctx, text: str, *, introduced: bool = True) -> list[Found]:
    """Incoming destinations, including bounded chart names outside payer and method positions."""
    ws = words(text)
    banks = [dict(r) for r in ctx.conn.execute(select(m.account).where(
        m.account.c.company_id == ctx.company_id, m.account.c.account_type == "bank",
        m.account.c.is_group.is_(False))).mappings()]
    chart = payment_name_spans(text, banks)
    explicit = account_phrases(text) if introduced else []
    # A full chart name can contain a number or a phrase stop (Current 1234, Cash in Hand).
    for f in chart:
        cued = introduced and (any(a == f.start for a, b in explicit) or
                               (f.start and ws[f.start - 1] in table()["account_into"]))
        explicit = [(a, b) for a, b in explicit if not f.start <= a < f.end]
        if cued:
            explicit.append((f.start, f.end))
    found = [Found(_as_written(text, (a, b)), a, b) for a, b in explicit]
    payers = [span for phrase in payer_phrases(text) if (span := _written_span(ws, phrase))]
    methods_ = [span for phrase in method_phrases(text) if (span := _written_span(ws, phrase))]
    for f in chart:
        if any(a <= f.start < b for a, b in payers + methods_) or any(p.start <= f.start < p.end for p in found):
            continue
        found.append(f)
    markers = _amount_marker_words(text)
    return sorted((f for f in found if figures(f.name) or not markers.intersection(range(f.start, f.end))),
                  key=lambda f: f.start)



def outgoing_account_phrases(text: str, accounts: list[dict], amount=None,
                             today: date | None = None) -> list[OutgoingAccountWords]:
    """Only outgoing `from` runs. Chart names win over stop words, but never over an unknown extension.

    Keep rejected extensions whole for the question; a lone unmarked number after a name is ambiguous even
    when the model called it the amount. Spans, rather than chart ids, retain the owner's spelling.
    """
    t, ws, raw = table(), words(text), _raw_words(text)
    stops = t["bound"] | t["phrase_stop"] | t["determiners"] | t["pronouns"] | t["coordinators"] | t["method_cues"]
    tokens = [mt for mt in re.finditer(r"\S+", text) if _norm(mt.group())]
    fs = figures(text)
    chart = {_norm(str(r[key])) for r in accounts for key in ("name", "number")}
    numeric_names = [f for f in payment_name_spans(text, accounts, bounded=False) if figures(f.name)]
    marker_words = _amount_marker_words(text)

    def boundary(k: int, start: int) -> bool:
        if k == len(ws):
            return True
        if ws[k] in stops or ws[k] in t["tomorrow"] or ws[k] in t["date_vocab"] or _calendar(ws, k):
            return True
        # Numeric dates and document numbers belong to their own readers, not an account's number.
        tail = " ".join(raw[k:])
        if document_mentions(tail, "INV", ("invoice", "inv", "payment", "pay")) and \
                (ws[k] in t["document_nouns"] or re.match(r"INV-\d", raw[k], re.I)):
            return True
        try:
            if any(ch.isdigit() for ch in ws[k]) and date_phrases(raw[k], today or date.today()):
                return True
        except Refusal:
            pass
        for value, s, e in fs:
            if tokens[k].start() <= s < tokens[k].end():
                if _marks_at(text, s, e):
                    return True
                # Equality alone is insufficient: the only figure might be an account-number extension.
                if value == _decimal(amount) and any(f == value and (b <= tokens[start].start() or
                                                                   a >= tokens[k].end()) for f, a, b in fs):
                    return True
        return False

    def read(start: int) -> OutgoingAccountWords | None:
        while start < len(ws) and (ws[start] in t["determiners"] or ws[start] == "either"):
            start += 1
        if start < len(ws) and ws[start] in t["account_nouns"]:
            start += 1
        if start == len(ws):
            return None
        limit = min(len(ws), start + 6)
        matches = [end for end in range(start + 1, limit + 1)
                   if _norm(_as_written(text, (start, end))) in chart and
                   not any(_marks_at(text, s, e) for _, s, e in fs
                           if tokens[start].start() <= s < tokens[end - 1].end())]
        marked_name = None
        if not matches:
            marked_name = next((f for f in numeric_names if f.start == start and f.end <= limit), None)
            if marked_name:
                matches = [marked_name.end]
        if matches:
            end = max(matches)
            if marked_name:
                while end < limit and end in marker_words:
                    end += 1
            if boundary(end, start):
                return OutgoingAccountWords(start, end, name=marked_name.name if marked_name else None)
            # Do not let fallback truncate Bank Overseas or Current 5678 back to the chart prefix.
            while end < limit and not boundary(end, start):
                end += 1
            return OutgoingAccountWords(start, end, True, marked_name.name if marked_name else None)
        if ws[start].isdigit():
            if any(_marks_at(text, s, e) for _, s, e in fs
                   if tokens[start].start() <= s < tokens[start].end()):
                return None
            return OutgoingAccountWords(start, start + 1)
        span = _run(ws, raw, start, 1, stops - t["account_nouns"])
        if span:
            a, b = span
            if ws[b - 1] in t["account_nouns"]:
                b -= 1
            if b > a:
                return OutgoingAccountWords(a, b)
        return None

    out = []
    for i, w in enumerate(ws):
        if w != "from":
            continue
        phrase = read(i + 1)
        while phrase:
            if phrase not in out:
                out.append(phrase)
            end = phrase.end
            if end < len(ws) and ws[end] in t["account_nouns"]:
                end += 1
            phrase = read(end + 1) if end < len(ws) and ws[end] in t["coordinators"] else None
    return out



def method_spans(text: str) -> list[tuple[int, int]]:
    """Whole method runs; only another field, coordination or a clause ends one."""
    t, ws, raw = table(), words(text), _raw_words(text)
    out = []
    # Share the date reader's whole-phrase patterns so "two days ago" and "end of month" end a
    # method before their first word, just as "last Friday" ends it before its qualifier.
    date_starts = {_word_at(text, mt.start())
                   for pattern in [_ago(), t["vague_dates"], t["month_end"]] + [p for p, _ in t["relative_days"]]
                   for mt in re.finditer(pattern, text, re.I)}

    def starts_field(k):
        w = ws[k]
        calendar = k in date_starts or w in t["today"] | t["yesterday"] | t["tomorrow"] | t["date_vocab"] or \
            w in t["weekdays"] or _calendar(ws, k)
        return w in t["account_into"] or w == "from" or w in t["document_nouns"] or calendar or \
            bool(figures(raw[k])) or bool(re.match(r"[A-Za-z]+-\d", raw[k])) or \
            (w in t["date_touch"] | t["month_qualifiers"] | {"on", "the"} and
             k + 1 < len(ws) and starts_field(k + 1))

    cues = [(i, w) for i, w in enumerate(ws) if w in t["method_cues"] | t["method_nouns"] or w == "actually" or (w == MESSAGE_BREAK and i + 1 < len(ws) and ws[i + 1] in t["methods"] - {"bank"})]
    if ws and ws[0] in t["methods"] - {"bank"}:
        cues.insert(0, (-1, "answer"))
    for i, w in cues:
        if i + 1 < len(ws) and ws[i + 1] in t["method_nouns"]:
            continue
        # "paid by Hooli" is a payer, while "paid by wire" is a method.
        if w == "by" and i and ws[i - 1] == t["payer_paid"] and \
                i + 1 < len(ws) and ws[i + 1] not in t["methods"]:
            continue
        j = i + 1
        while j < len(ws):
            k = j
            while k < len(ws) and ws[k] != MESSAGE_BREAK and ws[k] not in t["coordinators"] and not starts_field(k):
                k += 1
                if re.search(r"[,.;:!?]$", raw[k - 1]):
                    break
            if k == j:
                break
            if (j, k) not in out:
                out.append((j, k))
            if k < len(ws) and ws[k] in t["coordinators"]:
                j = k + 1
            else:
                break
    return sorted(out)



def method_phrases(text: str) -> list[str]:
    return list(dict.fromkeys(_as_written(text, span) for span in method_spans(text)))



def unknown_methods(text: str) -> list[str]:
    return [said for said in method_phrases(text) if any(w not in table()["methods"] for w in words(said))]
