"""Every value a card, a result and a read-back show is the stored value it stands for, exactly. A
card once showed `@ 0.12` for a stored rate of 0.1234, in three places; a check that pooled the shown values let
rounding and truncation through.

For every write contract, a card is drawn over awkward stored values (four-place rates, fractional quantities,
amounts that are not whole) on two invoice lines, approved and committed. Then each shown value is compared with its
own stored value, field by field: every item line (the lines a submit or a cancel card shows unchanged included)
against its stored line, the total, the amount received, what a payment is applied to and the outstanding it is
applied against, every books line against the ledger entries the commit wrote, and the result and read-back texts in
order. A card row with a value this test does not know fails it, as does a write contract with no scenario.
"""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from bookdiff import changes, snapshot
from erpclaw_core import approvals, models as m
from erpclaw_core.contracts import moves_money
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import tables as t

# every write that moves money; one that says it moves none is checked by test_moves_money instead
WRITES = sorted(c.name for c in REGISTRY.contracts.values() if c.effect != "read" and moves_money(c)) \
    if REGISTRY.contracts else []
NUMBER = re.compile(r"(?<![\w.\-])\d[\d,]*(?:\.\d+)?(?![\w])")
ITEM = re.compile(r"^(?P<q>\S+) x (?P<name>.+) @ (?P<r>\S+) = (?P<a>\S+)$")
BOOKS = re.compile(r"[+\-](?P<v>\d[\d,]*(?:\.\d+)?)$")
APPLIED = re.compile(r"(\S+) \((\S+) of (\S+) outstanding\)")
DELIVER = re.compile(r"^(?P<q>\S+) x (?P<name>.+) \(of (?P<o>\S+) ordered\)$")
FROM = re.compile(r"^(?P<wh>.+) \((?P<h>\S+) on hand\)$")
COST = re.compile(r"^Cost of goods sold (?P<c>\S+) \S+$")
CREDIT = re.compile(r"^line (?P<n>\d+): (?P<q>\S+) x (?P<name>.+) @ (?P<r>\S+) = (?P<a>\S+)$")
BACK = re.compile(r"^(?P<q>\S+) into (?P<wh>.+) against (?P<doc>\S+) at (?P<c>\S+)$")
OWING = re.compile(r"^(?P<num>\S+): \w+(?: \w+)?, (?P<o>\S+) outstanding, ")
NO_LINES = {"cancel_invoice", "deliver_order", "create_credit_note"}  # their own rows name the lines (below)
NO_VALUE = {"Company", "Customer", "Invoice", "Date", "Dated", "Due", "Tax", "Method", "Status now", "Email to",
            "Account", "Reference",  # a payment's account and reference are names and text, not stored amounts
            "Payment", "Reason", "Reversal date",  # a reversal names its payment, a reason and its date
            "Order", "Delivery date"}  # an order's number and the date asked for
LINES = {"items": [{"item": "copper wire", "quantity": "1000.5"},  # 1000.5 x 0.1234 = 123.4617, stored 123.46
                   {"item": "circuit board", "quantity": "2.25"}]}  # 2.25 x 45.0075 = 101.266875, stored 101.27


def _d(text) -> Decimal:
    return Decimal(str(text).replace(",", ""))


def _awkward_rates(engine):
    with engine.begin() as conn:
        for name, rate in (("Copper Wire", "0.1234"), ("Circuit Board", "45.0075")):
            conn.execute(update(m.item).where(m.item.c.name == name).values(standard_rate=Decimal(rate)))


def _commit(engine, contract, args):
    card = approvals.propose(engine, REGISTRY.contracts[contract], args)
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "committed", out.text
    return card, out


def _earlier_credit(engine, amount):
    """An earlier payment from Acme with no open invoice to settle: all credit, and the same amount as the card's,
    so the card shows both a fractional credit left and a possible-duplicate warning."""
    from datetime import timedelta
    from erpclaw_core.common import company_ctx
    from erpclaw_sales import service as s
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        acme = dict(conn.execute(select(m.party).where(m.party.c.name == "Acme Corporation")).mappings().one())
        s.record_payment(ctx, s.plan_payment(ctx, acme, Decimal(amount), ctx.today - timedelta(days=2)))


