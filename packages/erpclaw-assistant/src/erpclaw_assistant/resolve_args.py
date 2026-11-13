"""Resolution args."""
from __future__ import annotations

import re
from dataclasses import field
from typing import Callable
from sqlalchemy import select
from erpclaw_core import models as m
from erpclaw_core.catalogue import planned_instead
from erpclaw_core.common import Ctx, Refusal, is_question
from erpclaw_core.masters import STOP, _norm, customers, same_word
from erpclaw_core.plugins import call_service
from .resolve_text import MESSAGE_BREAK, _memo_phrases, _say, table
from .resolve_words import _as_written, _core, _leaves, _raw_words, _written_span, bounded_names, consumed_ranges, punct_after, without_names, words
from .resolve_types import Found, Occurrence, Result, WarehouseWords, _items, _line_model, _lines_shape, marks, reply_peers
from .resolve_amounts import _amount_marker_ranges, _amount_marker_words, _decimal, _has_amount_marker, _word_at, figures, literal_figure, literal_quantity, mask_money_spans
from .resolve_state import Planned, Session, _one_detail
from .resolve_dates import date_phrases, resolve_date
from .resolve_records import _document, _named_records, _owner_words_taken, resolve_record
from .resolve_payment import guard_payment_names, method_phrases, payer_phrases, payment_name_spans
from .resolve_warehouse import _warehouse, receipt_destination_phrases, warehouse_words
from .resolve_inventory import chosen_item_span, inventory
from .resolve_policy import NOT_DONE, _payment_date_text, guard_messages, invoice_date_text, invoice_not_done, memo_qualifier, unquoted_memo_date, planned_money, planned_stock, record_message_names


SINGLE_FIELDS = ("customer", "date", "document", "account", "paid_from", "method")  # one per write: a later mention replaces every earlier one


# Look up the facade at call time so its outgoing reader remains patchable.
def outgoing_account_phrases(*args, **kwargs):
    from . import resolve

    return resolve.outgoing_account_phrases(*args, **kwargs)



def request_words(ctx: Ctx, messages: list[str], calls: list[tuple[object, dict]]) -> str:
    """The request's words for the resolution step, its messages in order, each ended by MESSAGE_BREAK. The latest
    message that mentions a field supplies it: a customer, a date or a document number in a later message drops every
    earlier mention of its kind; an earlier figure or item drops out when the calls no longer use it and do use a later
    one of its kind. Anything else stays, so a later mention the calls ignore is still the owner's and is asked about."""
    if len(messages) < 2:
        return messages[0] if messages else ""
    items = _items(ctx)
    field_marks = [v for c, _ in calls if c is not None for v in marks(c.input_model).values()]
    outgoing = any(v.get("outgoing") for v in field_marks)
    incoming = any(v["kind"] == "account" and not v.get("outgoing") for v in field_marks) or \
        any(c is not None and c.name in {"reverse_payment", "cancel_invoice"} for c, _ in calls)
    occs = [inventory(ctx, text, items, outgoing=outgoing, incoming=incoming) for text in messages]
    leaves = list(_leaves([a for _, a in calls]))
    figures_used = {d for d in (_decimal(v) for v in leaves if not isinstance(v, bool)) if d is not None}
    names_used = {_norm(str(v)) for v in leaves if isinstance(v, str)}

    def used(o: Occurrence) -> bool:
        if o.kind == "figure":
            return o.value in figures_used
        return _norm(str(o.value or o.said)) in names_used

    kept = []
    for n, text in enumerate(messages):
        later = [o for found in occs[n + 1:] for o in found]
        marked_figures = {_word_at(text, a) for _, a, b in figures(text) if _has_amount_marker(text, a, b)}
        drop: set[int] = set()
        for o in occs[n]:
            same = [x for x in later if x.kind == o.kind]
            if not same:
                continue
            if o.kind in SINGLE_FIELDS or (o.kind in ("figure", "item") and not used(o) and any(used(x) for x in same)):
                if (incoming or outgoing) and o.kind in {"account", "paid_from", "customer", "item"} and \
                        marked_figures.intersection(range(o.start, o.end)):
                    continue  # a record correction cannot settle the money ambiguity on its inner figure
                drop.update(range(o.start, o.end))
        tokens = [mt for mt in re.finditer(r"\S+", text) if words(mt.group())]
        # Superseding any other kind must never erase an amount's original marker, even if
        # a settled name or phrase used the same word span.
        markers = {i for a, b in _amount_marker_ranges(text) for i in range(a, b)} if incoming or outgoing else set()
        ranges = [(i, i + 1) for k in drop for i in range(tokens[k].start(), tokens[k].end())
                  if i not in markers]
        kept.append(mask_money_spans(text, ranges))
    return f" . {MESSAGE_BREAK} ".join(kept)  # a sentence ends with each message, so a memo ends there too



def _payer(cust_rows: list[dict], text: str, v: dict, session: Session, path: str) -> str | None:
    """The payer the owner wrote, for a field whose customer is a payer: each payer phrase resolves from its own words
    alone, and two different payers are the owner's to pick. None when the owner wrote no payer phrase."""
    if not v.get("payer") or path in session.bindings:
        return None
    payers = payer_phrases(text)
    if not payers:
        return None
    said = f" {' '.join(words(text))} "

    def whole(p: str) -> str:  # a stored name the phrase is the end of ("September Labs" for "Labs"), written whole
        full = [r["name"] for r in cust_rows if _norm(r["name"]).endswith(_norm(p)) and f" {_norm(r['name'])} " in said]
        return full[0] if len(full) == 1 else p
    got = list(dict.fromkeys(resolve_record(cust_rows, "customer", whole(p), whole(p), session, path, item=False)
                             for p in payers))
    if len(got) > 1:
        raise _one_detail(path, session, "payers", got, "customer_ambiguous")
    return got[0]



