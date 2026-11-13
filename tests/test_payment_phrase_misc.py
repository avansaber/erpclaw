"""Payment phrase ownership: the owner's occurrences supply every field."""
from collections import Counter
from decimal import Decimal
from datetime import date

import pytest
from sqlalchemy import select

from erpclaw_assistant import resolve as rv
from erpclaw_assistant.model import Reply, call
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item, create_party
from erpclaw_sales import tables as t
from test_payment_resolve import pay, turns, kinds, lines, nothing_written, _add_bank
from test_outgoing_account import books, payment, snapshot
from test_money_spans import money_books
from conftest import empty_books, drop_everything
from erpclaw_core.setup import create_company
from erpclaw_sales import service as sales
from erpclaw_assistant import resolve_amounts, resolve_args, resolve_dates, resolve_inventory, resolve_payment, resolve_policy, resolve_records, resolve_types, resolve_words


DATE_SHAPED_NAMES = ("24/7 Services", "May 5 Supplies", "2026 Holdings", "Friday Foods", "Today Media",
                     "2026-10-24 Holdings", "24.10.2026 Services", "Two Days Ago Foods")


def test_non_payment_resolution_and_inventory_do_not_read_accounts(monkeypatch):
    from types import SimpleNamespace
    from erpclaw_core.common import Ctx
    from erpclaw_sales import CreateInvoice

    class NoBooks:
        def execute(self, *args, **kwargs):
            raise AssertionError("Non-payment resolution must not read accounts")

    items = [{"id": "wire", "name": "Copper Wire", "unit": "unit", "standard_rate": "2.5", "kind": "service"}]
    _replacement = (lambda ctx: [{'id': 'hooli', 'name': 'Hooli'}])
    for _module in (rv, resolve_args, resolve_inventory, resolve_records):
        monkeypatch.setattr(_module, 'customers', _replacement)
    _replacement = (lambda ctx: items)
    for _module in (rv, resolve_args, resolve_inventory, resolve_policy, resolve_types):
        monkeypatch.setattr(_module, '_items', _replacement)
    ctx = Ctx(NoBooks(), "company", "Books", "USD", date(2026, 11, 4))
    contract = SimpleNamespace(name="create_invoice", input_model=CreateInvoice)
    args = {"customer": "Hooli", "items": [{"item": "Copper Wire", "quantity": 5}]}
    session = rv.Session()
    # Both cross-turn reading and final occurrence accounting use the shared inventory.
    text = rv.request_words(ctx, ["invoice Hooli", "for 5 Copper Wire"], [(contract, args)])
    result = rv.resolve_args(ctx, contract, args, text, session)
    assert result.args["customer"] == "Hooli"
    assert result.args["items"] == [{"item": "Copper Wire", "quantity": "5"}]
    rv.account(ctx, text, [(contract, result.args)], [result], 0, session, [""])
    occurrences = rv.inventory(ctx, text, items)
    assert [o.value for o in occurrences if o.kind == "figure"] == [Decimal(5)]
    assert not any(o.kind in {"account", "paid_from", "method"} for o in occurrences)


@pytest.mark.parametrize("name", DATE_SHAPED_NAMES)
@pytest.mark.parametrize("role", ("payer", "supplier", "account", "item"))
@pytest.mark.parametrize("real_date", ("", " on 2026-10-24"))
def test_complete_stored_names_exclude_inner_dates(receipts, name, role, real_date):
    """The ordinary-name base result is unchanged by date-shaped spelling, with/without a real date.

    Resolve and account for a complete payment, including the item when one is written; an unrelated
    leftover question must not hide either a lost name occurrence or an invented inner date.
    """
    from dataclasses import replace
    from typing import Annotated
    from erpclaw_core.contracts import Input
    from erpclaw_core.plugins import REGISTRY
    from erpclaw_sales import RecordPayment
    from test_outgoing_account import DatedPay

    class Mention(Input):
        item: Annotated[str, {"erpclaw": {"kind": "item"}}]

    class WithItem(RecordPayment):
        items: Annotated[list[Mention], {"erpclaw": {"kind": "lines"}}]

    with receipts.begin() as conn:
        ctx = company_ctx(conn)
        if role == "payer":
            conn.execute(m.party.update().where(m.party.c.name == "Hooli").values(name=name))
        elif role == "supplier":
            create_party(ctx, "supplier", name)
        elif role == "account":
            conn.execute(m.account.update().where(m.account.c.number == "1100").values(name=name))
        else:
            create_item(ctx, name, Decimal("10"))
    contract = REGISTRY.contracts["record_payment"]
    args = {"customer": name if role == "payer" else "Hooli", "amount": 100}
    text = f"{name if role == 'payer' else 'Hooli'} paid 100"
    if role == "supplier":
        contract = replace(contract, input_model=DatedPay)
        args = {"supplier": name, "amount": 100}
        text = f"Paid {name} 100 from Bank"
    elif role == "account":
        text += f" into {name}"
    elif role == "item":
        contract = replace(contract, input_model=WithItem)
        args["items"] = [{"item": name}]
        text += f" for {name}"
    text += real_date
    before = snapshot(receipts)
    with receipts.connect() as conn:
        ctx = company_ctx(conn)
        result = rv.resolve_args(ctx, contract, args, text, rv.Session())
        date_field = "payment_date" if role == "supplier" else "date"
        assert result.args.get(date_field) == ("2026-10-24" if real_date else None), result.args
        assert result.args["amount"] == "100"
        rv.account(ctx, text, [(contract, result.args)], [result], 0, rv.Session(), [""])
        if role != "supplier":  # DatedPay's test-only date field is named payment_date.
            source, notices = rv.date_source_of(ctx, contract, contract.input_model.model_validate(result.args), text)
            assert source == ({"phrase": "on 2026-10-24", "date": "2026-10-24"} if real_date else {"default": True})
            assert notices == []
    assert snapshot(receipts) == before


def test_date_inside_payer_name_cannot_confirm_the_models_date(receipts):
    with receipts.begin() as conn:
        conn.execute(m.party.update().where(m.party.c.name == "Hooli").values(name="24/7 Services"))
    before = snapshot(receipts)
    ev = pay(receipts, "24/7 Services paid 100", customer="24/7 Services", amount=100, date="2026-07-24")
    # The base asks when the model claims a non-default date the owner never wrote.
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "date_unknown", ev
    assert snapshot(receipts) == before


@pytest.mark.parametrize("method,whole", [("by wire", "wire"), ("with PayPal", "PayPal"),
                                         ("using Cash on Delivery", "Cash on Delivery")])
@pytest.mark.parametrize("phrase,result", [("last Friday", "Received 23 Oct 2026"),
    ("next Monday", "refused"), ("on the 5th", "ask"), ("yesterday", "Received 25 Oct 2026"),
    ("2026-10-24", "Received 24 Oct 2026"), ("on 24 October", "Received 24 Oct 2026")])
