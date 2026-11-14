"""The chat window's own part of the turn loop: a card answered or changed by typing, a question answered by number,
and an approval that survives a lost reply.

At a card the window asks "Approve (a), decline (d), or type a change:"; anything other than a, approve, d or decline
is the owner's next message. At a question with choices they are numbered, control actions last, and a number typed
alone sends that choice's code; at a free question nothing is numbered and everything typed is text. Every approval
is first published to a journal in the window's data folder, one entry per approval, written atomically and held
under an exclusive lock while it is sent; a failed write sends nothing. A reply that never comes is checked by sending
the same approval again, in the same session on the owner's "y" and at the next chat's start; the engine answers a
repeat with what happened to that card, so a re-send never commits twice and never commits a card that has ended.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from typer.testing import CliRunner

from erpclaw_api.app import create_app
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_cli import journal, main
from erpclaw_core import approvals
from erpclaw_core import models as m
from erpclaw_core.common import now
from erpclaw_sales import tables as t

INVOICE = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 5}])
SIX = call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 6}])
PROMPT = "Approve (a), decline (d), or type a change:"
LOST = "I sent your approval but didn't get an answer. Check what happened? (y/n)"
UNSAFE = "I couldn't record your approval safely, so I didn't send it."
UNREACHABLE = "I still can't reach the books; your approval may or may not have been saved. I'll check again next time."
PW = "s3cret-pw"  # a stand-in password, joined into URLs at run time


def invoices(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(t.invoice)).scalar_one()


def quantities(engine):
    with engine.connect() as conn:
        return sorted(float(q) for q in conn.execute(select(t.invoice_line.c.quantity)).scalars())


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The window's data folder: the journal lives in its approvals/ folder."""
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path))
    return tmp_path


def entries(home):
    folder = home / "approvals"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


class Dropping:
    """The engine API as the window sees it, with chosen approvals' replies lost: `before` drops the request before
    the engine sees it, `after` lets the engine act and then drops its reply, `error` answers with a server error."""

    def __init__(self, app_client, drop: list[str]):
        self.client, self.drop, self.approvals = app_client, list(drop), []

    def post(self, path, **kw):
        if path.endswith("/approve"):
            self.approvals.append((path, kw.get("json")))
            how = self.drop.pop(0) if self.drop else None
            if how == "before":
                raise httpx.ConnectError("connection refused")
            if how == "error":
                return httpx.Response(502, json={"detail": "bad gateway"})
            r = self.client.post(path, **kw)
            if how == "after":
                raise httpx.ReadTimeout("timed out")
            return r
        return self.client.post(path, **kw)

    def get(self, path, **kw):
        return self.client.get(path, **kw)


def chat(monkeypatch, engine, replies, typed, drop=()):
    """Run `erpclaw chat` in-process with a scripted model; `typed` is what the owner types, line by line."""
    api = TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))
    window = Dropping(api, list(drop))
    monkeypatch.setattr(main, "_client", lambda url: window)
    out = CliRunner().invoke(main.app, ["chat"], input="\n".join([*typed, "quit"]) + "\n")
    out.window = window
    out.said = " ".join(out.output.split())  # the window's words, its line wrapping undone
    return out


def a_card(engine, replies=(Reply("", [INVOICE]),), said="invoice Acme Corporation for 5 copper wire"):
    """A pending card made through the API, as an earlier window would have shown it: (conversation, event)."""
    api = TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))
    cid = api.post("/v1/conversations").json()["id"]
    events = api.post(f"/v1/conversations/{cid}/messages", json={"text": said})
    card = next(e for e in events.json()["events"] if e["type"] == "card")
    return api, cid, card


def books_key():
    return main._books_key(None)


# --- the card prompt -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["a", "approve", "A", " Approve "])
def test_a_or_approve_at_the_card_prompt_saves_it(engine, monkeypatch, home, answer):
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE])], ["invoice Acme Corporation for 5 copper wire", answer])
    assert out.exit_code == 0, out.output
    assert PROMPT in out.output and "Saved and submitted invoice INV-2026-" in out.output
    assert invoices(engine) == n + 1


@pytest.mark.parametrize("answer", ["d", "decline", "D"])
def test_d_or_decline_at_the_card_prompt_saves_nothing(engine, monkeypatch, home, answer):
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE])], ["invoice Acme Corporation for 5 copper wire", answer])
    assert out.exit_code == 0, out.output
    assert "Nothing changed in your books" in out.output and invoices(engine) == n
    assert not out.window.approvals