def payment_binding(ctx: Ctx, text: str, session: Session, path: str, kind: str) -> bool:
    """A choice settles offered spans only until a later owner message mentions that same field kind."""
    if path not in session.bindings:
        return False
    settled = session.bindings.get("settled:" + path, {})
    through = settled.get("through")
    answered = next((i for i, msg in reversed(list(enumerate(session.owner_messages)))
                     if (through is not None and msg["id"] == through) or
                     (through is None and msg.get("field") == path)), None)
    if answered is not None:
        later = session.owner_messages[answered + 1:]
        # A choice answer belongs to its pending field, even when its label also names another field kind.
        if any(o.kind == kind for msg in later if not msg.get("answer")
               for o in inventory(ctx, msg["text"], _items(ctx), outgoing=kind == "paid_from", incoming=True)):
            session.bindings.pop(path, None)
            session.bindings.pop("settled:" + path, None)
            return False
    return True



def _outgoing_account(ctx: Ctx, text: str, amount, session: Session, path: str,
                      sources: list[WarehouseWords]) -> str:
    accounts = [dict(r) for r in ctx.conn.execute(select(m.account).where(
        m.account.c.company_id == ctx.company_id, m.account.c.account_type == "bank",
        m.account.c.is_group.is_(False)).order_by(m.account.c.number)).mappings()]
    names = [r["name"] for r in accounts]
    labels = [f"{r['number']} {r['name']}" for r in accounts]
    offered = [o for o in inventory(ctx, text, _items(ctx), outgoing=True, amount=amount, incoming=True)
               if o.kind == "paid_from"]
    session.offer(path, "paid_from", text, offered)

    def ask(said: str | None = None, code: str = "account_ambiguous"):
        question = f"Which account should the money come from? {', '.join(labels)}"
        if said:
            question = f"Which account did you mean by '{said}'? {', '.join(labels)}"
        rf = session.ask(path, question, code, labels, names, occurrences=offered)
        session.pending["payment_kind"] = "paid_from"
        return rf

    if payment_binding(ctx, text, session, path, "paid_from"):
        if session.bindings[path]:
            return session.bindings[path]
        raise ask()
    parties = [dict(r) for r in ctx.conn.execute(select(m.party).where(
        m.party.c.company_id == ctx.company_id, m.party.c.kind.in_(("supplier", "customer")))).mappings()]
    protected = bounded_names(words(text), [r["name"] for r in parties], item=False, bounded=False)
    said = []
    unknown = []
    phrases = outgoing_account_phrases(text, accounts, amount, ctx.today)
    marker_words = _amount_marker_words(text)
    for phrase in phrases:
        if marker_words.intersection(range(phrase.start, phrase.end)):
            continue
        # A supplier/customer name or an item's stock source owns its words before account candidacy.
        if any(f.start <= phrase.start < f.end for f in protected) or \
                any(s.start <= phrase.start < s.end for s in sources):
            continue
        value = _as_written(text, (phrase.start, phrase.end))
        try:
            resolve_record(parties, "supplier", value, value, Session(), path, item=False)
        except Refusal:
            pass
        else:
            continue
        if phrase.unknown_extension:
            unknown.append(value)
        if value not in said:
            said.append(value)
    # Method words inside a supplier, warehouse or account name are data, not a cash instruction.
    reserved = protected + sources + phrases
    cash_text = " ".join("name" if any(p.start <= i < p.end for p in reserved) else word
                         for i, word in enumerate(_raw_words(text)))
    if "cash" in [p.lower() for p in method_phrases(cash_text)]:
        cash = [r["name"] for r in accounts if "cash" in words(r["name"])]
        if not cash:
            # The add-account card for a missing cash account comes with a later change; until then it cannot post.
            raise ask("cash (there is no cash account yet)", "cash_account_missing")
        for value in cash:
            if value not in said:
                said.append(value)
    if len(said) > 1:
        rf = _one_detail(path, session, "accounts", said, "account_ambiguous",
                         offered)
        session.pending["payment_kind"] = "paid_from"
        raise rf
    if unknown:
        raise ask(unknown[0], "account_unknown")
    if said:
        return said[0]
    raise ask()