def _payment_number(engine, invoice_number, amount):
    """A payment of `amount` applied to the invoice, recorded through the service; its number."""
    from erpclaw_core.common import company_ctx
    from erpclaw_sales import service as s
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        inv = s.find_invoice(ctx, invoice_number)
        cust = dict(conn.execute(select(m.party).where(m.party.c.id == inv["customer_id"])).mappings().one())
        r = s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal(amount), ctx.today, invoice=inv))
        return r["number"]


def _invoice_number(engine, submit):
    _, out = _commit(engine, "create_invoice", {"customer": "Acme Corporation", **LINES, "submit": submit})
    with engine.connect() as conn:
        return conn.execute(select(t.invoice.c.number).where(t.invoice.c.operation_key == out.operation_key)).scalar_one()


def _stocked(engine):
    """Both items kept in stock in one default warehouse, received yesterday at awkward costs: 2000 Copper Wire for
    155.40 and 10 Circuit Boards for 123.46, so the costs a card shows are not whole."""
    from datetime import timedelta
    from erpclaw_core.common import company_ctx, new_id
    from erpclaw_stock import service as stock
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        wh = stock.create_warehouse(ctx, "Main Store", default=True)
        trusted = stock.Trusted("the card-values books' starting stock")
        for name, qty, rate in (("Copper Wire", "2000", "0.0777"), ("Circuit Board", "10", "12.3456")):
            item = conn.execute(select(m.item.c.id).where(m.item.c.company_id == ctx.company_id,
                                                          m.item.c.name == name)).scalar_one()
            stock.make_stock_item(ctx, item, "moving_average")
            stock.receive(ctx, f"pr-{new_id()[:8]}", item, wh, qty, rate, ctx.today - timedelta(days=1),
                          trusted=trusted)
        trusted.verify(ctx)


def _order_number(engine):
    _, out = _commit(engine, "create_sales_order", {"customer": "Acme Corporation", **LINES,
                                                    "delivery_date": "2026-11-30"})
    with engine.connect() as conn:
        return conn.execute(select(t.order.c.number).where(t.order.c.operation_key == out.operation_key)).scalar_one()


SCENARIOS = {  # each sets up the books, and returns the card's arguments
    "create_invoice": lambda e: {"customer": "Acme Corporation", **LINES},
    "submit_invoice": lambda e: {"invoice": _invoice_number(e, submit=False)},
    "record_payment": lambda e: (_earlier_credit(e, "300.25"), _invoice_number(e, submit=True),
                                 {"customer": "Acme Corporation", "amount": "300.25", "method": "check"})[2],
    "cancel_invoice": lambda e: {"invoice": _invoice_number(e, submit=True)},
    "reverse_payment": lambda e: {"payment": _payment_number(e, _invoice_number(e, submit=True), "50.25")},
    "create_sales_order": lambda e: {"customer": "Acme Corporation", **LINES, "delivery_date": "2026-11-30"},
    "deliver_order": lambda e: (_stocked(e), {"order": _order_number(e)})[1],
    "create_credit_note": lambda e: (_stocked(e), {"invoice": _invoice_number(e, submit=True), "everything": True})[1],
}


# These writes keep master prices or stock vouchers rather than invoice lines.
def _add_item_scenario(engine):
    return {"name": "Calibration labour", "unit": "hour", "price": "12.3456", "kind": "service"}


def _opening_stock_scenario(engine):
    from erpclaw_core.common import company_ctx
    from erpclaw_stock import service as stock
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        stock.create_warehouse(ctx, "Main Store", default=True)
        for name in ("Copper Wire", "Circuit Board"):
            item = conn.execute(select(m.item.c.id).where(m.item.c.company_id == ctx.company_id,
                                                          m.item.c.name == name)).scalar_one()
            stock.make_stock_item(ctx, item, "moving_average")
    return {"lines": [{"item": "Copper Wire", "warehouse": "Main Store", "quantity": "1000.5", "rate": "0.1234"},
                      {"item": "Circuit Board", "warehouse": "Main Store", "quantity": "2.25", "rate": "45.0075"}]}


