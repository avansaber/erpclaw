from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_core.common import company_ctx
from erpclaw_core.masters import create_item
from erpclaw_sales import CreateInvoice, tables


def invoice(rate=None, item="copper wire", quantity=5):
    line = {"item": item, "quantity": quantity}
    if rate is not None:
        line["rate"] = rate
    return call("create_invoice", customer="Hooli", items=[line])


def make(engine, write=None):
    return Assistant(engine, ScriptedModel([Reply("", [write or invoice()])] * 5))


def card(events):
    return next(e for e in events if e["type"] == "card")


def test_stated_rate_card_and_commit_match_books(engine):
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one()
    events = make(engine).turn("rate", "invoice Hooli for 5 copper wire at 30 each")
    assert [e["type"] for e in events] == ["card"], events
    c = card(events)
    assert any("@ 30.00 = 150.00" in v for k, v in c["card"]["lines"] if k == "Item")
    assert [v for k, v in c["card"]["lines"] if k == "Total"] == ["150.00 USD"]
    assert any(k == "List price" and "28.00" in v and "10.00" in v for k, v in c["card"]["lines"])
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == before
    result = approvals.approve(engine, c["approval_id"], c["digest"])
    assert result.status == "committed"
    with engine.connect() as conn:
        row = conn.execute(select(tables.invoice.c.total, tables.invoice_line.c.quantity,
                                  tables.invoice_line.c.rate, tables.invoice_line.c.amount)
                           .join(tables.invoice_line, tables.invoice_line.c.invoice_id == tables.invoice.c.id)
                           .where(tables.invoice.c.number == result.data["number"])).one()
    assert tuple(row) == (Decimal("150.00"), Decimal("5"), Decimal("30"), Decimal("150.00"))


def test_model_only_rate_cannot_set_price(engine):
    events = make(engine, invoice("1")).turn("model", "invoice Hooli for 5 copper wire")
    c = card(events)
    assert any("@ 28.00 = 140.00" in v for k, v in c["card"]["lines"] if k == "Item")


@pytest.mark.parametrize("rate", ["0", "-1", "1.00001", "1.00000"])
def test_contract_rejects_invalid_stated_rates(rate):
    with pytest.raises(ValidationError):
        CreateInvoice.model_validate({"customer": "Hooli", "items": [{"item": "Copper Wire", "quantity": 5, "rate": rate}]})


@pytest.mark.parametrize("rate", ["10", "50"])
def test_extreme_rate_warns_and_compares_to_list(engine, rate):
    c = card(make(engine).turn("extreme", f"invoice Hooli for 5 copper wire at {rate} each"))
    assert c["card"]["warnings"] and any(k == "List price" for k, _ in c["card"]["lines"])


def test_memo_decoy_requires_choice_and_stores_selected_rate(engine):
    a = make(engine)
    events = a.turn("memo", "invoice Hooli for 5 copper wire at 30 each, memo 99 each")
    assert not any(e["type"] == "card" for e in events)
    q = events[-1]
    assert q["code"] == "rate_choice_missing" and {"30.00", "99.00"} <= {c["label"] for c in q["choices"]}, events
    events = a.turn("memo", "30.00")
    assert not any(e["type"] == "card" for e in events), events
    assert events[-1]["code"] == "memo_choice_missing", events
    label = next(c["label"] for c in events[-1]["choices"] if c["label"].lower().startswith("leave out"))
    c = card(a.turn("memo", label))
    assert any("@ 30.00 = 150.00" in v for k, v in c["card"]["lines"] if k == "Item")
    result = approvals.approve(engine, c["approval_id"], c["digest"])
    assert result.status == "committed"
    with engine.connect() as conn:
        row = conn.execute(select(tables.invoice_line.c.rate, tables.invoice_line.c.amount)
                           .join(tables.invoice, tables.invoice.c.id == tables.invoice_line.c.invoice_id)
                           .where(tables.invoice.c.number == result.data["number"])).one()
    assert tuple(row) == (Decimal("30"), Decimal("150"))


@pytest.mark.parametrize("phrase", [" to be delivered by Friday", ", delivered by Friday",
                                    " to be shipped by 2026-11-30", ", delivered by 3 November 2026",
                                    ", shipped by 30/11/2026"])