def resolve_args(ctx: Ctx, contract, args: dict, text: str, session: Session, peers: dict | None = None,
                 scope: str = "", *, mixed: bool | None = None) -> Result:
    """The call's arguments made canonical from the owner's words, with the parts not done; or exactly one question
    (a `Refusal` carrying its question id and choices), or the planned reply (`Planned`). `peers` are the reply's
    arguments for customers and items (`reply_peers`); by default, this call's own. `scope` names this write in the
    reply ("create_invoice#0:"): its questions and answers belong to it alone.

    Support: a field the step has no rule for is never taken from the model; it is removed, so the contract's default
    applies, unless the owner's answer to a question bound it."""
    instead = planned_instead(ctx.conn, contract.name, text) if mixed is None \
        else planned_instead(ctx.conn, contract.name, text, mixed=mixed)
    if instead is not None:  # the owner's words name what is planned in its place: its answer, whatever was called
        raise Planned(instead.text, instead.capability)
    mk = marks(contract.input_model)
    if not mk:
        return Result(dict(args))
    peers = reply_peers([(contract, args)]) if peers is None else peers
    out = {k: val for k, val in args.items() if k in mk}  # nothing unmarked is taken from the model
    for key, bound in session.bindings.items():  # an answered contract question binds a field the step leaves alone
        k = key[len(scope):] if scope and key.startswith(scope) else (key if not scope else None)
        if k in contract.input_model.model_fields and k not in mk:
            out[k] = bound
    ws = words(text)
    cust_rows = customers(ctx) if any(v["kind"] == "customer" for v in mk.values()) else []
    supplier_rows = [dict(r) for r in ctx.conn.execute(select(m.party).where(
        m.party.c.company_id == ctx.company_id, m.party.c.kind == "supplier")).mappings()] \
        if any(v["kind"] == "supplier" for v in mk.values()) else []
    lines_field = next((n for n, v in mk.items() if v["kind"] == "lines"), None)
    payment_fields = any(v["kind"] == "account" for v in mk.values())
    item_rows = _items(ctx) if lines_field or payment_fields else []
    record_text = text
    if payment_fields:
        marker_words = _amount_marker_words(text)
        numeric_names = payment_name_spans(text, cust_rows + supplier_rows) + \
            payment_name_spans(text, item_rows, item=True)
        inner = {i for f in numeric_names if figures(f.name) for i in range(f.start, f.end)}
        tokens = [mt for mt in re.finditer(r"\S+", text) if words(mt.group())]
        record_text = mask_money_spans(text, [(tokens[k].start(), tokens[k].end()) for k in marker_words - inner])
    spans = bounded_names(ws, [r["name"] for r in cust_rows], item=False, breaks=punct_after(text)) + \
        bounded_names(ws, [r["name"] for r in item_rows], item=True, breaks=punct_after(text))
    receipt = bool(lines_field) and (contract.name == "record_goods_received" or
                                     any(v["kind"] == "supplier" for v in mk.values()))
    warehouse_reader = receipt_destination_phrases if receipt else warehouse_words
    warehouse_parties = cust_rows + supplier_rows if receipt else cust_rows
    source_spans = [Found(p.said, p.start, p.end) for p in warehouse_reader(text, item_rows, warehouse_parties, ctx)]
    supplier_text = record_text
    if receipt and source_spans:
        tokens = [mt for mt in re.finditer(r"\S+", text) if words(mt.group())]
        # Only the destination phrase owns these words; later supplier mentions remain visible.
        destinations = [(tokens[source.start].start(), tokens[source.end - 1].end()) for source in source_spans]
        # Preserve a field boundary so a trailing date cannot become a supplier-name extension.
        for start, end in reversed(destinations):
            supplier_text = supplier_text[:start] + f" {MESSAGE_BREAK} " + supplier_text[end:]
    spans += source_spans
    supplier_spans: list[Found] = []
    supplier_ranges: list[tuple[int, int]] = []
    customer_text = without_names(text, source_spans) if lines_field else text
    if payment_fields:
        customer_text = mask_money_spans(record_text, [
            r for f in source_spans for r in consumed_ranges(text, f.name, runs=[f])])
    emailed = False
    warehouse_not_done = []
    for name, v in mk.items():  # a switched-off option is removed, never refused for the whole request
        if v["kind"] == "off":
            emailed = bool(out.pop(name, False)) or emailed
    lm = marks(_line_model(contract.input_model, lines_field)) if lines_field else {}
    item_field = next((k for k, v in lm.items() if v["kind"] == "item"), None)
    customer_arg = next((out.get(n) for n, v in mk.items() if v["kind"] == "customer"), None)
    item_args = [line.get(item_field) for line in out.get(lines_field, []) if isinstance(line, dict)] \
        if isinstance(out.get(lines_field), list) else []
    if lines_field and (out.get(lines_field) == [] or (
            out.get(lines_field) is None and mk[lines_field].get("presence") != "optional")):
        item_args = [None]  # the implicit line resolves its item from the owner's words, just like an explicit line
    record_message_names(ctx, session, cust_rows, item_rows, customer_arg, item_args, record_text)
    if lines_field:
        guard_messages(session)
        planned_money(text, spans, ctx.today, check_money=not bool(session.owner_messages))
        # the planned stock reply, before any item question: a stock word, and no item's full name anywhere in
        # the owner's words (bounded or not), so an item question could only steer the owner to a service item
        if not bounded_names(ws, [r["name"] for r in item_rows], item=True, bounded=False):
            planned_stock(text, spans)
    future_invoice_date = False
    invoice_date_input = None
    delivery_date = False
    if contract.name in ("create_invoice", "submit_invoice"):
        # Complete stored names own their calendar words, even when an adjacent date makes the
        # normal name boundary uncertain. Ask unread dates before that uncertainty reaches a line.
        date_names = bounded_names(ws, [r["name"] for r in cust_rows], item=False, bounded=False) + \
            bounded_names(ws, [r["name"] for r in item_rows], item=True, bounded=False)
        date_text = invoice_date_text(without_names(text, date_names + source_spans))
        if contract.name == "create_invoice":
            months = "|".join(sorted(map(re.escape, table()["months"]), key=len, reverse=True))
            delivery_pattern = table()["delivery_date_clause"].replace("<months>", months)
            delivery_spans = [m.span() for m in re.finditer(delivery_pattern, date_text, re.I)]
            delivery_date = bool(delivery_spans)
            date_text = mask_money_spans(date_text, delivery_spans)
            # A delivery clause is not an extension of the item name. Its positions in
            # date_text differ after stored names have been removed.
            record_spans = [m.span() for m in re.finditer(delivery_pattern, record_text, re.I)
                            if not any(_norm(m.group()) in _norm(row["name"]) for row in item_rows)]
            record_text = mask_money_spans(record_text, record_spans)
        memo_date = unquoted_memo_date(without_names(text, date_names + source_spans), ctx.today,
                                       getattr(ctx, "date_order", None) or ctx.extra.get("date_order"))
        choice_path = f"{scope}memo_date"
        # An earlier date retains its existing question priority. Once resolved, classify the memo words.
        try:
            earlier_date = any(day != ctx.today for reading in date_phrases(date_text, ctx.today) for day in reading)
        except Refusal:
            earlier_date = True
        if memo_date and choice_path not in session.bindings and (not earlier_date or f"{scope}date" in session.bindings):
            raise session.ask(choice_path, _say("memo_date_question", said=memo_date), table()["memo_date_choice_code"],
                              [_say("memo_date_part"), _say("memo_date_invoice")], [False, True])
        if memo_date and session.bindings.get(choice_path) is True:
            date_text += " " + memo_date
        invoice_date_input = date_text
        if re.search(table()["negated_invoice_day"], date_text, re.I):
            raise session.ask(f"{scope}date", _say("date_unknown"), "date_unknown", [], [])
        try:
            readings = date_phrases(date_text, ctx.today, getattr(ctx, "date_order", None) or ctx.extra.get("date_order"))
        except Refusal:
            if contract.name == "create_invoice" and not source_spans:
                resolve_date(date_text, ctx.today, session, f"{scope}date")
        else:
            future_invoice_date = any(reading and all(day > ctx.today for day in reading) for reading in readings)
    # a document comes first: a derived field (the payer) is read from it; then the rest in the model's order
    order = [n for n, v in mk.items() if v["kind"] == "document"] + \
        [n for n, v in mk.items() if v.get("presence") == "derived"] + \
        [n for n, v in mk.items() if v["kind"] != "document" and v.get("presence") != "derived"]
    # A resolved supplier owns its name before amounts, dates or customers can use any of its words.
    order = [n for n in order if mk[n]["kind"] == "supplier"] + \
        [n for n in order if mk[n]["kind"] != "supplier"]
    guarded = mk.get("paid_from", {}).get("outgoing") and "settlement" in mk
    if guarded:
        order.remove("settlement")
        order.insert(0, "settlement")
    cust_names = {r["name"] for r in cust_rows}
    owned = None

    def occurrences():
        nonlocal owned
        if owned is None:
            owned = inventory(ctx, text, item_rows, supplier_spans, receipt=receipt,
                              outgoing=any(v.get("outgoing") for v in mk.values()),
                              amount=next((out.get(n) for n, v in mk.items() if v["kind"] == "figure"), None),
                              incoming=contract.name in {"reverse_payment", "cancel_invoice"} or
                              any(v["kind"] == "account" and not v.get("outgoing") for v in mk.values()))
            for o in owned:
                if o.kind in {"customer", "supplier", "item", "account", "paid_from", "warehouse"}:
                    name = o.value if isinstance(o.value, str) else o.said
                    if not consumed_ranges(text, name, runs=[Found(name, o.start, o.end)]):
                        raise session.ask(f"{scope}amount", _say("amount_missing"), "amount_missing", [], [])
            if any(v.get("outgoing") for v in mk.values()) and not any(o.kind == "figure" for o in owned):
                banks = [dict(r) for r in ctx.conn.execute(select(m.account).where(
                    m.account.c.company_id == ctx.company_id, m.account.c.account_type == "bank",
                    m.account.c.is_group.is_(False))).mappings()]
                amount = next((out.get(n) for n, v in mk.items() if v["kind"] == "figure"), None)
                if any(p.unknown_extension for p in outgoing_account_phrases(text, banks, amount, ctx.today)):
                    source = next(n for n, v in mk.items() if v.get("outgoing"))
                    _outgoing_account(ctx, text, amount, session, f"{scope}{source}",
                                      warehouse_words(text, item_rows, cust_rows + supplier_rows, ctx))
            guard_payment_names(text, owned, session, f"{scope}amount")
        return owned
    for name in order:
        v = mk[name]
        kind, presence = v["kind"], v.get("presence", "required")
        path = f"{scope}{name}"
        if kind == "flag":  # true or false only from the owner's words or answer; otherwise the contract's default
            out.pop(name, None)
            if path in session.bindings:
                out[name] = session.bindings[path]
            elif guarded and name == "settlement" and re.search(table()["still_owed"], text.lower()):
                out[name] = "still_owed"
            elif re.search(table()[v["words"]], text.lower()):
                out[name] = v["when"]
            continue
        if presence == "derived":
            # taken from the document only when the owner's words name no customer at all: not exactly, not by any
            # words the model took for one, not by a word of any customer's name ("Acme paid 100 against INV-6" asks),
            # and not by a payer phrase, which is resolved as the field's own words whatever the model passed
            payer = _payer(cust_rows, record_text, v, session, path)
            if payer:
                out[name] = payer
                continue
            named = bool(_named_records(cust_rows, text, [f.name for f in spans if f.name in cust_names])) \
                or path in session.bindings or bool(out.get(name) and _owner_words_taken(ws, text, out.get(name))) \
                or any((set(_norm(r["name"]).split()) - STOP) & set(ws) for r in cust_rows)
            got = None if named else v["derive"](ctx, out)
            out[name] = got if got is not None else resolve_record(cust_rows, "customer", customer_text, out.get(name),
                                                                   session, path, item=False,
                                                                   peers=peers["customer"],
                                                                   alike=peers["customer_calls"].get(contract.name))
        elif kind == "supplier":
            out[name] = resolve_record(supplier_rows, "supplier", supplier_text, out.get(name), session, path, item=False)
            own = bounded_names(ws, [out[name]], item=False, bounded=False, breaks=punct_after(text))
            supplier_spans.extend(own)
            spans.extend(own)
            supplier_ranges.extend(r for f in own for r in consumed_ranges(text, f.name, runs=[f]))
            customer_text = mask_money_spans(record_text, supplier_ranges + [
                r for f in source_spans for r in consumed_ranges(text, f.name, runs=[f])])
        elif kind == "customer":
            payer = _payer(cust_rows, record_text, v, session, path)
            if payer:
                out[name] = payer
                continue
            if presence == "optional" and not out.get(name) and \
                    not _named_records(cust_rows, text, [f.name for f in spans if f.name in cust_names]):
                continue
            out[name] = resolve_record(cust_rows, "customer", customer_text, out.get(name), session, path, item=False,
                                       peers=peers["customer"], alike=peers["customer_calls"].get(contract.name))
        elif kind == "lines":
            if presence == "optional" and out.get(name) is None:
                out.pop(name, None)  # delivery/credit: omitted means the rest/everything, not an empty item.
                continue
            if not _lines_shape(out.get(name)):
                continue  # not a list of objects: validation sends it back to the model
            new = []
            for i, li in enumerate(out.get(name) or [{}]):
                li = {k: val for k, val in (li or {}).items() if k in lm}  # nothing unmarked from the model
                for fname, fv in lm.items():
                    path = f"{scope}{name}.{i}.{fname}"
                    if fv["kind"] == "item":
                        li[fname] = resolve_record(item_rows, "item", record_text, li.get(fname), session, path, item=True,
                                                   peers=peers["item"])
                    elif fv["kind"] == "quantity":
                        unit = next((r.get("unit") for r in item_rows if r["name"] == li[item_field]), None)
                        li[fname] = literal_quantity(li.get(fname), text, li[item_field], session, path, unit,
                                                     names=[r["name"] for r in item_rows])
                    elif fv["kind"] == "warehouse":
                        item = next(r for r in item_rows if r["name"] == li[item_field])
                        # Match repeated names in line order, just as occurrence accounting claims each item.
                        mentions = [f for f in bounded_names(ws, [r["name"] for r in item_rows],
                                                            item=True, bounded=False) if f.name == item["name"]]
                        ordinal = sum(previous[item_field] == item["name"] for previous in new)
                        item_start = mentions[ordinal].start if ordinal < len(mentions) else None
                        phrases = [p for p in warehouse_reader(text, item_rows, warehouse_parties, ctx)
                                   if p.item == item["name"] and item_start is not None and p.item_start == item_start]
                        # Normalisation folds case-only names together; keep their distinct stored choices.
                        ties = []
                        said = session.bindings[path] if path in session.bindings else \
                            (phrases[0].said if len(phrases) == 1 else None)
                        if said:
                            try:
                                rows = call_service(ctx, "assistant", "stock.list_warehouses", {})["warehouses"]
                            except Refusal as refusal:
                                if refusal.code != "plugin_not_enabled":
                                    raise
                                rows = []
                            ties = list(dict.fromkeys(r["name"] for r in rows
                                        if _norm(r["name"]) == _norm(said)))
                        if len(ties) > 1 and session.bindings.get(path) is not False:
                            if session.bindings.get(path) not in ties:
                                raise _one_detail(path, session, "warehouses", ties, "warehouse_ambiguous")
                        chosen = _warehouse(ctx, item, phrases, session, path, fv)
                        if len(ties) > 1 and chosen in ties:
                            chosen = session.bindings[path]
                        li.pop(fname, None)
                        if chosen is not None:
                            li[fname] = chosen
                        elif session.bindings.get(path) is False:
                            source = ", ".join(p.said for p in phrases)
                            warehouse_not_done.append({"type": "not_done", "part": "warehouse",
                                                       "text": f"The warehouse source {source} was not used for {item['name']}."})
                new.append(li)
            out[name] = new
        elif kind == "figure":
            asker: Callable | None = v.get("ask")
            ask = asker(ctx, out) if asker else (_say("amount_missing"), [])
            figure_text = text if payment_fields else mask_money_spans(text, supplier_ranges)
            out[name] = literal_figure(out.get(name), figure_text, ctx.currency, session, path, ask,
                                       occurrences() if payment_fields else None)
        elif kind == "date":
            order = getattr(ctx, "date_order", None) or ctx.extra.get("date_order")
            date_text = mask_money_spans(text, [r for f in spans for r in consumed_ranges(text, f.name, runs=[f])]) \
                if supplier_spans else without_names(text, spans)
            if payment_fields:
                date_text = _payment_date_text(text, occurrences())
            d = resolve_date(date_text, ctx.today, session, path, out.get(name), order)
            if d is None and contract.input_model.model_fields[name].is_required():
                d = ctx.today  # a required date has no contract default to supply today
            if d is None:
                out.pop(name, None)  # absent: the contract's default (today)
            else:
                out[name] = d.isoformat()
        elif kind == "document":
            if contract.name == "cancel_invoice":
                out.pop(name, None)  # cancellation is grounded solely in the owner's written number
            _document(ctx, v, name, presence, out, text, session, path,
                      choose_written=not payment_fields or len(peers["customer_calls"].get(contract.name, [])) > 1)
        elif kind == "text":  # free text only as the owner wrote it; otherwise the contract's default
            bound = payment_binding(ctx, text, session, path, "method") if v.get("words") == "methods" else path in session.bindings
            if bound:
                if v.get("words") == "methods" and any(w not in table()["methods"] for w in words(session.bindings[path] or "")):
                    raise session.ask(path, _say("method_unknown", said=session.bindings[path]), f"{name}_unknown", [], [])
                out[name] = session.bindings[path]
                continue
            span = _written_span(ws, out.get(name))
            if v.get("words") == "methods":  # a method phrase the owner wrote decides, whatever the model passed
                method_occs = [o for o in occurrences() if o.kind == "method"]
                session.offer(path, "method", text, method_occs)
                written = list(dict.fromkeys(o.said for o in method_occs))
                unknown = [said for said in written if any(w not in table()["methods"] for w in words(said))]
                given = _as_written(text, span) if span else None
                if len(written) > 1:
                    raise _one_detail(path, session, "payment methods", written, f"{name}_ambiguous",
                                      [o for o in occurrences() if o.kind == "method"])
                if unknown:
                    raise session.ask(path, _say("method_unknown", said=unknown[0]), f"{name}_unknown", [], [])
                if given in written:
                    out[name] = given
                elif len(written) == 1:
                    out[name] = written[0]
                elif written:
                    raise session.ask(path, _say("which", what="payment method", names=", ".join(written)),
                                      f"{name}_ambiguous", written, written)
                else:
                    out.pop(name, None)
                if out.get(name) and session.pending and session.pending["field"] == path and not session.pending["choices"]:
                    session.record_free_answer(path, [o for o in occurrences() if o.kind == "method"])
                    session.bindings[path] = out[name]
            elif span:
                value = _as_written(text, span)
                if contract.name in {"add_customer", "add_supplier"} and len(value) >= 2 and \
                        {'"': '"', "'": "'", '“': '”', '‘': '’'}.get(value[0]) == value[-1]:
                    value = value[1:-1].strip()
                out[name] = value
            else:
                out.pop(name, None)
        elif kind == "account":  # only an account the owner named the money going into; otherwise the default
            if v.get("outgoing"):
                out.pop(name, None)
                if name == "paid_from" and guarded:
                    settlement = out.get("settlement", contract.input_model.model_fields["settlement"].default)
                    if settlement == "still_owed":
                        continue
                    if settlement != "paid_now":
                        raise session.ask(f"{scope}settlement", "Was this paid now, or is it still owed?",
                                          "settlement_missing", ["Paid now", "Still owed"], ["paid_now", "still_owed"])
                # Validate the payment figure even if the account precedes it in the input model.
                amount = None
                for field_name, mark in mk.items():
                    if mark["kind"] == "figure":
                        amount = literal_figure(out.get(field_name), text, ctx.currency, session,
                                                f"{scope}{field_name}", (_say("amount_missing"), []), occurrences())
                        break
                out[name] = _outgoing_account(ctx, text, amount, session, path,
                                              warehouse_words(text, item_rows, cust_rows + supplier_rows, ctx))
                continue
            account_occs = [o for o in occurrences() if o.kind == "account"]
            session.offer(path, "account", text, account_occs)
            if payment_binding(ctx, text, session, path, "account"):
                out[name] = session.bindings[path]
                continue
            said = list(dict.fromkeys(o.said for o in account_occs))
            if len(said) > 1:
                coordinated = any(set(ws[a.end:b.start]) & table()["coordinators"]
                                  for a, b in zip(account_occs, account_occs[1:]))
                if not coordinated and _norm(str(out.get(name))) == _norm(said[-1]):
                    out[name] = said[-1]
                    continue
                raise _one_detail(path, session, "accounts", said, "account_ambiguous", account_occs)
            if said:
                out[name] = said[0]
            elif set(ws) & table()["account_into"]:
                banks = [dict(r) for r in ctx.conn.execute(select(m.account).where(
                    m.account.c.company_id == ctx.company_id, m.account.c.account_type == "bank",
                    m.account.c.is_group.is_(False), m.account.c.frozen.is_(False)).order_by(m.account.c.number)).mappings()]
                labels = [f"{r['number']} {r['name']}" for r in banks]
                raise session.ask(path, f"Which account did the money go into: {', '.join(labels)}?",
                                  "account_ambiguous", labels, [r["number"] for r in banks])
            else:
                out.pop(name, None)
    chosen_items = []
    if lines_field and out.get(lines_field) and _lines_shape(out[lines_field]) and session.bindings:
        all_occurrences = inventory(ctx, text, item_rows)
        item_occurrences = [o for o in all_occurrences if o.kind == "item"]
        for i, line in enumerate(out[lines_field]):
            chosen = line.get(item_field)
            path = f"{scope}{lines_field}.{i}.{item_field}"
            if not chosen or session.bindings.get(path) != chosen:
                continue
            matches = [o for o in item_occurrences if o.value == chosen or
                       (o.value is None and any(same_word(o.said, w) for w in _core(chosen)))]
            if len(matches) == 1:
                chosen_items.append(Found(chosen, matches[0].start, matches[0].end))
            elif not matches:
                picked = chosen_item_span(text, item_rows, session, path, chosen, all_occurrences)
                if picked:
                    chosen_items.append(picked)
    events = invoice_not_done(ctx, text, spans, out[lines_field], item_rows, emailed,
                              reads_date=contract.name != "create_invoice" or any(v["kind"] == "date" for v in mk.values()), chosen_items=chosen_items, date_text=invoice_date_input, carry_rates=contract.name == "create_invoice", invoice_rate_context=contract.name in {"create_invoice", "submit_invoice"}) \
        if lines_field and lines_field in out and _lines_shape(out[lines_field]) else []
    if delivery_date:
        events.append({"type": "delivery_date", "part": "date"})
    if contract.name == "submit_invoice" and future_invoice_date:
        events.append({"type": "not_done", "part": "date", "capability": NOT_DONE["date"], "text": _say("nd_date")})
    if contract.name == "create_invoice" and lines_field in out and isinstance(out[lines_field], list) and _lines_shape(out[lines_field]):
        candidates = [e for e in events if e.get("type") == "rate_candidate"]
        latest = {e["item"]: max(x["message"] for x in candidates if x["item"] == e["item"]) for e in candidates}
        events = [e for e in events if e.get("type") != "rate_candidate" or e["message"] == latest[e["item"]]]
        candidates = [e for e in candidates if e["message"] == latest[e["item"]]]
        for i, line in enumerate(out[lines_field]):
            found = [e for e in candidates if e["item"] == line[item_field]]
            values = list(dict.fromkeys(e["value"] for e in found))
            path = f"{scope}rate"
            selected = session.bindings.get(path)
            if sum(other.get(item_field) == line[item_field] for other in out[lines_field]) > 1 and values:
                events.append({"type": "rate_unread", "part": "rate"})
                continue
            if selected in values:
                line["rate"] = selected
            elif len(values) == 1 and not any(e["in_memo"] for e in found):
                line["rate"] = values[0]
            elif values:
                # Defer this question until after an invoice-date question.
                for e in found:
                    e["ambiguous"] = True
    if not any(e.get("part") == "memo" for e in events) and \
            not any(v["kind"] == "text" and not v.get("words") for v in mk.values()) and \
            next(_memo_phrases(text), None) is not None:
        memos = [o for o in occurrences() if o.kind == "memo"]
        if memos and len({o.value.lower() for o in memos}) == 1:
            qualifier, notice = memo_qualifier(text)
            events.append({"type": "not_done", "part": "memo", "capability": NOT_DONE["memo"],
                           "text": _say(notice, qualifier=qualifier)})
    internal = [e for e in events if e.get("type") in {"rate_candidate", "rate_unread", "delivery_date"}]
    public = [e for e in events if e.get("type") not in {"rate_candidate", "rate_unread", "delivery_date"}]
    return Result(out, public + warehouse_not_done, future_invoice_date, internal)



