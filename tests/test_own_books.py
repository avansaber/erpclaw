"""The owner's own books by chat: `init --own-books` makes books with
one pending company and no business choice in them; while pending, only `set_up_company` is offered or accepted; the
setup card fixes the name, currency, fiscal year start and date order, opens the year holding today and the next, and
the runtime role can never change them afterwards; `add_customer` and `add_item` add the first masters on a card.

Stock items (method, warehouse, the stock plugin off, price never a cost) come in a later build; here a stock item
gets only the planned not-yet answer."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from bookdiff import assert_changes, snapshot
from conftest import PG_URL, cli_urls, drop_everything
from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall
from erpclaw_core import approvals, setup
from erpclaw_core import catalogue as cat
from erpclaw_core import models as m
from erpclaw_core.common import Refusal, is_question
from erpclaw_core.db import DEMO_MARK, make_engine
from erpclaw_core.db import owner as books_marks
from erpclaw_core.grants import apply_grants
from erpclaw_core.plugins import REGISTRY, load_plugins
from erpclaw_sales import tables as t

# The owner-facing wording, exactly as the language table gives it; the key beside each
PREFACE = "Let's set up your books first; it takes a minute."  # setup_first
Q_NAME = "What's your business called?"  # setup_name_missing
Q_CURRENCY = "Which currency do you keep your books in?"  # setup_currency_missing
USD_ONLY = ("I can keep books in US dollars only for now; other currencies come with the country packs (3.1). "
            "Nothing was set up.")  # setup_currency_planned
Q_YEAR = "When does your financial year start? January, or another month?"  # setup_fy_start_missing
Q_DATES = "How do you write dates: 03/04 for 3 April, or for 4 March?"  # setup_date_order_missing
NOT_SET_UP = "Your books aren't set up yet."  # books_not_set_up
CUSTOMER_EXISTS = "{name} is already a customer."  # customer_exists
CUSTOMER_NEAR = "Did you mean {name} ({city}), already a customer, or is {said} a new one?"  # customer_near
ITEM_UNIT_MISSING = "What unit do you sell {name} in: each, hour, meter, or another?"  # item_unit_missing
ITEM_PRICE_MISSING = "At what price do you usually sell {name}?"  # item_price_missing
ITEM_EXISTS = "{name} is already an item."  # item_exists
ITEM_NEAR = "Did you mean {name}, already an item, or is {said} a new one?"  # item_near
ITEM_STOCK_NOT_YET = ("Adding an item you keep in stock isn't available yet; I can add it as a service item, or you "
                      "can add it later.")  # item_stock_not_yet
STOCK_ENTRY = "add an item I keep in stock"  # the planned catalogue entry's name

# Owner-facing sentences outside the language table
ALREADY = "These books already exist."
READY = ("Your books are ready. You can add your first customer, like 'add a customer called Acme Corp, "
         "accounts@acme.example'.")
OPENING = ("If your business already has money in the bank or customers who owe you, opening balances come next; "
           "for now, new transactions only.")
CARD_CURRENCY = "Currency: US dollars (USD). This can't be changed after your first posting."
CARD_EARLIER = "Earlier years open only when you add opening balances."
CARD_CHART = (f"Your chart of accounts: {len(setup.CHART)} accounts for a small business (bank, receivables, sales, cost of goods, and "
              "the rest); more are added when you need them.")
NOTHING_POSTED = "Nothing is posted."

# The four fields set_up_company writes and the status: the runtime role's new grants on company
SETUP_COLUMNS = ("name", "functional_currency", "fiscal_year_start_month", "date_order", "status")
# set_up_company's input as the engine stores it (the chat binds the owner's words to these)
SETUP = {"name": "Lakeside Bakery", "currency": "USD", "fiscal_year_start_month": 1, "date_order": "DMY"}
# The company trigger's own refusal of the runtime role's update once the company is ready (the build's, 2.2)
FIXED_SQLSTATE = "P0001"
FIXED_MESSAGE = "The company's name, currency, fiscal year start and date order are fixed once the books are ready."


# ---------- helpers ----------

def tool(tool_name: str, /, **args) -> ToolCall:
    """A native tool call, built as `model.call` builds it; the tool's name is positional only, so an argument
    called `name` (a customer's, an item's, a business's) never collides with it."""
    return ToolCall(f"call_{tool_name}", tool_name, json.loads(json.dumps(args), parse_float=Decimal),
                    json.dumps(args))


def refused_by_the_trigger(err: DBAPIError) -> None:
    """The driver error is the company trigger's own: its SQLSTATE and its message, exactly."""
    assert getattr(err.orig, "sqlstate", None) == FIXED_SQLSTATE, err
    assert err.orig.diag.message_primary == FIXED_MESSAGE, err

def own_books(engine) -> str:
    """The test database, emptied, then the bootstrap's books: one pending company. Returns its id."""
    drop_everything(engine)
    load_plugins()
    return setup.create_own_books(engine)


def companies(engine) -> list[dict]:
    """Every company row, every column the database holds (not only the models')."""
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text("SELECT * FROM company")).mappings()]


def count(engine, table, *where) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def contract(name: str):
    return REGISTRY.contracts[name]


def propose(engine, name: str, args: dict, conversation: str | None = None):
    return approvals.propose(engine, contract(name), args, conversation)


def approved(engine, card):
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "committed", out.text
    return out


def card_text(card: dict) -> str:
    """Every string of a card (an event's or a preview's), whitespace collapsed: what the owner reads."""
    c = card.get("card", card)
    parts = [c["title"], *(f"{k}: {v}" for k, v in c["lines"]), *c["books"], *c["warnings"], c["footer"]]
    return " ".join(" ".join(str(p).split()) for p in parts)


def said(events) -> str:
    return " ".join(e.get("text") or "" for e in events)


def last_ask(events) -> dict:
    assert events and events[-1]["type"] == "ask", events
    return events[-1]


def only_card(events) -> dict:
    cards = [e for e in events if e["type"] == "card"]
    assert len(cards) == 1, events
    return cards[0]


def pick(ask: dict, words: str) -> dict:
    """The one choice of a question whose label holds these words."""
    hits = [c for c in ask["choices"] if words.lower() in c["label"].lower()]
    assert len(hits) == 1, ask
    return hits[0]


def role_of(url: str) -> str:
    return make_url(url).username