def test_method_continuations_stop_before_complete_date_phrases(receipts, method, whole, phrase, result):
    assert rv.method_phrases(f"Hooli paid 100 {method} {phrase}") == [whole]
    before = snapshot(receipts)
    ev = pay(receipts, f"Hooli paid 100 {method} {phrase}", amount=100, method=whole)
    if result == "ask":
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "date_unknown", ev
    elif whole != "wire":
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "method_unknown", ev
        assert ev[0]["text"] == f"I don't know the payment method '{whole}'. How was it paid?"
    elif result == "refused":
        assert kinds(ev) == ["refused"], ev
    else:
        assert kinds(ev) == ["card"], ev
        assert lines(ev[0])["Date"] == [result]
        assert lines(ev[0])["Method"] == ["wire"]
    assert snapshot(receipts, cards=False) == before[:4]


@pytest.mark.parametrize("name", DATE_SHAPED_NAMES)
@pytest.mark.parametrize("real_date", ("", " on 2026-10-24"))
def test_consumed_method_span_excludes_inner_dates(receipts, monkeypatch, name, real_date):
    # Exercise the ownership boundary independently of today's fixed method vocabulary: once a whole
    # method span is consumed, no date reader may split it. No method-table extension is shipped here.
    from erpclaw_core.plugins import REGISTRY

    text = f"Hooli paid 100 by {name}{real_date}"
    start, end = 4, 4 + len(rv.words(name))
    _replacement = (lambda text: [(start, end)])
    for _module in (rv, resolve_inventory, resolve_payment):
        monkeypatch.setattr(_module, 'method_spans', _replacement)
    dates = []
    original_date = rv.resolve_date

    def read_date(*args, **kwargs):
        result = original_date(*args, **kwargs)
        dates.append(result)
        return result

    for _module in (rv, resolve_args, resolve_dates):
        monkeypatch.setattr(_module, 'resolve_date', read_date)
    with receipts.connect() as conn:
        ctx = company_ctx(conn)
        occs = rv.inventory(ctx, text, [], incoming=True)
        assert [o.said for o in occs if o.kind == "method"] == [name]
        assert all(not (o.start < end and o.end > start) for o in occs if o.kind != "method")
        # Method validation is deliberately later than date resolution in RecordPayment. Its existing
        # whole-method question proves the date reader got past the consumed date-shaped name.
        from erpclaw_core.common import Refusal
        with pytest.raises(Refusal) as raised:
            rv.resolve_args(ctx, REGISTRY.contracts["record_payment"], {"amount": 100}, text, rv.Session())
        assert raised.value.code == "method_unknown"
        assert raised.value.message == f"I don't know the payment method '{name}'. How was it paid?"
        assert dates == [date(2026, 10, 24) if real_date else None]


@pytest.mark.parametrize("phrase", ("past Friday", "this Monday", "coming Monday", "on last Friday",
    "on the day before yesterday", "two days ago", "twenty-one days ago", "last night", "this morning",
    "end of month", "the other day", "next November 3"))
@pytest.mark.parametrize("method,whole", (("by wire", "wire"), ("with PayPal", "PayPal"),
                                        ("using Cash on Delivery", "Cash on Delivery")))
def test_method_stops_before_other_complete_calendar_phrases(phrase, method, whole):
    assert rv.method_phrases(f"Hooli paid 100 {method} {phrase}") == [whole]


@pytest.mark.parametrize("kind", ("customer", "supplier", "account", "paid_from", "item", "method", "warehouse"))
@pytest.mark.parametrize("name,day", (("24/7 Services", date(2026, 7, 24)),
    ("Friday Foods", date(2026, 10, 23)), ("Today Media", date(2026, 10, 26))))
def test_payment_date_source_uses_the_real_phrase_outside_consumed_names(monkeypatch, kind, name, day):
    from erpclaw_core.common import Ctx
    from erpclaw_core.contracts import Contract
    from erpclaw_sales import RecordPayment

    text = f"Hooli paid 100 for {name} on {day.isoformat()}"
    end = 4 + len(rv.words(name))
    consumed = [rv.Occurrence(kind, 4, end, name, name)]
    _replacement = (lambda *args, **kwargs: consumed)
    for _module in (rv, resolve_args, resolve_inventory, resolve_policy):
        monkeypatch.setattr(_module, 'inventory', _replacement)
    _replacement = (lambda ctx: [])
    for _module in (rv, resolve_args, resolve_inventory, resolve_policy, resolve_types):
        monkeypatch.setattr(_module, '_items', _replacement)
    contract = Contract("date_source_probe", "core", "write", "Resolver test", RecordPayment)
    inp = RecordPayment(customer="Hooli", amount="100", date=day)
    ctx = Ctx(None, "company", "Books", "USD", date(2026, 10, 26))
    source, notices = rv.date_source_of(ctx, contract, inp, text)
    assert source == {"phrase": f"on {day.isoformat()}", "date": day.isoformat()}
    assert notices == []


@pytest.fixture
def supplier_only_books(monkeypatch):
    from erpclaw_core.contracts import Contract
    from erpclaw_core.plugins import REGISTRY
    from test_outgoing_account import Pay, preview, no_commit, read_set

    eng = empty_books()
    create_company(eng, "Supplier phrase books")
    with eng.begin() as conn:
        create_party(company_ctx(conn), "supplier", "May 5 Supplies")
    contract = Contract("outgoing_account_probe", "core", "write", "Resolver test", Pay,
                        preview=preview, commit=no_commit, read_set=read_set)
    monkeypatch.setitem(REGISTRY.contracts, contract.name, contract)
    monkeypatch.setitem(REGISTRY.contract_plugin, contract.name, "core")
    yield eng, contract
    drop_everything(eng)
    eng.dispose()


@pytest.fixture
def receipts():
    eng = empty_books()
    create_company(eng, "Phrase books")
    with eng.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "customer", "Hooli")
        create_item(ctx, "Copper Wire", Decimal("28"))
        customer = dict(conn.execute(select(m.party)).mappings().one())
        item = dict(conn.execute(select(m.item)).mappings().one())
        for _ in range(6):
            sales.create_invoice(ctx, customer, [(item, Decimal("10"))], date(2026, 10, 1), submit=False)
        sales.create_invoice(ctx, customer, [(item, Decimal("10"))], date(2026, 10, 1))
    yield eng
    drop_everything(eng)
    eng.dispose()


def test_literal_amount_and_document_ownership(receipts):
    engine = receipts
    ev = pay(engine, "Hooli paid 100", amount=100, customer="Hooli")
    assert kinds(ev) == ["card"]
    assert lines(ev[0])["Amount received"] == ["100.00 USD"]
    clean = nothing_written(engine)
    ev = pay(engine, "Hooli paid invoice 7", "missing", amount=500, customer="Hooli")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "amount_missing"
    assert clean()


