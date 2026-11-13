"""Receipt suppliers and explicit warehouse destinations are separate owner-written fields."""
from datetime import date as Day
from types import SimpleNamespace
from typing import Annotated

import pytest
from pydantic import BaseModel

from erpclaw_assistant import resolve as rv
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.masters import create_item, create_party
import erpclaw_sales as sales
from erpclaw_stock import service as stock


class Receipt(BaseModel):
    supplier: Annotated[str, sales._mark("supplier")]
    items: Annotated[list[sales.StockInvoiceItem], sales._mark("lines")]
    date: Annotated[Day | None, sales._mark("date", presence="optional")] = None


CONTRACT = SimpleNamespace(name="record_goods_received", input_model=Receipt)
SCOPE = "record_goods_received#0:"


@pytest.fixture
def receipt_books(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        stock.create_warehouse(ctx, "Overflow", default=True)
        stock.create_warehouse(ctx, "Copperline")
        create_party(ctx, "supplier", "Copperline")
        item = create_item(ctx, "Widgets", "2.5")
        stock.make_stock_item(ctx, item, "moving_average")
    return engine


def read(ctx, text, *, supplier="Copperline", session=None):
    args = {"items": [{"item": "Widgets", "quantity": 10, "warehouse": "Copperline"}]}
    if supplier is not None:
        args["supplier"] = supplier
    session = session if session is not None else rv.Session()
    result = rv.resolve_args(ctx, CONTRACT, args, text, session, scope=SCOPE)
    rv.account(ctx, text, [(CONTRACT, result.args)], [result], 0, session, [SCOPE])
    return result


@pytest.mark.parametrize("cue", ["into", "to"])
def test_into_destination_is_read_apart_from_any_from_phrase(receipt_books, cue):
    text = f"received 10 Widgets {cue} Overflow"
    with receipt_books.connect() as conn:
        ctx = company_ctx(conn)
        items = rv._items(ctx)
        assert rv.warehouse_words(text, items, [], ctx) == []
        assert [p.said for p in rv.receipt_destination_phrases(text, items, [], ctx)] == ["Overflow"]
        session = rv.Session()
        with pytest.raises(Refusal) as error:
            read(ctx, text, supplier=None, session=session)
        assert error.value.code.startswith("supplier_")
        assert session.pending["field"] == SCOPE + "supplier"


def test_receipt_from_supplier_name_never_feeds_the_warehouse(receipt_books):
    text = "received 10 Widgets from Copperline"
    with receipt_books.connect() as conn:
        ctx = company_ctx(conn)
        items = rv._items(ctx)
        assert [p.said for p in rv.warehouse_words(text, items, [], ctx)] == ["Copperline"]
        assert rv.receipt_destination_phrases(text, items, [], ctx) == []
        result = read(ctx, text)
        assert result.args["supplier"] == "Copperline"
        assert "warehouse" not in result.args["items"][0]
        assert result.args["items"][0].get("warehouse") != "Copperline"
        spans = rv.bounded_names(rv.words(text), ["Copperline"], item=False, bounded=False)
        assert not any(o.kind == "warehouse" for o in rv.inventory(ctx, text, items, spans, receipt=True))
        assert any(o.kind == "warehouse" for o in rv.inventory(ctx, text, items))


@pytest.mark.parametrize("cue", ["into", "to"])
def test_supplier_and_destination_read_as_two_values(receipt_books, cue):
    text = f"received 10 Widgets from Copperline {cue} Overflow tonight"
    with receipt_books.connect() as conn:
        ctx = company_ctx(conn)
        result = read(ctx, text)
        assert result.args["supplier"] == "Copperline"
        assert result.args["items"] == [{"item": "Widgets", "quantity": "10", "warehouse": "Overflow"}]
        assert result.args["date"] == ctx.today.isoformat()
        items = rv._items(ctx)
        assert [p.said for p in rv.warehouse_words(text, items, [], ctx)] == [f"Copperline {cue} Overflow"]
        spans = rv.bounded_names(rv.words(text), ["Copperline"], item=False, bounded=False)
        owned = rv.inventory(ctx, text, items, spans, receipt=True)
        assert [(o.kind, o.said) for o in owned if o.kind in {"supplier", "warehouse"}] == [
            ("supplier", "Copperline"), ("warehouse", "Overflow")]
        assert any(o.kind == "date" for o in owned)


def test_supplier_after_destination_sentence_still_asks_about_ambiguity(receipt_books):
    text = "received 10 Widgets from Copperline into Overflow. Supplier is Harbor Supply."
    with receipt_books.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "supplier", "Harbor Supply")
        session = rv.Session()
        with pytest.raises(Refusal) as error:
            read(ctx, text, session=session)
        assert error.value.code == "supplier_ambiguous"
        assert session.pending["field"] == SCOPE + "supplier"
        assert {c["ref"] for c in session.pending["choices"]} == {"Copperline", "Harbor Supply"}