def test_delivery_date_is_not_invoice_date(engine, phrase):
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Website Audit", Decimal("100"))
    events = make(engine, invoice(item="website audit", quantity=1)).turn(
        "delivery", f"Bill Hooli for one website audit{phrase}.")
    c = card(events)
    assert [v for k, v in c["card"]["lines"] if k == "Date"] == ["26 Oct 2026"]
    assert any("delivery date cannot be set yet" in e.get("text", "").lower() for e in events)
    assert not any(e.get("code") == "date_choice_missing" for e in events)


def test_past_invoice_date_still_asks_with_delivery_date(engine):
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Website Audit", Decimal("100"))
    events = make(engine, invoice(item="website audit", quantity=1)).turn(
        "past", "Bill Hooli for one website audit dated 3 October 2026, to be delivered by Friday.")
    assert not any(e["type"] == "card" for e in events)
    assert events[-1].get("code") == "date_choice_missing" or any(e.get("part") == "date" for e in events), events


@pytest.mark.parametrize("text", ["invoice Hooli for 5 copper wire at -30 each",
                                  "invoice Hooli for 5 copper wire -30 each"])
def test_negative_rate_is_not_read_as_positive(engine, text):
    events = make(engine).turn("negative", text)
    assert not any(e["type"] == "card" for e in events), events


def test_later_rate_never_leaves_an_earlier_list_price_card(engine):
    a = make(engine)
    events = a.turn("correction", "invoice Hooli for 5 copper wire at 30 each, memo PO 4471")
    assert events[-1].get("code") == "memo_choice_missing", events
    events = a.turn("correction", "actually 35 each")
    cards = [e for e in events if e["type"] == "card"]
    if cards:
        assert "@ 35.00 = 175.00" in str(cards[0]["card"]["lines"]), events
    else:
        assert events[-1]["type"] in {"ask", "refused"}, events


def test_four_decimal_rate_is_shown_and_committed_exactly(engine):
    events = make(engine).turn("precision", "invoice Hooli for 5 copper wire at 30.1234 each")
    c = card(events)
    assert any("@ 30.1234 = 150.62" in v for k, v in c["card"]["lines"] if k == "Item")
    result = approvals.approve(engine, c["approval_id"], c["digest"])
    assert result.status == "committed"
    with engine.connect() as conn:
        row = conn.execute(select(tables.invoice_line.c.rate, tables.invoice_line.c.amount)
                           .join(tables.invoice, tables.invoice.c.id == tables.invoice_line.c.invoice_id)
                           .where(tables.invoice.c.number == result.data["number"])).one()
    assert tuple(row) == (Decimal("30.1234"), Decimal("150.62"))


def test_ambiguous_four_decimal_rates_are_not_rounded_in_the_question(engine):
    events = make(engine).turn("precision-choice", "invoice Hooli for 5 copper wire at 30.1234 each, memo 99.1234 each")
    assert not any(e["type"] == "card" for e in events)
    assert {"30.1234", "99.1234"} <= {c["label"] for c in events[-1]["choices"]}, events


def test_stated_rate_books_diff(engine):
    from bookdiff import changes, snapshot
    before = snapshot(engine)
    events = make(engine).turn("diff", "invoice Hooli for 5 copper wire at 30 each")
    c = card(events)
    assert "@ 30.00 = 150.00" in str(c["card"]["lines"])
    result = approvals.approve(engine, c["approval_id"], c["digest"])
    assert result.status == "committed"
    diff = changes(before, snapshot(engine))
    assert set(diff) == {"approval", "audit_log", "ledger_entry", "number_series", "operation_outcome",
                         "posting", "posting_chain_head", "sales_invoice", "sales_invoice_line"}, diff.keys()
    invoice_row, = diff["sales_invoice"]["added"]
    line, = diff["sales_invoice_line"]["added"]
    assert (invoice_row["total"], invoice_row["outstanding"]) == ("150.00", "150.00")
    assert (line["invoice_id"], line["quantity"], line["rate"], line["amount"]) == (
        invoice_row["id"], "5", "30", "150.00")
    entries = diff["ledger_entry"]["added"]
    assert sorted((entry["debit"], entry["credit"], entry["functional_debit"], entry["functional_credit"])
                  for entry in entries) == [
        ("0.00", "150.00", "0.00", "150.00"), ("150.00", "0.00", "150.00", "0.00")]


def test_negative_rate_and_past_date_asks_date_first(engine):
    events = make(engine).turn("date-first", "invoice Hooli for 5 copper wire at -30 each dated yesterday")
    assert events[-1].get("code") == "date_choice_missing", events
    assert not any(e["type"] == "card" for e in events)


