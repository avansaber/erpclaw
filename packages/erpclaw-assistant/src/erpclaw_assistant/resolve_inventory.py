"""Resolution inventory."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Iterable
from sqlalchemy import select
from erpclaw_core import models as m
from erpclaw_core.common import Ctx
from erpclaw_core.masters import _norm, customers, same_word
from .resolve_text import _memo_phrases, _say, table
from .resolve_words import _as_written, _candidates, _core, _occurrence_ranges, _written_span, bounded_names, consumed_ranges, punct_after, words
from .resolve_types import Found, Occurrence, _items, _line_model, _lines_shape, marks
from .resolve_amounts import _amount_marker_words, _decimal, _marks_at, _plural, _word_at, figures, mask_money_spans, number_words
from .resolve_state import Session
from .resolve_dates import _ago, _calendar, _recent, _unit
from .resolve_records import _item_alternatives, _same_document
from .resolve_payment import account_phrases, method_spans, payment_accounts, payment_name_spans
from .resolve_warehouse import receipt_destination_phrases, warehouse_words


NUMERAL = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")  # the exact written forms of a figure


# Look up the facade at call time so its outgoing reader remains patchable.
def outgoing_account_phrases(*args, **kwargs):
    from . import resolve

    return resolve.outgoing_account_phrases(*args, **kwargs)



def inventory(ctx: Ctx, text: str, items: list[dict], supplier_spans: Iterable[Found] = (), *,
              outgoing: bool = False, amount=None, incoming: bool = False,
              receipt: bool = False) -> list[Occurrence]:
    """Every recognised value phrase, each once; payment readers are enabled only by payment callers."""
    t, low, ws = table(), text.lower(), words(text)
    payment = incoming or outgoing
    markers = _amount_marker_words(text) if payment else set()
    taken: set[int] = set()
    date_names: set[int] = set()
    out: list[Occurrence] = []

    def at(ch: int) -> int:
        return len(words(text[:ch]))

    def add(kind: str, a: int, b: int, said: str, value=None) -> None:
        # A numeric stored name still reserves its inner figure so the marked-name guard asks.
        # Its marker is protected separately when a correction masks the name's word span.
        numeric_name = kind in {"account", "paid_from", "customer", "supplier", "item", "warehouse"} and \
            isinstance(value, str) and bool(figures(value))
        if b > a and not any(i in taken or (i in markers and not numeric_name) or
                            (kind == "date" and i in date_names) for i in range(a, b)):
            taken.update(range(a, b))
            out.append(Occurrence(kind, a, b, said, value))

    def chars(kind: str, pattern: str, value=None) -> None:
        for mt in re.finditer(pattern, low):
            add(kind, at(mt.start()), max(at(mt.end()), at(mt.start()) + 1), text[mt.start():mt.end()], value)

    supplier_spans = tuple(supplier_spans)
    destinations = receipt_destination_phrases(text, items, customers(ctx) +
        [{"name": f.name} for f in supplier_spans], ctx) if receipt else []
    for f in supplier_spans:
        if receipt and any(p.start <= f.start and f.end <= p.end for p in destinations):
            continue
        add("supplier", f.start, f.end, _as_written(text, (f.start, f.end)), f.name)
    custs = customers(ctx)
    names = list(supplier_spans) + bounded_names(ws, [r["name"] for r in custs], item=False, bounded=False) + \
        bounded_names(ws, [r["name"] for r in items], item=True, bounded=False)
    for start, end, value in _memo_phrases(text):
        if not any(f.start <= at(start) < f.end for f in names):
            add("memo", at(start), at(end), text[start:end], value)
    # Complete names own their inner words even when numeric dates are inventoried first. Keep the
    # contextual customer/item/account claim order below; only prevent dates from splitting a name.
    if payment:
        banks = [dict(r) for r in ctx.conn.execute(select(m.account).where(
            m.account.c.company_id == ctx.company_id, m.account.c.account_type == "bank",
            m.account.c.is_group.is_(False))).mappings()]
        date_names = {i for f in payment_name_spans(text, custs) + payment_name_spans(text, items, item=True) +
                      payment_name_spans(text, banks) for i in range(f.start, f.end)}
        date_names.update(i for a, b in method_spans(text) for i in range(a, b))
    for p in destinations if receipt else warehouse_words(text, items, custs, ctx):
        add("warehouse", p.start, p.end, p.said, {"item": p.item, "item_start": p.item_start})
    for mt in re.finditer(r"[^\s,;<>()]+@[^\s,;<>()]+\.[a-z]+", low):  # a customer's email is a mention of it;
        hit = [r["name"] for r in custs if (r.get("email") or "").lower() == mt.group(0)]  # another is emailing
        a = at(mt.start())
        target = set(ws[max(0, a - 3):a]) & t["email"]  # "email it to ap@...": the emailing phrase's own target
        reference = len(hit) == 1 and not target
        add("customer" if reference else "email", a, max(at(mt.end()), a + 1), text[mt.start():mt.end()],
            hit[0] if reference else None)
    chars("document", r"\b[a-z]{2,4}-\d{4}-\d+\b")
    for mt in re.finditer(r"\b(?:invoice|inv|payment|pay)\s*(?:no\.?|number)?\s*#?\s*(\d+)\b", low):
        if not (outgoing and mt.group().split()[0] == "pay"):
            add("document", at(mt.start(1)), at(mt.end(1)), mt.group(1))
    chars("date", r"\b\d{4}-\d{2}-\d{2}\b")
    chars("date", r"(?<![\w./])\d{1,2}/\d{1,2}(?:/(?:\d{2}|\d{4}))?(?![\w/])")  # as date_phrases reads them
    chars("date", r"(?<![\w./])\d{1,2}\.\d{1,2}\.\d{4}(?![\w.])")
    chars("date", _ago())
    for mt in re.finditer(t["rate"], low):
        add("rate", at(mt.start()), max(at(mt.end()), at(mt.start()) + 1), text[mt.start():mt.end()])
    destinations = payment_accounts(ctx, text, introduced=incoming) if payment else []
    explicit = account_phrases(text) if incoming else []
    for f in destinations:
        if any(a == f.start for a, b in explicit):
            add("account", f.start, f.end, _as_written(text, (f.start, f.end)), f.name)
    for f in bounded_names(ws, [r["name"] for r in custs], item=False, bounded=False):
        add("customer", f.start, f.end, " ".join(ws[f.start:f.end]), f.name)
    for r in custs:  # a unique city is a reference to its customer
        city = r.get("city")
        if city and sum(1 for x in custs if x.get("city") == city) == 1:
            cw = _norm(city).split()
            for i in range(len(ws) - len(cw) + 1):
                if cw and ws[i:i + len(cw)] == cw:
                    add("customer", i, i + len(cw), city, r["name"])
    # Invoice item choices retain their existing full-name occurrence after confirmation. Payment readers
    # reserve only bounded names, including numeric names with an adjacent amount marker.
    item_names = payment_name_spans(text, items, item=True) if incoming or outgoing else \
        bounded_names(ws, [r["name"] for r in items], item=True, bounded=False)
    for f in item_names:
        add("item", f.start, f.end, " ".join(ws[f.start:f.end]), f.name)
    for f in destinations:
        add("account", f.start, f.end, _as_written(text, (f.start, f.end)), f.name)
    if outgoing:
        banks = [dict(r) for r in ctx.conn.execute(select(m.account).where(
            m.account.c.company_id == ctx.company_id, m.account.c.account_type == "bank",
            m.account.c.is_group.is_(False))).mappings()]
        parties = [dict(r) for r in ctx.conn.execute(select(m.party).where(
            m.party.c.company_id == ctx.company_id, m.party.c.kind.in_(("supplier", "customer")))).mappings()]
        protected = bounded_names(ws, [r["name"] for r in parties], item=False, bounded=False)
        for p in outgoing_account_phrases(text, banks, amount, ctx.today):
            if (p.name or not markers.intersection(range(p.start, p.end))) and \
                    not any(f.start <= p.start < f.end for f in protected):
                # Only the outgoing contract assigns `from` runs this kind.
                out = [o for o in out if not (p.start <= o.start and o.end <= p.end)]
                taken.difference_update(range(p.start, p.end))
                add("paid_from", p.start, p.end, _as_written(text, (p.start, p.end)), p.name)
    for a, b in method_spans(text) if payment else []:
        add("method", a, b, _as_written(text, (a, b)), _as_written(text, (a, b)))
    # date phrases of words, after the stored names: a name holding a calendar word ("Year-End Review") is the name's
    for pattern, delta in t["relative_days"]:
        chars("date", pattern, "today" if delta == 0 else None)
    chars("date", t["vague_dates"])
    chars("date", t["month_end"])
    for i, w in enumerate(ws):
        if w in t["date_vocab"]:
            add("date", i, i + 1, w)
    months = t["months"]
    for i, w in enumerate(ws):
        if w in months:
            a, b = i, i + 1
            def day(w):
                number = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", w)
                return number is not None and 1 <= int(number[1]) <= 31
            if a and day(ws[a - 1]):
                a -= 1
            if b < len(ws) and day(ws[b]):
                b += 1
            if b < len(ws) and ws[b].isdigit() and len(ws[b]) == 4 and (a < i or b > i + 1):
                b += 1
            if b - a > 1:
                add("date", a, b, " ".join(ws[a:b]))
        elif w in t["today"] or w in t["yesterday"] or w in t["tomorrow"] or w in t["weekdays"]:
            add("date", i, i + 1, w, "today" if w in t["today"] else None)
    for d, st, en in figures(text):
        while en > st and text[en - 1] == ",":
            en -= 1
        a = at(en) - 1
        if a not in taken:
            add("figure", a, a + 1, text[st:en], d)
    i = 0
    absent_one = [(at(m.start()), at(m.end())) for m in re.finditer(t["absent_one"], low)]
    while i < len(ws):  # runs of number words; an article only directly before an item
        j = i
        while j < len(ws) and (ws[j] in t["quantity"] or ws[j] in t["scales"] or
                               (ws[j] in t["number_and"] and j > i and ws[j - 1] in t["scales"])):
            j += 1
        if j > i:
            run = ws[i:j]
            article = all(w in t["articles"] for w in run)
            item_next = any(o.kind == "item" and o.start == j for o in out)
            idiom = run == ["one"] and (((not item_next) and i and ws[i - 1] in t["idiomatic_one_before"]) or
                                          tuple(ws[j:j + 2]) == t["idiomatic_one_after"] or
                                          any(a <= i < b for a, b in absent_one))
            if (not article or item_next) and not idiom:
                add("number", i, j, " ".join(run), number_words(run))
            i = j
        else:
            i += 1
    cust_core = {w: r["name"] for r in custs for w in _core(r["name"])}
    item_core = [(w, r["name"]) for r in items for w in _core(r["name"])]
    for i, w in enumerate(ws):  # a core word of a stored name, outside every other occurrence
        if i in taken or w in t["generic_quantity_units"]:
            continue
        if w in cust_core:
            add("customer", i, i + 1, w, None)
        elif any(w == x or same_word(w, x) for x, _ in item_core):
            add("item", i, i + 1, w, None)
    for i, w in enumerate(ws):  # a calendar word nothing else took, and no unit or rate
        if i not in taken and _calendar(ws, i) and not _unit(ws, i):
            add("date", i, i + 1, w)
    chars("draft", t["draft"])
    absent = [(at(m.start()), at(m.end())) for m in re.finditer(t["absent_email"], low)]
    for i, w in enumerate(ws):
        if w in t["email"] and not any(a <= i < b for a, b in absent):
            add("email", i, i + 1, w)
    return sorted(out, key=lambda o: o.start)



def unclear_numerals(text: str, separator_after: set[int] | None = None) -> list[str]:
    """Figures written in no exact form: "1,5", "1,50", ".5", "- 5", "1 500"."""
    out = []
    for mt in re.finditer(r"(?<![\w-])(-\s+)?([.\d][\d.,]*\d|\d)(?=\s+\d|[^\w]|$)", text):
        raw = mt.group(2).rstrip(".,")
        prior = text[:mt.start()].rstrip()
        spaced = bool(mt.group(1)) and (mt.start() == 0 or not text[mt.start() - 1].isalnum()) and \
            len(words(prior)) not in (separator_after or set())
        after = re.match(r"\s+(\d[\d.,]*)", text[mt.end():])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/(?:\d{2}|\d{4}))?|\d{1,2}\.\d{1,2}\.\d{4}", raw):
            continue  # a figure date
        before = words(text[:mt.start()])
        month = table()["months"].get(before[-1]) if before else None
        year = after.group(1).rstrip(".,") if after else ""
        if not spaced and month and raw.isdigit() and len(raw) <= 2 and len(year) == 4 and year.isdigit() and \
                _recent(date.min, month, int(raw), int(year)) is not None:
            continue  # a named month's day and year, as date_phrases reads them
        if spaced or not NUMERAL.match(raw) or (after and not re.search(r"[a-zA-Z]", text[mt.end():mt.end() + 1])
                                               and NUMERAL.match(after.group(1).rstrip(".,"))):
            out.append(text[mt.start():mt.end()] + (after.group(0) if after else ""))
    return out



def _unclaimed(session: Session, path: str, occ: Occurrence, items: list[dict], customers_: list[dict]) -> None:
    """An occurrence no part of the reply claimed: the owner is asked. One hook, so a test can show the
    accounting carries the weight."""
    if occ.kind == "item":
        cands = [r["name"] for r in _candidates(items, [occ.said], item=True)]
        if len(cands) == 1:
            raise session.ask(path, _say("confirm", name=cands[0]), "item_ambiguous",
                              [cands[0], _say("someone_else")], [cands[0], None], confirm=True)
        if cands:
            raise session.ask(path, _say("which", what="item", names=", ".join(cands)), "item_ambiguous", cands, cands)
    kind = "account" if occ.kind == "paid_from" else occ.kind
    raise session.ask(path, _say("figure_unused", figure=occ.said), f"{kind}_ambiguous", [], [])



def chosen_item_span(text: str, items: list[dict], session: Session, path: str, name: str,
                     occurrences: list[Occurrence]) -> Found | None:
    """The owner's uniquely written near-name phrase, after its own item question was answered."""
    if session.bindings.get(path) != name:
        return None
    ws = words(text)
    phrase = words(session.bindings.get("item_phrase:" + path) or "")
    starts = [i for i in range(len(ws) - len(phrase) + 1) if phrase and ws[i:i + len(phrase)] == phrase]
    if len(starts) != 1 or name not in [r["name"] for r in _candidates(items, phrase, item=True)]:
        return None
    start, end = starts[0], starts[0] + len(phrase)
    if any(o.start < end and start < o.end and (o.kind != "item" or o.value != name) for o in occurrences):
        return None
    return Found(name, start, end)


