"""The hosted guards: a hosted model meets only demo or eval books, and no credential leaves the machine.

Offline: the real OpenAI SDK over a stubbed HTTP transport. Every key-shaped value is built at test time, never
written as a literal. The books tests run on PostgreSQL only, the one database ERPClaw supports.
"""
from __future__ import annotations

import json
import os
import platform
import secrets
import sqlite3
import string
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from erpclaw_api.app import create_app
from erpclaw_assistant import hosted, profiles, store
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import OpenAICompatModel
from erpclaw_assistant.muse import MuseModel
from erpclaw_assistant.profiles import ProfileModel
from erpclaw_core import models as m
from erpclaw_core.common import Refusal
from erpclaw_core.db import DEMO_MARK, EVAL_MARK, mark_demo, owner
from test_model_profiles import MSGS, OPENROUTER, SERVED, FakeServer, completion, synthetic_key


def openrouter(fake: FakeServer) -> ProfileModel:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return ProfileModel(OpenAICompatModel(profile=profiles.load()[OPENROUTER], http_client=client,
                                          api_key=synthetic_key(), sleep=lambda s: None))


def muse(fake_handler) -> MuseModel:
    client = httpx.Client(transport=httpx.MockTransport(fake_handler))
    return MuseModel(api_key=synthetic_key(), http_client=client, sleep=lambda s: None,
                     base_url="https://muse.test/v1")


def unmark(engine, *marks: str) -> None:
    with engine.begin() as conn:
        conn.execute(owner.delete().where(owner.c.mark.in_(marks)))


def plain(engine):
    """The suite's books with neither mark: the demo seed marks them as demo books."""
    unmark(engine, DEMO_MARK, EVAL_MARK)
    return engine


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


# --- (a) a hosted model meets only demo or eval books ------------------------------------------------------------

def test_a_hosted_model_is_refused_on_books_with_no_demo_or_eval_mark(engine):
    fake = FakeServer()
    with pytest.raises(hosted.HostedRefused, match="only works with demo or eval books"):
        Assistant(plain(engine), openrouter(fake))
    assert fake.bodies == []


def test_muse_is_refused_on_books_with_no_demo_or_eval_mark(engine):
    sent = []
    with pytest.raises(hosted.HostedRefused, match="only works with demo or eval books"):
        Assistant(plain(engine), muse(lambda r: sent.append(r) or httpx.Response(500)))
    assert sent == []


@pytest.mark.parametrize("mark", [DEMO_MARK, EVAL_MARK])
def test_a_hosted_model_runs_on_demo_or_eval_books(engine, mark):
    mark_demo(plain(engine), mark)
    fake = FakeServer(completion("Hello, how can I help?", metadata=SERVED))
    ev = Assistant(engine, openrouter(fake)).turn("c1", "hello")
    assert len(fake.bodies) == 1 and ev[-1]["type"] == "answer", ev


def test_a_mark_removed_while_the_process_runs_refuses_the_next_turn_and_sends_nothing(engine):
    mark_demo(plain(engine), EVAL_MARK)
    fake = FakeServer()
    a = Assistant(engine, openrouter(fake))
    unmark(engine, EVAL_MARK)
    cards = count(engine, m.approval)
    ev = a.turn("c1", "Invoice Acme Corporation for 5 copper wire")
    assert [(e["type"], e["code"]) for e in ev] == [("refused", "hosted_books_refused")], ev
    assert "nothing was sent and nothing was saved" in ev[0]["text"]
    assert fake.bodies == [] and count(engine, m.approval) == cards


def test_a_local_model_is_never_asked_for_a_mark(engine):
    fake = FakeServer(completion("Hello."))
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    local = OpenAICompatModel(profile=profiles.load()["qwen3_5_9b_lmstudio"], http_client=client)
    Assistant(plain(engine), local).turn("c1", "hello")  # unmarked books: a local model sends nothing out
    assert len(fake.bodies) == 1