def owner_order(ctx: Ctx, text: str, calls: list[tuple[object, dict]]) -> list[int]:
    """The calls' indexes in the order the owner wrote their requests: each call sits at the earliest mention of a
    part only it has (its customer, or that customer's email or city; a document number; an item; an amount), parts
    shared with another call aside; a call with no mention found keeps its place after those found, in call order."""
    ws = words(text)
    cust = {r["name"]: r for r in customers(ctx)}

    def at(phrase: str | None) -> int | None:
        if not phrase:
            return None
        pw = _norm(str(phrase)).split()
        for i in range(len(ws) - len(pw) + 1):
            if pw and ws[i:i + len(pw)] == pw:
                return i
        return None

    def parts(contract, args) -> dict[tuple, int | None]:
        out: dict[tuple, int | None] = {}
        for name, v in marks(contract.input_model).items():
            val = args.get(name)
            if v["kind"] == "customer" and val:
                r = cust.get(val, {})
                found = [p for p in (at(val), at(r.get("email")), at(r.get("city"))) if p is not None]
                out[("customer", val)] = min(found, default=None)
            elif v["kind"] == "document" and val:
                num = re.findall(r"\d+", str(val))
                hits = [len(words(text[:mt.start()])) for mt in re.finditer(r"\d+", text)
                        if num and int(mt.group(0)) == int(num[-1])]
                out[("document", val)] = min(hits, default=None)
            elif v["kind"] == "figure" and val is not None:
                hits = [len(words(text[:st])) for d, st, _ in figures(text) if d == _decimal(val)]
                out[("figure", str(val))] = min(hits, default=None)
            elif v["kind"] == "lines" and _lines_shape(val):
                im = [n for n, fv in marks(_line_model(contract.input_model, name)).items() if fv["kind"] == "item"]
                for li in val or []:
                    for n in (li.get(k) for k in im):
                        if isinstance(n, str):
                            out[("item", n)] = min((f.start for f in bounded_names(ws, [n], item=True, bounded=False)),
                                                   default=None)
        return out

    every = [parts(c, a) for c, a in calls]
    keyed = []
    for k, ps in enumerate(every):
        own = {p: i for p, i in ps.items() if not any(p in o for j, o in enumerate(every) if j != k)}
        found = [i for i in (own or ps).values() if i is not None]
        keyed.append((min(found) if found else len(ws) + k, k))
    return [k for _, k in sorted(keyed)]