def test_an_empty_answer_at_the_card_prompt_asks_again_and_sends_nothing(engine, monkeypatch, home):
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE])], ["invoice Acme Corporation for 5 copper wire", "", "", "a"])
    assert out.exit_code == 0, out.output
    assert out.output.count(PROMPT) == 3 and invoices(engine) == n + 1 and len(out.window.approvals) == 1


def test_a_change_typed_at_the_card_is_the_next_message_and_its_card_replaces_the_first(engine, monkeypatch, home):
    """The owner types the change at the card's own prompt: it goes to the conversation, the new card says the old one
    was set aside, and approving the new one saves only it."""
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE]), Reply("", [SIX])],
               ["invoice Acme Corporation for 5 copper wire", "make it 6", "a"])
    assert out.exit_code == 0, out.output
    assert out.output.count(PROMPT) == 2, out.output
    assert "was set aside; nothing was saved from it" in out.said
    assert invoices(engine) == n + 1 and quantities(engine).count(6.0) == 1 and len(out.window.approvals) == 1


def test_y_at_a_pending_card_never_approves_it_and_the_reply_says_the_card_was_set_aside(engine, monkeypatch, home):
    """The old prompt's y, typed at the new one: the owner's next message, never an approval. Nothing is sent to
    approve it and nothing is written; a lone "y" is not an acknowledgement the engine keeps the card for, so the card
    is set aside (superseded, never committed), and the reply says so in plain words. The prompt names its keys."""
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE]), Reply("Noted.", [])],
               ["invoice Acme Corporation for 5 copper wire", "y"])
    assert out.exit_code == 0, out.output
    assert "Approve (a), decline (d), or type a change" in out.said
    assert not out.window.approvals and invoices(engine) == n and entries(home) == []
    with engine.connect() as conn:
        assert conn.execute(select(m.approval.c.status)).scalars().all() == ["superseded"]
    said = out.said.split("type a change", 1)[-1]  # the reply after the card's prompt: the engine's own set-aside text
    assert "set it aside" in said and "nothing was saved" in said, out.said


@pytest.mark.parametrize("typed", ["y", "yes", "yes, approve it", "ok"])
def test_yes_typed_at_the_card_prompt_is_a_message_and_approves_nothing(engine, monkeypatch, home, typed):
    """Only a or approve approves: any other words at the card, "yes" among them, are the owner's next message."""
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE]), Reply("Noted.", [])],
               ["invoice Acme Corporation for 5 copper wire", typed, "d"])
    assert out.exit_code == 0, out.output
    assert invoices(engine) == n and not out.window.approvals


# --- questions ---------------------------------------------------------------------------------------------------

class Recording:
    """An API that records what the window posts and answers each message from a script of event lists."""

    def __init__(self, script: list[list[dict]]):
        self.script, self.sent = list(script), []

    def post(self, path, **kw):
        if path == "/v1/conversations":
            return httpx.Response(200, json={"id": "c1"})
        self.sent.append((path, kw.get("json")))
        return httpx.Response(200, json={"events": self.script.pop(0) if self.script else []})


FIELD = {"type": "ask", "text": "Which customer did you mean?", "code": "customer_ambiguous", "question_id": "q3",
         "kind": "field", "options": ["A&B", "AB"],
         "choices": [{"code": "q3-1", "label": "A&B", "control": False},
                     {"code": "q3-someone_else", "label": "Someone else", "control": True},
                     {"code": "q3-2", "label": "AB", "control": False},
                     {"code": "q3-cancel", "label": "Cancel", "control": True}]}
FREE = {"type": "ask", "text": "How much did Acme pay?", "code": "amount_missing", "question_id": "q4", "kind": "free",
        "options": [], "choices": [{"code": "q4-cancel", "label": "Cancel", "control": True}]}


def window(monkeypatch, script, typed):
    api = Recording(script)
    monkeypatch.setattr(main, "_client", lambda url: api)
    out = CliRunner().invoke(main.app, ["chat"], input="\n".join([*typed, "quit"]) + "\n")
    return out, api.sent


def test_a_field_question_numbers_its_choices_control_actions_last(monkeypatch, home):
    out, _ = window(monkeypatch, [[FIELD]], ["invoice ab"])
    assert out.exit_code == 0, out.output
    lines = [line.strip() for line in out.output.splitlines()]
    assert lines.index("1. A&B") < lines.index("2. AB") < lines.index("3. Someone else") < lines.index("4. Cancel")