def _stock_count_scenario(engine):
    _stocked(engine)
    return {"lines": [{"item": "Copper Wire", "warehouse": "Main Store", "counted": "999.5"},
                      {"item": "Circuit Board", "warehouse": "Main Store", "counted": "7.75"}]}


def _write_off_scenario(engine):
    _stocked(engine)
    return {"item": "Copper Wire", "warehouse": "Main Store", "quantity": "1000.5", "reason": "Damaged insulation"}


def _cancel_stock_scenario(engine):
    _, out = _commit(engine, "write_off_stock", _write_off_scenario(engine))
    return {"document": out.data["number"], "date": out.data["posting_date"]}


MASTER_STOCK_SCENARIOS = {
    "add_item": _add_item_scenario,
    "record_opening_stock": _opening_stock_scenario,
    "record_stock_count": _stock_count_scenario,
    "write_off_stock": _write_off_scenario,
    "cancel_stock_movement": _cancel_stock_scenario,
}
SCENARIOS.update(MASTER_STOCK_SCENARIOS)


def _stock_entries(conn, voucher_id: str) -> list[tuple[Decimal, Decimal, Decimal]]:
    """A voucher's stock entries in line order: (quantity, value, quantity after)."""
    from erpclaw_stock import tables as st
    e = st.entry
    return [(_d(r.qty), _d(r.value), _d(r.qty_after)) for r in conn.execute(
        select(e.c.qty, e.c.value, e.c.qty_after).where(e.c.voucher_id == voucher_id).order_by(e.c.line_no, e.c.seq))]