@pytest.mark.parametrize("where", ["", "books", "a/b"])
def test_a_hosted_model_refuses_the_old_real_books_home(monkeypatch, where):
    real = Path.home() / ".openclaw" / "erpclaw"
    monkeypatch.setenv("ERPCLAW_HOME", str(real / where) if where else str(real))
    with pytest.raises(hosted.HostedRefused, match="real books"):
        openrouter(FakeServer())
    with pytest.raises(hosted.HostedRefused, match="real books"):
        muse(lambda r: httpx.Response(500))


# --- (b) no credential leaves the machine ------------------------------------------------------------------------

def _shapes() -> dict[str, str]:
    """One value of each shape, built here so no literal credential sits in the repository."""
    h = secrets.token_hex
    return {
        "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIE" + h(20),
        "openai_key": "s" + "k-" + "proj-" + h(24),
        "openrouter_key": "s" + "k-or-v1-" + h(32),
        "aws_access_key": "AK" + "IA" + "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16)),
        "github_token": "gh" + "p_" + h(18),
        "slack_token": "xo" + "xb-" + str(secrets.randbelow(10**11)) + "-" + h(12),
        "bearer_token": "Bea" + "rer " + h(12),
        "jwt": "ey" + "J" + h(10) + ".ey" + "J" + h(12) + "." + h(16),
        "database_url_password": "postgresql://books_owner:" + h(8) + "@db.example:5432/books",
        "password_assignment": "pass" + "word=" + h(6),
    }


def test_every_label_has_a_shape_and_every_shape_a_test():
    assert set(_shapes()) == {label for label, _ in hosted.CREDENTIAL_SHAPES}


@pytest.mark.parametrize("label", sorted(_shapes()))
def test_a_message_holding_a_credential_is_refused_before_sending_and_only_its_label_is_kept(label):
    value = _shapes()[label]
    fake = FakeServer()
    mdl = openrouter(fake)
    msgs = MSGS + [{"role": "user", "content": f"here it is: {value} thanks"}]
    with pytest.raises(Refusal, match="was not sent to the hosted model") as e:
        mdl.complete(msgs, [])
    assert e.value.code == "credential_refused" and fake.bodies == []
    assert mdl.usage[-1]["refused"] == label and value not in repr(mdl.usage)


def test_muse_scans_before_sending_too():
    sent = []
    mdl = muse(lambda r: sent.append(r) or httpx.Response(500))
    with pytest.raises(Refusal) as e:
        mdl.complete(MSGS + [{"role": "user", "content": "key " + _shapes()["openrouter_key"]}], [])
    assert e.value.code == "credential_refused" and sent == []


def _quoted() -> dict[str, str]:
    """A password in the forms people paste: quoted, with its key quoted, and as a JSON field, as text or inside a
    tool call's arguments; each also with an apostrophe early in the value, and a JSON field whose value holds an
    escaped quote."""
    v = secrets.token_hex(6)
    odd = secrets.token_hex(2)[:3] + "'" + secrets.token_hex(7)[:13]  # 17 characters, an apostrophe after the third
    word, key = "pass" + "word", "api_" + "key"
    return {"double_quoted": f'{word} = "{v}"',
            "json_field": json.dumps({word: v}),
            "single_quoted_key": f"{key}: '{v}'",
            "key_in_single_quotes": f"'{key}': '{v}'",  # the key itself quoted, outside any JSON object
            "key_in_double_quotes": f'"{word}": "{v}"',
            "double_quoted_apostrophe": f'{word} = "{odd}"',
            "json_field_apostrophe": json.dumps({word: odd}),
            "single_quoted_apostrophe": f"{key}: '{odd}'",
            "json_field_escaped_quote": json.dumps({word: odd[:1] + '"' + odd[1:]})}  # a quote after the first


def _with_tool_call(value: str) -> list[dict]:
    args = json.dumps({"customer": "Acme Corporation", "memo": value})
    return MSGS + [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "create_invoice", "arguments": args}}]},
                   {"role": "tool", "tool_call_id": "c1", "content": "done"}]