@pytest.mark.parametrize("typed, sent", [
    ("2", {"text": "AB", "choice": "q3-2"}),
    (" 1 ", {"text": "A&B", "choice": "q3-1"}),
    ("3", {"text": "Someone else", "choice": "q3-someone_else"}),
    ("4", {"text": "Cancel", "choice": "q3-cancel"}),
    ("5", {"text": "5"}),  # no such choice: the owner's words, as typed
    ("0", {"text": "0"}),
    ("A&B", {"text": "A&B"}),  # a label is words: the engine matches it as displayed
    ("the second one", {"text": "the second one"}),
], ids=["second", "spaces_around_a_number", "a_control_action", "the_last_control", "past_the_end", "zero",
        "a_label_typed", "words"])
def test_a_number_alone_sends_that_choices_code_and_anything_else_is_text(monkeypatch, home, typed, sent):
    out, posted = window(monkeypatch, [[FIELD], []], ["invoice ab", typed])
    assert out.exit_code == 0, out.output
    assert posted[-1] == ("/v1/conversations/c1/messages", sent)


@pytest.mark.parametrize("kind", ["approval", "capability"])
def test_an_approval_or_a_capability_question_is_numbered_too(monkeypatch, home, kind):
    out, posted = window(monkeypatch, [[dict(FIELD, kind=kind)], []], ["invoice ab", "2"])
    assert out.exit_code == 0, out.output
    assert "1. A&B" in out.output and posted[-1][1] == {"text": "AB", "choice": "q3-2"}


def test_a_free_question_numbers_nothing_and_a_number_is_the_answer(monkeypatch, home):
    """At "How much did Acme pay?", 1 is the amount, never a choice; its control actions are named, not numbered."""
    out, posted = window(monkeypatch, [[FREE], []], ["Acme paid", "1"])
    assert out.exit_code == 0, out.output
    assert posted[-1] == ("/v1/conversations/c1/messages", {"text": "1"})
    assert "Cancel" in out.output and "1. Cancel" not in out.output


def test_a_question_answered_once_numbers_nothing_after_it(monkeypatch, home):
    """A number maps to the question just shown only: once answered (or after any other events), it is text."""
    out, posted = window(monkeypatch, [[FIELD], [{"type": "answer", "text": "Done."}], []], ["invoice ab", "2", "2"])
    assert out.exit_code == 0, out.output
    assert posted[-1][1] == {"text": "2"}


def test_a_client_ignores_an_event_type_and_a_field_it_does_not_know(monkeypatch, home):
    out, _ = window(monkeypatch, [[{"type": "hologram", "text": "?"}, dict(FIELD, shape="new")]], ["hello"])
    assert out.exit_code == 0, out.output
    assert "1. A&B" in out.output


# --- the journal -------------------------------------------------------------------------------------------------

def test_an_approval_is_published_whole_locked_and_private_before_it_is_sent(engine, monkeypatch, home):
    """Seen from inside the send: a complete <approval id>.json entry, mode 0600 in a 0700 folder, locked by this
    window, and no temporary file beside it."""
    api, cid, card = a_card(engine)
    seen = {}

    class Watching(Dropping):
        def post(self, path, **kw):
            if path.endswith("/approve"):
                folder = home / "approvals"
                seen["names"] = sorted(p.name for p in folder.iterdir())
                entry = folder / f"{card['approval_id']}.json"
                seen["entry"] = json.loads(entry.read_text())
                seen["modes"] = (stat.S_IMODE(folder.stat().st_mode), stat.S_IMODE(entry.stat().st_mode))
                with open(entry) as other:
                    try:
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        seen["locked"] = False
                    except BlockingIOError:
                        seen["locked"] = True
            return super().post(path, **kw)

    w = Watching(api, [])
    with journal.Journal(journal.folder(), books_key()) as j:
        main.approve(w, j, cid, card)
    assert seen["names"] == [f"{card['approval_id']}.json"]
    assert {k: seen["entry"][k] for k in ("approval_id", "digest", "conversation_id")} == {
        "approval_id": card["approval_id"], "digest": card["digest"], "conversation_id": cid}
    assert seen["entry"]["books"] == books_key() and seen["entry"]["time"]
    assert seen["modes"] == (0o700, 0o600) and seen["locked"]
    assert entries(home) == []  # the engine answered: the entry is removed


