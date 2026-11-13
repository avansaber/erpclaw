"""Resolution policy."""
from __future__ import annotations

import re
import secrets
from datetime import date
from erpclaw_core.approvals import takes_no_date
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.masters import _norm
from erpclaw_core.money import fmt_rate
from erpclaw_core.plugins import call_service
from .resolve_text import MESSAGE_BREAK, _memo_phrases, _say, table
from .resolve_words import _has_run, _outside, _raw_in, bounded_names, consumed_ranges, punct_after, without_names, words
from .resolve_types import Found, Occurrence, _items, marks
from .resolve_amounts import CODE_WORDS, _decimal, _marks_at, _plural, mask_money_spans, money_area
from .resolve_state import Planned, Session
from .resolve_dates import date_phrases
from .resolve_records import _resolve_record
from .resolve_warehouse import warehouse_words
from .resolve_inventory import inventory


def _due_date_asked(ws: list[str], text: str, today: date) -> bool:
    """ "due" followed by a date phrase or "in N days", or "net N": a due date the card would write differently."""
    t = table()
    for i, w in enumerate(ws):
        rest = ws[i + 1:i + 5]
        # Hyphenated terms (net-60) become one word; resolved names are already excluded.
        if re.fullmatch(re.escape(t["net"]) + r"\d+", w):
            return True
        if w == t["net"] and rest and rest[0].isdigit():
            return True
        if w != t["due"] or not rest:
            continue
        if rest[0] == t["in_days"][0] and len(rest) > 2 and rest[1].isdigit() and rest[2].rstrip("s") == "day":
            return True
        after = " ".join(rest)
        try:
            if date_phrases(after, today) or re.match(r"(on |by )?\d", after):
                return True
        except Refusal:
            return True  # a date the engine cannot read is still a date asked for
    return False



def record_message_names(ctx: Ctx, session: Session, custs: list[dict], items: list[dict],
                         customer_arg=None, item_args: list | None = None, resolution_text: str | None = None) -> None:
    """Resolve name occurrences for each unfinished original message, never rewriting an earlier result."""
    warehouses = None
    for msg in session.owner_messages:
        if msg.get("completed"):
            continue
        text = msg["text"]
        msg["spans"] = []
        money_answer = str(msg.get("field") or "").startswith("money_span:")
        if confirmation_choice_message(msg):
            msg["completed"] = True
            continue
        groups = [("customer", custs, False), ("item", items, True)]
        sources = warehouse_words(text, items, custs, ctx)
        if sources or str(msg.get("field") or "").endswith("warehouse"):
            if warehouses is None:
                try:
                    warehouses = call_service(ctx, "assistant", "stock.list_warehouses", {})["warehouses"]
                except Refusal as rf:
                    if rf.code != "plugin_not_enabled":
                        raise
                    warehouses = []
            groups.append(("warehouse", warehouses, False))
        for kind, rows, is_item in groups:
            # Choice answers consume only the chosen reference, even if another stored name occurs beside it.
            chosen = msg.get("answer_ref") if not money_answer else None
            choice_answer = msg.get("question_id") and not money_answer
            if choice_answer and (chosen is None or not str(msg.get("field") or "").endswith(kind)):
                continue
            if choice_answer:
                consumed = [chosen] if chosen is not None else []
            elif kind == "warehouse":
                consumed = []
                for source in sources:
                    matches = [r["name"] for r in rows if _norm(r["name"]) == _norm(source.said) or
                               (_norm(r["name"]) == "the " + _norm(source.said) and _raw_in(text, r["name"]))]
                    if len(matches) == 1:
                        consumed.extend(matches)
            else:
                consumed = []
                arguments = item_args if is_item else [customer_arg]
                for argument in arguments or []:
                    try:
                        resolving = resolution_text if resolution_text is not None else text
                        if kind == "customer":
                            source_spans = [Found(p.said, p.start, p.end)
                                            for p in warehouse_words(resolving, items, custs, ctx)]
                            resolving = without_names(resolving, source_spans)
                        consumed.append(_resolve_record(rows, kind, resolving, argument, Session(), kind,
                                                        item=is_item, peers=arguments))
                    except Refusal as rf:
                        # A full name awaiting confirmation can use the same choice to establish its meaning.
                        if rf.data.get("confirm"):
                            consumed.extend(n for n in rf.options if any(r["name"] == n for r in rows)
                                            and _raw_in(text, n))
            found = bounded_names(words(text), [r["name"] for r in rows], item=is_item,
                                  bounded=not choice_answer and kind != "warehouse",
                                  breaks=punct_after(text))
            found = [f for f in found if f.name in consumed]
            for name in set(consumed):
                if isinstance(name, str) and money_area(name) and not any(f.name == name for f in found):
                    found.extend(bounded_names(words(text), [name], item=is_item, bounded=False))
            for f in found:
                if chosen is not None and f.name != chosen:
                    continue
                for start, end in consumed_ranges(text, f.name, item=is_item, runs=[f]):
                    if kind == "warehouse" and not choice_answer and not any(
                            p.start <= len(words(text[:start])) < p.end for p in sources):
                        continue
                    if any(s["kind"] == kind and s["start"] == start and s["end"] == end for s in msg["spans"]):
                        continue
                    row = next(r for r in rows if r["name"] == f.name)
                    area = money_area(text[start:end])
                    msg["spans"].append({"id": secrets.token_hex(16), "start": start, "end": end,
                                         "kind": kind, "record_id": row["warehouse"] if kind == "warehouse" else row["id"],
                                         "name": row["name"],
                                         "area": area, "question": None, "classification": None})
        msg["spans"].sort(key=lambda s: (s["start"], s["end"]))
        if session.pending and not session.pending["choices"] and msg is session.owner_messages[-1]:
            path = session.pending["field"]
            if any(path.endswith(s["kind"]) for s in msg["spans"]):
                session.record_free_answer(path)
        msg["completed"] = True