def test_default_and_coordinated_accounts(receipts):
    engine = receipts
    assert kinds(pay(engine, "Hooli paid 100", amount=100)) == ["card"]
    _add_bank(engine, "1110", "Savings")
    clean = nothing_written(engine)
    for i, text in enumerate(("Hooli paid 100", "Hooli paid 100 into Bank or Missing")):
        ev = pay(engine, text, str(i), amount=100, account="Bank")
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "account_ambiguous", ev
        assert "Bank" in ev[0]["text"]
        assert ev[0]["options"] == (["1100 Bank", "1110 Savings"] if i == 0 else ["Bank", "Missing"])
    assert clean()


def test_bounded_bare_account_is_an_owned_occurrence(receipts):
    engine = receipts
    _add_bank(engine, "1110", "Savings")
    ev = pay(engine, "Hooli paid 100 Savings", amount=100, account="Bank")
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Account"] == ["1110 Savings"]


def test_payer_and_explicit_destination_have_contextual_ownership(receipts):
    engine = receipts
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        conn.execute(m.party.update().where(m.party.c.name == "Hooli").values(name="Chase"))
    _add_bank(engine, "1110", "Chase")
    ev = pay(engine, "Chase paid 100", amount=100, customer="Chase", account="Chase")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "account_ambiguous", ev
    assert ev[0]["options"] == ["1100 Bank", "1110 Chase"]
    with engine.begin() as conn:
        conn.execute(m.party.update().where(m.party.c.name == "Chase").values(name="Hooli"))
        create_party(company_ctx(conn), "customer", "Chase")
    ev = pay(engine, "Hooli paid 100 to Chase", "destination", amount=100, account="Bank")
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Customer"] == ["Hooli"]
    assert lines(ev[0])["Account"] == ["1110 Chase"]


def test_supplier_numeric_calendar_name_owns_its_words(supplier_only_books):
    engine, _ = supplier_only_books
    with engine.connect() as conn:
        assert conn.execute(select(t.invoice)).first() is None
        assert conn.execute(select(m.party).where(m.party.c.kind == "customer")).first() is None
    # The outgoing contract requires a source explicitly; Bank is written, so the result is a card.
    got = payment(supplier_only_books, "Paid May 5 Supplies 100 from Bank",
                  args={"supplier": "May 5 Supplies", "amount": "100"})
    assert got.kinds == ["card"], got.events
    assert got.args == {"supplier": "May 5 Supplies", "amount": "100", "pay_from": "Bank"}
    shown = dict(got.events[0]["card"]["lines"])
    assert shown["Amount"] == "100.00" and shown["Pay from"] == "1100 Bank"


def test_written_and_unwritten_invoice_choices(receipts):
    engine = receipts
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        customer = dict(conn.execute(select(m.party).where(m.party.c.name == "Hooli")).mappings().one())
        item = dict(conn.execute(select(m.item)).mappings().one())
        sales.create_invoice(ctx, customer, [(item, Decimal("10"))], date(2026, 10, 1))
    for i, phrase in enumerate(("invoice 7", "INV-2026-00007")):
        ev = pay(engine, f"Hooli paid 100 for {phrase}", str(i), amount=100, invoice="INV-2026-00008")
        assert kinds(ev) == ["card"], ev
        assert lines(ev[0])["Applied to"][0].startswith("INV-2026-00007")
    clean = nothing_written(engine)
    for i, suffix in enumerate(("", " for invoice 7 for invoice 8")):
        ev = pay(engine, "Hooli paid 100" + suffix, "unwritten" + str(i), amount=100, invoice="INV-2026-00007")
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "invoice_ambiguous", ev
    assert clean()


@pytest.mark.parametrize("phrase,expected", [
    ("yesterday", "Received 25 Oct 2026"), ("on 2026-10-24", "Received 24 Oct 2026"),
    ("on 24 October", "Received 24 Oct 2026"), ("on blursday", "ask"),
    ("yesterday today", "ask"), ("tomorrow", "refused"),
])
def test_dates_use_owner_words_and_the_existing_pinned_clock(receipts, phrase, expected):
    engine = receipts
    ev = pay(engine, f"Hooli paid 100 {phrase}", amount=100, date="2026-10-26")
    if expected in ("ask", "refused"):
        assert kinds(ev) == [expected], ev
    else:
        assert kinds(ev) == ["card"], ev
        assert lines(ev[0])["Date"] == [expected]


def test_unwritten_name_cannot_change_inventory_or_card(receipts):
    engine = receipts
    def read():
        with engine.connect() as conn:
            return Counter(o.kind for o in rv.inventory(company_ctx(conn), "Hooli paid 100 into Bank",
                                                      rv._items(company_ctx(conn)), incoming=True))
    before = read()
    original = pay(engine, "Hooli paid 100 into Bank", "before", amount=100)
    assert kinds(original) == ["card"], original
    assert lines(original[0])["Amount received"] == ["100.00 USD"]
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Token 100", Decimal("10"))
    assert read() == before
    assert before["figure"] == 1 and before["account"] == 1
    ev = pay(engine, "Hooli paid 100 into Bank", "after", amount=100)
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0]) == lines(original[0])


def test_account_number_cannot_supply_amount(receipts):
    with receipts.begin() as conn:
        conn.execute(m.account.update().where(m.account.c.number == "1100").values(name="Current 1234"))
    before = snapshot(receipts)
    ev = pay(receipts, "Hooli paid to Current 1234", amount=1234)
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "amount_missing", ev
    assert snapshot(receipts) == before


@pytest.mark.parametrize("marker", ["EUR", "euros", "€", "EUR100", "€100"])
def test_original_foreign_marker_survives_stored_currency_account(receipts, marker):
    _add_bank(receipts, "1110", "EUR")
    text = "Hooli paid " + (marker if "100" in marker else "100 " + marker) + " into Bank"
    ev = pay(receipts, text, amount=100)
    assert kinds(ev) == ["refused"] and ev[0]["code"] == "foreign_currency", ev


@pytest.mark.parametrize("marked", ["100 EUR", "100 euros", "100 €", "EUR100", "€100"])
def test_outgoing_foreign_mark_survives_a_stored_currency_account(supplier_only_books, marked):
    engine, _ = supplier_only_books
    _add_bank(engine, "1110", "EUR")
    before = snapshot(engine)
    got = payment(supplier_only_books, f"Paid May 5 Supplies {marked} from Bank",
                  args={"supplier": "May 5 Supplies", "amount": "100"})
    assert got.kinds == ["refused"] and got.events[0]["code"] == "foreign_currency", got.events
    assert snapshot(engine) == before