def test_an_entry_is_locked_before_it_has_its_name(monkeypatch, home):
    """The lock is taken on the temporary file and held through the rename, so no other window can lock a published
    entry before its own window does (two windows can never both hold one entry)."""
    renamed = []
    real = os.link

    def watching(src, dst):
        with open(src) as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                renamed.append("unlocked")
            except BlockingIOError:
                renamed.append("locked")
        return real(src, dst)

    monkeypatch.setattr(journal.os, "link", watching)
    with journal.Journal(journal.folder(), "b") as j:
        entry = j.publish("ap-1", "d" * 64, "c1")
        j.keep(entry)
    assert renamed == ["locked"]


@pytest.mark.parametrize("step", ["write", "fsync", "link", "folder_fsync", "lock"])
def test_a_journal_step_that_fails_sends_nothing_and_says_so(engine, monkeypatch, home, step):
    api, cid, card = a_card(engine)
    n = invoices(engine)
    broken = {"write": (journal.os, "write"), "fsync": (journal.os, "fsync"), "link": (journal.os, "link"),
              "folder_fsync": (journal, "_sync_folder"), "lock": (journal.fcntl, "flock")}[step]

    def fail(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(*broken, fail)
    w = Dropping(api, [])
    monkeypatch.setattr(main, "con", main.Console(record=True, width=200))
    with journal.Journal(journal.folder(), books_key()) as j:
        main.approve(w, j, cid, card)
    assert UNSAFE in main.con.export_text()
    assert not w.approvals and invoices(engine) == n
    monkeypatch.undo()
    assert [e for e in entries(home) if e.endswith(".json")] == []  # no complete entry was left to be sent later


def test_two_windows_approving_two_cards_at_once_each_keep_their_own_entry(engine, home):
    _, _, first = a_card(engine)
    _, _, second = a_card(engine)
    with journal.Journal(journal.folder(), "b") as one, journal.Journal(journal.folder(), "b") as two:
        a = one.publish(first["approval_id"], first["digest"], "c1")
        b = two.publish(second["approval_id"], second["digest"], "c2")
        assert sorted(entries(home)) == sorted([f"{first['approval_id']}.json", f"{second['approval_id']}.json"])
        assert one.recover() == [] and two.recover() == []  # each locked by its own window: neither takes the other's
        one.finish(a)
        assert entries(home) == [f"{second['approval_id']}.json"]
        two.finish(b)
    assert entries(home) == []


def test_a_second_window_starting_mid_publish_sends_nothing_and_removes_nothing_of_the_first(engine, monkeypatch, home):
    """The first window has written its temporary file but not renamed it, and holds the lock on a published entry:
    the second window's start sends neither and removes neither."""
    _, cid, card = a_card(engine)
    folder = journal.folder()
    with journal.Journal(folder, books_key()) as first:
        held = first.publish(card["approval_id"], card["digest"], cid)
        partial = folder / ".tmp-abcdefgh"
        partial.write_text('{"approval_id": "ap-half')
        n = invoices(engine)
        out = chat(monkeypatch, engine, [], [])
        assert out.exit_code == 0, out.output
        assert not out.window.approvals and invoices(engine) == n
        assert sorted(entries(home)) == sorted([".tmp-abcdefgh", f"{card['approval_id']}.json"])
        first.keep(held)


def test_a_temporary_file_is_never_sent_and_is_removed_only_when_a_day_old(engine, monkeypatch, home):
    """The window killed during the temporary write: nothing was sent, and the file is left until it is a day old."""
    folder = journal.folder()
    folder.mkdir(mode=0o700)
    young, old = folder / ".tmp-young", folder / ".tmp-oldold"
    for p in (young, old):
        p.write_text(json.dumps({"approval_id": "ap-x", "digest": "d" * 64, "conversation_id": "c", "books": books_key(),
                                 "time": "2026-11-01T10:00:00+00:00"}))
    day_ago = time.time() - 86400 - 60
    os.utime(old, (day_ago, day_ago))
    out = chat(monkeypatch, engine, [], [])
    assert out.exit_code == 0, out.output
    assert not out.window.approvals and entries(home) == [".tmp-young"]


def test_an_entry_published_but_never_sent_is_committed_once_at_the_next_start(engine, monkeypatch, home):
    """The window killed after publishing and before sending: the next chat sends it, the engine commits the pending
    card once, and the entry is removed."""
    _, cid, card = a_card(engine)
    n = invoices(engine)
    with journal.Journal(journal.folder(), books_key()) as j:
        j.keep(j.publish(card["approval_id"], card["digest"], cid))
    out = chat(monkeypatch, engine, [], [])
    assert out.exit_code == 0, out.output
    assert "wasn't confirmed. Checking it now" in out.said and "Saved and submitted invoice" in out.said
    assert invoices(engine) == n + 1 and len(out.window.approvals) == 1 and entries(home) == []


@pytest.mark.parametrize("removed", [False, True], ids=["after_sending", "after_the_reply_before_removal"])
def test_an_entry_whose_card_was_committed_is_read_back_at_the_next_start(engine, monkeypatch, home, removed):
    """The window killed after sending, or after the reply but before removing the entry: the next chat's re-send
    reads the committed result back, one posting, and removes the entry."""
    api, cid, card = a_card(engine)
    assert approvals.approve(engine, card["approval_id"], card["digest"], ownership_check=_ownership(engine)).status == "committed"
    n = invoices(engine)
    with journal.Journal(journal.folder(), books_key()) as j:
        j.keep(j.publish(card["approval_id"], card["digest"], cid))
    out = chat(monkeypatch, engine, [], [])
    assert out.exit_code == 0, out.output
    assert "INV-2026-" in out.said and invoices(engine) == n and entries(home) == []


@pytest.mark.parametrize("ended", ["expired", "superseded", "declined"])
def test_an_ended_card_re_sent_after_a_restart_returns_its_outcome_and_never_commits(engine, monkeypatch, home, ended):
    """A re-send never commits a card that is not pending; the window shows what the engine
    says of that card, the same words a repeat approval gets."""
    api, cid, card = a_card(engine)
    if ended == "declined":
        approvals.decline(engine, card["approval_id"], ownership_check=_ownership(engine))
    else:
        with engine.begin() as conn:
            conn.execute(update(m.approval).where(m.approval.c.id == card["approval_id"]).values(
                **({"expires_at": now() - timedelta(seconds=1)} if ended == "expired" else {"status": "superseded"})))
    n = invoices(engine)
    with journal.Journal(journal.folder(), books_key()) as j:
        j.keep(j.publish(card["approval_id"], card["digest"], cid))
    out = chat(monkeypatch, engine, [], [])
    assert out.exit_code == 0, out.output
    said = approvals.approve(engine, card["approval_id"], card["digest"], ownership_check=_ownership(engine))
    assert said.status == ended and said.text in out.said, (said, out.output)
    assert invoices(engine) == n and entries(home) == []


@pytest.mark.parametrize("url", ["https://owner:" + PW + "@books.example:8740/api", "http://tok3n" + "@127.0.0.1:8740"])
def test_the_books_an_entry_names_never_hold_a_password_or_userinfo_over_the_api(engine, monkeypatch, home, url):
    key = main._books_key(url)
    assert "s3cret-pw" not in key and "tok3n" not in key and "@" not in key, key
    assert key.startswith(url.split("://")[0] + "://") and key.endswith(url.split("@")[1]), key
    _, cid, card = a_card(engine)
    with journal.Journal(journal.folder(), key) as j:
        j.keep(j.publish(card["approval_id"], card["digest"], cid))
    stored = (home / "approvals" / f"{card['approval_id']}.json").read_text()
    assert "s3cret-pw" not in stored and "tok3n" not in stored, stored


def test_the_books_an_entry_names_never_hold_a_password_in_process(monkeypatch, home):
    monkeypatch.setenv("ERPCLAW_DATABASE_URL", "postgresql+psycopg://erpclaw_app:" + PW + "@db.example:5432/books")
    monkeypatch.delenv("ERPCLAW_MIGRATION_URL", raising=False)
    key = main._books_key(None)
    assert key.startswith("in-process ") and "db.example" in key and "books" in key, key
    assert "s3cret-pw" not in key, key


def test_an_entry_for_other_books_is_left_alone(engine, monkeypatch, home):
    _, cid, card = a_card(engine)
    with journal.Journal(journal.folder(), "https://elsewhere.example") as j:
        j.keep(j.publish(card["approval_id"], card["digest"], cid))
    n = invoices(engine)
    out = chat(monkeypatch, engine, [], [])
    assert out.exit_code == 0, out.output
    assert not out.window.approvals and invoices(engine) == n and entries(home) == [f"{card['approval_id']}.json"]


def test_an_entry_removed_by_its_window_after_the_listing_is_not_sent(engine, home):
    """Recovery locks each entry it listed, then checks it is still there: one its own window finished in between is
    gone, and is not another window's to send."""
    _, cid, card = a_card(engine)
    folder = journal.folder()
    with journal.Journal(folder, "b") as first, journal.Journal(folder, "b") as second:
        held = first.publish(card["approval_id"], card["digest"], cid)
        real = second._lockable
        second._lockable = lambda path: (first.finish(held), real(path))[1]  # finished while the second was listing
        assert second.recover() == []


# --- lost replies --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("drop", ["before", "after", "error"],
                         ids=["dropped_before_the_engine_got_it", "dropped_after_the_commit", "a_server_error"])
