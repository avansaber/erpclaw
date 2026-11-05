"""The request: what the owner wrote across the turns of one request reaches its card, what an ended request held
never does, and the request lives in the assistant's own store, beside the books, so it survives a restart. The
store's writes are short transactions under a claim: one turn per conversation at a time."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from sqlalchemy import func, select

from erpclaw_assistant import store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals
from erpclaw_core import models as m


def assistant(engine, *replies):
    return Assistant(engine, ScriptedModel(list(replies)))


def inv(customer, quantity, item="copper wire"):
    return call("create_invoice", customer=customer, items=[{"item": item, "quantity": quantity}])


def kinds(events) -> list[str]:
    return [e["type"] for e in events]


def line(card, name):
    return next(v for k, v in card["card"]["lines"] if k == name)


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


# ---------- the store ----------

def test_a_version_1_store_moves_to_version_2_keeping_its_demand():
    p = store.path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE store_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO store_version VALUES (1)")
    conn.execute("CREATE TABLE capability_demand (at TEXT NOT NULL, company_id TEXT, outcome TEXT NOT NULL, "
                 "entry_id TEXT, request TEXT NOT NULL)")
    conn.execute("INSERT INTO capability_demand VALUES ('2026-10-26T00:00:00Z', 'c', 'd', NULL, 'fly me')")
    conn.commit()
    conn.close()
    assert store.version() == 2
    assert [r[-1] for r in store.demand()] == ["fly me"]
    assert store.open_request("c1") is None


def test_a_live_claim_refuses_a_second_message_and_writes_nothing():
    first = store.claim("c1", "invoice Hooli")
    assert first is not None
    assert store.claim("c1", "invoice Acme") is None
    assert [r["text"] for r in store.unassigned("c1")] == ["invoice Hooli"]


def test_a_second_conversation_is_never_blocked_by_the_first():
    assert store.claim("c1", "a") is not None
    assert store.claim("c2", "b") is not None


def test_a_lapsed_claim_is_taken_at_a_higher_generation_and_the_stalled_turn_stops(monkeypatch):
    monkeypatch.setattr(store, "LEASE", timedelta(0))
    stalled = store.claim("c1", "a")
    monkeypatch.setattr(store, "LEASE", timedelta(seconds=300))
    taker = store.claim("c1", "b")
    assert taker is not None and taker.generation > stalled.generation
    assert not store.renew(stalled)
    r = store.new_request(taker)
    assert store.finalise(stalled, r["id"], [], pending_question=None, pending_card=None, state="open", why=None,
                          serial_now=0, bindings_now={}, intent_state="unused") is False


def test_finalise_needs_the_claim_and_an_unchanged_version():
    c = store.claim("c1", "a")
    r = store.new_request(c)
    assert store.finalise(c, r["id"], [{"type": "answer", "text": "x"}], pending_question=None, pending_card=None,
                          state="ended", why="answer", serial_now=0, bindings_now={}, intent_state="unused")
    assert not store.finalise(c, r["id"], [], pending_question=None, pending_card=None, state="ended", why="answer",
                              serial_now=0, bindings_now={}, intent_state="unused")  # released: written once
    assert store.events(r["id"]) == [{"type": "answer", "text": "x"}]


def test_an_outcome_is_applied_once():
    assert store.apply_outcome("a1", "consumed", "r1", "ended")
    assert not store.apply_outcome("a1", "consumed", "r1", "ended")


# ---------- the request in a turn ----------

def test_a_figure_given_before_an_engine_question_reaches_the_card(engine):
    a = assistant(engine, Reply("", [inv("Wayne", 5)]), Reply("", [inv("Wayne Enterprises", 5)]))
    first = a.turn("c1", "Invoice Wayne for 5 copper wire")
    assert kinds(first) == ["ask"], first
    ev = a.turn("c1", "Wayne Enterprises")
    assert kinds(ev) == ["card"], ev
    assert line(ev[0], "Customer").startswith("Wayne Enterprises") and "5" in str(ev[0]["card"]["lines"]), ev


def test_a_figure_from_an_ended_request_never_reaches_a_card(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [inv("Hooli", 5)]))
    card = a.turn("c1", "Invoice Acme Corporation for 5 copper wire")[-1]
    assert approvals.approve(engine, card["approval_id"], card["digest"]).status == "committed"
    ev = a.turn("c1", "Invoice Hooli for copper wire")
    assert kinds(ev) == ["ask"], ev  # the 5 was the ended request's


def test_a_planned_reply_in_between_ends_the_request(engine):
    a = assistant(engine, Reply("", [inv("Wayne", 5)]), Reply("", [call("get_metric", metric="sales")]),
                  Reply("", [inv("Wayne Enterprises", 5)]))
    assert kinds(a.turn("c1", "Invoice Wayne for 5 copper wire")) == ["ask"]
    assert kinds(a.turn("c1", "how much did we sell this year")) == ["answer"]
    ev = a.turn("c1", "Wayne Enterprises copper wire")
    assert kinds(ev) == ["ask"], ev  # the 5 ended with its request


def test_a_live_claim_on_the_conversation_refuses_the_turn_as_busy(engine):
    store.claim("c1", "an earlier message", turn_id="another-turn")
    before = count(engine, m.approval)
    ev = assistant(engine, Reply("", [inv("Acme Corporation", 5)])).turn("c1", "Invoice Acme Corporation for 5 copper wire")
    assert [(e["type"], e.get("code")) for e in ev] == [("refused", "busy")], ev
    assert count(engine, m.approval) == before


def test_the_request_and_its_question_survive_a_restart(engine):
    first = assistant(engine, Reply("", [inv("Wayne", 5)]))
    assert kinds(first.turn("c1", "Invoice Wayne for 5 copper wire")) == ["ask"]
    again = assistant(engine, Reply("", [inv("Wayne Enterprises", 5)]))  # a new process: nothing in memory
    ev = again.turn("c1", "yes")
    assert kinds(ev) == ["card"], ev
    assert line(ev[0], "Customer").startswith("Wayne Enterprises")


def test_a_message_after_an_out_of_band_approval_starts_a_new_request(engine):
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [inv("Hooli", 3)]))
    card = a.turn("c1", "Invoice Acme Corporation for 5 copper wire")[-1]
    old = store.open_request("c1")
    assert old is not None and old["pending_card"] == card["approval_id"]
    assert approvals.approve(engine, card["approval_id"], card["digest"]).status == "committed"
    ev = a.turn("c1", "Invoice Hooli for 3 copper wire")
    assert kinds(ev) == ["card"], ev
    new = store.open_request("c1")
    assert new["id"] != old["id"] and new["pending_card"] == ev[-1]["approval_id"]
    assert store.get_request(old["id"])["state"] == "ended"
    assert [x["text"] for x in store.messages(old["id"])] == ["Invoice Acme Corporation for 5 copper wire"]
    assert [x["text"] for x in store.messages(new["id"])] == ["Invoice Hooli for 3 copper wire"]


# ---------- the fence row is the turn's only write to the books ----------

def test_a_turn_that_changes_nothing_writes_one_fence_row_and_nothing_else(engine):
    from bookdiff import CONTROL, snapshot

    assert CONTROL == {"conversation_fence"}  # the one table a turn writes that is not a business table
    before = snapshot(engine)
    ev = assistant(engine, Reply("", [call("get_metric", metric="receivables")])).turn("c1", "who owes us money?")
    assert kinds(ev) == ["answer"], ev
    assert snapshot(engine) == before
    assert count(engine, m.conversation_fence) == 1


def test_a_card_turn_s_business_rows_are_still_counted_exactly(engine):
    from erpclaw_cli.evals import honesty
    from erpclaw_cli.evals.run import BOOKKEEPING, diff
    from erpclaw_cli.evals.run import snapshot as eval_snapshot
    from erpclaw_sales import tables as t

    assert honesty.FREE == {"approval", "operation_outcome", "audit_log", "conversation_fence"}
    assert BOOKKEEPING == {"approval", "operation_outcome", "audit_log", "number_series", "conversation_fence"}

    def touched(before, after):
        return {name: v for name, v in diff(before, after).items() if v["added"] or v["changed"] or v["removed"]}

    before = eval_snapshot(engine)
    card = assistant(engine, Reply("", [inv("Acme Corporation", 5)])).turn(
        "c1", "Invoice Acme Corporation for 5 copper wire")[-1]
    drafted = eval_snapshot(engine)
    assert set(touched(before, drafted)) == {"approval", "conversation_fence"}
    assert honesty.business(touched(before, drafted)) == {}  # a card and the fence, no business row
    assert approvals.approve(engine, card["approval_id"], card["digest"]).status == "committed"
    approved = touched(drafted, eval_snapshot(engine))
    assert t.invoice.name in honesty.business(approved) and "conversation_fence" not in approved


def test_an_answer_by_its_code_through_the_api_binds(engine):
    from fastapi.testclient import TestClient

    from erpclaw_api.app import create_app

    c = TestClient(create_app(engine, ScriptedModel([Reply("", [inv("Wayne", 5)]), Reply("", [inv("Wayne", 5)])]),
                              embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    [ask] = c.post(f"/v1/conversations/{cid}/messages", json={"text": "Invoice Wayne for 5 copper wire"}).json()["events"]
    pick = next(x for x in ask["choices"] if x["label"] == "Wayne Enterprises")
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": pick["label"], "choice": pick["code"]}).json()["events"]
    assert kinds(ev) == ["card"], ev
    assert line(ev[0], "Customer").startswith("Wayne Enterprises")


# ---------- reconciliation: a crash between the stores, and the books restored ----------

def _lapse(conversation_id):
    """The claim of a turn that was killed lapses (its lease is over)."""
    conn = sqlite3.connect(store.path())
    conn.execute("UPDATE conversation SET claim_until = '2000-01-01T00:00:00.000000Z' WHERE id = ?", (conversation_id,))
    conn.commit()
    conn.close()


def test_a_card_made_before_a_crash_is_set_aside_and_every_route_to_it_is_refused(engine, monkeypatch):
    """The next turn after a crash is a takeover: the dead turn's card is set aside, said once in words true whether
    its turn died or stalled; its intent closes with that outcome; no route approves it; asking again makes a new card."""
    import pytest
    from fastapi.testclient import TestClient

    from erpclaw_api.app import create_app
    from erpclaw_assistant.resolve import _say
    from erpclaw_sales import tables as t

    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Hello."), Reply("", [inv("Acme Corporation", 5)]))
    real = store.finalise

    def killed(*args, **kwargs):
        raise RuntimeError("the process was killed after the card was made")

    monkeypatch.setattr(store, "finalise", killed)
    with pytest.raises(RuntimeError):
        a.turn("c1", "Invoice Acme Corporation for 5 copper wire")
    monkeypatch.setattr(store, "finalise", real)
    made = approvals.pending_card(engine, "c1")
    assert made is not None
    _lapse("c1")
    invoices = count(engine, t.invoice)
    ev = a.turn("c1", "hello?")
    assert ev[0] == {"type": "notice", "code": "card_set_aside", "text": _say("turn_unfinished")}, ev  # no amount
    assert [e["type"] for e in ev].count("notice") == 1 and not any(e["type"] == "card" for e in ev), ev
    assert store.outcome_applied(made["id"], "superseded")  # the lost turn's intent closed with the set-aside
    assert store.intent("c1")[1] != "open"  # the stores agree: nothing open, nothing pending the books ended
    assert (store.open_request("c1") or {}).get("pending_card") is None
    assert approvals.approve(engine, made["id"], made["digest"]).status == "superseded"
    api = TestClient(create_app(engine, ScriptedModel([]), embedded_owner=True))  # a restart: a new process's API
    out = api.post(f"/v1/cards/{made['id']}/approve", json={"digest": made["digest"]}).json()["events"]
    assert all(e.get("status") != "committed" for e in out), out
    assert count(engine, t.invoice) == invoices  # no route committed it
    fresh = a.turn("c1", "Invoice Acme Corporation for 5 copper wire")
    assert kinds(fresh) == ["card"] and fresh[0]["approval_id"] != made["id"], fresh  # asking again: a new card


def test_an_exception_after_the_card_never_leaves_an_open_intent_behind_a_released_claim(engine, monkeypatch):
    """An exception between propose and finalise (not a kill) keeps the claim held, so the conversation stays the
    failed turn's until its lease lapses; the next turn is then a takeover that sets the card aside and closes the
    intent. A handler releasing the claim would leave the card pending behind a live token, unseen."""
    import pytest

    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Hello."))
    real = store.finalise

    def failing(*args, **kwargs):
        raise RuntimeError("an error between the card and its record")

    monkeypatch.setattr(store, "finalise", failing)
    with pytest.raises(RuntimeError):
        a.turn("c1", "Invoice Acme Corporation for 5 copper wire")
    monkeypatch.setattr(store, "finalise", real)
    made = approvals.pending_card(engine, "c1")
    assert made is not None
    held = a.turn("c1", "hello?")
    assert [(e["type"], e.get("code")) for e in held] == [("refused", "busy")], held  # the claim was not released
    _lapse("c1")
    ev = a.turn("c1", "hello?")
    assert ev[0]["code"] == "card_set_aside", ev
    assert approvals.approve(engine, made["id"], made["digest"]).status == "superseded"
    assert store.intent("c1")[1] != "open"


def test_a_turn_killed_before_its_card_leaves_nothing_waiting(engine, monkeypatch):
    import pytest

    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("Hello."))

    def killed(*args, **kwargs):
        raise RuntimeError("the process was killed before the card was made")

    real = approvals.propose
    monkeypatch.setattr(approvals, "propose", killed)
    with pytest.raises(RuntimeError):
        a.turn("c1", "Invoice Acme Corporation for 5 copper wire")
    monkeypatch.setattr(approvals, "propose", real)  # only this; undo() would also lift the test's own data root
    _lapse("c1")
    ev = a.turn("c1", "hello?")
    assert kinds(ev) == ["answer"] and approvals.pending_card(engine, "c1") is None, ev


def test_a_books_restore_behind_the_store_ends_the_request_and_shows_no_card(engine, tmp_path):
    import secrets

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from conftest import PG_URL
    from erpclaw_core import backup as bk
    from erpclaw_core.db import make_engine

    bk.backup(engine, tmp_path / "before")
    a = assistant(engine, Reply("", [inv("Acme Corporation", 5)]), Reply("", [inv("Hooli", 3)]))
    done = a.turn("c1", "Invoice Acme Corporation for 5 copper wire")[-1]
    assert approvals.approve(engine, done["approval_id"], done["digest"]).status == "committed"
    assert kinds(a.turn("c1", "Invoice Hooli for 3 copper wire")) == ["card"]
    name = f"restored_{secrets.token_hex(4)}_test"
    admin = create_engine(make_url(PG_URL).set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        bk.restore(tmp_path / "before", bk.NewPostgresDatabase(PG_URL, name, None))
        restored = make_engine(make_url(PG_URL).set(database=name).render_as_string(hide_password=False))
        b = assistant(restored, Reply("Hello."), Reply("", [inv("Hooli", 3)]))
        ev = b.turn("c1", "hello?")
        assert [e.get("code") for e in ev if e["type"] == "notice"] == ["card_restored_away"], ev
        assert not any(e["type"] in ("card", "result") for e in ev), ev
        assert kinds(b.turn("c1", "Invoice Hooli for 3 copper wire")) == ["card"]  # a new request works
        restored.dispose()
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


def test_the_request_and_its_question_survive_a_new_process(engine):
    """The first turn runs in a process of its own, which then ends; the answer, in this one, binds its question."""
    import os
    import subprocess
    import sys
    import textwrap

    from conftest import PG_URL

    script = textwrap.dedent(f"""
        from erpclaw_assistant.graph import Assistant
        from erpclaw_assistant.model import Reply, ScriptedModel, call
        from erpclaw_core.db import make_engine
        from erpclaw_core.plugins import load_plugins

        load_plugins()
        reply = Reply("", [call("create_invoice", customer="Wayne", items=[{{"item": "copper wire", "quantity": 5}}])])
        ev = Assistant(make_engine({PG_URL!r}), ScriptedModel([reply])).turn("c1", "Invoice Wayne for 5 copper wire")
        assert [e["type"] for e in ev] == ["ask"], ev
    """)
    done = subprocess.run([sys.executable, "-c", script], env=dict(os.environ), capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
    ev = assistant(engine, Reply("", [inv("Wayne Enterprises", 5)])).turn("c1", "yes")
    assert kinds(ev) == ["card"], ev
    assert line(ev[0], "Customer").startswith("Wayne Enterprises")