def confirmation_choice_message(msg: dict) -> bool:
    """Only the choice label itself (or an empty coded answer) is consumed as confirmation text."""
    return str(msg.get("field") or "").startswith("money_span:") and (
        not msg["text"].strip() or msg["text"].strip().casefold() == str(msg.get("choice_label") or "").casefold())


def guard_messages(session: Session) -> None:
    """Read only recorded ranges and original text, including every answer in the request."""
    for msg in session.owner_messages:
        if confirmation_choice_message(msg):
            continue
        names = [(s["start"], s["end"]) for s in msg["spans"]
                 if s.get("classification") != "instruction"]
        area = money_area(msg["text"], names)
        if area:
            raise Planned(_say("tax" if area == "tax" else "discount"),
                          "planned.tax_engine" if area == "tax" else "planned.discounts")
    for msg in session.owner_messages:
        for span in msg["spans"]:
            if not span.get("area") or span.get("classification"):
                continue
            if span.get("question"):
                pending = span["question"]
                session.pending = pending
                raise Refusal(pending["text"], "money_name_ambiguous", [c["label"] for c in pending["choices"]],
                              field=pending["field"], question_id=pending["id"], span_id=span["id"],
                              choices=[{k: c[k] for k in ("code", "label")} for c in pending["choices"]])
            rf = session.ask(f"money_span:{span['id']}", f"Do you mean the {span['kind']} {span['name']}?",
                             "money_name_ambiguous", [span["name"], "no, I mean tax or a discount"],
                             ["name", "instruction"], span_id=span["id"])
            session.pending.update(span_id=span["id"], text=rf.message)
            span["question"] = dict(session.pending)
            raise rf



def planned_money(text: str, spans: list[Found], today: date, *, check_money: bool = True) -> None:
    """The parts that would make the card write something other than what the owner asked (amounts, dates):
    the whole request gets the planned reply. Words inside a resolved name never count."""
    t = table()
    names = [r for f in spans for r in consumed_ranges(text, f.name, item=True, runs=[f])]
    outside = [w for k, w in enumerate(words(text)) if not any(f.start <= k < f.end for f in spans)]
    if check_money:
        area = money_area(text, names)
        if area:
            raise Planned(_say("tax" if area == "tax" else "discount"),
                          "planned.tax_engine" if area == "tax" else "planned.discounts")
    if any(_has_run(outside, r) for r in t["terms_words"]) or _due_date_asked(outside, text, today):
        raise Planned(_say("terms"), "planned.payment_terms")



def planned_stock(text: str, spans: list[Found]) -> None:
    if _outside(words(text), spans) & table()["stock"]:
        raise Planned(_say("stock"), "planned.stock")



NOT_DONE = {"email": "planned.messaging", "rate": "planned.invoice_fields", "date": "planned.invoice_fields",
            "memo": "planned.invoice_fields"}



