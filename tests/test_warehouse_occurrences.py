"""Warehouse sources belong to individual item mentions and leave trailing dates visible."""

from datetime import date
import json

import pytest
from sqlalchemy import select

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx, new_id
from erpclaw_stock import service as stock
from erpclaw_stock.service import Trusted


@pytest.fixture
def warehouse_books(engine):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        default = stock.create_warehouse(ctx, "Default Store", default=True)
        explicit = stock.create_warehouse(ctx, "Explicit Store")
        monday = stock.create_warehouse(ctx, "Monday Store")
        item = conn.execute(select(m.item.c.id).where(
            m.item.c.company_id == ctx.company_id, m.item.c.name == "Copper Wire")).scalar_one()
        stock.make_stock_item(ctx, item, "moving_average", default)
        trusted = Trusted("Starting stock for warehouse occurrence tests")
        for warehouse in (default, explicit, monday):
            stock.receive(ctx, f"pr-{new_id()[:8]}", item, warehouse, "20", "10.00",
                          date(2026, 10, 1), trusted=trusted)
        trusted.verify(ctx)
        assert conn.execute(select(m.item.c.kind).where(
            m.item.c.company_id == ctx.company_id, m.item.c.name == "Circuit Board")).scalar_one() == "service"
    return engine


def invoice_turn(engine, text, *items):
    invoice = call("create_invoice", customer="Acme Corporation",
                   items=[{"item": item, "quantity": 5} for item in items])
    assistant = Assistant(engine, ScriptedModel([Reply("", [invoice])] * 3))
    return assistant, assistant.turn("warehouse-occurrences", "invoice Acme Corporation for " + text)


def card_sources(engine, events):
    assert not any(e["type"] in {"ask", "not_done"} for e in events), events
    cards = [e for e in events if e["type"] == "card"]
    assert len(cards) == 1, events
    card = cards[0]
    with engine.connect() as conn:
        args = json.loads(conn.execute(select(m.approval.c.args_json).where(
            m.approval.c.id == card["approval_id"])).scalar_one())
    assert [(row["item"], row["quantity"]) for row in args["items"]] == [
        ("Copper Wire", "5")] * len(args["items"])
    sources = [value for label, value in card["card"]["lines"] if label == "From"]
    return card, args, sources


def test_stock_source_keeps_today_outside_the_warehouse(warehouse_books):
    _, events = invoice_turn(warehouse_books, "5 Copper Wire from Explicit Store today", "Copper Wire")
    card, args, sources = card_sources(warehouse_books, events)
    assert sources == ["Explicit Store (20 on hand)"]
    assert args["items"][0]["warehouse"] == "Explicit Store"
    with warehouse_books.connect() as conn:
        today = company_ctx(conn).today
    assert ("Date", today.strftime("%d %b %Y")) in [tuple(row) for row in card["card"]["lines"]]


def test_two_sources_for_repeated_item_use_their_own_occurrences(warehouse_books):
    _, events = invoice_turn(warehouse_books,
                            "5 Copper Wire from Explicit Store and 5 Copper Wire from Monday Store",
                            "Copper Wire", "Copper Wire")
    _, args, sources = card_sources(warehouse_books, events)
    assert sources == ["Explicit Store (20 on hand)", "Monday Store (20 on hand)"]
    assert [row["warehouse"] for row in args["items"]] == ["Explicit Store", "Monday Store"]


def test_source_for_first_occurrence_leaves_second_on_its_default(warehouse_books):
    _, events = invoice_turn(warehouse_books, "5 Copper Wire from Explicit Store and 5 Copper Wire",
                            "Copper Wire", "Copper Wire")
    _, args, sources = card_sources(warehouse_books, events)
    assert sources == ["Explicit Store (20 on hand)", "Default Store (20 on hand)"]
    assert [row.get("warehouse") for row in args["items"]] == ["Explicit Store", None]


def test_source_without_an_item_occurrence_is_refused(warehouse_books):
    _, events = invoice_turn(warehouse_books, "5 Copper Wire. From Explicit Store.", "Copper Wire")
    refusals = [e for e in events if e["type"] in {"ask", "not_done"}]
    assert refusals, events
    assert any("Explicit Store" in e["text"] for e in refusals), events
    assert not any(e["type"] == "card" for e in events), events


