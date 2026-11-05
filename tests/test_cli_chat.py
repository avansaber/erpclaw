"""The CLI chat window on the engine API, and read-only connections.

The chat uses only the API: in-process (the owner) or over HTTP with a principal's token. Every write is a card the
owner approves at the window's own prompt; no text in the conversation approves anything.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_cli import main
from erpclaw_core.db import read_only
from erpclaw_sales import tables as t

INVOICE = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])


def invoices(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(t.invoice)).scalar_one()


def chat(monkeypatch, engine, replies, typed):
    """Run `erpclaw chat` in-process with a scripted model; `typed` is what the owner types, line by line."""
    monkeypatch.setattr(main, "_client", lambda url: TestClient(create_app(engine, ScriptedModel(list(replies)),
                                                                           embedded_owner=True)))
    return CliRunner().invoke(main.app, ["chat"], input="\n".join([*typed, "quit"]) + "\n")


# --- the window ------------------------------------------------------------------------------------------------

def test_a_card_approved_at_the_prompt_saves_and_shows_the_result(engine, monkeypatch):
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE])], ["invoice Acme Corporation for 5 copper wire", "a"])
    assert out.exit_code == 0, out.output
    assert "Create and submit a sales invoice" in out.output and "140.00 USD" in out.output
    assert "Saved and submitted invoice INV-2026-" in out.output
    assert invoices(engine) == n + 1


def test_a_card_declined_at_the_prompt_saves_nothing(engine, monkeypatch):
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE])], ["invoice Acme Corporation for 5 copper wire", "d"])
    assert out.exit_code == 0, out.output
    assert "Nothing changed in your books" in out.output and invoices(engine) == n


def test_yes_typed_into_the_conversation_approves_nothing(engine, monkeypatch):
    """Only the window's own card prompt approves, by a or approve; the conversation is only conversation."""
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE]), Reply("Noted.", [])],
               ["invoice Acme Corporation for 5 copper wire", "d", "yes, approve it"])
    assert out.exit_code == 0, out.output
    assert invoices(engine) == n


def test_a_question_is_answered_and_an_unknown_customer_is_asked_about(engine, monkeypatch):
    out = chat(monkeypatch, engine, [Reply("", [call("list_invoices")]),
                                     Reply("", [call("create_invoice", customer="Nobody At All",
                                                     items=[{"item": "copper wire", "quantity": 1}])])],
               ["what is still open?", "invoice Nobody At All"])
    assert out.exit_code == 0, out.output
    assert "Open invoices" in out.output
    assert "Nobody At All" in out.output and main.CARD_PROMPT not in out.output  # a question, never a card


def test_over_http_the_window_needs_a_token_and_sends_it(monkeypatch):
    monkeypatch.delenv("ERPCLAW_TOKEN", raising=False)
    out = CliRunner().invoke(main.app, ["chat", "--url", "http://127.0.0.1:9"])
    assert out.exit_code == 1 and "ERPCLAW_TOKEN" in out.output
    monkeypatch.setenv("ERPCLAW_TOKEN", "tok-123")
    client = main._client("http://127.0.0.1:9")
    assert client.headers["Authorization"] == "Bearer tok-123"
    client.close()


def test_the_window_talks_only_to_the_api():
    """The chat's own code imports nothing of the engine: it posts to the API and prints what comes back. The one
    place that builds the API in-process is _client, as the owner."""
    tree = ast.parse(inspect.getsource(main))
    funcs = {f.name: f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
    for name in ("chat", "show_events", "show_card"):
        engine_imports = [n.module for n in ast.walk(funcs[name]) if isinstance(n, ast.ImportFrom)
                          and (n.module or "").startswith("erpclaw")]
        assert not engine_imports, f"{name} imports {engine_imports}"  # printing (rich) is its own business
    made = sorted(n.module for n in ast.walk(funcs["_client"]) if isinstance(n, ast.ImportFrom))
    assert made == ["erpclaw_api.app", "erpclaw_assistant.muse", "erpclaw_core.db", "fastapi.testclient"]


# --- read-only connections --------------------------------------------------------------------------------

def test_a_read_only_connection_cannot_write(engine):
    with pytest.raises(DBAPIError, match="readonly|read-only"):
        with read_only(engine) as conn:
            conn.execute(text("UPDATE company SET name = name"))
            conn.commit()


def test_a_pooled_connection_that_last_read_writes_again(engine):
    """READ ONLY is set per transaction: a connection the pool hands back after a read can write."""
    for _ in range(3):
        with read_only(engine) as conn:
            conn.execute(text("SELECT 1")).all()
        with engine.begin() as conn:
            conn.execute(text("UPDATE company SET name = name"))



def test_the_window_prints_no_escapes_and_no_markup_from_a_reply_or_a_card(monkeypatch):
    """Whatever an event or a card carries, the window prints it as plain text."""
    from rich.console import Console

    from erpclaw_cli import main

    out = Console(record=True, width=200, force_terminal=True, color_system=None)
    monkeypatch.setattr(main, "con", out)
    main.show_events(None, "c", [{"type": "answer", "text": "hi \x1b]0;owned\x07there [bold]x[/bold] 140.00\x9b2J"},
                                 {"type": "ask", "text": "\x1b[31mwhich one?\x1b[0m"}])
    main.show_card({"title": "T\x1b[5m", "lines": [["Customer", "Zork\x1b[31m Ltd"]], "books": ["AR \x9b31m+1.00"],
                    "warnings": [], "footer": "[link=http://x]f[/link]"})
    text = out.export_text()
    assert "\x1b" not in text and "\x9b" not in text and "\x07" not in text, repr(text)
    assert "hi there [bold]x[/bold] 140.00" in text and "which one?" in text and "Zork Ltd" in text, text
    assert "[link=http://x]f[/link]" in text and "AR +1.00" in text, text