def _date_phrase(text: str, today: date, d: date) -> str | None:
    """The owner's own words that read as the date `d`: the shortest run of their words that reads exactly that date,
    taking in a qualifier beside it while the reading stays the same; cut from the text as written."""
    t = table()
    toks = [(mt.start(), mt.end()) for mt in re.finditer(r"\S+", text)]

    def reads(a: int, b: int) -> bool:
        try:
            got = date_phrases(text[toks[a][0]:toks[b - 1][1]], today)
        except Refusal:
            return False
        return len(got) == 1 and d in got[0]
    joinable = t["date_touch"] | t["weekday_ahead"] | t["weekday_behind"] | {"on", "the"}
    for size in range(1, min(7, len(toks)) + 1):
        for a in range(len(toks) - size + 1):
            b = a + size
            if not reads(a, b):
                continue
            while a and _norm(text[toks[a - 1][0]:toks[a - 1][1]]) in joinable and reads(a - 1, b):
                a -= 1
            while b < len(toks) and _norm(text[toks[b][0]:toks[b][1]]) in joinable and reads(a, b + 1):
                b += 1
            return re.sub(r"[,.;:!?]+$", "", text[toks[a][0]:toks[b - 1][1]])
    return None



def _payment_date_text(text: str, occurrences: list[Occurrence]) -> str:
    """The date reader and its card citation see the same text outside other owned fields."""
    tokens = [mt for mt in re.finditer(r"\S+", text) if words(mt.group())]
    ranges = [(tokens[o.start].start(), tokens[o.end - 1].end()) for o in occurrences
              if o.kind in {"method", "account", "paid_from", "item", "supplier", "customer", "warehouse", "document"}]
    ranges.extend((tokens[o.start - 1].start(), tokens[o.start - 1].end()) for o in occurrences
                  if o.kind == "document" and o.start and words(tokens[o.start - 1].group()) == ["on"])
    return mask_money_spans(text, ranges)



def date_source_of(ctx: Ctx, contract, inp, text: str) -> tuple[dict, list[dict]]:
    """A drafted card's date source, from what the step read, and any row said beside the card: "not applicable" for
    a write that takes no date (with a row when the owner wrote one anyway, never silently ignored); the owner's
    phrase for a date read from their words; otherwise the default."""
    marked = marks(contract.input_model)
    if any(v["kind"] == "account" for v in marked.values()):
        suppliers = [getattr(inp, n) for n, v in marked.items() if v["kind"] == "supplier" and
                     isinstance(getattr(inp, n, None), str)]
        supplier_spans = bounded_names(words(text), suppliers, item=False, bounded=False, breaks=punct_after(text))
        text = _payment_date_text(text, inventory(ctx, text, _items(ctx), supplier_spans,
            outgoing=any(v.get("outgoing") for v in marked.values()),
            incoming=any(v["kind"] == "account" and not v.get("outgoing") for v in marked.values()),
            amount=next((getattr(inp, n, None) for n, v in marked.items() if v["kind"] == "figure"), None)))
    if takes_no_date(ctx, inp):
        try:
            written = bool(date_phrases(text, ctx.today))
        except Refusal:
            written = True
        rows = [{"type": "not_done", "part": "date", "capability": NOT_DONE["date"], "text": _say("nd_no_date")}]
        return {"not_applicable": True}, rows if written else []
    d = getattr(inp, "date", None)
    phrase = _date_phrase(text, ctx.today, d) if d is not None else None
    if phrase:
        return {"phrase": phrase, "date": d.isoformat()}, []
    return {"default": True}, []



def memo_qualifier(text: str) -> tuple[str, str]:
    """Display label and notice key for the latest supported memo occurrence, without changing field resolution.

    A language-table cue can identify an explicit qualifier inside that occurrence, without an earlier
    occurrence renaming a later one.
    """
    t = table()
    mentions = list(_memo_phrases(text))
    kind = t["memo_qualifier_default"]
    if mentions:
        start, end, _ = mentions[-1]
        occurrence = text.lower()[start:end]
        kind = re.match(r"\w+", occurrence).group(0)
        kind = next((value for pattern, value in t["memo_qualifier_cues"] if re.match(pattern, occurrence)), kind)
    return t["memo_qualifiers"][kind]