def _stored(engine, contract, args, out, before) -> dict:
    if contract in MASTER_STOCK_SCENARIOS:
        return _stored_master_stock(engine, contract, args, out, before)
    """What the commit stored, read from the books: the document, its lines, the allocations, the new ledger entries."""
    new_ledger = changes(before, snapshot(engine)).get("ledger_entry", {}).get("added", [])
    with engine.connect() as conn:
        items = dict(conn.execute(select(m.item.c.id, m.item.c.name)).all())
        if contract == "create_sales_order":
            order = conn.execute(select(t.order).where(t.order.c.operation_key == out.operation_key)).mappings().one()
            lines = conn.execute(select(t.order_line).where(t.order_line.c.order_id == order["id"])).mappings().all()
            return {"invoice": order, "ledger": new_ledger, "lines": sorted(
                (items[ln["item_id"]], _d(ln["quantity"]), _d(ln["rate"]), _d(ln["amount"])) for ln in lines)}
        if contract == "deliver_order":
            d = conn.execute(select(t.delivery).where(t.delivery.c.operation_key == out.operation_key)).mappings().one()
            ol = {r["id"]: r for r in conn.execute(select(t.order_line)).mappings()}
            dls = conn.execute(select(t.delivery_line).where(t.delivery_line.c.delivery_id == d["id"]).order_by(
                t.delivery_line.c.stock_line)).mappings().all()
            return {"delivery": d, "ledger": new_ledger, "issues": _stock_entries(conn, d["id"]),
                    "lines": [(items[ol[x["order_line_id"]]["item_id"]], _d(x["quantity"]),
                               _d(ol[x["order_line_id"]]["quantity"])) for x in dls]}
        if contract == "create_credit_note":
            cn = conn.execute(select(t.credit_note).where(t.credit_note.c.operation_key == out.operation_key)
                              ).mappings().one()
            cls = conn.execute(select(t.credit_note_line).where(t.credit_note_line.c.credit_note_id == cn["id"])
                               .order_by(t.credit_note_line.c.line_no)).mappings().all()
            inv = conn.execute(select(t.invoice).where(t.invoice.c.id == cn["invoice_id"])).mappings().one()
            back = conn.execute(select(t.credit_note_return).join(
                t.credit_note_line, t.credit_note_line.c.id == t.credit_note_return.c.credit_note_line_id).where(
                t.credit_note_line.c.credit_note_id == cn["id"]).order_by(t.credit_note_line.c.line_no)).mappings().all()
            return {"credit": cn, "ledger": new_ledger, "invoice_after": inv,
                    "invoice_before": {k[0]: r for k, r in before["sales_invoice"].items()}[inv["id"]],
                    "lines": [(items[x["item_id"]], _d(x["quantity"]), _d(x["rate"]), _d(x["amount"])) for x in cls],
                    "back": [(_d(r["quantity"]), _d(r["cost"])) for r in back]}
        if contract == "reverse_payment":
            pay = conn.execute(select(t.payment).where(t.payment.c.number == args["payment"])).mappings().one()
            alloc = conn.execute(select(t.allocation).where(t.allocation.c.payment_id == pay["id"])).mappings().all()
            inv = {r["id"]: r for r in conn.execute(select(t.invoice)).mappings()}
            return {"reversed": pay, "ledger": new_ledger,
                    "owes": sorted((inv[a["invoice_id"]]["number"], _d(a["amount"]), _d(inv[a["invoice_id"]]["outstanding"]))
                                   for a in alloc)}
        if contract == "record_payment":
            pay = conn.execute(select(t.payment).where(t.payment.c.operation_key == out.operation_key)).mappings().one()
            alloc = conn.execute(select(t.allocation).where(t.allocation.c.payment_id == pay["id"])).mappings().all()
            numbers = dict(conn.execute(select(t.invoice.c.id, t.invoice.c.number)).all())
            prior = {k[0]: r for k, r in before["sales_invoice"].items()}
            return {"payment": pay, "ledger": new_ledger,
                    "applied": sorted((numbers[a["invoice_id"]], _d(a["amount"]), _d(prior[a["invoice_id"]]["outstanding"]))
                                      for a in alloc)}
        where = (t.invoice.c.operation_key == out.operation_key) if contract == "create_invoice" \
            else (t.invoice.c.number == args["invoice"])
        inv = conn.execute(select(t.invoice).where(where)).mappings().one()
        lines = conn.execute(select(t.invoice_line).where(t.invoice_line.c.invoice_id == inv["id"])).mappings().all()
    return {"invoice": inv, "ledger": new_ledger,
            "lines": sorted((items[ln["item_id"]], _d(ln["quantity"]), _d(ln["rate"]), _d(ln["amount"])) for ln in lines)}