def plain_init_help() -> str:
    """Typer reads terminal overrides at import time, so help needs a fresh process even after other CLI tests."""
    env = {**os.environ, "_TYPER_FORCE_DISABLE_TERMINAL": "1", "COLUMNS": "120", "TERMINAL_WIDTH": "120"}
    result = subprocess.run([sys.executable, "-m", "erpclaw_cli.main", "init", "--help"],
                            env=env, capture_output=True, text=True, check=True)
    # Strip any remaining terminal styling as a backstop for different Rich versions.
    return " ".join(re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout).split())


def runtime_may_update(engine, role: str, column: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT has_column_privilege(:r, 'company', :c, 'UPDATE')"),
                            {"r": role, "c": column}).scalar()


class OwnerWords:
    """A scripted model that answers from the owner's words in the request, whichever turn calls it: a turn the
    engine answers without the model shifts nothing."""

    name = "scripted"

    def __init__(self, script):
        self.script = script
        self.tools_seen: list[list[str]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        self.tools_seen.append([x["function"]["name"] for x in tools])
        users = [x["content"] for x in messages if x.get("role") == "user" and isinstance(x.get("content"), str)]
        return self.script(" ".join(users))


def client(engine, script):
    model = OwnerWords(script)
    c = TestClient(create_app(engine, model, embedded_owner=True))
    return c, c.post("/v1/conversations").json()["id"], model


def say(c, cid, words: str, choice: dict | None = None) -> list[dict]:
    body = {"text": choice["label"], "choice": choice["code"]} if choice else {"text": words}
    return c.post(f"/v1/conversations/{cid}/messages", json=body).json()["events"]


def approve_event(c, cid, card: dict) -> list[dict]:
    r = c.post(f"/v1/cards/{card['approval_id']}/approve", params={"conversation_id": cid},
               json={"digest": card["digest"]})
    assert r.status_code == 200, r.text
    return r.json()["events"]


def offered_contracts(model: OwnerWords) -> set[str]:
    return {n for tools in model.tools_seen for n in tools if n in REGISTRY.contracts}


def setup_words(words: str) -> Reply:
    """The model in setup: the owner's own words for the name and the currency, nothing else; a greeting is text."""
    args = {}
    if "Lakeside Bakery" in words:
        args["name"] = "Lakeside Bakery"
    for currency in ("US dollars", "euros"):
        if currency in words:
            args["currency"] = currency
    return Reply("", [tool("set_up_company", **args)]) if args else Reply("Hello! How can I help?")


def first_run(words: str) -> Reply:
    """The model of the first conversation, from the owner's words of the current request."""
    if "add a customer" in words.lower():
        return Reply("", [tool("add_customer", name="Acme Corp", email="accounts@acme.example")])
    if "add an item" in words.lower():
        return Reply("", [tool("add_item", name="Consulting", unit="hour", price="80")])
    if "invoice" in words.lower():
        return Reply("", [tool("create_invoice", customer="Acme Corp", items=[{"item": "Consulting", "quantity": 3}])])
    return setup_words(words)


def chat_setup(c, cid) -> tuple[list[list[dict]], dict]:
    """The four questions of 2.2 answered in turn; returns every turn's events and the setup card."""
    turns = [say(c, cid, "hi")]
    turns.append(say(c, cid, "Lakeside Bakery"))
    turns.append(say(c, cid, "US dollars"))
    turns.append(say(c, cid, "", pick(last_ask(turns[-1]), "January")))
    turns.append(say(c, cid, "", pick(last_ask(turns[-1]), "3 April")))
    return turns, only_card(turns[-1])


# ---------- the bootstrap ----------

def test_init_own_books_leaves_one_pending_company_with_the_base_chart_its_owner_and_no_business_choice(
        engine, runtime_login, monkeypatch):
    from erpclaw_cli.main import app

    with engine.connect() as conn:  # what create_company installs: the bootstrap installs the same plugins
        plugins_wanted = set(conn.execute(select(m.plugin_install.c.name)).scalars())
    drop_everything(engine)
    cli_urls(monkeypatch, engine, runtime_login)
    out = CliRunner().invoke(app, ["init", "--own-books"])
    assert out.exit_code == 0, out.output
    assert "token" not in out.output.lower()  # nothing the owner must keep (the launcher signs the owner in)
    [co] = companies(engine)
    assert co["status"] == "pending"
    assert (co["name"], co["functional_currency"], co["fiscal_year_start_month"], co["date_order"]) == \
        (None, None, None, None)
    with engine.connect() as conn:
        accounts = conn.execute(select(m.account.c.number, m.account.c.name, m.account.c.root_type,
                                       m.account.c.account_type, m.account.c.is_group)
                                .where(m.account.c.company_id == co["id"]).order_by(m.account.c.number)).all()
        head = conn.execute(select(m.posting_chain_head).where(
            m.posting_chain_head.c.company_id == co["id"])).mappings().one()
        owner_id = conn.execute(select(m.principal.c.id).where(m.principal.c.name == "owner")).scalar_one()
        perms = set(conn.execute(select(m.principal_grant.c.permission).where(
            m.principal_grant.c.principal_id == owner_id,
            m.principal_grant.c.company_id == co["id"])).scalars())
        plugins = set(conn.execute(select(m.plugin_install.c.name)).scalars())
        marks = set(conn.execute(select(books_marks.c.mark)).scalars())
    assert [tuple(a) for a in accounts] == sorted((n, a, r, ty, g) for n, a, r, ty, g, _ in setup.CHART)
    assert len(accounts) == len(setup.CHART)  # 18 today; the chart may grow
    assert (head["last_seq"], head["last_hash"]) == (0, "0" * 64)
    assert perms == set(m.PERMISSIONS)
    assert plugins == plugins_wanted
    assert DEMO_MARK not in marks
    for table in (m.fiscal_year, m.fiscal_period, m.party, m.item, m.posting):
        assert count(engine, table) == 0, table.name


def test_a_second_own_books_bootstrap_is_refused_and_changes_nothing(engine, runtime_login, monkeypatch):
    from erpclaw_cli.main import app

    drop_everything(engine)
    cli_urls(monkeypatch, engine, runtime_login)
    assert CliRunner().invoke(app, ["init", "--own-books"]).exit_code == 0
    before = snapshot(engine)
    out = CliRunner().invoke(app, ["init", "--own-books"])
    assert out.exit_code != 0 and ALREADY in " ".join(out.output.split()), out.output
    assert_changes(before, snapshot(engine), {})
    assert len(companies(engine)) == 1


def test_own_books_over_books_that_already_hold_a_company_is_refused(engine, runtime_login, monkeypatch):
    from erpclaw_cli.main import app

    cli_urls(monkeypatch, engine, runtime_login)
    before = snapshot(engine)
    out = CliRunner().invoke(app, ["init", "--own-books"])
    assert out.exit_code != 0 and ALREADY in " ".join(out.output.split()), out.output
    assert_changes(before, snapshot(engine), {})


def test_plain_init_still_makes_the_demo_books_ready_and_says_so_in_its_help(engine, runtime_login, monkeypatch):
    from erpclaw_cli.main import app

    drop_everything(engine)
    cli_urls(monkeypatch, engine, runtime_login)
    out = CliRunner().invoke(app, ["init", "--date-order", "MDY"])
    assert out.exit_code == 0, out.output
    [co] = companies(engine)
    assert (co["name"], co["functional_currency"], co["status"]) == ("Stark Manufacturing", "USD", "ready")
    with engine.connect() as conn:
        assert DEMO_MARK in set(conn.execute(select(books_marks.c.mark)).scalars())
    assert count(engine, t.invoice) > 0
    helped = plain_init_help()
    assert "--own-books" in helped and "demo" in helped.lower()


def test_a_bootstrap_that_stops_after_its_schema_leaves_nothing_and_the_next_run_finishes_it(
        engine, runtime_login, monkeypatch):
    """The bootstrap is one transaction: stopped right after its schema step, it leaves no table. Books that hold the
    schema and no company (an earlier step that stopped) are not "books that already exist": the next init finishes
    them, with exactly one pending company."""
    from erpclaw_cli.main import app
    from erpclaw_core.db import create_all, holds_tables

    drop_everything(engine)
    real = setup._schema

    def stops(conn):
        real(conn)
        raise RuntimeError("stopped after the schema")

    monkeypatch.setattr(setup, "_schema", stops)
    with pytest.raises(RuntimeError, match="stopped after the schema"):
        setup.create_own_books(engine)
    assert not holds_tables(engine)  # rolled back whole, the schema with it
    monkeypatch.setattr(setup, "_schema", real)
    create_all(engine)  # a schema and no company
    assert companies(engine) == []
    cli_urls(monkeypatch, engine, runtime_login)
    out = CliRunner().invoke(app, ["init", "--own-books"])
    assert out.exit_code == 0, out.output
    [co] = companies(engine)
    assert co["status"] == "pending" and co["name"] is None
    assert count(engine, m.account, m.account.c.company_id == co["id"]) == len(setup.CHART)


# ---------- the pending gate ----------

@pytest.mark.parametrize("words, reply", [
    ("hi", Reply("Hello! How can I help?")),
    ("Invoice Acme Corporation for 5 copper wire",
     Reply("", [tool("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])])),
    ("Add a customer called Acme Corp", Reply("", [tool("add_customer", name="Acme Corp")])),
], ids=["greeting", "invoice", "customer"])
def test_while_pending_every_request_gets_the_setup_question_once_and_no_card(engine, words, reply):
    own_books(engine)
    c, cid, model = client(engine, lambda _: reply)
    before = snapshot(engine)
    events = say(c, cid, words)
    assert said(events) == f"{PREFACE} {Q_NAME}", events  # the preface once, then the setup's next question
    assert last_ask(events)["text"].endswith(Q_NAME)
    assert not [e for e in events if e["type"] == "card"], events
    assert_changes(before, snapshot(engine), {})
    assert offered_contracts(model) <= {"set_up_company"}  # no other operation is offered to the model


def test_while_pending_a_read_answers_that_the_books_are_not_set_up(engine):
    own_books(engine)
    c, cid, _ = client(engine, lambda _: Reply("", [tool("get_metric", metric="receivables")]))
    events = say(c, cid, "How much am I owed?")
    assert said(events) == f"{NOT_SET_UP} {PREFACE} {Q_NAME}", events
    assert last_ask(events)["text"].endswith(Q_NAME)
    assert not [e for e in events if e.get("amounts")], events


@pytest.mark.parametrize("name, args", [
    ("create_invoice", {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 5}]}),
    ("record_payment", {"customer": "Acme Corporation", "amount": "100.00", "oldest_first": True, "method": "check"}),
    ("add_customer", {"name": "Acme Corp"}),
    ("add_item", {"name": "Consulting", "unit": "hour", "price": "80.00"}),
])
def test_while_pending_propose_refuses_every_operation_but_set_up_company_and_writes_nothing(engine, name, args):
    own_books(engine)
    before = snapshot(engine)
    with pytest.raises(Refusal) as r:
        propose(engine, name, args)
    assert r.value.code == "books_not_set_up"
    assert_changes(before, snapshot(engine), {})


# ---------- set_up_company ----------

def test_each_missing_setup_field_is_asked_in_turn_and_nothing_is_written_before_the_card(engine):
    cid_company = own_books(engine)
    c, cid, model = client(engine, setup_words)
    before = snapshot(engine)
    turns = [say(c, cid, "hi")]
    assert said(turns[-1]) == f"{PREFACE} {Q_NAME}", turns[-1]
    turns.append(say(c, cid, "Lakeside Bakery"))
    assert said(turns[-1]) == last_ask(turns[-1])["text"] == Q_CURRENCY, turns[-1]
    turns.append(say(c, cid, "US dollars"))
    assert said(turns[-1]) == last_ask(turns[-1])["text"] == Q_YEAR, turns[-1]
    turns.append(say(c, cid, "", pick(last_ask(turns[-1]), "January")))  # a choice code, never assumed
    assert said(turns[-1]) == last_ask(turns[-1])["text"] == Q_DATES, turns[-1]
    assert_changes(before, snapshot(engine), {})  # four questions, nothing written
    turns.append(say(c, cid, "", pick(last_ask(turns[-1]), "3 April")))
    card = only_card(turns[-1])
    assert card["card"]["title"] == "Set up your books"
    assert sum(said(ev).count(PREFACE) for ev in turns) == 1  # said once
    [co] = companies(engine)
    assert (co["id"], co["status"], co["name"]) == (cid_company, "pending", None)  # the card is not the commit
    assert offered_contracts(model) <= {"set_up_company"}


def test_euros_are_refused_with_the_planned_answer_and_nothing_is_set_up(engine):
    own_books(engine)
    c, cid, _ = client(engine, setup_words)
    say(c, cid, "hi")
    assert last_ask(say(c, cid, "Lakeside Bakery"))["text"] == Q_CURRENCY
    before = snapshot(engine)
    events = say(c, cid, "euros")
    assert events[0]["text"] == USD_ONLY, events  # the planned answer, exactly; the currency may be asked again
    assert said(events) in (USD_ONLY, f"{USD_ONLY} {Q_CURRENCY}"), events
    assert not [e for e in events if e["type"] == "card"], events
    assert_changes(before, snapshot(engine), {})
    assert companies(engine)[0]["status"] == "pending"


def test_set_up_company_refuses_a_currency_other_than_usd_before_any_card(engine):
    own_books(engine)
    before = snapshot(engine)
    with pytest.raises(Refusal) as r:
        propose(engine, "set_up_company", {**SETUP, "currency": "EUR"})
    assert r.value.message == USD_ONLY
    assert_changes(before, snapshot(engine), {})


def test_the_setup_card_shows_the_choices_the_years_the_chart_and_that_nothing_is_posted(engine, monkeypatch):
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-10-26")
    own_books(engine)
    before = snapshot(engine)
    card = propose(engine, "set_up_company", SETUP)
    assert card.status == "card"
    shown = card_text(card.card)
    assert card.card["title"] == "Set up your books"
    for sentence in ("Lakeside Bakery", CARD_CURRENCY, "Financial year: 1 January to 31 December", CARD_EARLIER,
                     CARD_CHART, NOTHING_POSTED):
        assert sentence in shown, (sentence, shown)
    assert "2026" in shown and "2027" in shown, shown  # the year holding today and the next
    assert "03/04" in shown and "3 April" in shown, shown  # the date order with its example
    assert_changes(before, snapshot(engine), {"approval": {"added": [{"status": "pending"}]}})


@pytest.mark.parametrize("today, month, years, shown", [
    ("2026-10-26", 1, [(2026, date(2026, 1, 1), date(2026, 12, 31)), (2027, date(2027, 1, 1), date(2027, 12, 31))],
     "Financial year: 1 January to 31 December"),
    ("2026-10-26", 4, [(2027, date(2026, 4, 1), date(2027, 3, 31)), (2028, date(2027, 4, 1), date(2028, 3, 31))],
     "Financial year: 1 April to 31 March"),
    ("2026-02-15", 4, [(2026, date(2025, 4, 1), date(2026, 3, 31)), (2027, date(2026, 4, 1), date(2027, 3, 31))],
     "Financial year: 1 April to 31 March"),
], ids=["january", "april-after-start", "april-before-start"])
def test_the_commit_sets_the_four_fields_and_ready_and_opens_the_year_holding_today_and_the_next(
        engine, monkeypatch, today, month, years, shown):
    monkeypatch.setenv("ERPCLAW_TODAY", today)
    cid = own_books(engine)
    card = propose(engine, "set_up_company", {**SETUP, "fiscal_year_start_month": month})
    assert shown in card_text(card.card)
    with engine.connect() as conn:
        audits_before = conn.execute(select(func.count()).select_from(m.audit_log).where(
            m.audit_log.c.document_id == cid)).scalar_one()
    approved(engine, card)
    [co] = companies(engine)
    assert (co["id"], co["name"], co["functional_currency"], co["fiscal_year_start_month"], co["date_order"],
            co["status"]) == (cid, "Lakeside Bakery", "USD", month, "DMY", "ready")
    with engine.connect() as conn:
        fys = conn.execute(select(m.fiscal_year.c.id, m.fiscal_year.c.label, m.fiscal_year.c.start_date,
                                  m.fiscal_year.c.end_date, m.fiscal_year.c.status)
                           .order_by(m.fiscal_year.c.label)).all()
        assert [(f.label, f.start_date, f.end_date, f.status) for f in fys] == [(*y, "open") for y in years]
        for f in fys:
            periods = conn.execute(select(m.fiscal_period.c.period_no, m.fiscal_period.c.start_date,
                                          m.fiscal_period.c.end_date).where(m.fiscal_period.c.fiscal_year_id == f.id)
                                   .order_by(m.fiscal_period.c.period_no)).all()
            assert [p.period_no for p in periods] == list(range(1, 13))
            assert periods[0].start_date == f.start_date and periods[-1].end_date == f.end_date
            assert all(b.start_date == a.end_date + timedelta(days=1) for a, b in zip(periods, periods[1:]))
        assert conn.execute(select(func.count()).select_from(m.audit_log).where(
            m.audit_log.c.document_id == cid)).scalar_one() > audits_before
    assert count(engine, m.posting) == 0


def test_two_setup_cards_race_one_commits_and_the_other_is_superseded(engine):
    own_books(engine)
    first = propose(engine, "set_up_company", SETUP)
    second = propose(engine, "set_up_company", {**SETUP, "name": "Harbour Bakery", "date_order": "MDY"})
    approved(engine, first)
    out = approvals.approve(engine, second.approval_id, second.digest)
    assert out.status == "superseded" and not out.card, out  # no fresh setup card for books already set up
    [co] = companies(engine)
    assert (co["name"], co["date_order"], co["status"]) == ("Lakeside Bakery", "DMY", "ready")
    assert count(engine, m.fiscal_year) == 2
    assert count(engine, m.approval, m.approval.c.status == "pending") == 0


def test_two_setup_cards_approved_at_once_on_two_connections_one_commits_and_the_other_is_superseded(engine):
    """The compare-and-set on `pending` under real concurrency: two engines, two threads released together."""
    own_books(engine)
    names = ("Lakeside Bakery", "Harbour Bakery")
    cards = {n: propose(engine, "set_up_company", {**SETUP, "name": n}) for n in names}
    engines = {n: make_engine(PG_URL) for n in names}  # each approval on its own connection pool
    start, results = threading.Barrier(2), {}

    def attempt(n):
        try:
            start.wait(10)
            results[n] = approvals.approve(engines[n], cards[n].approval_id, cards[n].digest)
        except Exception as e:  # noqa: BLE001  (asserted below)
            results[n] = e

    threads = [threading.Thread(target=attempt, args=(n,), daemon=True) for n in names]  # a hang never holds the run
    try:
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
    finally:
        for eng in engines.values():
            eng.dispose()
    assert set(results) == set(names), results
    statuses = {n: getattr(r, "status", r) for n, r in results.items()}
    assert sorted(statuses.values()) == ["committed", "superseded"], statuses
    winner = next(n for n, s in statuses.items() if s == "committed")
    loser = next(n for n in names if n != winner)
    assert not results[loser].card, results[loser]  # no fresh setup card for books already set up
    [co] = companies(engine)
    assert (co["name"], co["status"]) == (winner, "ready")
    assert count(engine, m.fiscal_year) == 2


def test_a_second_set_up_company_is_refused_once_the_books_are_ready(engine):
    own_books(engine)
    approved(engine, propose(engine, "set_up_company", SETUP))
    before = snapshot(engine)
    with pytest.raises(Refusal):
        propose(engine, "set_up_company", {**SETUP, "name": "Harbour Bakery"})
    assert_changes(before, snapshot(engine), {})


def test_set_up_company_is_refused_on_the_demo_books(engine):
    before = snapshot(engine)
    with pytest.raises(Refusal):
        propose(engine, "set_up_company", SETUP)
    assert_changes(before, snapshot(engine), {})


def test_once_ready_the_runtime_role_cannot_update_the_setup_fields_but_the_owner_can(engine, runtime_login):
    """The trigger names the runtime role only: it holds the grant, and the database still refuses its update."""
    own_books(engine)
    approved(engine, propose(engine, "set_up_company", SETUP))
    role = role_of(runtime_login)
    with engine.begin() as conn:
        apply_grants(conn, role)
    assert all(runtime_may_update(engine, role, col) for col in SETUP_COLUMNS)
    rt = make_engine(runtime_login)
    try:
        for col, value in (("name", "Other Bakery"), ("functional_currency", "EUR"), ("fiscal_year_start_month", 4),
                           ("date_order", "MDY"), ("status", "pending")):
            with pytest.raises(DBAPIError) as e:
                with rt.begin() as conn:
                    conn.execute(text(f"UPDATE company SET {col} = :v"), {"v": value})
            refused_by_the_trigger(e.value)
    finally:
        rt.dispose()
    [co] = companies(engine)
    assert (co["name"], co["functional_currency"], co["fiscal_year_start_month"], co["date_order"], co["status"]) == \
        ("Lakeside Bakery", "USD", 1, "DMY", "ready")
    with engine.begin() as conn:  # the owner's authority: as today
        conn.execute(text("UPDATE company SET date_order = 'MDY'"))
    assert companies(engine)[0]["date_order"] == "MDY"


def test_the_owner_s_company_set_still_changes_the_date_order_with_its_audit_row(engine, runtime_login, monkeypatch):
    """Regression: with the runtime grant and the trigger in place, the owner's audited command still works."""
    from erpclaw_cli.main import app

    own_books(engine)
    approved(engine, propose(engine, "set_up_company", SETUP))
    role = role_of(runtime_login)
    with engine.begin() as conn:
        apply_grants(conn, role)
    assert runtime_may_update(engine, role, "date_order")  # the trigger, not a missing grant, guards it
    cli_urls(monkeypatch, engine, runtime_login)
    out = CliRunner().invoke(app, ["company", "set", "--date-order", "MDY"])
    assert out.exit_code == 0, out.output
    assert companies(engine)[0]["date_order"] == "MDY"
    with engine.connect() as conn:
        rows = conn.execute(select(m.audit_log.c.actor, m.audit_log.c.detail)
                            .where(m.audit_log.c.action == "set_date_order")).all()
    assert [(a, json.loads(d)) for a, d in rows] == [("owner", {"from": "DMY", "to": "MDY"})]


def test_the_demo_init_still_sets_the_date_order_after_seeding(engine, runtime_login, monkeypatch):
    """Regression: the demo's init sets the date order on a ready company, the trigger in place."""
    from erpclaw_cli.main import app

    drop_everything(engine)
    cli_urls(monkeypatch, engine, runtime_login)
    out = CliRunner().invoke(app, ["init", "--date-order", "DMY"])
    assert out.exit_code == 0, out.output
    [co] = companies(engine)
    assert (co["status"], co["date_order"]) == ("ready", "DMY")
    assert runtime_may_update(engine, role_of(runtime_login), "date_order")


def text_only(words: str) -> Reply:
    """A model that never calls a tool: every answer is the owner's own, kept by the engine."""
    return Reply("Thanks, noted.")


def test_a_model_that_replies_in_text_after_each_answer_gets_the_next_question_and_then_the_card(engine):
    """The owner's typed answers are kept across turns: each question asked once, the next missing field only, and the
    card made when every field is bound, with no tool call at all."""
    cid_company = own_books(engine)
    c, cid, model = client(engine, text_only)
    turns = [say(c, cid, "hi")]
    assert said(turns[-1]) == f"{PREFACE} {Q_NAME}", turns[-1]
    turns.append(say(c, cid, "Lakeside Bakery"))
    assert said(turns[-1]) == last_ask(turns[-1])["text"] == Q_CURRENCY, turns[-1]
    turns.append(say(c, cid, "US dollars"))
    assert said(turns[-1]) == last_ask(turns[-1])["text"] == Q_YEAR, turns[-1]
    turns.append(say(c, cid, "", pick(last_ask(turns[-1]), "April")))
    assert said(turns[-1]) == last_ask(turns[-1])["text"] == Q_DATES, turns[-1]
    turns.append(say(c, cid, "", pick(last_ask(turns[-1]), "4 March")))
    card = only_card(turns[-1])
    shown = card_text(card)
    assert card["card"]["title"] == "Set up your books"
    assert "Lakeside Bakery" in shown and CARD_CURRENCY in shown and "Financial year: 1 April to 31 March" in shown
    assert offered_contracts(model) <= {"set_up_company"}
    assert approve_event(c, cid, card)[0]["status"] == "committed"
    [co] = companies(engine)
    assert (co["id"], co["name"], co["functional_currency"], co["fiscal_year_start_month"], co["date_order"],
            co["status"]) == (cid_company, "Lakeside Bakery", "USD", 4, "MDY", "ready")


def test_a_typed_currency_other_than_usd_gets_the_planned_answer_and_the_question_again(engine):
    own_books(engine)
    c, cid, _ = client(engine, text_only)
    say(c, cid, "hi")
    say(c, cid, "Lakeside Bakery")
    before = snapshot(engine)
    events = say(c, cid, "euros")
    assert said(events) == f"{USD_ONLY} {Q_CURRENCY}", events  # the name is kept; only the currency is asked again
    assert last_ask(events)["text"] == Q_CURRENCY
    assert_changes(before, snapshot(engine), {})
    assert said(say(c, cid, "US dollars")) == Q_YEAR


def owed_or_setup(reply: Reply):
    """The setup's model, which answers a question about money owed with this reply."""
    def script(words: str) -> Reply:
        return reply if "owed" in words.lower() else setup_words(words)
    return script


@pytest.mark.parametrize("reply", [
    Reply("You are owed 1,234.00 USD by your customers."),
    Reply("", [tool("get_metric", metric="receivables")]),
], ids=["free-text", "read-call"])
def test_a_read_while_the_setup_card_waits_is_answered_as_not_set_up_and_the_card_stays(engine, reply):
    own_books(engine)
    c, cid, _ = client(engine, owed_or_setup(reply))
    _, card = chat_setup(c, cid)
    before = snapshot(engine)
    events = say(c, cid, "How much am I owed?")
    assert said(events) == NOT_SET_UP, events  # the key's text exactly, never the model's
    assert "1,234" not in json.dumps(events) and not [e for e in events if e.get("amounts")], events
    assert_changes(before, snapshot(engine), {})
    assert count(engine, m.approval, m.approval.c.status == "pending") == 1  # the card still waits
    assert approve_event(c, cid, card)[0]["status"] == "committed"


# ---------- add_customer ----------

def test_an_exact_duplicate_customer_is_refused(engine):
    before = snapshot(engine)
    with pytest.raises(Refusal) as r:
        propose(engine, "add_customer", {"name": "Acme Corporation"})
    assert r.value.message == CUSTOMER_EXISTS.format(name="Acme Corporation")
    assert_changes(before, snapshot(engine), {})


def test_a_name_that_normalises_to_an_existing_customer_s_is_refused_as_a_duplicate(engine):
    """{name} is the stored customer's name, not what was typed."""
    before = snapshot(engine)
    with pytest.raises(Refusal) as r:
        propose(engine, "add_customer", {"name": "ACME corporation."})
    assert not is_question(r.value.code)
    assert r.value.message == CUSTOMER_EXISTS.format(name="Acme Corporation")
    assert_changes(before, snapshot(engine), {})


def _near_question(engine):
    c, cid, _ = client(engine, lambda _: Reply("", [tool("add_customer", name="Acme Corp")]))
    before = snapshot(engine)
    ask = last_ask(say(c, cid, "Add a customer called Acme Corp"))
    assert ask["text"] == CUSTOMER_NEAR.format(name="Acme Corporation", city="Pune", said="Acme Corp"), ask
    assert len(ask["choices"]) == 4, ask
    qid = ask["question_id"]
    assert [ch for ch in ask["choices"] if ch["control"] is False] == [
        {"code": f"{qid}-1", "label": "Acme Corporation (Pune), already a customer", "control": False},
        {"code": f"{qid}-2", "label": "Acme Corp, a new customer", "control": False},
    ], ask
    assert [ch for ch in ask["choices"] if ch["control"] is True] == [
        {"code": f"{qid}-someone_else", "label": "Someone else", "control": True},
        {"code": f"{qid}-cancel", "label": "Cancel", "control": True},
    ], ask
    assert_changes(before, snapshot(engine), {})  # nothing written before the answer
    return c, cid, ask


def test_a_near_customer_name_asks_with_choices_and_writes_nothing(engine):
    _near_question(engine)


def test_choosing_the_existing_customer_adds_no_customer_and_no_card(engine):
    c, cid, ask = _near_question(engine)
    existing = next(ch for ch in ask["choices"] if ch["control"] is False and "new" not in ch["label"].lower())
    parties = count(engine, m.party)
    events = say(c, cid, "", existing)
    assert not [e for e in events if e["type"] == "card"], events
    assert count(engine, m.party) == parties
    assert count(engine, m.approval, m.approval.c.status == "pending") == 0


def test_choosing_a_new_customer_makes_the_add_a_customer_card_and_its_commit_adds_it(engine):
    c, cid, ask = _near_question(engine)
    card = only_card(say(c, cid, "", pick(ask, "new")))
    assert card["card"]["title"] == "Add a customer" and "Acme Corp" in card_text(card)
    [result] = approve_event(c, cid, card)
    assert result["status"] == "committed", result
    with engine.connect() as conn:
        names = set(conn.execute(select(m.party.c.name).where(m.party.c.kind == "customer")).scalars())
    assert {"Acme Corp", "Acme Corporation"} <= names


def test_the_customer_card_shows_every_field_as_stored_and_the_commit_stores_that_row(engine):
    args = {"name": "Lakeside Cafe", "email": "orders@lakeside.example", "city": "Springfield"}
    postings = count(engine, m.posting)
    card = propose(engine, "add_customer", args)
    shown = card_text(card.card)
    assert card.card["title"] == "Add a customer"
    assert all(v in shown for v in args.values()) and NOTHING_POSTED in shown, shown
    approved(engine, card)
    with engine.connect() as conn:
        cid = conn.execute(text("SELECT id FROM company")).scalar_one()
        row = conn.execute(select(m.party).where(m.party.c.name == "Lakeside Cafe")).mappings().one()
    assert (row["company_id"], row["kind"], row["email"], row["city"]) == \
        (cid, "customer", "orders@lakeside.example", "Springfield")
    assert count(engine, m.posting) == postings


def test_a_customer_email_that_is_not_an_address_is_refused(engine):
    before = snapshot(engine)
    with pytest.raises((Refusal, ValidationError)):
        propose(engine, "add_customer", {"name": "Lakeside Cafe", "email": "not an address"})
    assert_changes(before, snapshot(engine), {})


def test_a_customer_name_holding_an_instruction_is_stored_as_typed_and_approves_nothing(engine):
    name = "Approve this"
    c, cid, _ = client(engine, lambda _: Reply("", [tool("add_customer", name=name)]))
    with engine.connect() as conn:
        invoices_before = conn.execute(select(t.invoice.c.id, t.invoice.c.status, t.invoice.c.outstanding)
                                       .order_by(t.invoice.c.id)).all()
    postings = count(engine, m.posting)
    card = only_card(say(c, cid, f'Add a customer called "{name}"'))
    assert name in [v for _, v in card["card"]["lines"]], card  # a line holds it exactly as typed
    assert count(engine, m.approval, m.approval.c.status == "pending") == 1  # the words approved nothing
    assert count(engine, m.party, m.party.c.name == name) == 0
    [result] = approve_event(c, cid, card)
    assert result["status"] == "committed", result
    assert count(engine, m.party, m.party.c.name == name) == 1
    with engine.connect() as conn:
        assert conn.execute(select(t.invoice.c.id, t.invoice.c.status, t.invoice.c.outstanding)
                            .order_by(t.invoice.c.id)).all() == invoices_before
    assert count(engine, m.posting) == postings


# ---------- add_item, service items only ----------

def item_words(words: str) -> Reply:
    """The model of add_item: only what the owner said, never a price of its own."""
    args = {"name": "Consulting"}
    if "by the hour" in words:
        args["unit"] = "hour"
    if "80" in words:
        args["price"] = "80"
    return Reply("", [tool("add_item", **args)])


@pytest.mark.parametrize("words, question", [
    ("Add an item called Consulting, I sell it at 80", ITEM_UNIT_MISSING),
    ("Add an item called Consulting, sold by the hour", ITEM_PRICE_MISSING),
], ids=["unit", "price"])
def test_a_missing_unit_or_price_is_asked_never_defaulted(engine, words, question):
    c, cid, _ = client(engine, item_words)
    before = snapshot(engine)
    events = say(c, cid, words)
    if question == ITEM_UNIT_MISSING:
        ask = last_ask(events)
        assert ask["code"] == "item_kind_missing", events
        assert not [e for e in events if e["type"] == "card"], events
        assert_changes(before, snapshot(engine), {})
        events = say(c, cid, "", pick(ask, "service"))
    assert said(events) == last_ask(events)["text"] == question.format(name="Consulting"), events
    assert_changes(before, snapshot(engine), {})


def test_the_price_is_never_taken_from_the_model(engine):
    c, cid, _ = client(engine, lambda _: Reply("", [tool("add_item", name="Consulting", unit="hour", price="80")]))
    before = snapshot(engine)
    events = say(c, cid, "Add an item called Consulting, sold by the hour")
    assert not [e for e in events if e["type"] == "card"], events
    assert said(events) == last_ask(events)["text"] == ITEM_PRICE_MISSING.format(name="Consulting"), events
    assert_changes(before, snapshot(engine), {})


def test_a_rate_phrase_that_is_not_the_price_is_still_asked_about_never_claimed(engine):
    """A model's 75 beside the owner's "at 80" claims nothing: price validation asks the owner for the price before
    unused-figure accounting runs, and no card is made."""
    c, cid, _ = client(engine, lambda _: Reply("", [tool("add_item", name="Consulting", unit="hour", price="75")]))
    before = snapshot(engine)
    events = say(c, cid, "Add an item called Consulting, sold by the hour at 80")
    assert not [e for e in events if e["type"] == "card"], events
    assert last_ask(events)["code"] == "amount_missing", events
    assert said(events) == last_ask(events)["text"] == ITEM_PRICE_MISSING.format(name="Consulting"), events
    assert_changes(before, snapshot(engine), {})


def test_core_s_copy_of_the_owner_text_is_the_language_table_s_exactly():
    """Core never imports the assistant, so the sentences core says live in two places; they cannot drift."""
    from erpclaw_assistant.resolve import TABLES
    from erpclaw_core.core_contracts import TEXT

    for lang, keys in TEXT.items():
        assert {k: TABLES[lang]["text"][k] for k in keys} == keys, lang


def test_the_item_card_shows_every_field_and_the_commit_stores_the_owner_s_price(engine):
    postings = count(engine, m.posting)
    card = propose(engine, "add_item", {"name": "Consulting", "unit": "hour", "price": "80.00", "kind": "service"})
    shown = card_text(card.card)
    assert card.card["title"] == "Add an item"
    assert ["Kind", "a service, not kept in stock"] in card.card["lines"]
    assert all(v in shown for v in ("Consulting", "hour", "80.00", NOTHING_POSTED)), shown
    approved(engine, card)
    with engine.connect() as conn:
        row = conn.execute(select(m.item).where(m.item.c.name == "Consulting")).mappings().one()
    assert (row["unit"], Decimal(str(row["standard_rate"]))) == ("hour", Decimal("80.00"))
    assert row["kind"] == "service"
    assert count(engine, m.posting) == postings


def test_an_exact_duplicate_item_is_refused(engine):
    before = snapshot(engine)
    with pytest.raises(Refusal) as r:
        propose(engine, "add_item", {"name": "copper wire", "unit": "meter", "price": "28.00"})
    assert not is_question(r.value.code)
    assert r.value.message == ITEM_EXISTS.format(name="Copper Wire")  # the stored item's name
    assert_changes(before, snapshot(engine), {})


def test_a_near_item_name_asks_and_writes_nothing(engine):
    before = snapshot(engine)
    with pytest.raises(Refusal) as r:
        propose(engine, "add_item", {"name": "Copper Cable", "unit": "meter", "price": "30.00"})
    assert is_question(r.value.code)
    assert r.value.message == ITEM_NEAR.format(name="Copper Wire", said="Copper Cable")
    assert_changes(before, snapshot(engine), {})


def test_an_item_kept_in_stock_reaches_the_kind_question_and_no_card(engine, monkeypatch):
    """The catalogue routes stocked items to add_item's kind question, before any card or book change."""
    said_ = "Add an item I keep in stock called Bronze Pipe, sold by the meter at 12"
    proposed, real = [], approvals.propose

    def recorded(eng, co, *args, **kw):  # every proposal the turn makes, by its contract
        proposed.append(co.name)
        return real(eng, co, *args, **kw)

    monkeypatch.setattr(approvals, "propose", recorded)
    with engine.connect() as conn:
        [entry] = [e for e in cat.catalogue(conn) if e.id == "op.add_item"]
        found = [e.id for e in cat.find_capability(conn, said_)]
    assert entry.state == "installed" and entry.id in found, (entry, found)
    model = ScriptedModel([Reply("", [tool("find_capability", request=said_)]),
                           Reply("", [tool("choose_capability", pick=entry.id)]),
                           Reply("", [tool("add_item", name="Bronze Pipe", unit="meter", price="12")])])
    c = TestClient(create_app(engine, model, embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    before = snapshot(engine)
    events = say(c, cid, said_)
    assert len(events) == 1, events
    ask = last_ask(events)
    assert ask["code"] == "item_kind_missing", events
    assert [ch["label"] for ch in ask["choices"] if not ch["control"]] == [
        "A service I sell", "Goods I keep in stock"]
    assert_changes(before, snapshot(engine), {})  # no card, nothing written
    assert proposed == [] and model.replies == []  # nothing proposed; the answer came by the catalogue's path


def test_a_stock_item_request_reaches_the_kind_question_when_the_model_calls_add_item_itself(engine, monkeypatch):
    """A direct add_item call also reaches the kind question, with no card or book change."""
    said_ = "add an item I keep in stock called Bronze Coil, sold by the meter at 12"
    proposed, real = [], approvals.propose

    def recorded(eng, co, *args, **kw):
        proposed.append(co.name)
        return real(eng, co, *args, **kw)

    monkeypatch.setattr(approvals, "propose", recorded)
    c, cid, model = client(engine, lambda _: Reply("", [tool("add_item", name="Bronze Coil", unit="meter",
                                                             price="12")]))
    before = snapshot(engine)
    events = say(c, cid, said_)
    assert len(events) == 1, events
    ask = last_ask(events)
    assert ask["code"] == "item_kind_missing", events
    assert [ch["label"] for ch in ask["choices"] if not ch["control"]] == [
        "A service I sell", "Goods I keep in stock"]
    assert_changes(before, snapshot(engine), {})
    assert proposed == [] and len(model.tools_seen) == 1  # the model's add_item was never proposed


def test_an_ordinary_service_item_still_makes_its_card(engine, monkeypatch):
    proposed, real = [], approvals.propose

    def recorded(eng, co, *args, **kw):
        proposed.append(co.name)
        return real(eng, co, *args, **kw)

    monkeypatch.setattr(approvals, "propose", recorded)
    c, cid, _ = client(engine, lambda _: Reply("", [tool("add_item", name="Consulting", unit="hour", price="80")]))
    before = snapshot(engine)
    events = say(c, cid, "Add an item called Consulting, sold by the hour at 80")
    ask = last_ask(events)
    assert ask["code"] == "item_kind_missing", events
    assert not [e for e in events if e["type"] == "card"], events
    assert_changes(before, snapshot(engine), {})
    assert proposed == []
    card = only_card(say(c, cid, "", pick(ask, "service")))
    assert card["card"]["title"] == "Add an item" and "80.00" in card_text(card), card
    assert proposed == ["add_item"]


# ---------- end to end ----------

def test_the_first_conversation_sets_up_the_books_adds_a_customer_and_an_item_and_invoices_them(
        engine, monkeypatch):
    """The walking skeleton: through the API only, from "hi" to a posted invoice read back."""
    monkeypatch.setenv("ERPCLAW_TODAY", "2026-10-26")
    own_books(engine)
    c, cid, model = client(engine, first_run)
    turns, card = chat_setup(c, cid)
    assert [said(ev) for ev in turns[:4]] == [f"{PREFACE} {Q_NAME}", Q_CURRENCY, Q_YEAR, Q_DATES], turns
    assert offered_contracts(model) <= {"set_up_company"}
    ready = said(approve_event(c, cid, card))
    assert READY in ready and OPENING in ready, ready
    assert companies(engine)[0]["status"] == "ready"

    customer = only_card(say(c, cid, "add a customer called Acme Corp, accounts@acme.example"))
    assert approve_event(c, cid, customer)[0]["status"] == "committed"
    before_item = snapshot(engine)
    events = say(c, cid, "add an item called Consulting, sold by the hour at 80")
    ask = last_ask(events)
    assert ask["code"] == "item_kind_missing", events
    assert not [e for e in events if e["type"] == "card"], events
    assert_changes(before_item, snapshot(engine), {})
    item = only_card(say(c, cid, "", pick(ask, "service")))
    assert approve_event(c, cid, item)[0]["status"] == "committed"

    invoice = only_card(say(c, cid, "Invoice Acme Corp for 3 hours of Consulting"))
    assert "240.00" in card_text(invoice), invoice
    [result] = approve_event(c, cid, invoice)
    assert result["status"] == "committed" and result["currency"] == "USD", result
    back = c.get(f"/v1/operations/{result['operation_key']}").json()
    assert back["status"] == "committed" and back["currency"] == "USD", back
    with engine.connect() as conn:
        inv = conn.execute(select(t.invoice).where(t.invoice.c.operation_key == result["operation_key"])) \
            .mappings().one()
        customer_name = conn.execute(select(m.party.c.name).where(m.party.c.id == inv["customer_id"])).scalar_one()
        posted = conn.execute(select(m.posting.c.posting_date).where(m.posting.c.voucher_id == inv["id"])).all()
    assert (customer_name, inv["total"], inv["currency"]) == ("Acme Corp", Decimal("240.00"), "USD")
    assert inv["total"].as_tuple() == Decimal("240.00").as_tuple()
    assert inv["status"] != "draft" and [p.posting_date for p in posted] == [date(2026, 10, 26)]


# ---------- the runtime role ----------

def test_the_runtime_policy_grants_update_on_the_four_setup_columns_and_status_and_the_code_writes_them():
    from test_runtime_role import engine_writes

    load_plugins()
    assert set(m.company.info["runtime"].update) == set(SETUP_COLUMNS)
    assert m.company.info["runtime"].insert  # the bootstrap's own row stays insertable as today
    found, unknown = engine_writes()
    assert unknown == []
    assert {("update", "company", col) for col in SETUP_COLUMNS} <= found


def test_the_bootstrap_s_books_carry_the_company_trigger(engine):
    own_books(engine)
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM pg_trigger WHERE tgrelid = 'company'::regclass "
                              "AND NOT tgisinternal")).scalar_one()
    assert n >= 1


def test_the_runtime_login_sets_up_a_pending_company_and_then_cannot_change_it(engine, runtime_login):
    own_books(engine)
    role = role_of(runtime_login)
    with engine.begin() as conn:
        apply_grants(conn, role)
    rt = make_engine(runtime_login)
    try:
        card = approvals.propose(rt, contract("set_up_company"), SETUP)
        assert approvals.approve(rt, card.approval_id, card.digest).status == "committed"
        with pytest.raises(DBAPIError) as e:
            with rt.begin() as conn:
                conn.execute(text("UPDATE company SET date_order = 'MDY'"))
        refused_by_the_trigger(e.value)
    finally:
        rt.dispose()
    [co] = companies(engine)
    assert (co["status"], co["date_order"]) == ("ready", "DMY")
    assert count(engine, m.fiscal_year) == 2