@pytest.mark.parametrize("answer", ["Invoice it without a warehouse", "Cancel"])
def test_service_source_before_today_uses_existing_warehouse_question(warehouse_books, answer):
    assistant, events = invoice_turn(warehouse_books, "5 Circuit Board from Explicit Store today", "Circuit Board")
    assert [e["type"] for e in events] == ["ask"], events
    ask = events[0]
    assert ask["code"] == "warehouse_ambiguous"
    assert ask["text"] == "Circuit Board is a service item; it cannot come from a warehouse."
    assert [c["label"] for c in ask["choices"] if not c["control"]] == ["Invoice it without a warehouse"]
    cancel = next(c for c in ask["choices"] if c["label"] == "Cancel")
    assert cancel == {"code": ask["question_id"] + "-cancel", "label": "Cancel", "control": True}
    choice = next(c for c in ask["choices"] if c["label"] == answer)
    events = assistant.turn("warehouse-occurrences", "", choice=choice["code"])
    if answer == "Cancel":
        assert [e["type"] for e in events] == ["answer"], events
        assert events[0]["text"] == "Cancelled; nothing was saved."
        with warehouse_books.connect() as conn:
            assert conn.execute(select(m.approval.c.id)).all() == []
    else:
        assert not any(e["type"] == "ask" for e in events), events
        assert any(e["type"] == "card" for e in events), events
        assert [e["text"] for e in events if e["type"] == "not_done" and e.get("part") == "warehouse"] == [
            "The warehouse source Explicit Store was not used for Circuit Board."]


def test_calendar_word_inside_warehouse_name_stays_in_the_name(warehouse_books):
    _, events = invoice_turn(warehouse_books, "5 Copper Wire from Monday Store", "Copper Wire")
    _, args, sources = card_sources(warehouse_books, events)
    assert sources == ["Monday Store (20 on hand)"]
    assert args["items"][0]["warehouse"] == "Monday Store"


def supported_trailing_dates():
    from erpclaw_assistant.resolve import table

    t = table()
    phrases = set(t["today"]) | set(t["yesterday"]) | set(t["tomorrow"])
    for day in t["weekdays"]:
        phrases.add(day)
        phrases.add("on " + day)
        phrases.update(prefix + " " + day for prefix in t["weekday_ahead"] | t["weekday_behind"])
        phrases.add(day + " " + " ".join(t["weekday_back"]))
        phrases.add(day + " " + " ".join(t["weekday_on"]))
    for month in t["months"]:
        phrases.update({f"{month} 23 2026", f"23 {month} 2026", f"on 23 {month} 2026"})
    # These readers use regular expressions rather than a finite word table.
    phrases.update({"tonight", "this morning", "this afternoon", "this evening", "last night",
                    "day before yesterday", "the day before yesterday", "day after tomorrow",
                    "the day after tomorrow", "end of month", "end of the month", "eom",
                    "a day ago"})
    phrases.update({"a week ago", "one day ago", "two weeks ago", "3 days ago",
                    "twenty-one days ago", "one hundred and five days ago",
                    "2026-10-23", "on 2026-10-23", "on the 2026-10-23",
                    "23/10/2026", "10/23/2026", "23.10.2026", "23/10", "23/10/26"})
    return sorted(phrases)


@pytest.mark.parametrize("phrase", supported_trailing_dates())
@pytest.mark.parametrize("answer", ["Invoice it without a warehouse", "Cancel"])
def test_service_source_before_supported_date_uses_warehouse_round(warehouse_books, phrase, answer):
    from erpclaw_assistant.resolve import date_phrases

    with warehouse_books.connect() as conn:
        today = company_ctx(conn).today
    assert len(date_phrases(phrase, today)) == 1
    assistant, events = invoice_turn(warehouse_books,
                                    f"5 Circuit Board from Explicit Store {phrase}", "Circuit Board")
    assert [e["type"] for e in events] == ["ask"], events
    ask = events[0]
    assert ask["code"] == "warehouse_ambiguous"
    assert ask["text"] == "Circuit Board is a service item; it cannot come from a warehouse."
    assert [c["label"] for c in ask["choices"] if not c["control"]] == ["Invoice it without a warehouse"]
    cancel = next(c for c in ask["choices"] if c["label"] == "Cancel")
    assert cancel == {"code": ask["question_id"] + "-cancel", "label": "Cancel", "control": True}
    choice = next(c for c in ask["choices"] if c["label"] == answer)
    events = assistant.turn("warehouse-occurrences", "", choice=choice["code"])
    if answer == "Cancel":
        assert [e["type"] for e in events] == ["answer"], events
        assert events[0]["text"] == "Cancelled; nothing was saved."
        with warehouse_books.connect() as conn:
            assert conn.execute(select(m.approval.c.id)).all() == []
        return
    if date_phrases(phrase, today) != [[today]]:
        assert [e["type"] for e in events] == ["ask"], events
        assert events[0]["code"] == "date_choice_missing"
        events = assistant.turn("warehouse-occurrences", "Date it today")
    assert not any(e["type"] == "ask" for e in events), events
    assert len([e for e in events if e["type"] == "card"]) == 1, events
    assert [e["text"] for e in events if e["type"] == "not_done" and e.get("part") == "warehouse"] == [
        "The warehouse source Explicit Store was not used for Circuit Board."]