def invoice_date_text(text: str) -> str:
    """Exclude memo content, keeping quoted boundaries and later owner messages."""
    ranges = []
    pairs = {'"': '"', "'": "'", '“': '”', '‘': '’'}
    for start, end, _ in _memo_phrases(text):
        if any(a <= start < b for a, b in ranges):
            continue
        cue = re.match(table()["memo"], text[start:], re.I)
        if cue is None:
            continue
        value = start + cue.end() - 1
        while value < end and text[value].isspace():
            value += 1
        if value < end and text[value] in pairs:
            boundary = re.search(r"\b" + re.escape(MESSAGE_BREAK) + r"\b", text[value + 1:])
            limit = value + 1 + boundary.start() if boundary else len(text)
            close = next((i for i in range(value + 1, limit) if text[i] == pairs[text[value]] and not (
                text[i] in {"'", '’'} and i + 1 < len(text) and text[i - 1].isalnum() and text[i + 1].isalnum())), -1)
            stop = close + 1 if close >= 0 else value
        else:
            boundary = re.search(r"\b" + re.escape(MESSAGE_BREAK) + r"\b", text[value:])
            stop = value + boundary.start() if boundary else len(text)
        if stop > value:
            ranges.append((value, stop))
    return mask_money_spans(text, ranges)



def unquoted_memo_date(text: str, today: date, date_order: str | None = None) -> str | None:
    """Find the last date-bearing portion of an unquoted memo, within its owner message."""
    pairs = {'"', "'", '“', '‘'}
    found = None
    quoted_until = 0
    for start, end, _ in _memo_phrases(text):
        if start < quoted_until:
            continue
        cue = re.match(table()["memo"], text[start:], re.I)
        if not cue:
            continue
        value_start = start + cue.end() - 1
        value = text[value_start:end].strip()
        if not value:
            continue
        if value[0] in pairs:
            opening = value_start + len(text[value_start:end]) - len(text[value_start:end].lstrip())
            closing = {'"': '"', "'": "'", '“': '”', '‘': '’'}[value[0]]
            boundary = re.search(r"\b" + re.escape(MESSAGE_BREAK) + r"\b", text[opening + 1:])
            limit = opening + 1 + boundary.start() if boundary else len(text)
            close = next((i for i in range(opening + 1, limit) if text[i] == closing and not (
                closing in {"'", '’'} and i + 1 < limit and text[i - 1].isalnum() and text[i + 1].isalnum())), -1)
            quoted_until = close + 1 if close >= 0 else opening + 1
            continue
        boundary = re.search(r"\b" + re.escape(MESSAGE_BREAK) + r"\b", text[value_start:])
        stop = value_start + boundary.start() if boundary else len(text)
        value = text[value_start:stop]
        # A later quoted memo retains its own boundary even after an unquoted cue.
        for inner_start, inner_end, _ in _memo_phrases(value):
            inner_cue = re.match(table()["memo"], value[inner_start:], re.I)
            inner_value = inner_start + inner_cue.end() - 1 if inner_cue else inner_end
            if inner_value < len(value) and value[inner_value] in pairs:
                closing = {'"': '"', "'": "'", '“': '”', '‘': '’'}[value[inner_value]]
                close = next((i for i in range(inner_value + 1, len(value)) if value[i] == closing and not (
                    closing in {"'", '’'} and i + 1 < len(value) and
                    value[i - 1].isalnum() and value[i + 1].isalnum())), -1)
                if close >= 0:
                    value = value[:inner_value] + " " * (close + 1 - inner_value) + value[close + 1:]
        for portion in re.split(r"[,;!?]|(?<!\d)\.(?!\d)", value):
            try:
                has_date = bool(date_phrases(portion, today, date_order))
            except Refusal:
                has_date = True
            if has_date:
                found = portion.strip()
    return found