def test_choice_number_is_not_the_invoice_rate(engine):
    a = make(engine, invoice(item="copper"))
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one()
    events = a.turn("choice", "invoice Hooli for 5 copper at 30 each")
    assert len(events) == 1 and events[0]["type"] == "ask" and events[0].get("code") == "item_ambiguous", events
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(tables.invoice)).scalar_one() == before
    events = a.turn("choice", "1")
    c = card(events)
    assert any("@ 30.00 = 150.00" in value for key, value in c["card"]["lines"] if key == "Item")


def test_rate_evidence_is_not_a_public_not_done_notice(engine):
    from erpclaw_assistant import resolve as rv
    from erpclaw_core.plugins import REGISTRY
    with engine.connect() as conn:
        resolved = rv.resolve_args(company_ctx(conn), REGISTRY.contracts["create_invoice"],
                                   {"customer": "Hooli", "items": [{"item": "Copper Wire", "quantity": 5}]},
                                   "invoice Hooli for 5 copper wire at 28 each", rv.Session())
    assert resolved.not_done == []
    assert Decimal(resolved.args["items"][0]["rate"]) == Decimal("28")


def test_sales_order_keeps_its_existing_rate_question(engine):
    write = call("create_sales_order", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])
    a = make(engine, write)
    events = a.turn("order", "Book a sales order for Hooli for 5 copper wire at 30 each")
    assert events[-1].get("code") == "rate_choice_missing", events
    assert not any(e["type"] == "card" for e in events)
    c = card(a.turn("order", "Use the list price"))
    assert "@ 28.00 = 140.00" in str(c["card"]["lines"])


@pytest.mark.parametrize("phrase", ["at 30% of list price", "at 30 percent of list", "30% off"])
def test_percentage_price_is_not_a_currency_rate(engine, phrase):
    from bookdiff import changes, snapshot
    before = snapshot(engine)
    events = make(engine).turn("percentage", f"Invoice Hooli for 5 copper wire {phrase}")
    assert not any(e["type"] == "card" for e in events), events
    assert events[-1]["type"] in {"ask", "refused"} or (
        events[-1]["type"] == "answer" and "not available" in events[-1]["text"].replace("aren't", "are not")), events
    assert not changes(before, snapshot(engine))


@pytest.mark.parametrize("phrase", ["with a 30% discount", "at a 30% discount", "30 percent discount"])
def test_percentage_discount_is_not_silently_dropped_through_turn(engine, phrase):
    from bookdiff import changes, snapshot
    before = snapshot(engine)
    write = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])
    events = make(engine, write).turn("discount", f"Invoice Acme Corporation for 5 copper wire {phrase}")
    assert not any(e["type"] == "card" for e in events), events
    assert (len(events) == 1 and events[0]["type"] == "ask") or any(
        e.get("capability") == "planned.discounts" for e in events), events
    assert not changes(before, snapshot(engine))


@pytest.mark.parametrize("rate", ["1000000000", "1001", "1000.0001"])
def test_excessive_rate_refused_at_preview_and_commit(engine, rate):
    from bookdiff import changes, snapshot
    from erpclaw_core.common import Refusal
    from erpclaw_sales import commit_invoice
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Boundary Service", Decimal("100"))
    before = snapshot(engine)
    events = make(engine, invoice(item="boundary service")).turn(
        "excessive", f"Invoice Hooli for 5 boundary service at {rate} each")
    assert not any(e["type"] == "card" for e in events), events
    assert events[-1]["type"] == "refused", events
    assert rate in events[-1]["text"].replace(",", "") and "100.00" in events[-1]["text"], events
    with engine.begin() as conn:
        inp = CreateInvoice.model_validate({"customer": "Hooli", "items": [{"item": "Boundary Service", "quantity": 5, "rate": rate}]})
        with pytest.raises(Refusal):
            commit_invoice(company_ctx(conn), inp, "edited-card")
    assert not changes(before, snapshot(engine))


@pytest.mark.parametrize("rate", ["999", "1000"])
def test_rate_under_excessive_boundary_still_warns(engine, rate):
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Boundary Service", Decimal("100"))
    c = card(make(engine, invoice(item="boundary service")).turn(
        "under-boundary", f"Invoice Hooli for 5 boundary service at {rate} each"))
    assert c["card"]["warnings"]


