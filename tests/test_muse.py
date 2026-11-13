"""Muse through the Responses API, offline: the real OpenAI SDK over a stubbed HTTP transport (no network, no key).

Each test fails when its rule is removed. A key-shaped value is built at test time, never written as a literal.
The tests that need books run on PostgreSQL only, the one database ERPClaw supports.
"""
from __future__ import annotations

import json
import logging
import secrets
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from erpclaw_api.app import create_app
from erpclaw_assistant import muse
from erpclaw_assistant.model import OpenAICompatModel
from erpclaw_assistant.muse import LocalModel, MuseConfigError, MuseModel, model_from_env, to_responses
from erpclaw_core import models as m
from erpclaw_core.common import Refusal
from erpclaw_sales import tables as t


def synthetic_key() -> str:
    return "".join(["LL", "M_"]) + str(secrets.randbelow(10**9)) + "_" + secrets.token_hex(16)


def resp(output: list[dict], status: str = "completed", reason: str | None = None, reasoning_tokens: int = 7) -> dict:
    return {"id": "resp_1", "object": "response", "created_at": 0, "model": "muse-spark-1.3", "status": status,
            "incomplete_details": {"reason": reason} if reason else None, "output": output,
            "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 11, "output_tokens": 13, "total_tokens": 24,
                      "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
                      "input_tokens_details": {"cached_tokens": 0}}}


def text(s: str) -> dict:
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": s, "annotations": []}]}


def fcall(call_id: str, name: str, args: str) -> dict:
    return {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id, "name": name, "arguments": args,
            "status": "completed"}