@pytest.mark.parametrize("marked", ["5 €", "5€", "€5", "€ 5", "5 EUR", "EUR 5", "5 %", "5%", "5 $", "5 USD",
                                    "5EUR", "EUR5", "$5", "$ 5", "USD5", "USD 5", "5USD", "%5", "% 5"])
@pytest.mark.parametrize("outgoing", [False, True])
def test_numeric_name_marker_is_ambiguity_not_an_amount(request, marked, outgoing):
    books = request.getfixturevalue("books") if outgoing else None
    engine = books[0] if outgoing else request.getfixturevalue("receipts")
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Token 5", Decimal("10"))
    before = snapshot(engine)
    text = f"Hooli paid {'supplier ' if outgoing else ''}100 for Token {marked} {'from' if outgoing else 'into'} Bank"
    if outgoing:
        got = payment(books, text.replace("Hooli paid supplier", "pay Copperline"),
                      args={"supplier": "Copperline", "amount": "100"})
        ev = got.events
    else:
        ev = pay(engine, text, amount=100)
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "money_name_ambiguous", ev
    assert "100" in ev[0]["text"] and "Token" in ev[0]["text"], ev
    assert snapshot(engine) == before


def test_marker_guard_carries_weight_without_leftover_item_asking(receipts, monkeypatch):
    with receipts.begin() as conn:
        create_item(company_ctx(conn), "Token 5", Decimal("10"))
    # Deliberately leave accounting intact: marker ambiguity must precede an unrelated item question.
    ev = pay(receipts, "Hooli paid 100 for Token 5 EUR into Bank", amount=100)
    assert ev[0]["code"] == "money_name_ambiguous", ev
    original = rv._marks_at
    def no_inner_marks(text, start, end):
        return [] if text[start:end] == "5" else original(text, start, end)
    for _module in (rv, resolve_amounts, resolve_payment, resolve_records):
        monkeypatch.setattr(_module, '_marks_at', no_inner_marks)
    ev = pay(receipts, "Hooli paid 100 for Token 5 EUR into Bank", "mutated", amount=100)
    assert ev[0].get("code") != "money_name_ambiguous", ev


@pytest.mark.parametrize("phrase,whole", [
    ("by Cash App", "Cash App"), ("with PayPal", "PayPal"), ("using Cash on Delivery", "Cash on Delivery"),
    ("by card on file", "card on file"), ("via Cash App", "Cash App"), ("in PayPal", "PayPal"),
])
def test_unknown_method_is_read_whole_to_the_account_introducer(receipts, phrase, whole):
    before = snapshot(receipts)
    ev = pay(receipts, f"Hooli paid 100 {phrase} into Bank", amount=100, method=whole.split()[0])
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "method_unknown", ev
    assert ev[0]["text"] == f"I don't know the payment method '{whole}'. How was it paid?"
    assert snapshot(receipts) == before


def test_known_method_stops_before_account_introducer(receipts):
    ev = pay(receipts, "Hooli paid 100 by wire into Bank", amount=100, method="cash")
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Method"] == ["wire"]
    assert lines(ev[0])["Account"] == ["1100 Bank"]


def test_several_known_methods_ask_with_whole_options(receipts):
    ev = pay(receipts, "Hooli paid 100 by cash or wire", amount=100, method="cash")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "method_ambiguous", ev
    assert ev[0]["options"] == ["cash", "wire"]


@pytest.mark.parametrize("phrase,options", [
    ("cash or Zelle", ["cash", "Zelle"]),
    ("Cash App or wire", ["Cash App", "wire"]),
    ("PayPal or Zelle", ["PayPal", "Zelle"]),
])
def test_coordinated_unknown_methods_keep_every_whole_option(receipts, phrase, options):
    before = snapshot(receipts)
    ev = pay(receipts, f"Hooli paid 100 by {phrase}", amount=100, method="cash")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "method_ambiguous", ev
    assert ev[0]["options"] == options
    assert snapshot(receipts) == before


def test_choosing_an_unknown_method_still_asks_before_a_card(receipts):
    stale = Reply("", [call("record_payment", amount=100, method="cash")])
    a = turns(receipts, stale, stale, stale)
    before = snapshot(receipts)
    first = a.turn("unknown-method-choice", "Hooli paid 100 by cash or Zelle")
    assert kinds(first) == ["ask"] and first[0]["code"] == "method_ambiguous", first
    second = a.turn("unknown-method-choice", "Zelle")
    assert kinds(second) == ["ask"] and second[0]["code"] == "method_unknown", second
    assert second[0]["text"] == "I don't know the payment method 'Zelle'. How was it paid?"
    assert snapshot(receipts) == before
    final = a.turn("unknown-method-choice", "wire")
    assert kinds(final) == ["card"], final
    assert lines(final[0])["Method"] == ["wire"]


def test_method_words_in_account_name_are_data(receipts):
    with receipts.begin() as conn:
        conn.execute(m.account.update().where(m.account.c.number == "1100").values(name="Cash in Hand"))
    ev = pay(receipts, "Hooli paid 100 into Cash in Hand", amount=100, method="cash")
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Method"] == ["not stated (will be saved as not recorded)"]
    assert lines(ev[0])["Account"] == ["1100 Cash in Hand"]


def test_later_account_and_method_answer_supersedes_binding(receipts):
    _add_bank(receipts, "1110", "Savings")
    stale = Reply("", [call("record_payment", customer="Hooli", amount=100, account="Bank", method="PayPal")])
    a = turns(receipts, stale, stale, stale)
    before = snapshot(receipts)
    first = a.turn("correction", "Hooli paid 100 into Bank or Savings by PayPal")
    assert kinds(first) == ["ask"] and first[0]["code"] == "account_ambiguous", first
    second = a.turn("correction", "Bank")
    assert kinds(second) == ["ask"] and second[0]["code"] == "method_unknown", second
    assert snapshot(receipts) == before
    final = a.turn("correction", "wire into Savings")
    assert kinds(final) == ["card"], final
    assert lines(final[0])["Account"] == ["1110 Savings"]
    assert lines(final[0])["Method"] == ["wire"]


def test_later_method_correction_while_invoice_pends(receipts):
    with receipts.begin() as conn:
        ctx = company_ctx(conn)
        customer = dict(conn.execute(select(m.party).where(m.party.c.name == "Hooli")).mappings().one())
        item = dict(conn.execute(select(m.item)).mappings().one())
        sales.create_invoice(ctx, customer, [(item, Decimal("10"))], date(2026, 10, 1))
    stale = Reply("", [call("record_payment", customer="Hooli", amount=100, method="cash")])
    a = turns(receipts, stale, stale, stale)
    before = snapshot(receipts)
    first = a.turn("method-correction", "Hooli paid 100 into Bank by cash or wire")
    assert kinds(first) == ["ask"] and first[0]["code"] == "method_ambiguous", first
    second = a.turn("method-correction", "cash")
    assert kinds(second) == ["ask"] and second[0]["code"] == "invoice_ambiguous", second
    assert snapshot(receipts) == before
    final = a.turn("method-correction", "invoice 7 actually wire")
    assert kinds(final) == ["card"], final
    assert lines(final[0])["Method"] == ["wire"]
    assert lines(final[0])["Account"] == ["1100 Bank"]
    assert lines(final[0])["Applied to"][0].startswith("INV-2026-00007")