def test_stale_card_cannot_commit_after_list_price_falls(engine):
    from erpclaw_core import models as m
    from bookdiff import changes, snapshot
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Boundary Service", Decimal("100"))
    c = card(make(engine, invoice(item="boundary service")).turn(
        "stale-boundary", "Invoice Hooli for 5 boundary service at 999 each"))
    with engine.begin() as conn:
        conn.execute(m.item.update().where(m.item.c.name == "Boundary Service").values(standard_rate=Decimal("99")))
    before = snapshot(engine)
    result = approvals.approve(engine, c["approval_id"], c["digest"])
    assert result.status != "committed"
    diff = changes(before, snapshot(engine))
    assert not set(diff) & {"sales_invoice", "sales_invoice_line", "posting", "ledger_entry"}


@pytest.mark.parametrize("operation", ["preview", "commit"])
def test_absolute_rate_ceiling_refuses_at_contract_boundary(engine, operation):
    from bookdiff import changes, snapshot
    from erpclaw_core.common import Refusal
    from erpclaw_sales import preview_invoice, commit_invoice
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Ceiling Service", Decimal("200000"))
    before = snapshot(engine)
    inp = CreateInvoice.model_validate({"customer": "Hooli", "items": [{"item": "Ceiling Service", "quantity": 5, "rate": "1000000.01"}]})
    with engine.begin() as conn:
        with pytest.raises(Refusal, match="1,000,000"):
            if operation == "preview":
                preview_invoice(company_ctx(conn), inp)
            else:
                commit_invoice(company_ctx(conn), inp, "stale-card")
    assert not changes(before, snapshot(engine))


def test_absolute_rate_ceiling_itself_is_carded_with_warning(engine):
    with engine.begin() as conn:
        create_item(company_ctx(conn), "Ceiling Service", Decimal("200000"))
    events = make(engine, invoice(item="ceiling service")).turn(
        "ceiling", "Invoice Hooli for 5 ceiling service at 1000000.00 each")
    c = card(events)
    assert "1,000,000.00" in str(c["card"]["lines"]), events
    assert c["card"]["warnings"], events


def test_old_sales_contract_card_is_superseded_without_business_writes(engine):
    import json
    from bookdiff import changes, snapshot
    from erpclaw_core import models as m
    c = card(make(engine).turn("old-version", "Invoice Hooli for 5 copper wire"))
    with engine.begin() as conn:
        row = dict(conn.execute(select(m.approval).where(m.approval.c.id == c["approval_id"])).mappings().one())
        args = json.loads(row["args_json"])
        for item in args["items"]:
            item.pop("rate")
        row["contract_version"] = "0.0.2"
        old_digest = approvals.digest(approvals._fields(row, args, json.loads(row["read_set_json"]), json.loads(row["card_json"])))
        conn.execute(m.approval.update().where(m.approval.c.id == row["id"]).values(
            contract_version="0.0.2", args_json=approvals.canonical(args), digest=old_digest))
    before = snapshot(engine)
    result = approvals.approve(engine, c["approval_id"], old_digest)
    assert result.status == "superseded" and result.text == approvals.FRESH, result
    assert not set(changes(before, snapshot(engine))) & {"sales_invoice", "sales_invoice_line", "posting", "ledger_entry"}


def test_two_rates_in_one_message_name_both_before_choice(engine):
    a = make(engine)
    events = a.turn("two-rates", "Invoice Hooli for 5 copper wire at 30 each, no wait 35 each")
    assert len(events) == 1 and events[0]["type"] == "ask", events
    q = events[0]
    assert q["code"] == "rate_choice_missing"
    assert "30.00" in q["text"] and "35.00" in q["text"] and "copper wire" in q["text"].lower()
    c = card(a.turn("two-rates", "35.00"))
    assert "@ 35.00 = 175.00" in str(c["card"]["lines"])


@pytest.mark.parametrize('reverse', [False, True])
def test_mixed_invoice_rate_and_payment_amount_ask_for_one_change(engine, reverse):
    invoice_call = invoice()
    payment_call = call('record_payment', customer='Hooli', amount=500)
    calls = [payment_call, invoice_call] if reverse else [invoice_call, payment_call]
    said = ('Record a $500 payment from Hooli and invoice Hooli for 5 copper wire at $35 each'
            if reverse else 'Invoice Hooli for 5 copper wire at $35 each and record a $500 payment from Hooli')
    events = Assistant(engine, ScriptedModel([Reply('', calls)])).turn('mixed-invoice-payment', said)
    assert len(events) == 1 and events[0]['code'] == 'changes_ambiguous', events