class FakeMuse:
    """Answers each request with the next scripted reply: a dict (JSON 200), an int (that status), or a callable."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[tuple[str, str, dict | None]] = []
        self.headers: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        self.headers.append(dict(request.headers))
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "muse-spark-1.3", "object": "model"}]})
        r = self.replies.pop(0) if self.replies else resp([text("I have nothing more to add.")])
        if callable(r):
            r = r(body)
        if isinstance(r, httpx.Response):
            return r
        if isinstance(r, tuple):
            status, headers = r
            return httpx.Response(status, headers=headers, json={"error": {"message": "x", "type": "t", "code": "c"}})
        if isinstance(r, int):
            return httpx.Response(r, json={"error": {"message": "x", "type": "t", "code": "c"}})
        return httpx.Response(200, json=r)

    def posts(self) -> list[dict]:
        return [b for meth, path, b in self.requests if meth == "POST"]


def make(fake: FakeMuse, sleeps: list | None = None, **kw) -> MuseModel:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    sleeps = [] if sleeps is None else sleeps
    return MuseModel(api_key=kw.pop("api_key", synthetic_key()), http_client=client, sleep=sleeps.append,
                     base_url="https://muse.test/v1", **kw)


TOOLS = [{"type": "function", "function": {"name": "create_invoice", "description": "Invoice a customer.",
                                            "parameters": {"type": "object", "properties": {"customer": {"type": "string"}},
                                                           "required": ["customer"]}}}]
CONVO = [{"role": "system", "content": "You are the bookkeeper."},
         {"role": "user", "content": "invoice Acme for 5 copper wire"},
         {"role": "assistant", "content": "Let me draft it.", "tool_calls": [
             {"id": "call_1", "type": "function", "function": {"name": "create_invoice", "arguments": "{\"customer\": \"Acme\"}"}}]},
         {"role": "tool", "tool_call_id": "call_1", "content": "not valid: items missing"}]


# --- request shape --------------------------------------------------------------------------------------------

def test_request_shape():
    fake = FakeMuse(resp([text("ok")]))
    make(fake).complete(CONVO, TOOLS)
    [b] = fake.posts()
    assert b["instructions"] == "You are the bookkeeper."
    assert b["input"] == [
        {"type": "message", "role": "user", "content": "invoice Acme for 5 copper wire"},
        {"type": "message", "role": "assistant", "phase": "commentary",
         "content": [{"type": "output_text", "text": "Let me draft it."}]},
        {"type": "function_call", "call_id": "call_1", "name": "create_invoice", "arguments": "{\"customer\": \"Acme\"}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "not valid: items missing"}]
    assert b["tools"] == [{"type": "function", "name": "create_invoice", "description": "Invoice a customer.",
                           "parameters": TOOLS[0]["function"]["parameters"], "strict": False}]
    assert b["store"] is False and b["parallel_tool_calls"] is False
    assert b["reasoning"] == {"effort": "medium"} and b["max_output_tokens"] == 8192 and b["model"] == "muse-spark-1.3"
    for absent in ("temperature", "top_p", "tool_choice", "previous_response_id", "include"):
        assert absent not in b


def test_final_answer_phase():
    _, items = to_responses([{"role": "assistant", "content": "Done: INV-2026-00007."}])
    assert items == [{"type": "message", "role": "assistant", "phase": "final_answer",
                      "content": [{"type": "output_text", "text": "Done: INV-2026-00007."}]}]


def test_commentary_replay_is_required():
    """Meta returns 400 for untagged text before a call; the stub does the same, so dropping the tag fails here."""
    def meta_rule(body):
        for i, it in enumerate(body["input"][:-1]):
            nxt = body["input"][i + 1]
            if it.get("role") == "assistant" and it.get("phase") != "commentary" and nxt["type"] == "function_call":
                return 400
        return resp([text("ok")])
    fake = FakeMuse(meta_rule)
    assert make(fake).complete(CONVO, TOOLS).text == "ok"


# --- reading the reply ---------------------------------------------------------------------------------------

def test_reply_text_only():
    r = make(FakeMuse(resp([text("Sales were good.")]))).complete(CONVO[:2], TOOLS)
    assert r.text == "Sales were good." and r.calls == [] and r.finish == "stop"


def test_reply_one_call_with_a_decimal():
    r = make(FakeMuse(resp([fcall("c9", "create_invoice", "{\"customer\": \"Acme\", \"rate\": 0.1234}")]))).complete(
        CONVO[:2], TOOLS)
    [c] = r.calls
    assert (c.id, c.name, c.error) == ("c9", "create_invoice", None)
    assert c.args["rate"] == Decimal("0.1234") and not isinstance(c.args["rate"], float)


def test_reply_broken_arguments_are_an_error_not_a_call():
    r = make(FakeMuse(resp([fcall("c9", "create_invoice", "{\"customer\": ")]))).complete(CONVO[:2], TOOLS)
    assert r.calls[0].args is None and r.calls[0].error


def test_reply_call_in_text_is_refused_and_counted():
    r = make(FakeMuse(resp([text("<tool_call>{\"name\": \"create_invoice\"}</tool_call>")]))).complete(CONVO[:2], TOOLS)
    assert r.calls == [] and r.text_calls == 1 and "tool_call" not in r.text


def test_reply_incomplete_is_length():
    r = make(FakeMuse(resp([text("partial")], status="incomplete", reason="max_output_tokens"))).complete(CONVO[:2], TOOLS)
    assert r.finish == "length"


def test_reply_reasoning_counted_never_kept_and_unknown_items_counted():
    fake = FakeMuse(resp([{"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "think"}]},
                          {"type": "web_search_call", "id": "ws_1", "status": "completed"}, text("ok")]))
    model = make(fake)
    r = model.complete(CONVO[:2], TOOLS)
    assert r.text == "ok" and r.reasoning_chars == len("think") and "think" not in r.text
    assert model.usage[-1]["other_items"] == 1 and model.usage[-1]["reasoning_tokens"] == 7


@pytest.mark.parametrize("envelope", [
    {"status": "failed", "error": {"code": "server_error", "message": "x"}},
    {"status": "cancelled"},
    {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
    {"status": "completed", "error": {"code": "server_error", "message": "x"}},
])
def test_a_response_that_is_not_an_answer_is_refused_and_nothing_in_it_runs(envelope):
    body = resp([fcall("c9", "create_invoice", json.dumps({"customer": "Acme Corporation"}))])
    body.update(envelope)
    with pytest.raises(Refusal, match="could not answer") as e:
        make(FakeMuse(body)).complete(CONVO[:2], TOOLS)
    assert e.value.code == "model_unavailable"


def test_a_failed_response_with_a_valid_call_makes_no_card(engine):
    body = resp([fcall("c9", "create_invoice",
                       json.dumps({"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 5}]}))])
    body.update({"status": "failed", "error": {"code": "server_error", "message": "x"}})
    c = TestClient(create_app(engine, make(FakeMuse(body)), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(m.approval)).scalar_one()
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Acme for 5 copper wire"}).json()["events"]
    assert ev == [{"type": "refused", "code": "model_unavailable", "currency": "USD",
                   "text": "The Muse model could not answer this request. Nothing was saved."}]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.approval)).scalar_one() == before


# --- refusals before any request ------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["muse-spark-1.3-contributor", "MUSE-SPARK-1.2-Contributor"])
def test_contributor_models_refused(bad):
    with pytest.raises(MuseConfigError, match="Contributor"):
        make(FakeMuse(), model=bad)
    assert make(FakeMuse(), model="muse-spark-1.3").name == "muse-spark-1.3"


@pytest.mark.parametrize("bad", ["max", "none", "extreme"])
def test_unknown_effort_refused(bad):
    with pytest.raises(MuseConfigError, match="effort"):
        make(FakeMuse(), effort=bad)
    assert make(FakeMuse(), effort="medium").effort == "medium"


def test_no_key_refused():
    with pytest.raises(MuseConfigError, match="erpclaw-v3-muse"):
        MuseModel(key_reader=lambda: None)


def test_bad_tool_name_refused_before_sending():
    fake = FakeMuse()
    bad = [{"type": "function", "function": {"name": "a.b.c", "parameters": {"type": "object"}}}]
    with pytest.raises(Refusal):
        make(fake).complete(CONVO[:2], bad)
    assert fake.requests == []


def test_unmatched_call_id_refused_before_sending():
    fake = FakeMuse()
    convo = CONVO[:2] + [{"role": "tool", "tool_call_id": "call_never_made", "content": "x"}]
    with pytest.raises(Refusal, match="^The assistant asked for something it can't do, so nothing was saved[.]$"):
        make(fake).complete(convo, TOOLS)
    assert fake.requests == []


# --- errors and retries ---------------------------------------------------------------------------------------

def test_401_is_not_retried():
    fake = FakeMuse(401)
    with pytest.raises(Refusal, match=r"refused the request \(401\)") as e:
        make(fake).complete(CONVO[:2], TOOLS)
    assert e.value.code == "model_refused" and len(fake.posts()) == 1


def test_429_waits_retry_after_then_retries_once():
    sleeps: list = []
    fake = FakeMuse((429, {"retry-after": "1"}), resp([text("ok")]))
    assert make(fake, sleeps).complete(CONVO[:2], TOOLS).text == "ok"
    assert len(fake.posts()) == 2 and sleeps and sleeps[0] >= 1


def test_retry_after_as_an_http_date_is_honoured_and_a_long_wait_is_not_retried():
    from email.utils import formatdate
    t0 = 1_800_000_000.0
    sleeps: list = []
    fake = FakeMuse((503, {"retry-after": formatdate(t0 + 2, usegmt=True)}), resp([text("ok")]))
    assert make(fake, sleeps, now=lambda: t0).complete(CONVO[:2], TOOLS).text == "ok"
    assert len(fake.posts()) == 2 and 1.9 <= sleeps[0] <= 2.1
    sleeps = []
    fake = FakeMuse((429, {"retry-after": formatdate(t0 + 60, usegmt=True)}), resp([text("ok")]))
    with pytest.raises(Refusal, match=r"^The model service is busy right now\.$") as e:
        make(fake, sleeps, now=lambda: t0).complete(CONVO[:2], TOOLS)
    assert e.value.code == "model_rate_limited"
    assert len(fake.posts()) == 1 and sleeps == []  # longer than the cap: no retry inside the owner's turn


def test_503_then_200_succeeds_and_503_twice_is_unavailable():
    fake = FakeMuse(503, resp([text("ok")]))
    assert make(fake).complete(CONVO[:2], TOOLS).text == "ok" and len(fake.posts()) == 2
    fake = FakeMuse(503, 503)
    with pytest.raises(Refusal, match="unavailable") as e:
        make(fake).complete(CONVO[:2], TOOLS)
    assert e.value.code == "model_unavailable" and len(fake.posts()) == 2


def test_timeout_counts_as_504_and_is_retried_once():
    def boom(_):
        raise httpx.ReadTimeout("slow")
    fake = FakeMuse(boom, boom)
    model = make(fake)
    with pytest.raises(Refusal, match="unavailable"):
        model.complete(CONVO[:2], TOOLS)
    assert model.usage[-1]["statuses"] == [504, 504]


def test_a_model_failure_saves_nothing(engine):
    model = make(FakeMuse(503, 503))
    c = TestClient(create_app(engine, model, embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    with engine.connect() as conn:
        before = (conn.execute(select(func.count()).select_from(t.invoice)).scalar_one(),
                  conn.execute(select(func.count()).select_from(m.approval)).scalar_one())
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Acme for 5 copper wire"}).json()["events"]
    assert ev == [{"type": "refused", "code": "model_unavailable", "currency": "USD",
                   "text": "The Muse model is unavailable right now. Nothing was saved."}]
    with engine.connect() as conn:
        after = (conn.execute(select(func.count()).select_from(t.invoice)).scalar_one(),
                 conn.execute(select(func.count()).select_from(m.approval)).scalar_one())
    assert after == before


# --- the key -----------------------------------------------------------------------------------------------

def test_key_from_keychain_never_in_argv_repr_errors_usage_or_logs(caplog):
    key = synthetic_key()
    seen: list[list[str]] = []

    class Out:
        returncode, stdout = 0, key + "\n"

    def run(argv, **kw):
        seen.append(argv)
        return Out()

    assert muse.read_key(run) == key or muse.os.environ.get("ERPCLAW_MUSE_API_KEY")
    assert seen and seen[0] == ["security", "find-generic-password", "-s", "erpclaw-v3-muse", "-w"]
    assert all(key not in a for a in seen[0])
    caplog.set_level(logging.DEBUG)
    fake = FakeMuse(401)
    model = make(fake, api_key=key)
    with pytest.raises(Refusal) as e:
        model.complete(CONVO[:2], TOOLS)
    assert fake.headers[-1]["authorization"] == "Bearer " + key  # sent to the server, nowhere else
    for where in (repr(model), str(e.value), e.value.message, json.dumps(model.usage), caplog.text,
                  json.dumps(model.describe())):
        assert key not in where


# --- the provider choice ----------------------------------------------------------------------------------

def test_provider_choice(monkeypatch):
    monkeypatch.delenv("ERPCLAW_MODEL_PROVIDER", raising=False)
    local = model_from_env()
    assert isinstance(local, LocalModel) and isinstance(local._inner, OpenAICompatModel)
    assert local.describe()["provider"] == "openai-compatible"
    monkeypatch.setenv("ERPCLAW_MODEL_PROVIDER", "muse")
    monkeypatch.setenv("ERPCLAW_MUSE_API_KEY", synthetic_key())
    assert isinstance(model_from_env(), MuseModel)
    monkeypatch.setenv("ERPCLAW_MODEL_PROVIDER", "anthropic")
    with pytest.raises(MuseConfigError, match="openai-compatible, muse"):
        model_from_env()


# --- the whole turn, through a replay -------------------------------------------------------------------------

def test_whole_turn_through_a_replay_then_approve(engine):
    """First reply: commentary and a call whose lines are not a list of lines, which the engine cannot read; the engine
    answers with the tool error; the second request must carry the commentary, the call and
    its output with the same call id."""
    def second(body):
        kinds = [(it["type"], it.get("phase"), it.get("call_id")) for it in body["input"]]
        assert ("message", "commentary", None) in kinds
        calls = [it for it in body["input"] if it["type"] == "function_call"]
        outs = [it for it in body["input"] if it["type"] == "function_call_output"]
        assert calls and outs and calls[-1]["call_id"] == outs[-1]["call_id"] == "call_a"
        assert kinds.index(("message", "commentary", None)) < kinds.index(("function_call", None, "call_a"))
        return resp([fcall("call_b", "create_invoice",
                           json.dumps({"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 5}]}))])
    fake = FakeMuse(resp([text("I'll draft the invoice."),
                          fcall("call_a", "create_invoice", json.dumps({
                              "customer": "Acme Corporation", "items": "5 copper wire"}))]),
                    second)
    c = TestClient(create_app(engine, make(fake), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(t.invoice)).scalar_one()
    ev = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Acme Corporation for 5 copper wire"}).json()["events"]
    assert [e["type"] for e in ev] == ["card"] and len(fake.posts()) == 2
    card = ev[0]
    assert ["Total", "140.00 USD"] in card["card"]["lines"]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.invoice)).scalar_one() == before  # nothing until approval
    ev2 = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    assert ev2[0]["type"] == "result" and ev2[0]["status"] == "committed"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.invoice)).scalar_one() == before + 1
        total = conn.execute(select(t.invoice.c.total).order_by(t.invoice.c.number.desc())).first()[0]
    assert Decimal(total) == Decimal("140.00")


# --- the eval path to the report -----------------------------------------------------------------------------

def test_eval_report_lines_name_muse_and_check_ready():
    """The eval runner's report lines and readiness check come from the provider (the runner itself builds its eval
    books, which is the runner's own business; this checks what the Muse provider gives it)."""
    from erpclaw_cli.evals import run

    model = make(FakeMuse())
    d = model.describe()
    assert run._model_line(d) + "k = 1" == ("Model `muse-spark-1.3` at `https://muse.test/v1`, reasoning effort medium, "
                                            "store false, k = 1")
    assert "Thinking" not in run._settings_sentence(d) and "medium" in run._settings_sentence(d)
    assert model.check_ready() is None
    assert make(FakeMuse()).check_ready() is None
    down = MuseModel(api_key=synthetic_key(), base_url="https://muse.test/v1", sleep=lambda s: None,
                     http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))))
    assert down.check_ready() == "The Muse model refused the request (401)."