def test_within_message_latest_selected_account_leaves_earlier_ask(receipts):
    _add_bank(receipts, "1110", "Savings")
    ev = pay(receipts, "Hooli paid 100 into Bank into Savings", amount=100, account="Savings")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "account_ambiguous", ev
    assert "Bank" in ev[0]["text"] and "Savings" not in ev[0]["text"], ev


def test_new_ambiguity_reopens_a_settled_account(receipts):
    _add_bank(receipts, "1110", "Savings")
    a = turns(receipts, *[Reply("", [call("record_payment", amount=100, account="Bank", method="PayPal")])] * 3)
    a.turn("reask", "Hooli paid 100 into Bank or Savings by PayPal")
    a.turn("reask", "Bank")
    ev = a.turn("reask", "wire into Savings or Missing")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "account_ambiguous", ev
    assert ev[0]["options"] == ["Savings", "Missing"]


@pytest.mark.parametrize("later,keep,clear", [
    ("into Savings", "source", "destination"), ("from Savings", "destination", "source"),
])
def test_incoming_and_outgoing_corrections_have_separate_kinds(receipts, later, keep, clear):
    _add_bank(receipts, "1110", "Savings")
    session = rv.Session(bindings={"destination": "Bank", "source": "Bank",
        "settled:destination": {"through": 1}, "settled:source": {"through": 1}},
        owner_messages=[{"id": 1, "text": "Bank", "spans": []}, {"id": 2, "text": later, "spans": []}])
    with receipts.connect() as conn:
        ctx = company_ctx(conn)
        for path, kind in (("destination", "account"), ("source", "paid_from")):
            rv.payment_binding(ctx, later, session, path, kind)
    assert session.bindings[keep] == "Bank"
    assert clear not in session.bindings


def test_source_choice_answer_does_not_correct_the_incoming_destination(receipts):
    _add_bank(receipts, "1110", "Savings")
    session = rv.Session(
        pending={"id": "q2", "field": "source", "payment_kind": "paid_from",
                 "choices": [{"code": "q2-1", "label": "Savings", "ref": "Savings"}]},
        bindings={"destination": "Bank", "settled:destination": {"kind": "account", "through": 1}},
        owner_messages=[{"id": 1, "text": "Bank", "spans": []},
                        {"id": 2, "text": "Savings", "answer": 1, "field": "source", "spans": []}])
    assert session.answer("q2-1")
    with receipts.connect() as conn:
        assert rv.payment_binding(company_ctx(conn), "Savings", session, "destination", "account")
    assert session.bindings["destination"] == "Bank"
    assert session.bindings["source"] == "Savings"


def test_failed_owned_name_projection_asks_instead_of_unmasking(receipts, monkeypatch):
    _replacement = (lambda *args, **kwargs: [])
    for _module in (rv, resolve_args, resolve_inventory, resolve_policy, resolve_words):
        monkeypatch.setattr(_module, 'consumed_ranges', _replacement)
    before = snapshot(receipts)
    ev = pay(receipts, "Hooli paid 100 into Bank", amount=100)
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "amount_missing", ev
    assert snapshot(receipts) == before


def test_marker_mutation_makes_card_when_item_is_claimed(receipts, monkeypatch):
    from dataclasses import replace
    from typing import Annotated
    from erpclaw_core import approvals
    from erpclaw_core.contracts import Input
    from erpclaw_core.common import Refusal
    from erpclaw_core.plugins import REGISTRY
    from erpclaw_sales import RecordPayment

    class Mention(Input):
        item: Annotated[str, {"erpclaw": {"kind": "item"}}]

    class WithItem(RecordPayment):
        items: Annotated[list[Mention], {"erpclaw": {"kind": "lines"}}]

    with receipts.begin() as conn:
        create_item(company_ctx(conn), "Token 5", Decimal("10"))
    contract = replace(REGISTRY.contracts["record_payment"], input_model=WithItem)
    text = "Hooli paid 100 for Token 5€ into Bank"
    before = snapshot(receipts)

    def attempt():
        session = rv.Session(bindings={"items.0.item": "Token 5"})
        with receipts.connect() as conn:
            ctx = company_ctx(conn)
            result = rv.resolve_args(ctx, contract, {"amount": 100, "items": [{"item": "Token 5"}]}, text, session)
            rv.account(ctx, text, [(contract, result.args)], [result], 0, session, [""])
        return approvals.card_events(approvals.propose(receipts, contract, result.args))

    with pytest.raises(Refusal) as raised:
        attempt()
    assert raised.value.code == "money_name_ambiguous"
    assert snapshot(receipts) == before
    original = rv._marks_at
    _replacement = (lambda text, start, end: [] if text[start:end] == '5' else original(text, start, end))
    for _module in (rv, resolve_amounts, resolve_payment, resolve_records):
        monkeypatch.setattr(_module, '_marks_at', _replacement)
    ev = attempt()
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Amount received"] == ["100.00 USD"]
    assert lines(ev[0])["Applied to"][0].startswith("INV-2026-00007")
    assert snapshot(receipts, cards=False) == before[:4]


def test_superseding_an_account_preserves_unowned_marker_characters(receipts):
    with receipts.begin() as conn:
        create_item(company_ctx(conn), "Token 5", Decimal("10"))
    _add_bank(receipts, "1110", "Savings")
    with receipts.connect() as conn:
        text = rv.request_words(company_ctx(conn), ["Hooli paid 100 for Token 5 € into Bank", "into Savings"], [])
    assert "€" in text


@pytest.mark.parametrize("text,code", [
    ("Hooli2 paid 100 for invoice 7", "customer_ambiguous"),
    ("Hooli paid 100 into Missing1234", "account_unknown"),
])
def test_explicit_unknown_alphanumeric_party_or_account_is_asked(receipts, text, code):
    ev = pay(receipts, text, amount=100, customer="Hooli", invoice="INV-2026-00007", account="Bank")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == code, ev


def test_alphanumeric_values_keep_their_explicit_phrase_positions():
    assert rv.payer_phrases("Hooli2 paid 100 for invoice 7") == ["Hooli2"]
    assert rv.account_phrases("Hooli paid 100 into Missing1234") == [(4, 5)]


def test_existing_non_payment_document_selection_keeps_written_argument():
    mark = {"prefix": "INV", "nouns": ("invoice",), "what": "invoice", "resolve": lambda ctx, said: said}
    args = {"invoice": "8"}
    rv._document(None, mark, "invoice", "required", args, "cancel invoice 7 and invoice 8", rv.Session())
    assert args == {"invoice": "8"}