def problems(contract: str, card: dict, texts: list[str], stored: dict) -> list[str]:
    if contract in MASTER_STOCK_SCENARIOS:
        return _master_stock_problems(contract, card, texts, stored)
    """Each shown value against the stored value it stands for; an empty list when every one is exact."""
    out = []

    def same(what, shown, value):
        if _d(shown) != _d(value):
            out.append(f"{what}: shown {shown}, stored {value}")

    rows = [(k, v) for k, v in card["lines"]]
    issues, delivered, credits, back = (list(stored.get("issues", [])), list(stored["lines"]) if "delivery" in stored
                                        else [], list(stored["lines"]) if "credit" in stored else [],
                                        list(stored.get("back", [])))
    if "lines" in stored and contract not in NO_LINES:
        matched = [ITEM.match(v) for k, v in rows if k == "Item"]
        if None in matched or len(matched) != len(stored["lines"]):
            out.append(f"item lines: shown {[v for k, v in rows if k == 'Item']}, stored {stored['lines']}")
        shown = sorted((mt["name"], mt["q"], mt["r"], mt["a"]) for mt in matched if mt)
        for (name, q, r, a), (s_name, s_q, s_r, s_a) in zip(shown, stored["lines"]):
            if name != s_name:
                out.append(f"item line {name}: stored {s_name}")
            same(f"{name} quantity", q, s_q)
            same(f"{name} rate", r, s_r)
            same(f"{name} amount", a, s_a)
    for k, v in rows:
        if k == "Invoice" and "credit" in stored:  # the invoice as it stands before the credit
            mt = OWING.match(v)
            if mt is None:
                out.append(f"invoice row: {v}")
            else:
                same("outstanding before the credit", mt["o"], stored["invoice_before"]["outstanding"])
            continue
        if (k == "Item" and contract not in NO_LINES) or k in NO_VALUE:
            continue
        if k == "Deliver":
            mt = DELIVER.match(v)
            if mt is None or not delivered:
                out.append(f"deliver row: {v}, stored {delivered}")
                continue
            name, q, ordered = delivered.pop(0)
            if mt["name"] != name:
                out.append(f"deliver {mt['name']}: stored {name}")
            same(f"{name} delivered", mt["q"], q)
            same(f"{name} ordered", mt["o"], ordered)
        elif k == "From":
            mt = FROM.match(v)
            if mt is None or not issues:
                out.append(f"from row: {v}, stored issues {issues}")
                continue
            q, value, after = issues[0]
            same("on hand before the issue", mt["h"], q + after)
        elif k == "Cost":
            mt = COST.match(v)
            if mt is None or not issues:
                out.append(f"cost row: {v}, stored issues {issues}")
                continue
            same("cost of goods sold", mt["c"], issues.pop(0)[1])
        elif k == "Credit":
            mt = CREDIT.match(v)
            if mt is None or not credits:
                out.append(f"credit row: {v}, stored {credits}")
                continue
            name, q, r, a = credits.pop(0)
            if mt["name"] != name:
                out.append(f"credit {mt['name']}: stored {name}")
            same(f"{name} credited", mt["q"], q)
            same(f"{name} rate credited", mt["r"], r)
            same(f"{name} amount credited", mt["a"], a)
        elif k == "Back into stock":
            mt = BACK.match(v)
            if mt is None or not back:
                out.append(f"back row: {v}, stored {back}")
                continue
            q, c = back.pop(0)
            same("returned", mt["q"], q)
            same("cost returned", mt["c"], c)
        elif k == "Total credit":
            same("total credit", v.split()[0], stored["credit"]["total"])
        elif k == "Outstanding after":
            same("outstanding after", v.split()[0], stored["invoice_after"]["outstanding"])
        elif k == "Total":
            same("total", v.split()[0], stored["invoice"]["total"])
        elif k == "Amount received" and "payment" in stored:
            same("amount received", v.split()[0], stored["payment"]["amount"])
        elif k == "Owes again":
            parts = APPLIED.findall(v)
            for (num, amt, of) in parts:
                match = [o for o in stored.get("owes", []) if o[0] == num]
                if not match:
                    out.append(f"owes again {num}: not among the stored allocations")
                    continue
                same(f"owes again {num}", amt, match[0][1])
                same(f"outstanding of {num} after", of, match[0][2])
        elif k == "Credit removed":
            same("credit removed", v.split()[0], stored["reversed"]["unallocated"])
        elif k == "Amount received" and "reversed" in stored:
            same("amount reversed", v.split()[0], stored["reversed"]["amount"])
        elif k == "Credit left":
            same("credit left", v.split()[0], stored["payment"]["unallocated"])
        elif k == "Applied to":
            parts = sorted(APPLIED.findall(v))
            if len(parts) != len(stored["applied"]):
                out.append(f"applied to: shown {v}, stored {stored['applied']}")
            for (num, amt, of), (s_num, s_amt, s_of) in zip(parts, stored["applied"]):
                if num != s_num:
                    out.append(f"applied to {num}: stored {s_num}")
                same(f"applied to {num}", amt, s_amt)
                same(f"outstanding of {num}", of, s_of)
        elif k == "Item" and contract in NO_LINES:
            out.append(f"a {contract} card now shows item lines: add it to the line check")
        elif NUMBER.search(v):
            out.append(f"a card row this test does not know shows a value: {k}: {v}")
    for what, left in (("deliver", delivered), ("issue", issues), ("credit", credits), ("return", back)):
        if left:
            out.append(f"stored {what} rows the card does not show: {left}")
    for w in card.get("warnings", []):
        if "payment" in stored and (mt := re.search(r"already has a (\S+) \S+ payment", w)):
            same("possible duplicate", mt.group(1), stored["payment"]["amount"])  # the same amount, by the rule
        if "payment" in stored and (mt := re.search(r"; (\S+) \S+ stays as credit", w)):
            same("credit warning", mt.group(1), stored["payment"]["unallocated"])
    books = card["books"] if isinstance(card["books"], list) else [card["books"]]
    shown_books = Counter(_d(mt["v"]) for b in books if (mt := BOOKS.search(b)))
    stored_books = Counter(_d(e[c]) for e in stored["ledger"] for c in ("debit", "credit") if _d(e[c]) != 0)
    if shown_books != stored_books:
        out.append(f"books: shown {sorted(shown_books.elements())}, ledger entries {sorted(stored_books.elements())}")
    if "payment" in stored:
        want = [stored["payment"]["amount"], *(a for _, a, _ in stored["applied"]),
                *([stored["payment"]["unallocated"]] if _d(stored["payment"]["unallocated"]) else [])]
    elif "delivery" in stored:  # a delivery's result: its goods at the cost they left at
        want = [sum((v for _, v, _ in stored["issues"]), Decimal(0))]
    elif "credit" in stored:
        want = [stored["credit"]["total"], stored["invoice_after"]["outstanding"]]
    elif "reversed" in stored:
        want = [stored["reversed"]["amount"], *(a for _, a, _ in stored["owes"]),
                *([stored["reversed"]["unallocated"]] if _d(stored["reversed"]["unallocated"]) else [])]
    else:
        want = [stored["invoice"]["total"]]
    for text in texts:  # the result, then the read-back: their amounts in order (an amount always has its places)
        got = [n for n in NUMBER.findall(text) if "." in n]
        if [_d(n) for n in got] != [_d(v) for v in want]:
            out.append(f"text: shown {got}, stored {[str(v) for v in want]}: {text}")
    return out