@pytest.mark.parametrize("phrase", supported_trailing_dates())
def test_stock_source_keeps_warehouse_and_supported_date(warehouse_books, phrase):
    from erpclaw_assistant import resolve as rv

    with warehouse_books.connect() as conn:
        ctx = company_ctx(conn)
        today = ctx.today
        text = f"invoice Acme Corporation for 5 Copper Wire from Explicit Store {phrase}"
        sources = rv.warehouse_words(text, [{"name": "Copper Wire", "kind": "stock"}], ctx=ctx)
    assert len(sources) == 1
    assert sources[0].said == "Explicit Store"
    assert rv.words(text)[sources[0].end:] == rv.words(phrase)
    reading = rv.date_phrases(phrase, today)
    assert len(reading) == 1
    assert rv.date_phrases(rv.without_names(text, [rv.Found("Explicit Store", sources[0].start,
                                                         sources[0].end)]), today) == reading
    assistant, events = invoice_turn(warehouse_books, f"5 Copper Wire from Explicit Store {phrase}", "Copper Wire")
    if reading != [[today]]:
        assert [e["type"] for e in events] == ["ask"], events
        assert events[0]["code"] == "date_choice_missing"
        events = assistant.turn("warehouse-occurrences", "Date it today")
        assert [e["part"] for e in events if e["type"] == "not_done"] == ["date"], events
        events = [e for e in events if e["type"] != "not_done"]
    card, args, sources = card_sources(warehouse_books, events)
    assert sources == ["Explicit Store (20 on hand)"]
    assert args["items"][0]["warehouse"] == "Explicit Store"
    assert ("Date", today.strftime("%d %b %Y")) in [tuple(row) for row in card["card"]["lines"]]


@pytest.mark.parametrize("warehouse", ["Monday Store", "Friday Depot"])
@pytest.mark.parametrize("phrase", ["", "tonight", "on 2026-10-23", "next friday"])
def test_calendar_warehouse_name_stays_whole_with_trailing_date(warehouse_books, warehouse, phrase):
    from erpclaw_assistant import resolve as rv

    with warehouse_books.begin() as conn:
        ctx = company_ctx(conn)
        if warehouse == "Friday Depot":
            wh = stock.create_warehouse(ctx, warehouse)
            item = conn.execute(select(m.item.c.id).where(
                m.item.c.company_id == ctx.company_id, m.item.c.name == "Copper Wire")).scalar_one()
            trusted = Trusted("Starting stock for a calendar warehouse name")
            stock.receive(ctx, f"pr-{new_id()[:8]}", item, wh, "20", "10.00",
                          date(2026, 10, 1), trusted=trusted)
            trusted.verify(ctx)
        today = ctx.today
        text = f"invoice Acme Corporation for 5 Copper Wire from {warehouse} {phrase}".strip()
        sources = rv.warehouse_words(text, [{"name": "Copper Wire", "kind": "stock"}], ctx=ctx)
    assert len(sources) == 1
    assert sources[0].said == warehouse
    assert rv.words(text)[sources[0].end:] == rv.words(phrase)
    assistant, events = invoice_turn(warehouse_books, f"5 Copper Wire from {warehouse} {phrase}", "Copper Wire")
    if phrase and rv.date_phrases(phrase, today) != [[today]]:
        assert [e["type"] for e in events] == ["ask"], events
        assert events[0]["code"] == "date_choice_missing"
        events = assistant.turn("warehouse-occurrences", "Date it today")
        assert [e["part"] for e in events if e["type"] == "not_done"] == ["date"], events
        events = [e for e in events if e["type"] != "not_done"]
    _, args, sources = card_sources(warehouse_books, events)
    assert args["items"][0]["warehouse"] == warehouse
    assert sources == [warehouse + " (20 on hand)"]