def invoice_not_done(ctx: Ctx, text: str, spans: list[Found], lines: list[dict], items: list[dict],
                     emailed: bool, *, reads_date: bool = False, chosen_items: list[Found] | None = None,
                     date_text: str | None = None, carry_rates: bool = False, invoice_rate_context: bool = False) -> list[dict]:
    """Parts of an invoice request that change nothing written: one event each, beside the card."""
    t, out = table(), []
    instruction_text = text
    low = instruction_text.lower()
    if emailed or _outside(words(instruction_text), spans) & t["email"]:
        out.append({"type": "not_done", "part": "email", "capability": NOT_DONE["email"], "text": _say("nd_email")})
    rates = {r["name"]: r["standard_rate"] for r in items}
    for compact in re.finditer(t["compact_currency_rate"] if invoice_rate_context else r"(?!)", instruction_text, re.I):
        for token, names in _marks_at(instruction_text, *compact.span(1)):
            if names is None or ctx.currency not in names:
                theirs = token if names is None or len(names) != 1 else next(iter(names))
                raise Refusal(_say("foreign_currency", ours=ctx.currency, theirs=theirs), "foreign_currency",
                              capability="planned.multi_currency")
    percentages = list(re.finditer(t["percent_figure"], low))
    if carry_rates and re.search(t["percent_price"], low):
        out.append({"type": "rate_unread", "part": "rate"})
    for mt in re.finditer(t["invoice_rate"] if invoice_rate_context else t["rate"], low):
        number_group = 1 if mt.group(1) else 2
        start, end = mt.span(number_group)
        suffix_word = re.match(r"\s*([a-z]{3})\b", instruction_text[end:], re.I)
        if invoice_rate_context and suffix_word and suffix_word.group(1).upper() in CODE_WORDS:
            if carry_rates:
                out.append({"type": "rate_unread", "part": "rate"})
            continue
        if any(p.start() <= start < p.end() for p in percentages):
            if carry_rates and not any(e.get("type") == "rate_unread" for e in out):
                out.append({"type": "rate_unread", "part": "rate"})
            continue
        currency_marks = _marks_at(instruction_text, start, end)
        # A distant currency can be separated from the figure by other words or figures.
        # Conservatively refuse an incompatible mark within the same owner message.
        tail = instruction_text[end:].split(MESSAGE_BREAK, 1)[0]
        for token in re.findall(r"[A-Za-z]+|[^\w\s]", tail):
            currency_marks.extend(_marks_at("0 " + token, 0, 1))
        for token, names in currency_marks:
            if names is None or ctx.currency not in names:
                theirs = token if names is None or len(names) != 1 else next(iter(names))
                raise Refusal(_say("foreign_currency", ours=ctx.currency, theirs=theirs), "foreign_currency",
                              capability="planned.multi_currency")
        said = _decimal(mt.group(1) or mt.group(2))
        if carry_rates and re.search(r"-\s*$", instruction_text[max(0, start - 8):start]):
            out.append({"type": "rate_unread", "part": "rate"})
            continue
        before = words(text[:mt.start()])
        found = bounded_names(before, [r["name"] for r in items], item=True,  # every stored item: the guard sees all
                              breaks=punct_after(text[:mt.start()]))
        found += [f for f in chosen_items or [] if f.end <= len(before)]
        ends = {ln["item"]: max((f.end for f in found if f.name == ln["item"]), default=-1) for ln in lines}
        named = [n for n, e in ends.items() if e >= 0]
        if said is None or not named:
            if carry_rates:
                out.append({"type": "rate_unread", "part": "rate"})
            continue
        name = max(named, key=lambda n: ends[n])  # the nearest item named before the rate
        if carry_rates:
            memo = next(((a, b, value) for a, b, value in _memo_phrases(instruction_text)
                         if a <= mt.start() < b), None)
            # PO references can end at a comma; a subsequent rate remains an instruction.
            in_memo = bool(memo and not (instruction_text[memo[0]:].lower().startswith(t["po_memo_prefixes"]) and
                                        "," in instruction_text[memo[0]:mt.start()]))
            out.append({"type": "rate_candidate", "part": "rate", "item": name,
                        "value": format(said, "f"), "in_memo": in_memo,
                        "message": instruction_text[:mt.start()].count(MESSAGE_BREAK)})
        elif said != rates[name]:
            out.append({"type": "not_done", "part": "rate", "capability": NOT_DONE["rate"], "text": _say(
                "nd_rate", said=fmt_rate(said, ctx.currency), list=fmt_rate(rates[name], ctx.currency),
                item=_plural(name))})
    if not reads_date:
        try:
            dated = any(d != ctx.today for reading in date_phrases(date_text if date_text is not None else invoice_date_text(without_names(instruction_text, spans)), ctx.today)
                        for d in reading)  # today: the default
        except Refusal:
            dated = True
        if dated:
            out.append({"type": "not_done", "part": "date", "capability": NOT_DONE["date"], "text": _say("nd_date")})
    memos = [(start, end) for start, end, _ in _memo_phrases(text)
             if not any(f.start <= len(words(text[:start])) < f.end for f in spans)]
    if memos:
        start, end = memos[-1]
        qualifier, notice = memo_qualifier(text[start:end])
        out.append({"type": "not_done", "part": "memo", "capability": NOT_DONE["memo"],
                    "text": _say(notice, qualifier=qualifier)})
    return out