def _run(engine, contract):
    _awkward_rates(engine)
    args = SCENARIOS[contract](engine)
    before = snapshot(engine)
    card, out = _commit(engine, contract, args)
    stored = _stored(engine, contract, args, out, before)
    return card.card, [out.text, approvals.read_back(engine, out.operation_key).text], stored


def test_every_write_contract_has_a_scenario():
    assert set(WRITES) == set(SCENARIOS), "a new write contract needs its card-values scenario here"


@pytest.mark.parametrize("contract", WRITES)
def test_every_value_a_card_and_its_result_show_is_its_stored_value_exactly(engine, contract):
    card, texts, stored = _run(engine, contract)
    assert stored.get("lines") or stored.get("payment") or stored.get("reversed"), f"{contract}: nothing stored to compare"
    if contract in ("deliver_order", "create_credit_note"):  # stock moved and its cost is on the card
        assert stored.get("issues") or stored.get("back"), contract
    assert problems(contract, card, texts, stored) == []


@pytest.mark.parametrize("contract, old, new, where", [  # each value mutation is caught
    ("submit_invoice", "1000.5 x", "1000 x", "card"),
    ("submit_invoice", "1000.5 x", "1000.50 x", "none"),  # the same value, written longer: not a defect
    ("record_payment", "+300.25", "+300.3", "card"),
    ("record_payment", "75.52 USD", "75.5 USD", "card"),  # the credit left
    ("record_payment", "a 300.25 USD payment", "a 300.2 USD payment", "warnings"),  # a possible duplicate
    ("cancel_invoice", "-224.73", "-224.7", "card"),
    ("create_invoice", "@ 0.1234", "@ 0.12", "card"),
    ("create_invoice", "@ 45.0075", "@ 45.01", "card"),
    ("record_payment", "of 224.73 outstanding", "of 224.7 outstanding", "card"),
    ("create_invoice", "224.73 USD", "224.7 USD", "texts"),
    ("record_payment", "75.52 USD", "75.5 USD", "texts"),
    ("reverse_payment", "(50.25 of", "(50.2 of", "card"),  # what an invoice owes again
    ("reverse_payment", "-50.25", "-50.3", "card"),
    ("reverse_payment", "50.25 USD again", "50.2 USD again", "texts"),
])
def test_the_check_catches_each_rounding_and_truncation(engine, contract, old, new, where):
    card, texts, stored = _run(engine, contract)
    assert problems(contract, card, texts, stored) == []
    on_card = " ".join([*(v for _, v in card["lines"]), *card["books"]])
    where_text = {"texts": " ".join(texts), "warnings": " ".join(card["warnings"])}.get(where, on_card)
    assert old in where_text, f"{old!r} is not where the mutation expects it"
    lines = [[k, v.replace(old, new)] for k, v in card["lines"]]
    bad_card = {**card, "lines": lines, "books": [b.replace(old, new) for b in card["books"]],
                "warnings": [w.replace(old, new) for w in card["warnings"]]}
    if where == "texts":
        found = problems(contract, card, [x.replace(old, new) for x in texts], stored)
    else:
        found = problems(contract, bad_card, texts, stored)
    assert bool(found) == (where != "none"), (old, new, found)