def test_amount_correction_keeps_settled_method_occurrences(receipts):
    with receipts.begin() as conn:
        ctx = company_ctx(conn)
        customer = dict(conn.execute(select(m.party).where(m.party.c.name == "Hooli")).mappings().one())
        item = dict(conn.execute(select(m.item)).mappings().one())
        sales.create_invoice(ctx, customer, [(item, Decimal("10"))], date(2026, 10, 1))
    a = turns(receipts,
              Reply("", [call("record_payment", amount=100, method="cash")]),
              Reply("", [call("record_payment", amount=100, method="cash")]),
              Reply("", [call("record_payment", amount=200, method="cash")]))
    ev = a.turn("amount-correction", "Hooli paid 100 into Bank by cash or wire")
    assert ev[0]["code"] == "method_ambiguous", ev
    ev = a.turn("amount-correction", "cash")
    assert ev[0]["code"] == "invoice_ambiguous", ev
    ev = a.turn("amount-correction", "invoice 7, make it 200")
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Method"] == ["cash"]
    assert lines(ev[0])["Amount received"] == ["200.00 USD"]


def test_unknown_incoming_account_answer_settles_the_offered_occurrence(receipts):
    stale = Reply("", [call("record_payment", amount=100, account="Missing")])
    a = turns(receipts, stale, stale)
    first = a.turn("unknown-account", "Hooli paid 100 into Missing")
    assert kinds(first) == ["ask"] and first[0]["code"] == "account_unknown", first
    final = a.turn("unknown-account", "1100 Bank")
    assert kinds(final) == ["card"], final
    assert lines(final[0])["Account"] == ["1100 Bank"]


def test_invoice_to_a_customer_keeps_its_customer_occurrence(receipts):
    a = turns(receipts, Reply("", [call("create_invoice", customer="Hooli",
                                        items=[{"item": "Copper Wire", "quantity": 5}])]))
    ev = a.turn("invoice-to", "invoice to Hooli for 5 Copper Wire")
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Customer"] == ["Hooli"]


@pytest.mark.parametrize("cue", ["by", "via", "with", "in", "using"])
def test_method_after_account_keeps_both_fields(receipts, cue):
    ev = pay(receipts, f"Hooli paid 100 into Bank {cue} wire", amount=100, method="cash")
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Account"] == ["1100 Bank"]
    assert lines(ev[0])["Method"] == ["wire"]


def test_account_name_keeps_literal_field_words(receipts):
    with receipts.begin() as conn:
        conn.execute(m.account.update().where(m.account.c.number == "1100").values(name="Current into 1234"))
    ev = pay(receipts, "Hooli paid 100 into Current into 1234", amount=100)
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Account"] == ["1100 Current into 1234"]


@pytest.mark.parametrize("cue", ["into", "to"])
def test_unfinished_account_instruction_cannot_choose_a_default(receipts, cue):
    before = snapshot(receipts)
    ev = pay(receipts, "Hooli paid 100 " + cue, amount=100)
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "account_ambiguous", ev
    assert ev[0]["options"] == ["1100 Bank"]
    assert snapshot(receipts) == before


def test_marker_cleanup_cannot_erase_a_literal_stored_name():
    assert rv.payment_name_spans("Token USD5", [{"name": "Token USD5"}], item=True) == [rv.Found("Token USD5", 0, 2)]


@pytest.mark.parametrize("written", ["EUR100", "US$100", "100EUR", "euros100", "100euros"])
def test_standalone_currency_figures_bound_a_full_supplier_name(written):
    assert rv.resolve_record([{"name": "May 5 Supplies"}], "supplier",
                             f"Paid May 5 Supplies {written} from Bank", "May 5 Supplies",
                             rv.Session(), "supplier", item=False) == "May 5 Supplies"


def test_setting_aside_method_choices_keeps_the_empty_binding():
    from typing import Annotated
    from erpclaw_core.common import Ctx
    from erpclaw_core.contracts import Contract, Input

    class MethodOnly(Input):
        method: Annotated[str | None, {"erpclaw": {"kind": "text", "words": "methods"}}] = None

    contract = Contract("method_probe", "core", "write", "Resolver test", MethodOnly)
    session = rv.Session(pending={"id": "q1", "field": "method", "choices": []})
    session.control("someone_else")
    ctx = Ctx(None, "company", "Books", "USD", date(2026, 10, 26))
    assert rv.resolve_args(ctx, contract, {}, "by cash", session).args == {"method": None}


def test_currency_figure_bounds_belong_to_their_own_word_position():
    from erpclaw_core.common import Refusal

    with pytest.raises(Refusal) as raised:
        rv.resolve_record([{"name": "Hooli"}], "supplier", "pay Hooli US100 then US$100", "Hooli",
                          rv.Session(), "supplier", item=False)
    assert raised.value.code == "supplier_ambiguous"
    assert rv.resolve_record([{"name": "Hooli"}], "supplier", "pay Hooli US$100 then US100", "Hooli",
                             rv.Session(), "supplier", item=False) == "Hooli"


def test_confirmed_invoice_item_keeps_its_whole_occurrence(money_books):
    from test_money_spans import assistant, request, question, answer, card

    events = assistant(money_books).turn("money", request("item"))
    assert question(events)["code"] == "item_ambiguous"
    result = card(answer(assistant(money_books), events))
    assert ("Total", "140.00 USD") in [tuple(row) for row in result["card"]["lines"]]


def test_sales_order_calendar_date_owns_its_written_year(money_books):
    from test_money_spans import card

    order = call("create_sales_order", customer="Acme Corporation",
                 items=[{"item": "Copper Wire", "quantity": 5}], delivery_date="2026-11-30")
    a = turns(money_books, Reply("", [order]))
    card(a.turn("order", "Order for Acme Corporation for 5 Copper Wire on 30 November 2026"))


def test_name_confirmation_preserves_completed_item_consumption_for_payment(money_books):
    from test_money_spans import assistant, confirmation, choose, card

    first = assistant(money_books, customer="Discount Depot", items=[{"item": "Design Work", "quantity": 5}]).turn(
        "money", "Invoice Discount Depot for 5 Design Work")
    confirmation(first, "Discount Depot")
    a = turns(money_books, Reply("", [call("record_payment", customer="Discount Depot", amount=5)]))
    result = card(choose(a, first, "Discount Depot"))
    assert ("Customer", "Discount Depot") in [tuple(row) for row in result["card"]["lines"]]


