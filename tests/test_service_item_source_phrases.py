"""Service-item source phrases name warehouses only when the owner's words identify one."""

import json

import pytest
from sqlalchemy import select

from erpclaw_assistant import resolve as rv
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import service as stock
from erpclaw_stock import tables as st
from erpclaw_sales import tables as sales


SCOPE = "create_invoice#0:"
SOURCES = [
    pytest.param("stock", False, id="ordinary-stock"),
    pytest.param("our Dallas office", False, id="ordinary-office"),
    pytest.param("Pune", False, id="unknown-bare-name"),
    pytest.param("Main Store annex", False, id="different-full-name"),
    pytest.param("Main Store", True, id="known-warehouse"),
    pytest.param("the main store", True, id="known-warehouse-normalized"),
    pytest.param("Monday 2 Store", True, id="known-warehouse-calendar-words"),
    pytest.param("the Pune warehouse", True, id="explicit-unknown-warehouse"),
]


@pytest.fixture
def service_books(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        stock.create_warehouse(ctx, "Main Store", default=True)
        stock.create_warehouse(ctx, "Monday 2 Store")
        assert next(r for r in rv._items(ctx) if r["name"] == "Copper Wire")["kind"] == "service"
    return engine


@pytest.mark.parametrize("source,is_warehouse", SOURCES)
def test_service_source_resolution_and_card(service_books, source, is_warehouse):
    text = f"invoice Acme Corporation for 5 Copper Wire from {source}"
    contract = REGISTRY.contracts["create_invoice"]
    args = {"customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": 5}]}
    session = rv.Session()
    with service_books.connect() as conn:
        ctx = company_ctx(conn)
        if is_warehouse:
            with pytest.raises(Refusal) as error:
                rv.resolve_args(ctx, contract, args, text, session, scope=SCOPE)
            assert error.value.code == "warehouse_ambiguous"
            assert error.value.message == "Copper Wire is a service item; it cannot come from a warehouse."
            assert session.pending["field"] == SCOPE + "items.0.warehouse"
            return
        result = rv.resolve_args(ctx, contract, args, text, session, scope=SCOPE)
        assert result.args["items"] == [{"item": "Copper Wire", "quantity": "5"}]
        assert result.not_done == []
        rv.account(ctx, text, [(contract, result.args)], [result], 0, session, [SCOPE])
    assert approvals.propose(service_books, contract, result.args).approval_id


@pytest.mark.parametrize("source,is_warehouse", SOURCES)
def test_service_source_occurrence_accounting(service_books, source, is_warehouse):
    text = f"invoice Acme Corporation for 5 Copper Wire from {source}"
    contract = REGISTRY.contracts["create_invoice"]
    args = {"customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": "5"}]}
    with service_books.connect() as conn:
        ctx = company_ctx(conn)
        occurrences = rv.inventory(ctx, text, rv._items(ctx))
        warehouses = [o for o in occurrences if o.kind == "warehouse"]
        if is_warehouse:
            assert len(warehouses) == 1
            assert warehouses[0].said == source.removeprefix("the ")
            item = next(o for o in occurrences if o.kind == "item")
            assert warehouses[0].value == {"item": "Copper Wire", "item_start": item.start}
            assert not any(o.kind == "date" for o in occurrences)
            with pytest.raises(Refusal) as error:
                rv.account(ctx, text, [(contract, args)], [rv.Result(args)], 0, rv.Session(), [SCOPE])
            assert error.value.code == "warehouse_ambiguous"
        else:
            assert warehouses == []
            rv.account(ctx, text, [(contract, args)], [rv.Result(args)], 0, rv.Session(), [SCOPE])


def test_ordinary_service_source_does_not_hide_a_date(service_books):
    text = "invoice Acme Corporation for 5 Copper Wire from our office tomorrow"
    contract = REGISTRY.contracts["create_invoice"]
    args = {"customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": 5}]}
    with service_books.connect() as conn:
        ctx = company_ctx(conn)
        session = rv.Session()
        result = rv.resolve_args(ctx, contract, args, text, session, scope=SCOPE)
        assert [row["part"] for row in result.not_done] == ["date"]
        rv.account(ctx, text, [(contract, result.args)], [result], 0, session, [SCOPE])