def test_a_lost_reply_is_checked_on_y_by_sending_the_same_approval_and_commits_once(engine, monkeypatch, home, drop):
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE])],
               ["invoice Acme Corporation for 5 copper wire", "a", "y"], drop=[drop])
    assert out.exit_code == 0, out.output
    assert LOST in out.said and "INV-2026-" in out.said
    sent = out.window.approvals
    assert len(sent) == 2 and sent[0] == sent[1]  # the same approval, the same digest
    assert invoices(engine) == n + 1 and entries(home) == []


def test_a_lost_reply_not_checked_keeps_its_entry_for_the_next_start(engine, monkeypatch, home):
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE])],
               ["invoice Acme Corporation for 5 copper wire", "a", "n"], drop=["before"])
    assert out.exit_code == 0, out.output
    assert LOST in out.said and len(out.window.approvals) == 1 and invoices(engine) == n
    kept = entries(home)
    assert len(kept) == 1 and kept[0].endswith(".json")
    out = chat(monkeypatch, engine, [], [])
    assert "wasn't confirmed. Checking it now" in out.said and invoices(engine) == n + 1 and entries(home) == []


def test_books_still_unreachable_keep_the_entry_and_say_so(engine, monkeypatch, home):
    n = invoices(engine)
    out = chat(monkeypatch, engine, [Reply("", [INVOICE])],
               ["invoice Acme Corporation for 5 copper wire", "a", "y"], drop=["before", "before"])
    assert out.exit_code == 0, out.output
    assert UNREACHABLE in out.said and invoices(engine) == n and len(entries(home)) == 1
    out = chat(monkeypatch, engine, [], [], drop=["before"])
    assert UNREACHABLE in out.said and len(entries(home)) == 1
    out = chat(monkeypatch, engine, [], [])
    assert invoices(engine) == n + 1 and entries(home) == []