def describe(contract, args: dict) -> str:
    """A drafted or pending change, named from its canonical arguments only."""
    form = table()["drafts"].get(contract.name)
    try:
        return form.format(**args) if form else contract.name.replace("_", " ")
    except (KeyError, IndexError):
        return contract.name.replace("_", " ")



def one_at_a_time(drafted: tuple[object, dict], pending: tuple[object, dict]) -> dict:
    """The notice for a write the reply did not run: the reply drafts one card."""
    return {"type": "not_done", "part": "one_at_a_time", "capability": "planned.several_changes",
            "text": _say("one_at_a_time", drafted=describe(*drafted), pending=describe(*pending))}



def with_codes(rf: Refusal, session: Session, scope: str = "") -> Refusal:
    """A contract's own question, given an id and single-use codes like the step's, so every ask has one shape and an
    answer by code binds what the choice names. Not a question, or already coded: unchanged."""
    if not is_question(rf.code) or rf.data.get("question_id"):
        return rf
    data = dict(rf.data)
    # without references from the contract, only a customer's or item's label is its stored name, so only those bind;
    # any other choice gets its code but binds nothing (the field is asked again)
    stored = rf.code.startswith(("customer_", "item_"))
    choices = data.pop("choices", None) or [{"label": o, "ref": o if stored else None} for o in rf.options]
    field = scope + (data.pop("field", None) or rf.code.rsplit("_", 1)[0])
    return session.ask(field, rf.message, rf.code, [c["label"] for c in choices], [c.get("ref") for c in choices],
                       **data)