@pytest.mark.parametrize("source", ["Pune", "our Dallas office"])
def test_unknown_stock_source_still_asks_company_warehouses(service_books, source):
    text = f"invoice Acme Corporation for 5 Copper Wire from {source}"
    contract = REGISTRY.contracts["create_invoice"]
    args = {"customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": 5}]}
    with service_books.begin() as conn:
        ctx = company_ctx(conn)
        item = next(r for r in rv._items(ctx) if r["name"] == "Copper Wire")
        stock.make_stock_item(ctx, item["id"], "moving_average")
    with service_books.connect() as conn:
        session = rv.Session()
        with pytest.raises(Refusal) as error:
            rv.resolve_args(company_ctx(conn), contract, args, text, session, scope=SCOPE)
        assert error.value.code == "warehouse_unknown"
        assert session.pending["field"] == SCOPE + "items.0.warehouse"
        assert [c["ref"] for c in session.pending["choices"]] == ["Main Store", "Monday 2 Store"]


def _service_source_question(engine):
    invoice = call("create_invoice", customer="Acme Corporation",
                   items=[{"item": "Copper Wire", "quantity": 5, "warehouse": "Main Store"}])
    assistant = Assistant(engine, ScriptedModel([Reply("", [invoice])] * 3))
    events = assistant.turn("service-source", "invoice Acme Corporation for 5 Copper Wire from Main Store")
    assert [e["type"] for e in events] == ["ask"]
    assert events[0]["code"] == "warehouse_ambiguous"
    return assistant, events[0]


@pytest.mark.parametrize("answer_by", ["code", "label"])
def test_service_source_can_continue_without_warehouse(service_books, answer_by):
    assistant, ask = _service_source_question(service_books)
    candidates = [c for c in ask["choices"] if not c["control"]]
    assert [c["label"] for c in candidates] == ["Invoice it without a warehouse"]
    assert [(c["code"], c["label"]) for c in ask["choices"] if c["control"]] == [
        (ask["question_id"] + "-someone_else", "Someone else"),
        (ask["question_id"] + "-cancel", "Cancel"),
    ]
    choice = candidates[0]
    events = assistant.turn("service-source", choice["label"] if answer_by == "label" else "",
                            **({"choice": choice["code"]} if answer_by == "code" else {}))
    assert not any(e["type"] == "ask" for e in events), events
    card = next(e for e in events if e["type"] == "card")
    notices = [e["text"] for e in events if e["type"] == "not_done" and e.get("part") == "warehouse"]
    assert notices == ["The warehouse source Main Store was not used for Copper Wire."]
    with service_books.connect() as conn:
        args = json.loads(conn.execute(select(m.approval.c.args_json).where(
            m.approval.c.id == card["approval_id"])).scalar_one())
    assert args["items"][0]["item"] == "Copper Wire"
    assert args["items"][0]["quantity"] == "5"
    assert args["items"][0].get("warehouse") is None


@pytest.mark.parametrize("answer_by", ["code", "label"])
def test_service_source_cancel_drafts_nothing_and_changes_no_books(service_books, answer_by):
    tables = [m.approval, sales.invoice, sales.payment, m.posting, m.ledger_entry, st.entry, st.head]

    def snapshot():
        with service_books.connect() as conn:
            return {t.name: conn.execute(select(t)).mappings().all() for t in tables}

    before = snapshot()
    assistant, ask = _service_source_question(service_books)
    cancel = next(c for c in ask["choices"] if c["label"] == "Cancel")
    assert cancel == {"code": ask["question_id"] + "-cancel", "label": "Cancel", "control": True}
    events = assistant.turn("service-source", "Cancel" if answer_by == "label" else "",
                            **({"choice": cancel["code"]} if answer_by == "code" else {}))
    assert [e["type"] for e in events] == ["answer"]
    assert events[0]["text"] == "Cancelled; nothing was saved."
    assert snapshot() == before


def test_service_source_answer_is_bound_to_the_line(service_books):
    contract = REGISTRY.contracts["create_invoice"]
    args = {"customer": "Acme Corporation", "items": [{"item": "Copper Wire", "quantity": 5}]}
    text = "invoice Acme Corporation for 5 Copper Wire from Main Store"
    session = rv.Session()
    with service_books.connect() as conn:
        ctx = company_ctx(conn)
        with pytest.raises(Refusal):
            rv.resolve_args(ctx, contract, args, text, session, scope=SCOPE)
        assert [c["label"] for c in session.pending["choices"]] == ["Invoice it without a warehouse"]
        assert session.answer(session.pending["choices"][0]["code"])
        assert SCOPE + "items.0.warehouse" in session.bindings
        session = rv.Session.load(session.dump())
        for _ in range(2):
            result = rv.resolve_args(ctx, contract, args, text, session, scope=SCOPE)
            assert result.args["items"] == [{"item": "Copper Wire", "quantity": "5"}]
            rv.account(ctx, text, [(contract, result.args)], [result], 0, session, [SCOPE])
        assert session.pending is None