def test_the_start_names_the_time_of_the_unconfirmed_approval(engine, monkeypatch, home):
    _, cid, card = a_card(engine)
    with journal.Journal(journal.folder(), books_key()) as j:
        entry = j.publish(card["approval_id"], card["digest"], cid)
        when = entry.data["time"]
        j.keep(entry)
    from datetime import datetime
    shown = datetime.fromisoformat(when).astimezone().strftime("%H:%M")
    out = chat(monkeypatch, engine, [], [])
    assert f"An approval from {shown} wasn't confirmed. Checking it now ..." in out.said


def test_a_question_returned_at_the_start_is_answered_in_the_cards_own_conversation(monkeypatch, home):
    """The re-send came back with the approval-time question: the owner's answer goes to the conversation the card
    belongs to, and completes it."""
    with journal.Journal(journal.folder(), main._books_key(None)) as j:
        j.keep(j.publish("ap-q", "d" * 64, "c-old"))
    api = Recording([[dict(FIELD, status="superseded")], []])
    monkeypatch.setattr(main, "_client", lambda url: api)
    out = CliRunner().invoke(main.app, ["chat"], input="2\nquit\n")
    assert out.exit_code == 0, out.output
    assert api.sent[0][0] == "/v1/cards/ap-q/approve"
    assert api.sent[-1] == ("/v1/conversations/c-old/messages", {"text": "AB", "choice": "q3-2"})


# --- the approval's own question, and an engine that fails --------------------------------------------------------

def payments(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(t.payment)).scalar_one()