@pytest.mark.parametrize("marker", ("EUR", "GBP", "USD", "JPY", "%"))
@pytest.mark.parametrize("outgoing", (False, True))
@pytest.mark.parametrize("correction", ("two_turns", "same_message"))
def test_amount_markers_survive_account_corrections(books, marker, outgoing, correction):
    """Base oracle: foreign marks refuse; USD and independent percent figures make the base card.

    Leave the model's amount out on turn one so the literal-amount question keeps the request
    open; the second turn supplies it while correcting the account. The earlier marked figure
    remains the owner's even though its spelling is also a chart name.
    """
    engine, contract = books[0], books[1] if outgoing else None
    if not outgoing:
        from test_payment import new_invoice
        new_invoice(engine)
    _add_bank(engine, "1120", marker)
    from erpclaw_core.plugins import REGISTRY
    from erpclaw_assistant.model import ToolCall
    contract = contract or REGISTRY.contracts["record_payment"]
    party = {"supplier": "Copperline"} if outgoing else {"customer": "Hooli"}
    start = "pay Copperline" if outgoing else "Hooli paid"
    cue = "from" if outgoing else "into"
    first_call = ToolCall("first", contract.name, party)
    final_call = ToolCall("final", contract.name, {**party, "amount": "100"})
    a = turns(engine, Reply("", [first_call]), Reply("", [final_call]))
    before = snapshot(engine)
    if correction == "two_turns":
        first = a.turn("marker-correction", f"{start} 100 {marker}")
        assert kinds(first) == ["ask"] and first[0]["code"] == "amount_missing", first
        assert snapshot(engine) == before
        events = a.turn("marker-correction", f"{cue} Bank")
    else:
        a = turns(engine, Reply("", [final_call]))
        events = a.turn("marker-correction", f"{start} 100 {marker} {cue} Bank")
    if marker in {"EUR", "GBP", "JPY"}:
        assert kinds(events) == ["refused"] and events[0]["code"] == "foreign_currency", events
        assert snapshot(engine) == before
    else:
        assert kinds(events) == ["card"], events
        shown = lines(events[0])
        assert shown["Pay from" if outgoing else "Account"] == ["1100 Bank"], events
        assert shown["Amount" if outgoing else "Amount received"] == ["100.00" if outgoing else "100.00 USD"], events


@pytest.mark.parametrize("kind", ("account", "method", "customer", "item"))
@pytest.mark.parametrize("marker", ("EUR", "GBP", "USD", "JPY", "%"))
def test_corrections_of_other_kinds_preserve_attached_amount_markers(receipts, monkeypatch, kind, marker):
    """Exercise the common supersession boundary independently of field-resolution order."""
    from erpclaw_core.plugins import REGISTRY
    original = rv.inventory
    text = f"Hooli paid 100 {marker}"
    marker_index = len(rv.words(text)) - 1
    def inventory(ctx, said, items, **kwargs):
        found = original(ctx, said, items, **kwargs)
        # Only genuine non-marker occurrences are eligible for supersession. Use the real
        # inventory for the first message, and a correction of each kind for the second.
        return found if said == text else [rv.Occurrence(kind, 0, 1, "replacement", "replacement")]
    for _module in (rv, resolve_args, resolve_inventory, resolve_policy):
        monkeypatch.setattr(_module, 'inventory', inventory)
    _add_bank(receipts, "1120", marker)
    with receipts.connect() as conn:
        ctx = company_ctx(conn)
        occurrences = original(ctx, text, rv._items(ctx), incoming=True)
        assert not any(o.kind in {"account", "method", "customer", "item"} and
                       o.start <= marker_index < o.end for o in occurrences), occurrences
        kept = rv.request_words(ctx, [text, "replacement"],
                                [(REGISTRY.contracts["record_payment"], {"amount": 100})])
        # A binding recorded before the current inventory must also be unable to mask a marker.
        _replacement = (lambda ctx, said, items, **kwargs: [rv.Occurrence(kind, marker_index, marker_index + 1, marker, marker)] if said == text else [rv.Occurrence(kind, 0, 1, 'replacement', 'replacement')])
        for _module in (rv, resolve_args, resolve_inventory, resolve_policy):
            monkeypatch.setattr(_module, 'inventory', _replacement)
        settled = rv.request_words(ctx, [text, "replacement"],
                                   [(REGISTRY.contracts["record_payment"], {"amount": 100})])
    assert marker in kept, kept
    assert marker in settled, settled


@pytest.mark.parametrize("phrase", ("sold by the hour at 80", "80 per hour", "at 80 each"))
def test_rate_backed_new_item_prices_keep_base_resolution(engine, phrase):
    from erpclaw_core.plugins import REGISTRY
    contract = REGISTRY.contracts["add_item"]
    text = f"add an item called Bookkeeping, {phrase}"
    args = {"name": "Bookkeeping", "unit": "hour" if "hour" in phrase else "each", "price": "80"}
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        session = rv.Session()
        result = rv.resolve_args(ctx, contract, args, text, session)
        rv.account(ctx, text, [(contract, result.args)], [result], 0, session, [""])
    assert result.args == args
    assert result.not_done == []


@pytest.mark.parametrize("operation", ("create_invoice", "create_sales_order", "create_credit_note"))
@pytest.mark.parametrize("phrase", ("5 Copper Wire at 28.00", "5 Copper Wire at 28 each",
                                    "Copper Wire 5 at 28.00", "Copper Wire 5 at 28 each",
                                    "5 Copper Wire 28 per hour", "5 Copper Wire sold by the hour at 28"))
def test_line_item_rates_keep_base_resolution(engine, operation, phrase):
    from erpclaw_core.plugins import REGISTRY
    contract = REGISTRY.contracts[operation]
    credit = operation == "create_credit_note"
    text = f"{'credit INV-2026-00003 for' if credit else 'invoice Hooli for'} {phrase}"
    args = {**({"invoice": "INV-2026-00003"} if credit else {"customer": "Hooli"}),
            "items": [{"item": "Copper Wire", "quantity": 5}]}
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        session = rv.Session(bindings={"everything": False} if credit else {})
        if "sold by the hour" in phrase:
            from erpclaw_core.common import Refusal
            with pytest.raises(Refusal) as raised:
                rv.resolve_args(ctx, contract, args, text, session)
            assert raised.value.code == "item_ambiguous"
            assert raised.value.options == ["Copper Wire", "someone else"]
            return
        result = rv.resolve_args(ctx, contract, args, text, session)
        rv.account(ctx, text, [(contract, result.args)], [result], 0, session, [""])
    assert result.args["items"][0]["item"] == "Copper Wire"
    assert result.args["items"][0]["quantity"] == "5"
    assert result.not_done == []