def strict_customers(calls: list[tuple[object, dict]], session: Session, scopes: list[str] | None = None) -> None:
    """Customers are strict across the reply's calls of one operation, by each call's final customer, whether the
    owner named it, the call left it out, or it was taken from a document."""
    seen: set = set()
    for k, (contract, args) in enumerate(calls):
        for name, v in marks(contract.input_model).items():
            if v["kind"] == "customer" and args.get(name):
                if (contract.name, args[name]) in seen:
                    raise session.ask(f"{scopes[k] if scopes else ''}{name}", _say("twice", name=args[name]),
                                      "customer_ambiguous", [], [])
                seen.add((contract.name, args[name]))



def write_scopes(ctx: Ctx, text: str, calls: list[tuple[object, dict]]) -> list[str]:
    """Each write's name in the reply: its operation and its place among the reply's writes of that operation, in the
    owner's order ("create_invoice#1:"), so a question and its answer belong to one write."""
    rank = owner_order(ctx, text, calls)
    seen: dict[str, int] = {}
    out = [""] * len(calls)
    for k in rank:
        name = calls[k][0].name
        out[k] = f"{name}#{seen.get(name, 0)}:"
        seen[name] = seen.get(name, 0) + 1
    return out



def write_intents(text: str) -> int:
    """How many changes the owner asked for: each write verb with its document, once ("send Wayne an invoice" is one;
    "and submit it" is the same write; "and submit invoice 8" is another)."""
    t = table()
    write_verbs = t["write_verbs"]
    # Insert a normal clause link only when punctuation introduces another action.
    pieces = re.split(t["intent_separator"], text)
    ws = words(pieces[0])
    for piece in pieces[1:]:
        following = words(piece)
        start = max((j + 1 for j, w in enumerate(ws) if w in t["intent_clause_links"]), default=0)
        prior = ws[start:]
        payment = bool(set(prior) & t["payment_explicit_verbs"]) or (
            bool(set(prior) & t["payment_intent_verbs"]) and (
                bool(set(prior) & t["payment_nouns"]) or
                (bool(set(prior) & t["payer_from"]) and any(any(c.isdigit() for c in word) for word in prior))))
        if any(w in (write_verbs | t["payment_intent_verbs"])
               and not (i and following[i - 1] in (t["articles"] | t["determiners"]))
               and not (payment and i and w == "invoice" and _names_a_document(following, i)
                        and following[i - 1] in t["payment_reference_prepositions"])
               for i, w in enumerate(following)):
            ws.append(t["intent_separator_link"])
        ws.extend(following)
    # Undo is supported only as the verb introducing an invoice cancellation. It is not a
    # general write word: customer names, item names and explanatory text may contain it.
    undo_verbs, undo_documents = set(), set()
    clause = 0
    for i, w in enumerate(ws):
        if w in {"and", "then"}:
            clause = i + 1
        if w != "undo" or ws[clause:i] not in ([], ["please"], ["can", "you"], ["could", "you"],
                                               ["can", "you", "please"], ["could", "you", "please"]):
            continue
        j = i + 1
        while j < len(ws) and ws[j] in (t["articles"] | t["determiners"]):
            j += 1
        if j < len(ws) and (ws[j] == "invoice" or re.fullmatch(r"inv\d+", ws[j])):
            undo_verbs.add(i)
            undo_documents.add(j)
    count = 0
    for i, w in enumerate(ws):
        if w not in write_verbs and i not in undo_verbs:
            end = next((j for j in range(i + 1, len(ws)) if ws[j] in t["intent_clause_links"]), len(ws))
            if w not in t["payment_intent_verbs"] or not set(ws[i + 1:end]) & t["payment_nouns"]:
                continue
        # A numbered invoice linked to a payment is the payment's document, even
        # when the payer and amount put it more than three words from the verb.
        start = max((j + 1 for j in range(i) if ws[j] in t["intent_clause_links"]), default=0)
        prior = ws[start:i]
        payment = bool(set(prior) & t["payment_explicit_verbs"]) or (
            bool(set(prior) & t["payment_intent_verbs"]) and (
                bool(set(prior) & t["payment_nouns"]) or
                (bool(set(prior) & t["payer_from"]) and any(any(c.isdigit() for c in word) for word in prior))))
        if (count and w in t["payment_explicit_verbs"] and set(prior) & t["payment_intent_verbs"]
                and set(prior) & t["payment_nouns"]):
            continue  # the payment's own verb, not another change
        if payment and w in t["document_nouns"] and _names_a_document(ws, i):
            continue
        near = ws[max(0, i - 3):i]
        if i and ws[i - 1] in (t["articles"] | t["determiners"]):
            continue  # "an invoice", "as a draft": a noun, not a second intent
        if w == "invoice" and (set(near) & write_verbs or i in undo_documents):
            continue  # "send Wayne an invoice", "submit invoice 8": the document of the verb before it
        if w in t["invoice_completion"] and count and "invoice" in ws[:i]:
            tail = ws[i + 1:]
            if not tail or (tail[0] in t["same_document"] and not _names_a_document(tail, 1)) or (
                    tail[:2] == ["the", "invoice"] and not _names_a_document(tail, 1)):
                continue
        if i + 1 < len(ws) and ws[i + 1] in t["same_document"] and count and not _names_a_document(ws, i + 2):
            continue  # "and submit it": the write already asked for ("submit this invoice 8" names another)
        count += 1
    # A coordinated email to an address after adding a party is an action, not the
    # party's contact label. Ignore occurrences inside a quoted value.
    add_party = re.search(r'\b(?:add|create|make)\s+(?:(?:a|an|new)\s+)*(?:customer|supplier)\b', text, re.I)
    quoted = list(re.finditer(r'''(?<!\w)(?:"[^"]*"|'[^']*'|“[^”]*”|‘[^’]*’)''', text))
    if add_party:
        for action in re.finditer(r'''\band\s+email\s+["'“‘]?[^\s,;<>"'“”‘’]+@[^\s,;<>"'“”‘’]+\.[a-z]+''', text, re.I):
            if action.start() > add_party.end() and not any(q.start() < action.start() < q.end() for q in quoted):
                count = max(count, 2)
                break
    return count



def _names_a_document(ws: list[str], j: int) -> bool:
    """Whether the words from j name a document by its number: "invoice 8", "INV-2026-00008"."""
    def digits(k: int) -> bool:
        return k < len(ws) and any(ch.isdigit() for ch in ws[k])
    return digits(j) or (j < len(ws) and ws[j] in table()["document_nouns"] and digits(j + 1))