def test_an_approval_time_question_is_shown_with_its_words_and_a_label_typed_gets_the_card(engine, monkeypatch, home):
    """Through the real API: a payment card, then a second open invoice for its customer; approving it (here, the
    re-send at the chat's start) asks which invoice. The question comes with its option words, unnumbered (the
    approval's own question carries no codes yet); the owner types a label, and the request goes on to a card."""
    from erpclaw_core.plugins import REGISTRY

    pay = Reply("", [call("record_payment", customer="Initech", amount="100", method="check")])
    _, cid, card = a_card(engine, (pay,), "Initech paid 100 by check")
    extra = approvals.propose(engine, REGISTRY.contracts["create_invoice"],
                              {"customer": "Initech", "items": [{"item": "copper wire", "quantity": 5}]})
    assert approvals.approve(engine, extra.approval_id, extra.digest).status == "committed"
    with journal.Journal(journal.folder(), books_key()) as j:
        j.keep(j.publish(card["approval_id"], card["digest"], cid))
    n = payments(engine)
    out = chat(monkeypatch, engine, [pay], ["oldest first", "a"])
    assert out.exit_code == 0, out.output
    assert "oldest first" in out.said and "1. oldest first" not in out.said, out.said
    assert payments(engine) == n + 1, out.said


def _in_process(monkeypatch, engine, replies):
    """The window's own in-process client, on the test's books and a scripted model."""
    import erpclaw_assistant.muse as muse
    import erpclaw_core.db as db

    monkeypatch.setattr(db, "make_engine", lambda url: engine)
    monkeypatch.setattr(main, "_books_url", lambda owner=False: "postgresql+psycopg://runtime@localhost/books")
    monkeypatch.setattr(muse, "model_from_env", lambda: ScriptedModel(list(replies)))


def test_an_engine_that_fails_during_the_approval_is_a_lost_reply_checked_on_y(engine, monkeypatch, home):
    """In process, an exception inside the engine is the server's error, never a traceback in the window: the
    approval is checked again on y and commits once."""
    _in_process(monkeypatch, engine, [Reply("", [INVOICE])])
    real, calls = approvals.approve, []

    def failing_once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the books went away")
        return real(*a, **k)

    monkeypatch.setattr(approvals, "approve", failing_once)
    n = invoices(engine)
    out = CliRunner().invoke(main.app, ["chat"], input="invoice Acme Corporation for 5 copper wire\na\ny\nquit\n")
    said = " ".join(out.output.split())
    assert out.exit_code == 0, out.output
    assert "Traceback" not in out.output and LOST in said, out.output
    assert invoices(engine) == n + 1 and len(calls) == 2 and entries(home) == []