def test_eval_report_names_muse(monkeypatch, tmp_path):
    """The eval runner, end to end on its disposable PostgreSQL books, with Muse behind the stubbed transport."""
    import functools

    from conftest import PG_URL
    from erpclaw_cli.evals import run

    client = httpx.Client(transport=httpx.MockTransport(FakeMuse().handler))
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("ERPCLAW_ALLOW_OWNER_RUNTIME")  # the eval connects as a genuine runtime login
    monkeypatch.delenv("ERPCLAW_EVAL_PG_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_PG_TEST_URL", PG_URL)
    monkeypatch.setenv("ERPCLAW_MODEL_PROVIDER", "muse")
    monkeypatch.setenv("ERPCLAW_MUSE_API_KEY", synthetic_key())
    monkeypatch.setattr(muse, "MuseModel", functools.partial(MuseModel, http_client=client, sleep=lambda s: None))
    run.main(k=1, only=["R1"])
    report = (tmp_path / "eval-report.md").read_text()
    assert "Model `muse-spark-1.3` at `https://api.meta.ai/v1`, reasoning effort medium, store false, k = 1" in report
    assert "thinking requested" not in report


def test_an_asctime_retry_after_is_read_as_gmt_in_any_local_zone(monkeypatch):
    import time as _time
    from datetime import datetime, timezone as _tz
    t0 = 1_800_000_000.0
    stamp = datetime.fromtimestamp(t0 + 2, _tz.utc).strftime("%a %b %d %H:%M:%S %Y")  # asctime form, no zone
    for zone in ("UTC", "America/Chicago", "Asia/Tokyo"):
        monkeypatch.setenv("TZ", zone)
        _time.tzset()
        sleeps: list = []
        fake = FakeMuse((503, {"retry-after": stamp}), resp([text("ok")]))
        assert make(fake, sleeps, now=lambda: t0).complete(CONVO[:2], TOOLS).text == "ok", zone
        assert 1.9 <= sleeps[0] <= 2.1, (zone, sleeps)
    monkeypatch.delenv("TZ")
    _time.tzset()


def test_local_report_lines_are_todays():
    from erpclaw_cli.evals import run

    d = {"provider": "openai-compatible", "model": "qwen3.5-9b-mlx", "base_url": "http://127.0.0.1:1234/v1",
         "thinking": "off"}
    assert run._model_line(d) + "k = 3" == ("Model `qwen3.5-9b-mlx` at `http://127.0.0.1:1234/v1`, thinking requested off "
                                            "(see the reasoning count below), k = 3")
    assert run._settings_sentence(d) == (". Thinking was requested off; a non-zero reasoning count means the server "
                                         "did not honour 'off'.")