@pytest.mark.parametrize("marker", ("EUR", "GBP", "JPY"))
@pytest.mark.parametrize("outgoing", (False, True))
def test_currency_marker_within_message_account_correction(books, marker, outgoing):
    from erpclaw_core.common import Refusal
    from erpclaw_core.plugins import REGISTRY

    engine, contract = books
    _add_bank(engine, "1120", marker)
    contract = contract if outgoing else REGISTRY.contracts["record_payment"]
    cue = "from" if outgoing else "into"
    party = {"supplier": "Copperline"} if outgoing else {"customer": "Hooli"}
    text = f"{'pay Copperline' if outgoing else 'Hooli paid'} 100 {marker} {cue} Savings {cue} Bank"
    before = snapshot(engine)
    with engine.connect() as conn:
        with pytest.raises(Refusal) as raised:
            rv.resolve_args(company_ctx(conn), contract, {**party, "amount": 100}, text, rv.Session())
    assert raised.value.code == "foreign_currency"
    assert snapshot(engine) == before


@pytest.mark.parametrize("role", ("payer", "supplier", "item", "paid_from"))
def test_currency_marker_cannot_supply_another_payment_field(books, role):
    from dataclasses import replace
    from typing import Annotated
    from erpclaw_core.common import Refusal, is_question
    from erpclaw_core.contracts import Input
    from erpclaw_core.plugins import REGISTRY
    from erpclaw_sales import RecordPayment

    class Mention(Input):
        item: Annotated[str, {"erpclaw": {"kind": "item"}}]

    class WithItem(RecordPayment):
        items: Annotated[list[Mention], {"erpclaw": {"kind": "lines"}}]

    engine, outgoing = books
    contract = outgoing if role in {"supplier", "paid_from"} else REGISTRY.contracts["record_payment"]
    args = {"amount": 100}
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if role == "payer":
            create_party(ctx, "customer", "USD")
            args["customer"] = "USD"
            text = "USD 100 paid into Bank"
        elif role == "supplier":
            create_party(ctx, "supplier", "USD")
            args["supplier"] = "USD"
            text = "pay USD 100 from Bank"
        elif role == "item":
            create_item(ctx, "USD", Decimal("10"))
            contract = replace(contract, input_model=WithItem)
            args.update(customer="Hooli", items=[{"item": "USD"}])
            text = "Hooli paid 100 USD into Bank"
        else:
            args["supplier"] = "Copperline"
            text = "pay Copperline from USD 100"
    if role == "paid_from":
        _add_bank(engine, "1120", "USD")
    before = snapshot(engine)
    with engine.connect() as conn:
        with pytest.raises(Refusal) as raised:
            rv.resolve_args(company_ctx(conn), contract, args, text, rv.Session())
    assert is_question(raised.value.code)
    assert raised.value.code.startswith({"payer": "customer", "supplier": "supplier",
                                         "item": "item", "paid_from": "account"}[role])
    assert snapshot(engine) == before


@pytest.mark.parametrize("kind", ("account", "paid_from", "customer", "item"))
@pytest.mark.parametrize("marker", ("EUR", "%"))
def test_record_correction_cannot_remove_a_marked_numeric_name(books, kind, marker):
    from erpclaw_core.common import Refusal
    from erpclaw_core.plugins import REGISTRY

    engine, outgoing = books
    name = {"account": "Current 5", "paid_from": "Current 5",
            "customer": "Payer 5", "item": "Token 5"}[kind]
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if kind == "customer":
            create_party(ctx, "customer", name)
        elif kind == "item":
            create_item(ctx, name, Decimal("10"))
    if kind in {"account", "paid_from"}:
        _add_bank(engine, "1120", name)
    first, later = {
        "account": (f"Hooli paid 100 into {name} {marker}", "into Bank"),
        "paid_from": (f"pay Copperline 100 from {name} {marker}", "from Bank"),
        "customer": (f"{name} {marker} paid 100 into Bank", "Hooli"),
        "item": (f"Hooli paid 100 for {name} {marker} into Bank", "for Copper Wire"),
    }[kind]
    contract = outgoing if kind == "paid_from" else REGISTRY.contracts["record_payment"]
    args = {"amount": 100, "customer": "Hooli", "items": [{"item": "Copper Wire"}]}
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        text = rv.request_words(ctx, [first, later], [(contract, args)])
        occurrences = rv.inventory(ctx, text, rv._items(ctx), outgoing=kind == "paid_from", incoming=True)
        with pytest.raises(Refusal) as raised:
            rv.guard_payment_names(text, occurrences, rv.Session(), "amount")
    assert raised.value.code == "money_name_ambiguous"


@pytest.mark.parametrize("name", ("EUR", "GBP", "USD", "JPY"))
@pytest.mark.parametrize("outgoing", (False, True))
def test_currency_spelling_as_explicit_account_keeps_base_card(books, name, outgoing):
    engine, contract = books
    _add_bank(engine, "1120", name)
    if outgoing:
        got = payment(books, f"pay Copperline 100 from {name}", args={"supplier": "Copperline", "amount": 100})
        assert got.kinds == ["card"], got.events
        assert ["Pay from", f"1120 {name}"] in got.events[0]["card"]["lines"]
    else:
        from test_payment import new_invoice
        new_invoice(engine)
        events = pay(engine, f"Hooli paid 100 into {name}", customer="Hooli", amount=100)
        assert kinds(events) == ["card"], events
        assert lines(events[0])["Account"] == [f"1120 {name}"]


@pytest.mark.parametrize("tail", ("", " by wire", " yesterday"))
def test_document_reference_after_on_is_not_an_unknown_date(receipts, tail):
    from erpclaw_core.plugins import REGISTRY
    contract = REGISTRY.contracts["record_payment"]
    with receipts.connect() as conn:
        result = rv.resolve_args(company_ctx(conn), contract,
            {"customer": "Hooli", "amount": 100, "invoice": "INV-2026-09999"},
            f"Hooli paid 100 on INV-2026-00007{tail}", rv.Session())
    assert "invoice" not in result.args
    assert result.args.get("date") == ("2026-10-25" if "yesterday" in tail else None)


@pytest.mark.parametrize("prefix", ("from", "from the", "from account"))
@pytest.mark.parametrize("marker", ("EUR", "USD", "%"))
def test_complete_numeric_source_names_keep_markers_through_corrections(books, prefix, marker):
    from erpclaw_core.common import Refusal

    engine, contract = books
    _add_bank(engine, "1120", "Current 5")
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        with pytest.raises(Refusal) as missing:
            rv.resolve_args(ctx, contract, {"supplier": "Copperline", "amount": 5},
                            f"pay Copperline {prefix} Current 5 {marker}", rv.Session())
        assert missing.value.code == "amount_missing"
        text = rv.request_words(ctx,
            [f"pay Copperline 100 {prefix} Current 5 {marker}", "from Bank"],
            [(contract, {"supplier": "Copperline", "amount": 100})])
        occurrences = rv.inventory(ctx, text, rv._items(ctx), outgoing=True, incoming=False)
        with pytest.raises(Refusal) as ambiguous:
            rv.guard_payment_names(text, occurrences, rv.Session(), "amount")
        assert ambiguous.value.code == "money_name_ambiguous"