@pytest.mark.parametrize("form", sorted(_quoted()))
@pytest.mark.parametrize("where", ["text", "tool_call"])
def test_a_quoted_or_json_credential_is_refused_on_the_openrouter_route(form, where):
    value = _quoted()[form]
    fake = FakeServer()
    mdl = openrouter(fake)
    msgs = MSGS + [{"role": "user", "content": f"here: {value}"}] if where == "text" else _with_tool_call(value)
    with pytest.raises(Refusal) as e:
        mdl.complete(msgs, [])
    assert e.value.code == "credential_refused" and fake.bodies == []
    assert mdl.usage[-1]["refused"] == "password_assignment" and value not in repr(mdl.usage)


@pytest.mark.parametrize("form", sorted(_quoted()))
@pytest.mark.parametrize("where", ["text", "tool_call"])
def test_a_quoted_or_json_credential_is_refused_on_the_muse_route(form, where):
    value, sent = _quoted()[form], []
    mdl = muse(lambda r: sent.append(r) or httpx.Response(500))
    msgs = MSGS + [{"role": "user", "content": f"here: {value}"}] if where == "text" else _with_tool_call(value)
    with pytest.raises(Refusal) as e:
        mdl.complete(msgs, [])
    assert e.value.code == "credential_refused" and sent == []


def test_ordinary_invoice_text_is_not_refused():
    fake = FakeServer(completion(metadata=SERVED))
    text = ("Invoice Acme Corporation for 5 copper wire at 28.00 each, PO 4471, INV-2026-00007, "
            "send it to ap@acme.example; they asked for a password reset link last week.")
    openrouter(fake).complete(MSGS + [{"role": "user", "content": text}], [])
    assert len(fake.bodies) == 1


# --- the name rule: a credential's name, then ':' or '=', then any value, whatever its characters -----------------

PW = "pass" + "word"
NAMES = (PW, "passwd", "pwd", "secret", "api_key", "api-key", "apikey", "token", "access_key", "access-key",
         "accesskey", "authorization", "Authorization")
CONTROLS = ("secret santa", "token of thanks", PW + " reset link sent",  # no ':' or '=' after the name
            "the bearer of this invoice")


def _round_3() -> dict[str, str]:
    """Redaction cases with fresh characters: a single-quoted value with an early comma or space, a
    double-quoted one with an escaped quote, and a JSON memo holding an assignment."""
    h = secrets.token_hex(5)
    return {"single_quoted_early_comma": f"{PW} = '{h[:2]},{h[2:]}'",
            "single_quoted_early_space": f"{PW} = '{h[:2]} {h[2:]}'",
            "double_quoted_escaped_quote": f'{PW} = "{h[:2]}\\"{h[2:]}"',
            "json_memo_holding_an_assignment": json.dumps({"memo": f'{PW} = "{h}"'})}


def _forms(name: str, v: str) -> dict[str, str]:
    escaped = v.replace("\\", "\\\\").replace('"', '\\"')
    return {"single_quoted": f"{name} = '{v}'", "double_quoted": f'{name} = "{escaped}"', "bare": f"{name}={v}",
            "key_quoted": f"'{name}': '{v}'", "json": json.dumps({name: v})}


def _corpus() -> list[tuple[str, str]]:
    """Every ASCII punctuation character and the space, at a value's start, middle and end, in every form, the
    names taken in turn; a name written with a JSON escape; and other redaction cases."""
    out, i = [], 0
    for c in string.punctuation + " ":
        for at in ("start", "middle", "end"):
            h = secrets.token_hex(5)
            v = {"start": c + h, "middle": h[:5] + c + h[5:], "end": h + c}[at]
            for form, text in _forms(NAMES[i % len(NAMES)], v).items():
                out.append((f"{form} {at} {c!r} {NAMES[i % len(NAMES)]}", text))
            i += 1
    h = secrets.token_hex(5)  # a name written with a JSON escape reads as the name once decoded, in a value or a key
    escaped = [("json escaped name in a value", '{"memo": "pass\\u0077ord = ' + h + '"}'),
               ("json escaped name as a key", '{"pass\\u0077ord": "' + h + '"}')]
    return out + escaped + list(_round_3().items())