def test_an_engine_that_fails_during_a_message_is_a_plain_reply(engine, monkeypatch, home):
    from erpclaw_assistant.graph import Assistant

    _in_process(monkeypatch, engine, [])
    monkeypatch.setattr(Assistant, "turn", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = CliRunner().invoke(main.app, ["chat"], input="hello\nquit\n")
    assert out.exit_code == 0, out.output
    assert "Traceback" not in out.output and main.NO_ANSWER in " ".join(out.output.split()), out.output


class Refusing(Recording):
    """An API answering messages with an error status after its script runs out."""

    def __init__(self, script, status, body):
        super().__init__(script)
        self.status, self.body = status, body

    def post(self, path, **kw):
        if path.endswith("/messages") and not self.script:
            self.sent.append((path, kw.get("json")))
            return httpx.Response(self.status, json=self.body)
        return super().post(path, **kw)


CARD = {"type": "card", "approval_id": "ap-1", "digest": "d" * 64,
        "card": {"title": "Create and submit a sales invoice", "lines": [["Customer", "Acme"]], "books": [],
                 "warnings": [], "footer": ""}}


@pytest.mark.parametrize("status, body", [(422, {"detail": [{"loc": ["body", "text"], "msg": "too long"}]}),
                                          (401, {"detail": "Not authenticated"}), (403, {"detail": "Forbidden"})])
@pytest.mark.parametrize("where", ["a_change_at_the_card", "a_message"])
def test_a_message_the_engine_refuses_gets_a_plain_reply_never_a_crash(monkeypatch, home, status, body, where):
    script = [[CARD]] if where == "a_change_at_the_card" else []
    api = Refusing(script, status, body)
    monkeypatch.setattr(main, "_client", lambda url: api)
    typed = ["invoice Acme", "make it 6"] if where == "a_change_at_the_card" else ["hello"]
    out = CliRunner().invoke(main.app, ["chat"], input="\n".join([*typed, "d", "quit"]) + "\n")
    said = " ".join(out.output.split())
    assert out.exit_code == 0, out.output
    assert "Traceback" not in out.output and "'loc'" not in said and "KeyError" not in out.output, out.output
    expected = body["detail"] if isinstance(body["detail"], str) else main.NO_ANSWER
    assert expected in said, said


def test_a_folder_sync_that_fails_after_the_entry_is_named_leaves_no_entry_to_send_later(engine, monkeypatch, home):
    """The entry got its name, then the folder sync failed: the name is taken back (the temporary name still holds the
    entry), so nothing is sent now and nothing at the next start either."""
    _, cid, card = a_card(engine)
    with monkeypatch.context() as failing:  # only the sync fails, and only here: the books stay as the fixtures set them
        failing.setattr(journal, "_sync_folder", lambda path: (_ for _ in ()).throw(OSError(5, "I/O error")))
        with journal.Journal(journal.folder(), books_key()) as j:
            with pytest.raises(journal.NotRecorded):
                j.publish(card["approval_id"], card["digest"], cid)
    assert [e for e in entries(home) if e.endswith(".json")] == [], entries(home)
    n = invoices(engine)
    out = chat(monkeypatch, engine, [], [])
    assert not out.window.approvals and invoices(engine) == n


def test_an_entry_whose_name_cannot_be_taken_back_is_sent_as_published(engine, monkeypatch, home):
    """The entry got its name, the folder sync failed, and the name cannot be taken back either: it is whole and this
    window holds its lock, so it stays published and is sent, never left to be told 'not sent' and sent later."""
    _, cid, card = a_card(engine)
    real_unlink = journal.os.unlink

    def unlink(path, *a, **k):
        if str(path).endswith(".json"):
            raise OSError(5, "I/O error")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(journal, "_sync_folder", lambda path: (_ for _ in ()).throw(OSError(5, "I/O error")))
    monkeypatch.setattr(journal.os, "unlink", unlink)
    with journal.Journal(journal.folder(), books_key()) as j:
        entry = j.publish(card["approval_id"], card["digest"], cid)
        assert entry.path.name == f"{card['approval_id']}.json" and entry.path.exists()
        j.keep(entry)



# --- one entry per approval, whatever the windows do ------------------------------------------

def test_an_approval_already_being_sent_by_another_window_is_not_sent_again(engine, monkeypatch, home):
    """Two windows approving the same card: the second finds the first's locked entry, sends nothing, and the first's
    entry is untouched (the same file, still locked)."""
    api, cid, card = a_card(engine)
    folder = journal.folder()
    with journal.Journal(folder, books_key()) as first:
        held = first.publish(card["approval_id"], card["digest"], cid)
        inode = os.stat(held.path).st_ino
        w = Dropping(api, [])
        monkeypatch.setattr(main, "con", main.Console(record=True, width=200))
        with journal.Journal(folder, books_key()) as second:
            assert main.approve(w, second, cid, card) is None
        assert not w.approvals and main.ALREADY in main.con.export_text()
        assert os.stat(held.path).st_ino == inode and entries(home) == [f"{card['approval_id']}.json"]
        first.keep(held)


def test_an_unconfirmed_entry_for_the_card_being_approved_again_is_taken_over_and_sent_once(engine, monkeypatch, home):
    """An entry left by an earlier window (unlocked) for the very card the owner approves again now: this window takes
    that entry over instead of replacing it, sends the approval once, and removes the entry."""
    api, cid, card = a_card(engine)
    with journal.Journal(journal.folder(), books_key()) as earlier:
        earlier.keep(earlier.publish(card["approval_id"], card["digest"], cid))
    n = invoices(engine)
    w = Dropping(api, [])
    with journal.Journal(journal.folder(), books_key()) as j:
        main.approve(w, j, cid, card)
    assert len(w.approvals) == 1 and invoices(engine) == n + 1 and entries(home) == []


def test_an_entry_gone_before_the_re_send_is_not_sent(engine, monkeypatch, home):
    """The window checks its entry is still there and still its own before every send: an entry removed under it (by
    anything) stops the re-send on y; the owner is told the first attempt still has an unknown outcome."""
    api, cid, card = a_card(engine)
    w = Dropping(api, ["before"])
    monkeypatch.setattr(main, "_ask", lambda text: (os.unlink(journal.folder() / f"{card['approval_id']}.json"), "y")[1])
    monkeypatch.setattr(main, "con", main.Console(record=True, width=200))
    with journal.Journal(journal.folder(), books_key()) as j:
        main.approve(w, j, cid, card)
    assert len(w.approvals) == 1, w.approvals  # the first send only
    shown = main.con.export_text()
    assert main.UNSAFE not in shown
    assert "may or may not have been saved" in shown


def _ownership(engine):
    from erpclaw_assistant import store
    from erpclaw_core import principals

    with engine.connect() as conn:
        owner = principals.owner(conn)["id"]
    return lambda cid: store.conversation_owner(cid) == owner