def account(ctx: Ctx, text: str, calls: list[tuple[object, dict]], results: list, lead: int | None,
            session: Session, scopes: list[str], *, superseded_rates=frozenset()) -> None:
    """Coverage by occurrence accounting: each recognised occurrence is claimed by one part of the reply that
    actually appears in it, exactly; anything left asks. `results` are the writes' resolutions, `lead` the write the
    reply drafts (only its not_done rows are emitted)."""
    if not any(marks(c.input_model) for c, _ in calls):
        return  # no write the step resolves: the accounting covers only those
    if any(v["kind"] == "lines" and not _lines_shape(a.get(n)) for c, a in calls for n, v in marks(c.input_model).items()):
        return  # lines the step cannot read go back to the model first (validation); accounting follows their retry
    first = scopes[lead] if lead is not None else scopes[0]
    ws = words(text)
    supplier_names = list(dict.fromkeys(a[n] for c, a in calls for n, v in marks(c.input_model).items()
                                        if v["kind"] == "supplier" and isinstance(a.get(n), str)))
    supplier_spans = bounded_names(ws, supplier_names, item=False, bounded=False, breaks=punct_after(text))
    supplier_ranges = [r for f in supplier_spans for r in consumed_ranges(text, f.name, runs=[f])]
    custs = customers(ctx)
    separators = {f.end for f in bounded_names(ws, [r["name"] for r in custs], item=False, bounded=False)}
    bad = unclear_numerals(mask_money_spans(text, supplier_ranges), separators)
    if bad:
        raise session.ask(f"{first}figure", _say("numeral_unclear", figure=bad[0].strip()), "figure_ambiguous", [], [])
    lined = any(v["kind"] == "lines" for c, _ in calls for v in marks(c.input_model).values())
    items = _items(ctx) if lined or any(v["kind"] == "figure" for c, _ in calls for v in marks(c.input_model).values()) else []
    outgoing = []
    for c, a in calls:
        marked = marks(c.input_model)
        for n, v in marked.items():
            if not v.get("outgoing"):
                continue
            if n == "paid_from" and "settlement" in marked and \
                    a.get("settlement", c.input_model.model_fields["settlement"].default) != "paid_now":
                continue
            outgoing.append((c, a))
            break
    amount = next((a.get(n) for c, a in outgoing for n, v in marks(c.input_model).items() if v["kind"] == "figure"), None)
    incoming = any(c.name in {"reverse_payment", "cancel_invoice"} or
                   any(v["kind"] == "account" and not v.get("outgoing") for v in marks(c.input_model).values())
                   for c, _ in calls)
    receipt = any((c.name == "record_goods_received" or any(v["kind"] == "supplier" for v in marks(c.input_model).values()))
                  and any(v["kind"] == "lines" for v in marks(c.input_model).values()) for c, _ in calls)
    occs = inventory(ctx, text, items, supplier_spans, outgoing=bool(outgoing), amount=amount, incoming=incoming,
                     receipt=receipt)
    if outgoing:
        for o in occs:
            if o.kind == "document" and o.start and ws[o.start - 1] == "pay" and _decimal(o.said) == _decimal(amount):
                o.kind, o.value = "figure", _decimal(amount)
    claimed: set[int] = set()
    for index, start, end, value in superseded_rates:
        if index < len(occs):
            occurrence = occs[index]
            if (occurrence.kind, occurrence.start, occurrence.end, _rate_figure(occurrence.said)) == (
                    "rate", start, end, value):
                claimed.add(index)

    if not lined:
        # A completed name resolution survives a pure confirmation, including when the next call has no
        # item lines. Settle only the original consumed span; a later mention remains its own occurrence.
        for msg in session.owner_messages:
            if not msg.get("completed"):
                continue
            offset = text.find(msg["text"])
            if offset < 0:
                continue
            for span in msg.get("spans", []):
                if span["kind"] != "item" or span.get("classification") == "instruction":
                    continue
                a = _word_at(text, offset + span["start"])
                b = _word_at(text, offset + span["end"] - 1) + 1
                for n, o in enumerate(occs):
                    if o.kind == "item" and (o.start, o.end) == (a, b) and o.value == span["name"]:
                        claimed.add(n)

    def claim(kind: str, test) -> Occurrence | None:
        for n, o in enumerate(occs):
            if n not in claimed and o.kind == kind and test(o):
                claimed.add(n)
                return o
        return None

    def mentions(name: str, o: Occurrence) -> bool:
        if o.value is not None:
            return o.value == name
        return o.said in _core(name) or any(same_word(o.said, w) for w in _core(name))

    def settled_occurrence(o: Occurrence, settled: dict) -> bool:
        if settled.get("chars"):
            return _occurrence_ranges(text, [o])[0] in settled["chars"]
        return [o.start, o.end] in settled.get("spans", [])

    lines_of: dict[int, str] = {}  # an item occurrence a line claimed, and that line's item
    for k, (contract, args) in enumerate(calls):
        mk = marks(contract.input_model)
        flagged = [n for n, v in mk.items() if v["kind"] == "flag" and n in args]
        for name, v in mk.items():
            val = args.get(name)
            if v["kind"] == "supplier" and val:
                while claim("supplier", lambda o, val=val: o.value == val):
                    pass
            elif v["kind"] == "customer" and val:
                # Loose words of the resolved name are placed; complete references still claim one mention.
                while claim("customer", lambda o, val=val: o.value is None and o.said in words(val)):
                    pass
                claim("customer", lambda o, val=val: mentions(val, o))
                confirmed_mentions = sum(s["kind"] == "customer" and s["name"] == val and
                                         s.get("classification") == "name"
                                         for msg in session.owner_messages for s in msg["spans"])
                for _ in range(max(0, confirmed_mentions - 1)):
                    claim("customer", lambda o, val=val: mentions(val, o))
            elif v["kind"] == "document" and val:
                claim("document", lambda o, val=val: _same_document(o.said, val) or o.said == str(val))
                party = v.get("party")
                if party:  # a mention of the document's own customer is the document's; another customer's is not
                    who = party(ctx, val)
                    claim("customer", lambda o, who=who: mentions(who, o))
            elif v["kind"] == "figure" and val is not None:  # "at 80": a rate phrase, the figure's own
                if not claim("figure", lambda o, val=val: o.value == _decimal(val)):
                    rate = claim("rate", lambda o, val=val: _rate_figure(o.said) == _decimal(val))
                    if rate is None and any(mark["kind"] == "account" for mark in mk.values()):
                        # A number inside the paying account cannot also supply the payment amount.
                        raise session.ask(f"{scopes[k]}{name}", _say("amount_missing"), "amount_missing", [], [])
                if contract.name == "add_item" and name == "price":
                    while claim("rate", lambda o, val=val: _rate_figure(o.said) == _decimal(val)):
                        pass
            elif v["kind"] == "date" and val:
                claim("date", lambda o: True)
            elif v["kind"] == "text" and v.get("words") == "methods" and val:
                path = f"{scopes[k]}{name}"
                settled = session.bindings.get("settled:" + path, {})
                for n, o in enumerate(occs):
                    if o.kind == "method" and settled_occurrence(o, settled):
                        claimed.add(n)
                claim("method", lambda o, val=val: _norm(o.said) == _norm(val))
            elif v["kind"] in ("text", "account") and val:  # what lies inside its own phrase is its own
                if v.get("outgoing") and "cash" in words(val):
                    claim("method", lambda o: o.said.lower() == "cash")
                occurrence_kind = "paid_from" if v.get("outgoing") else "account"
                settled = session.bindings.get("settled:" + f"{scopes[k]}{name}", {})
                for n, o in enumerate(occs):
                    if o.kind == occurrence_kind and settled_occurrence(o, settled):
                        claimed.add(n)
                own = claim(occurrence_kind, lambda o, val=val: _norm(o.said) == _norm(val)) if v["kind"] == "account" else None
                span = (own.start, own.end) if own else _written_span(ws, val)
                if span:
                    a, b = span
                    for n, o in enumerate(occs):
                        if n not in claimed and ((a <= o.start and o.end <= b) or
                                                 (o.kind == "memo" and (o.start + 1, o.end) == (a, b))):
                            claimed.add(n)
                    if name == "email" and "@" in str(val) and any(
                            o.kind == "email" and a <= o.start and o.end <= b for o in occs):
                        # The immediately preceding contact label describes this address.
                        for n, o in enumerate(occs):
                            if o.kind == "email" and o.end == a and o.said.lower() == "email":
                                claimed.add(n)
            elif v["kind"] == "lines" and _lines_shape(val):
                lm = marks(_line_model(contract.input_model, name))
                im = next((n for n, fv in lm.items() if fv["kind"] == "item"), None)
                qm = next((n for n, fv in lm.items() if fv["kind"] == "quantity"), None)
                wm = next((n for n, fv in lm.items() if fv["kind"] == "warehouse"), None)
                # A choice between written alternatives settles the other offered names too. It does
                # not settle additional lines or a later, separate occurrence of a name.
                alternatives = _item_alternatives(ws, items)
                for line_index, line in enumerate(val or []):
                    path = f"{scopes[k]}{name}.{line_index}.{im}"
                    chosen = line.get(im)
                    if session.bindings.get(path) != chosen or chosen not in {f.name for f in alternatives}:
                        continue
                    for other in alternatives:
                        if other.name != chosen:
                            excluded = claim("item", lambda o, other=other: o.value == other.name and
                                             (o.start, o.end) == (other.start, other.end))
                            if excluded:
                                def plain_quantity(o):
                                    return not any(_marks_at(o.said, start, end)
                                                   for _, start, end in figures(o.said))
                                claim("figure", lambda o: o.end == excluded.start and plain_quantity(o))
                                number = claim("number", lambda o: o.end == excluded.start)
                                if number:
                                    claim("figure", lambda o: o.end == number.start and plain_quantity(o))
                for line_index, li in enumerate(val or []):
                    it = claim("item", lambda o, li=li: mentions(li.get(im), o))
                    item_path = f"{scopes[k]}{name}.{line_index}.{im}"
                    if it is None and li.get(im):
                        picked = chosen_item_span(text, items, session, item_path, li[im], occs)
                        if picked:
                            it = Occurrence("item", picked.start, picked.end,
                                            _as_written(text, (picked.start, picked.end)), picked.name)
                            occs.append(it)
                            claimed.add(len(occs) - 1)
                    warehouse_path = f"{scopes[k]}{name}.{line_index}.{wm}"
                    if wm and (li.get(wm) is not None or session.bindings.get(warehouse_path) is False):
                        bound = warehouse_path in session.bindings
                        claim("warehouse", lambda o, li=li, it=it, bound=bound:
                              o.value["item"] == li.get(im) and it is not None and
                              o.value["item_start"] == it.start and (bound or _norm(o.said) == _norm(li[wm])))
                    q = _decimal(li.get(qm)) if qm else None
                    if q is None:
                        continue
                    if it is None:  # an answered or default item: the quantity still needs the owner's own figure
                        if not claim("figure", lambda o: o.value == q and not _before_an_item(o, occs)):
                            raise session.ask(f"{scopes[k]}{name}", _say("quantity_missing", plural=_plural(li.get(im))),
                                              "quantity_missing", [], [])
                        continue
                    lines_of[it.start] = li.get(im)
                    places = {it.start}  # the quantity written just before its own item (or its unit and "of")
                    if it.start >= 2 and ws[it.start - 1] == "of":
                        places.add(it.start - 2)
                    there = [n for n, o in enumerate(occs) if o.kind in ("figure", "number") and o.end in places]
                    if there:
                        o = occs[there[0]]
                        scale = o.kind == "number" and o.value is not None and any(
                            p.kind == "figure" and p.end == o.start for p in occs)
                        lead_fig = next((p for p in occs if p.kind == "figure" and p.end == o.start), None) if scale \
                            else None
                        if there[0] not in claimed and o.value == q:
                            claimed.add(there[0])
                            continue
                        if lead_fig and lead_fig.value * o.value == q and there[0] not in claimed \
                                and occs.index(lead_fig) not in claimed:
                            claimed.update({there[0], occs.index(lead_fig)})  # "2 dozen"
                            continue
                        raise session.ask(f"{scopes[k]}{name}", _say("quantity_missing", plural=_plural(li.get(im))),
                                          "quantity_missing", [], [])
                    if not claim("figure", lambda o: o.value == q and not _before_an_item(o, occs)):
                        raise session.ask(f"{scopes[k]}{name}", _say("quantity_missing", plural=_plural(li.get(im))),
                                          "quantity_missing", [], [])
        if flagged:
            same = sum(1 for c, _ in calls if c.name == contract.name)
            if same > 1 and any(o.kind == "draft" for o in occs):
                raise session.ask(f"{scopes[k]}{flagged[0]}", _say("which_draft"), "draft_ambiguous", [], [])
            while claim("draft", lambda o: True):
                pass
        if k == lead:  # the rows the reply emits claim their phrases
            for e in results[k].not_done:
                kind = {"email": "email", "date": "date", "rate": "rate", "memo": "memo"}.get(e.get("part"))
                while kind and claim(kind, lambda o: True):
                    pass
    if any(v["kind"] == "lines" for c, _ in calls for v in marks(c.input_model).values()):
        while claim("date", lambda o: o.value == "today"):
            pass  # a new invoice is dated today: "today" restates that default (a submit keeps its own date: asked)
    price = {r["name"]: Decimal(r["standard_rate"]) for r in items}

    def own_price(o: Occurrence) -> bool:  # the rate's own item, the nearest a line claimed before it
        before = [p for p in lines_of if p < o.start]
        return bool(before) and _rate_figure(o.said) == price.get(lines_of[max(before)])
    while claim("rate", own_price):
        pass  # a rate equal to its own item's list price restates the default the card uses
    left = [o for n, o in enumerate(occs) if n not in claimed]
    memos = [o for o in left if o.kind == "memo"]
    if len({o.value for o in memos}) > 1:
        # Preserve the existing single question and its complete same-sentence wording.
        first_memo = memos[0]
        first_memo.said = " ".join(o.said for o in memos)
    rank = {"item": 0, "customer": 1, "document": 2}  # the question about a record first, then the rest
    for o in sorted(left, key=lambda o: (rank.get(o.kind, 3), o.start)):
        _unclaimed(session, f"{first}{o.kind}", o, items, custs)



def _rate_figure(said: str) -> Decimal | None:
    found = figures(said)
    return found[0][0] if found else None



def _before_an_item(o: Occurrence, occs: list[Occurrence]) -> bool:
    """A figure written just before an item's words is that item's quantity, never another line's."""
    return any(p.kind == "item" and p.start == o.end for p in occs)