class _Hosted:
    hosted = True

    def __init__(self):
        self.usage = []


def _sent(mdl, text: str, where: str) -> bool:
    msgs = MSGS + [{"role": "user", "content": f"here: {text}"}] if where == "text" else _with_tool_call(text)
    try:
        mdl.complete(msgs, [])
    except Refusal as e:
        return e.code != "credential_refused"
    return True


@pytest.mark.parametrize("case", sorted(_round_3()))
@pytest.mark.parametrize("where", ["text", "tool_call"])
def test_the_third_review_s_cases_are_neither_sent_nor_stored_on_either_route(case, where):
    value, fake, sent = _round_3()[case], FakeServer(), []
    assert not _sent(openrouter(fake), value, where) and fake.bodies == []
    assert not _sent(muse(lambda r: sent.append(r) or httpx.Response(500)), value, where) and sent == []
    assert hosted.refused_input(_Hosted(), value)["code"] == "credential_refused"


def test_any_value_after_a_credential_s_name_is_refused_in_every_form_on_both_routes_sent_and_stored():
    fake, sent = FakeServer(), []
    routes = {"openrouter": openrouter(fake), "muse": muse(lambda r: sent.append(r) or httpx.Response(500))}
    missed = [f"{name} sent on {route} as {where}" for name, text in _corpus() for route, mdl in routes.items()
              for where in ("text", "tool_call") if _sent(mdl, text, where)]
    missed += [f"{name} stored" for name, text in _corpus() if not hosted.refused_input(_Hosted(), text)]
    assert missed == [] and fake.bodies == [] and sent == []
    assert all(u.get("refused") == "password_assignment" for u in routes["openrouter"].usage)


@pytest.mark.parametrize("text", CONTROLS)
def _bearer() -> dict[str, str]:
    """A bearer token as a header, alone, and an authorization value given by its name."""
    h = secrets.token_hex
    return {"header": "Authori" + "zation: Bea" + "rer " + h(16), "alone": "Bea" + "rer " + h(12),
            "named": "authori" + "zation=" + h(6)}


@pytest.mark.parametrize("case", sorted(_bearer()))
@pytest.mark.parametrize("where", ["text", "tool_call"])
def test_a_bearer_token_or_an_authorization_value_is_neither_sent_nor_stored_on_either_route(case, where):
    value, fake, sent = _bearer()[case], FakeServer(), []
    assert not _sent(openrouter(fake), value, where) and fake.bodies == []
    assert not _sent(muse(lambda r: sent.append(r) or httpx.Response(500)), value, where) and sent == []
    assert hosted.refused_input(_Hosted(), value)["code"] == "credential_refused"


@pytest.mark.parametrize("text", CONTROLS)
def test_a_credential_s_name_with_no_colon_or_equals_after_it_is_not_refused(text):
    fake = FakeServer(completion(metadata=SERVED))
    openrouter(fake).complete(MSGS + [{"role": "user", "content": text}], [])
    assert len(fake.bodies) == 1 and hosted.refused_input(_Hosted(), text) is None


def test_the_check_before_storage_and_the_check_before_sending_never_disagree():
    ordinary = ["Invoice Acme Corporation for 5 copper wire at 28.00 each, PO 4471, INV-2026-00007, "
                "send it to ap@acme.example", *CONTROLS]
    texts = [t for _, t in _corpus()] + ordinary + list(_shapes().values())
    disagree = [t[:40] for t in texts
                if (hosted.refused_input(_Hosted(), t) is None) != (hosted.scan([{"role": "user", "content": t}], [])
                                                                    is None)]
    assert disagree == []


# --- the check's cost: a text of braces is read in bounded time, never half-read, and never raises ----------------

BRACES = {"open_braces": "{" * 100_000, "mixed": "[{" * 50_000, "unclosed_objects": '{"a":' * 20_000,
          "open_brackets": "[" * 100_000, "deep_but_closed": "[" * 50_000 + "]" * 50_000}