def test_supplier_inside_destination_span_is_excluded_in_same_sentence(receipt_books):
    text = "received 10 Widgets from Copperline into Overflow tonight"
    with receipt_books.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "supplier", "Overflow")
        destinations = rv.receipt_destination_phrases(text, rv._items(ctx), [], ctx)
        assert [p.said for p in destinations] == ["Overflow"]
        supplier_mention, = rv.bounded_names(rv.words(text), ["Overflow"], item=False, bounded=False)
        assert destinations[0].start <= supplier_mention.start < supplier_mention.end <= destinations[0].end
        session = rv.Session()
        result = read(ctx, text, session=session)
        assert result.args["supplier"] == "Copperline"
        assert result.args["items"][0]["warehouse"] == "Overflow"
        assert result.args["date"] == ctx.today.isoformat()
        assert session.pending is None


def test_single_supplier_with_destination_sentence_binds_without_question(receipt_books):
    with receipt_books.begin() as conn:
        ctx = company_ctx(conn)
        create_party(ctx, "supplier", "Harbor Supply")
        session = rv.Session()
        result = read(ctx, "received 10 Widgets from Copperline into Overflow.", session=session)
        assert result.args["supplier"] == "Copperline"
        assert result.args["items"][0]["warehouse"] == "Overflow"
        assert session.pending is None


def test_bare_in_is_not_a_receipt_destination(receipt_books):
    text = "received 10 Widgets from Copperline in Overflow"
    with receipt_books.connect() as conn:
        ctx = company_ctx(conn)
        assert rv.receipt_destination_phrases(text, rv._items(ctx), [], ctx) == []
        result = rv.resolve_args(ctx, CONTRACT,
                                {"supplier": "Copperline", "items": [{"item": "Widgets", "quantity": 10,
                                                                      "warehouse": "Overflow"}]},
                                text, rv.Session(), scope=SCOPE)
        assert "warehouse" not in result.args["items"][0]
        assert not any(o.kind == "warehouse" for o in rv.inventory(ctx, text, rv._items(ctx), receipt=True))


def test_multiple_receipt_destinations_ask_one_detail(receipt_books):
    text = "received 10 Widgets from Copperline into Overflow to Copperline"
    with receipt_books.connect() as conn:
        session = rv.Session()
        with pytest.raises(Refusal) as error:
            read(company_ctx(conn), text, session=session)
        assert error.value.code == "warehouse_ambiguous"
        assert "one detail" in error.value.message.lower()
        assert session.pending["field"] == SCOPE + "items.0.warehouse"
        assert [c["ref"] for c in session.pending["choices"]] == ["Overflow", "Copperline"]


def test_unknown_receipt_destination_keeps_head_and_asks(receipt_books):
    text = "received 10 Widgets from Copperline into Nowhere Depot by Friday"
    with receipt_books.connect() as conn:
        ctx = company_ctx(conn)
        assert [p.said for p in rv.receipt_destination_phrases(text, rv._items(ctx), [], ctx)] == ["Nowhere Depot"]
        session = rv.Session()
        with pytest.raises(Refusal) as error:
            read(ctx, text, session=session)
        assert error.value.code.startswith("warehouse_")
        assert session.pending["field"] == SCOPE + "items.0.warehouse"


def test_shared_spelling_is_a_destination_only_when_explicitly_written(receipt_books):
    with receipt_books.connect() as conn:
        ctx = company_ctx(conn)
        result = read(ctx, "received 10 Widgets from Copperline into Copperline")
        assert result.args["supplier"] == "Copperline"
        assert result.args["items"][0]["warehouse"] == "Copperline"
        text = "received 10 Widgets from Copperline into Copperline"
        spans = rv.bounded_names(rv.words(text), ["Copperline"], item=False, bounded=False)
        owned = rv.inventory(ctx, text, rv._items(ctx), spans, receipt=True)
        assert [(o.kind, o.said) for o in owned if o.kind in {"supplier", "warehouse"}] == [
            ("supplier", "Copperline"), ("warehouse", "Copperline")]
        session = rv.Session()
        with pytest.raises(Refusal) as error:
            read(ctx, "received 10 Widgets into Copperline", supplier=None, session=session)
        assert error.value.code.startswith("supplier_")
        assert session.pending["field"] == SCOPE + "supplier"


def test_destination_before_supplier_still_keeps_two_values(receipt_books):
    with receipt_books.connect() as conn:
        ctx = company_ctx(conn)
        text = "received 10 Widgets into Overflow from Copperline"
        session = rv.Session()
        with pytest.raises(Refusal) as error:
            read(ctx, text, session=session)
        assert error.value.code == "item_ambiguous"
        assert session.pending["field"] == SCOPE + "items.0.item"
        assert [c["ref"] for c in session.pending["choices"]] == ["Widgets", None]
        assert session.answer(session.pending["choices"][0]["code"])
        result = read(ctx, text, session=session)
        assert result.args["supplier"] == "Copperline"
        assert result.args["items"][0]["warehouse"] == "Overflow"


def test_destination_cue_inside_a_longer_known_name_stays_in_the_name(receipt_books):
    with receipt_books.begin() as conn:
        ctx = company_ctx(conn)
        stock.create_warehouse(ctx, "Door to Door")
        text = "received 10 Widgets from Copperline into Door to Door tonight"
        assert [p.said for p in rv.receipt_destination_phrases(text, rv._items(ctx), [], ctx)] == ["Door to Door"]
        result = read(ctx, text)
        assert result.args["items"][0]["warehouse"] == "Door to Door"