def _stored_master_stock(engine, contract, args, out, before):
    """Read each master/voucher line, movement and newly posted ledger line independently of its card."""
    from erpclaw_stock import tables as st
    added = changes(before, snapshot(engine)).get("ledger_entry", {}).get("added", [])
    with engine.connect() as conn:
        if contract == "add_item":
            item = dict(conn.execute(select(m.item).where(m.item.c.name == out.data["name"])).mappings().one())
            return {"lines": [item], "ledger": added}
        voucher = dict(conn.execute(select(st.voucher).where(st.voucher.c.id == out.data["voucher_id"])
                                    ).mappings().one())
        lines = [dict(r) for r in conn.execute(select(st.voucher_line).where(
            st.voucher_line.c.voucher_id == voucher["id"]).order_by(st.voucher_line.c.line_no)).mappings()]
        entries = [dict(r) for r in conn.execute(select(st.entry).where(
            st.entry.c.voucher_id == voucher["id"], st.entry.c.voucher_type == out.data["voucher_type"]
        ).order_by(st.entry.c.line_no)).mappings()]
        items = {r["id"]: dict(r) for r in conn.execute(select(m.item)).mappings()}
        warehouses = {r["id"]: dict(r) for r in conn.execute(select(st.warehouse)).mappings()}
        accounts = {r["id"]: dict(r) for r in conn.execute(select(m.account)).mappings()}
    assert len(lines) == len(entries) > 0
    for line, entry in zip(lines, entries):
        assert line["item_id"] == entry["item_id"] and line["warehouse_id"] == entry["warehouse_id"]
        assert _d(line["qty_moved"]) == _d(entry["qty"]) and _d(line["value"]) == _d(entry["value"])
    return {"lines": lines, "entries": entries, "voucher": voucher, "items": items,
            "warehouses": warehouses, "accounts": accounts, "ledger": added}