@pytest.mark.parametrize("case", sorted(BRACES))
def test_a_long_text_of_braces_is_checked_in_near_linear_time_and_refused_never_half_read(case):
    from timeit import repeat

    def cpu_samples(value):
        # timeit pauses cyclic GC so earlier tests' garbage cannot skew the samples.
        return repeat(lambda: hosted.check(value), timer=time.process_time, repeat=5, number=1)

    text, fake = BRACES[case], FakeServer()
    # Keep both ends so the 10x shorter deep_but_closed input is still balanced.
    end = len(text) // 20
    short_samples = cpu_samples(text[:end] + text[-end:])
    long_samples = cpu_samples(text)
    short_cpu = sorted(short_samples)[len(short_samples) // 2]
    long_cpu = sorted(long_samples)[len(long_samples) // 2]
    label = hosted.check(text)
    # CPU time excludes runner scheduling delays; medians dampen measurement noise.
    assert long_cpu <= 20 * short_cpu, json.dumps({
        "case": case, "short_cpu": short_cpu, "long_cpu": long_cpu,
        "short_samples": short_samples, "long_samples": long_samples,
        "sys.version": sys.version, "platform": platform.platform(), "os.cpu_count()": os.cpu_count(),
        "json.decoder.c_scanstring": json.decoder.c_scanstring is not None,
        "json.scanner.c_make_scanner": json.scanner.c_make_scanner is not None,
        "process_time.resolution": time.get_clock_info("process_time").resolution,
        "process_time.implementation": time.get_clock_info("process_time").implementation,
    }, indent=2)
    assert label == "unreadable_json" and hosted.refused_input(_Hosted(), text)["code"] == "credential_refused"
    assert not _sent(openrouter(fake), text, "text") and fake.bodies == []


def test_the_braces_timing_failure_reports_every_sample_and_its_runtime(monkeypatch):
    import timeit

    short = [0.02, 0.01, 0.05, 0.03, 0.04]
    long = [0.8, 0.7, 1.1, 0.9, 1.0]
    batches = iter((short, long))
    monkeypatch.setattr(timeit, "repeat", lambda *args, **kwargs: next(batches))
    monkeypatch.setattr(hosted, "check", lambda value: "unreadable_json")
    with pytest.raises(AssertionError) as failure:
        test_a_long_text_of_braces_is_checked_in_near_linear_time_and_refused_never_half_read("open_brackets")
    # Decode the message even when pytest appends its rewritten comparison.
    diagnostics, _ = json.JSONDecoder().raw_decode(str(failure.value))
    assert diagnostics == {
        "case": "open_brackets", "short_cpu": 0.03, "long_cpu": 0.9,
        "short_samples": short, "long_samples": long,
        "sys.version": sys.version, "platform": platform.platform(), "os.cpu_count()": os.cpu_count(),
        "json.decoder.c_scanstring": json.decoder.c_scanstring is not None,
        "json.scanner.c_make_scanner": json.scanner.c_make_scanner is not None,
        "process_time.resolution": time.get_clock_info("process_time").resolution,
        "process_time.implementation": time.get_clock_info("process_time").implementation,
    }


@pytest.mark.parametrize("case", ["open_brackets", "deep_but_closed"])
@pytest.mark.parametrize("prefix", ["", 'an unmatched prose quote: " '])
def test_excessive_json_nesting_is_refused_before_using_the_decoder_s_native_stack(monkeypatch, case, prefix):
    attempts = []
    raw_decode = json.JSONDecoder.raw_decode

    def record(self, text, idx=0):
        attempts.append(idx)
        return raw_decode(self, text, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", record)
    assert hosted.check(prefix + BRACES[case]) == "unreadable_json"
    # A larger native stack must not turn one RecursionError into 65 full-depth parses.
    assert attempts == []


@pytest.mark.parametrize("scanner", ["c", "python"])
@pytest.mark.parametrize("opening, closing", [("[", "]"), ('{"item":', "}")])
def test_the_json_nesting_limit_allows_its_boundary_and_refuses_the_next_level(monkeypatch, scanner, opening, closing):
    if scanner == "c" and json.scanner.c_make_scanner is None:
        pytest.skip("JSON C accelerator unavailable")
    monkeypatch.setattr(json.scanner, "make_scanner",
                        json.scanner.c_make_scanner if scanner == "c" else json.scanner.py_make_scanner)
    text = opening * hosted.JSON_DEPTH + "0" + closing * hosted.JSON_DEPTH
    assert hosted.check(text) is None
    assert hosted.check(opening + text + closing) == "unreadable_json"


@pytest.mark.parametrize("scanner", ["c", "python"])
def test_the_json_depth_check_ignores_quoted_braces_and_escaped_quotes(monkeypatch, scanner):
    if scanner == "c" and json.scanner.c_make_scanner is None:
        pytest.skip("JSON C accelerator unavailable")
    monkeypatch.setattr(json.scanner, "make_scanner",
                        json.scanner.c_make_scanner if scanner == "c" else json.scanner.py_make_scanner)
    value = {"memo": '\\"' + "[" * (hosted.JSON_DEPTH + 1) + '\\\\"' + "}" * (hosted.JSON_DEPTH + 1)}
    # The outer JSON is shallow even though its string holds many braces. Its decoded
    # strings are checked separately by _texts and _fields.
    assert list(hosted._held_json(json.dumps(value))) == [value]
    assert hosted.check(json.dumps([[] for _ in range(hosted.JSON_DEPTH * 2)])) is None


def test_a_model_tool_call_the_check_cannot_read_ends_its_turn_cleanly_and_stores_nothing(engine):
    mark_demo(engine, EVAL_MARK)
    stray = "{" * 5000  # past the starts the check reads: unreadable, so the next request is refused
    call = {"id": "c1", "type": "function",
            "function": {"name": "find_capability", "arguments": json.dumps({"request": stray})}}
    fake = FakeServer(completion("", calls=[call], finish="tool_calls", metadata=SERVED),
                      completion("Hello, how can I help?", metadata=SERVED))
    mdl = openrouter(fake)
    c = TestClient(create_app(engine, mdl, embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "what can you do for my stock?"}).json()["events"]
    assert (ev[-1]["type"], ev[-1].get("code")) == ("refused", "credential_refused"), ev
    assert len(fake.bodies) == 1 and mdl.usage[-1]["refused"] == "unreadable_json"
    assert not _stored(stray)
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "hello"}).json()["events"]
    assert ev[-1]["type"] == "answer" and len(fake.bodies) == 2, ev  # the turn ended; its claim was released


def test_a_large_valid_result_and_a_few_stray_braces_are_read_quickly_and_not_refused():
    rows = json.dumps([{"invoice": f"INV-2026-{i:05d}", "customer": "Acme Corporation", "total": "28.00"}
                       for i in range(2000)])
    for text in (rows, "see the note {in braces} and the [draft] tag, then {another} one"):
        started = time.perf_counter()
        assert hosted.check(text) is None and time.perf_counter() - started < 1.0


def test_a_credential_in_a_chat_turn_is_refused_and_saves_nothing(engine):
    mark_demo(engine, EVAL_MARK)
    fake = FakeServer()
    cards = count(engine, m.approval)
    c = TestClient(create_app(engine, openrouter(fake), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    ev = c.post(f"/v1/conversations/{cid}/messages",
                json={"text": "use this key " + _shapes()["openrouter_key"]}).json()["events"]
    assert [e.get("code") for e in ev] == ["credential_refused"], ev
    assert fake.bodies == [] and count(engine, m.approval) == cards


def _stored(value: str) -> bool:
    """Whether the assistant's own store holds the value anywhere; a store not yet made holds nothing."""
    if not store.path().exists():
        return False
    with sqlite3.connect(store.path()) as conn:
        return any(value in line for line in conn.iterdump())


@pytest.mark.parametrize("label,value", [("openrouter_key", _shapes()["openrouter_key"]),
                                         ("password_assignment", _quoted()["double_quoted_apostrophe"]),
                                         ("password_assignment", _quoted()["json_field_escaped_quote"]),
                                         *[("password_assignment", v) for v in _round_3().values()]],
                         ids=["openrouter_key", "double_quoted_apostrophe", "json_field_escaped_quote",
                              "single_quoted_early_comma", "single_quoted_early_space",
                              "double_quoted_escaped_quote", "json_memo_holding_an_assignment"])
def test_a_credential_typed_in_chat_is_never_stored_and_the_conversation_goes_on(engine, label, value):
    mark_demo(engine, EVAL_MARK)
    fake = FakeServer(completion("Hello, how can I help?", metadata=SERVED))
    mdl = openrouter(fake)
    c = TestClient(create_app(engine, mdl, embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "use this: " + value}).json()["events"]
    assert [e.get("code") for e in ev] == ["credential_refused"], ev
    assert not _stored(value) and value not in repr(mdl.usage) and mdl.usage[-1]["refused"] == label
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "hello"}).json()["events"]
    assert ev[-1]["type"] == "answer" and len(fake.bodies) == 1, ev  # not busy, and not refused again
    assert store.path().exists() and not _stored(value)  # the store the next turn made holds nothing of it


def _turn_rows(cid: str) -> list:
    """What the store keeps of one conversation's turns, without its ids and times."""
    with sqlite3.connect(store.path()) as conn:
        rid = [r[0] for r in conn.execute(
            "SELECT id FROM request WHERE conversation_id = ? ORDER BY created_at", (cid,))]
        marks = {r: f"request-{i}" for i, r in enumerate(rid)}
        rows = [("request", marks[r], *conn.execute("SELECT state, ended_why, pending_question, pending_card FROM "
                                                    "request WHERE id = ?", (r,)).fetchone()) for r in rid]
        rows += [("message", marks.get(r), t, ch, a) for r, t, ch, a in conn.execute(
            "SELECT request_id, text, choice, answer FROM request_message WHERE conversation_id = ? ORDER BY id",
            (cid,))]
        rows += [("event", marks[r], e) for r, e in conn.execute(
            "SELECT request_id, event FROM request_event WHERE request_id IN (%s) ORDER BY id"
            % ",".join("?" * len(rid)), rid)]
    return rows


def test_naming_the_server_changes_no_turn_only_the_usage_record(engine):
    """The same turn with OpenRouter's server fields and with none, as LM Studio or Ollama reply: the events and the
    stored rows are the same, and only the usage record names the server."""
    mark_demo(engine, EVAL_MARK)
    seen = {}
    for name, extra in (("named", SERVED), ("unnamed", None)):
        fake = FakeServer(completion("Hello, how can I help?", metadata=extra))
        mdl = openrouter(fake)
        c = TestClient(create_app(engine, mdl, embedded_owner=True))
        cid = c.post("/v1/conversations").json()["id"]
        events = c.post(f"/v1/conversations/{cid}/messages", json={"text": "hello"}).json()["events"]
        seen[name] = (events, _turn_rows(cid), mdl.usage[-1]["served_by"])
    assert seen["named"][:2] == seen["unnamed"][:2]
    assert (seen["named"][2], seen["unnamed"][2]) == ("PrimeIntellect", None)


def test_a_refusal_from_the_hosted_model_finishes_its_turn_and_the_next_message_goes_on(engine):
    mark_demo(engine, EVAL_MARK)
    fake = FakeServer(404, completion("Hello, how can I help?", metadata=SERVED))
    c = TestClient(create_app(engine, openrouter(fake), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "hello"}).json()["events"]
    assert [(e["type"], e.get("code")) for e in ev] == [("refused", "model_refused")], ev
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "hello again"}).json()["events"]
    assert ev[-1]["type"] == "answer" and len(fake.bodies) == 2, ev  # the claim was released, not left to lapse