def _master_stock_problems(contract, card, texts, stored):
    """Compare complete value-bearing strings with persisted fields, including quantities and balances."""
    def qty(value):
        return format(_d(value).normalize(), "f")

    def amount(value):
        return f"{_d(value):,.2f}"

    def rate(value):
        places = max(2, -_d(value).normalize().as_tuple().exponent)
        return f"{_d(value):,.{places}f}"

    def dated(value):
        return f"{value.day} {value:%b %Y}"

    expected, out = [], []
    if contract == "add_item":
        item = stored["lines"][0]
        expected = [("Item", item["name"]), ("Kind", "a service, not kept in stock"), ("Unit", item["unit"]),
                    ("Selling price", f"{rate(item['standard_rate'])} USD per {item['unit']}")]
        result = f"I added {item['name']} at {rate(item['standard_rate'])} USD per {item['unit']}."
        books = ["Nothing is posted."]
        if stored["ledger"]:
            out.append(f"adding an item unexpectedly posted ledger entries: {stored['ledger']}")
    else:
        voucher, entries = stored["voucher"], stored["entries"]
        when = entries[0]["posting_date"]
        if contract == "record_opening_stock":
            expected.append(("Date", f"Opening stock dated {dated(when)} (when these books start)"))
        elif contract == "cancel_stock_movement":
            expected += [("Original date", dated(voucher["posting_date"])), ("Reversal date", dated(when))]
        else:
            expected.append(("Date", dated(when)))
        for line, entry in zip(stored["lines"], entries):
            item = stored["items"][entry["item_id"]]
            wh = stored["warehouses"][entry["warehouse_id"]]["name"]
            q, value, unit = qty(entry["qty"]), amount(entry["value"]), item["unit"]
            if contract == "record_opening_stock":
                text = f"{q} {unit} in {wh} at {rate(line['rate'])} USD per {unit}: {value} USD"
            elif contract == "record_stock_count":
                difference = _d(line["counted_qty"]) - _d(line["books_qty"])
                assert difference < 0 and entry["direction"] == "out"
                assert _d(line["counted_qty"]) == _d(entry["qty_after"])
                text = (f"{wh}: books {qty(line['books_qty'])}, counted {qty(line['counted_qty'])}, "
                        f"difference {qty(difference)} {unit}, value {value} USD; moving-average cost")
            elif contract == "write_off_stock":
                text = f"{q} {unit} from {wh} ({qty(_d(entry['qty_after']) + _d(entry['qty']))} on hand)"
            else:
                assert entry["reverses_entry_id"] and entry["direction"] == "in"
                text = (f"{wh}: restore {q} {unit}, value {value} USD; afterward {qty(entry['qty_after'])} {unit}, "
                        f"value {amount(entry['value_after'])} USD")
            expected.append((item["name"], text))
        total = sum((_d(e["value"]) for e in entries), Decimal(0))
        if contract == "record_opening_stock":
            expected.append(("Total", f"{amount(total)} USD"))
            result = f"Recorded opening stock {voucher['number']}, dated {dated(when)}: {amount(total)} USD."
        elif contract == "record_stock_count":
            result = (f"Recorded stock count {voucher['number']}, dated {dated(when)}: {len(entries)} stock adjustments, "
                      f"{amount(total)} USD written off.")
        elif contract == "write_off_stock":
            expected += [("Reason", voucher["reason"]), ("Stock cost", f"Moving average: {amount(total)} USD")]
            result = (f"Wrote off {q} {unit} of {item['name']} from {wh}: {amount(total)} USD at book cost, "
                      f"{voucher['number']}, dated {dated(when)}.")
        else:
            result = (f"Cancelled {voucher['number']} with reversal entries dated {dated(when)}; "
                      f"{amount(total)} USD of stock value is reversed.")
        expected.append(("Accounting destination", "ERPClaw general ledger"))
        if contract == "cancel_stock_movement":
            expected.append(("Original document", "Kept unchanged; linked reversal entries record this cancellation."))
        books = []
        for entry in stored["ledger"]:
            account = stored["accounts"][entry["account_id"]]
            debit = _d(entry["debit"])
            side, value = ("debit", debit) if debit else ("credit", _d(entry["credit"]))
            books.append(f"{account['number']} {account['name']}: {side} {amount(value)} USD")
        assert books, "the scenario must produce a real ledger posting"
    shown = [(k, v) for k, v in card["lines"] if k != "Company"]
    if shown != expected:
        out.append(f"card lines: shown {shown}, stored {expected}")
    if Counter(card["books"]) != Counter(books):
        out.append(f"books: shown {card['books']}, stored {books}")
    for text in texts:
        if text != result:
            out.append(f"text: shown {text}, stored {result}")
    return out
