"""The plain-text sweep, every turn path: with a scripted model, through the
API as the owner's window calls it, every owner-facing string of every event passes the checker.

The paths: a normal answer, a question, the refusals a scripted request reaches, a card and its approval and decline,
a model that returns garbage, an unknown tool, a tool call with bad arguments, and each provider failure (Muse over a
stubbed HTTP transport: 401, 429 with and without Retry-After, 500, a timeout, a connection error, a malformed body).
A path whose text breaks a rule fails with the text and the rule."""
from __future__ import annotations

import json
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from erpclaw_api.app import create_app
from erpclaw_assistant import store
from erpclaw_assistant.model import Reply, ScriptedModel, ToolCall, call
from erpclaw_assistant.muse import MuseModel
from erpclaw_core.plugins import REGISTRY, disable_plugin
from erpclaw_sales import tables as t
from owner_text import (DeferredOwnerText, ROOT, defer_owner_text, event_violations, owner_strings,
                        owner_text_violations, refusal_calls, refusal_location,
                        observe_refusal_codes, refusal_code_report)


def chat(engine, model, *messages: str, client_kw: dict | None = None) -> list[dict]:
    """The messages of one conversation, in order; every event they returned."""
    c = TestClient(create_app(engine, model, embedded_owner=True), **(client_kw or {}))
    cid = c.post("/v1/conversations").json()["id"]
    events = []
    for said in messages:
        r = c.post(f"/v1/conversations/{cid}/messages", json={"text": said})
        assert r.status_code == 200, (r.status_code, r.text)
        events += r.json()["events"]
    return events


def scripted(*replies: Reply) -> ScriptedModel:
    return ScriptedModel(list(replies))


def swept(events: list[dict], types: set[str] | None = None) -> None:
    """The path was reached (its event types) and every owner-facing string passes the checker."""
    assert events and all(owner_strings(e) for e in events), events  # every event shows the owner something
    if types is not None:
        assert types & {e["type"] for e in events}, events
    print("Checked owner-facing event codes:", sorted({e["code"] for e in events if e.get("code")}))
    bad = event_violations(events)
    assert not bad, "\n".join(bad)


def number(engine, status: str) -> str:
    with engine.connect() as conn:
        return conn.execute(select(t.invoice.c.number).where(t.invoice.c.status == status)
                            .order_by(t.invoice.c.number)).scalars().first()


INVOICE = {"customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 5}]}
PAY = {"customer": "Hooli", "amount": 100}


# --- answers, questions and refusals a scripted request reaches ---------------------------------------------------

TURNS = {  # name -> (the owner's words, the model's replies, the event types the path gives)
    "answer: a figure": ("who owes us money?", [Reply("", [call("get_metric", metric="receivables")])], {"answer"}),
    "answer: a list": ("list Acme Corporation's invoices",
                       [Reply("", [call("list_invoices", customer="Acme Corporation")])], {"answer"}),
    "answer: the model's words": ("hello", [Reply("Hello! I can help with your books.")], None),
    "answer: an invented figure replaced": ("how are we doing this month?",
                                            [Reply("Your MRR is 12,500.00 this month.")], {"answer"}),
    "answer: a planned figure": ("what's our MRR?", [Reply("Your MRR is 12,500.00 this month.")], {"answer"}),
    "answer: a claim of a change replaced": ("thanks", [Reply("Done! I've saved the invoice.")], None),
    "question: an unknown customer": ("invoice ABC for 3 circuit boards",
                                      [Reply("", [call("create_invoice", customer="ABC",
                                                       items=[{"item": "circuit board", "quantity": 3}])])], {"ask"}),
    "question: a customer not said": ("invoice for 5 copper wire",
                                      [Reply("", [call("create_invoice", customer="Acme Corporation",
                                                       items=[{"item": "copper wire", "quantity": 5}])])], {"ask"}),
    "question: a quantity not said": ("invoice Acme Corporation for copper wire",
                                      [Reply("", [call("create_invoice", **INVOICE)])], {"ask"}),
    "question: two changes at once": ("invoice Acme Corporation for 5 copper wire and Hooli for 2 circuit boards",
                                      [Reply("", [call("create_invoice", **INVOICE),
                                                  call("create_invoice", customer="Hooli",
                                                       items=[{"item": "circuit board", "quantity": 2}])])], {"ask"}),
    "question: a date the engine cannot read": ("Hooli paid 100 next week",
                                                [Reply("", [call("record_payment", **PAY)])], {"ask"}),
    "question: an unknown payment method": ("Hooli paid 100 via Zelle",
                                            [Reply("", [call("record_payment", **PAY, method="Zelle")])], None),
    "refusal: a payment in another currency": ("Hooli paid 100 EUR",
                                               [Reply("", [call("record_payment", **PAY)])], None),
    "refusal: sales tax": ("invoice Initech for 2 circuit boards and add 8% sales tax",
                           [Reply("", [call("create_invoice", customer="Initech",
                                            items=[{"item": "circuit board", "quantity": 2}])])], None),
    "refusal: an invoice that does not exist": ("cancel INV-2026-09999",
                                                [Reply("", [call("cancel_invoice", invoice="INV-2026-09999")])], None),
    "refusal: a payment that does not exist": ("reverse PAY-2026-09999",
                                               [Reply("", [call("reverse_payment", payment="PAY-2026-09999")])],
                                               None),
    "catalogue: nothing fits": ("fly me to the moon", [Reply("", [call("find_capability", request="fly me to the moon")]),
                                                       Reply("", [call("choose_capability", pick="none")])], None),
    "catalogue: unsure": ("do the thing", [Reply("", [call("find_capability", request="do the thing")]),
                                           Reply("", [call("choose_capability", pick="unsure")])], None),
    "catalogue: a planned area": ("Invoice Initech for 2 hours of consulting and add 8% sales tax.",
                                  [Reply("", [call("find_capability",
                                                   request="Invoice Initech for 2 hours of consulting and add 8% "
                                                           "sales tax.")]),
                                   Reply("", [call("choose_capability", pick="planned.tax_engine")])], None),
}


@pytest.mark.parametrize("name", list(TURNS))
def test_every_answer_question_and_refusal_reads_plainly(engine, name):
    said, replies, types = TURNS[name]
    swept(chat(engine, scripted(*replies), said), types)


def test_a_document_in_the_wrong_state_is_refused_plainly(engine):
    submitted, cancelled = number(engine, "submitted"), number(engine, "cancelled")
    assert submitted and cancelled
    swept(chat(engine, scripted(Reply("", [call("submit_invoice", invoice=submitted)])), f"submit {submitted}"))
    swept(chat(engine, scripted(Reply("", [call("cancel_invoice", invoice=cancelled)])), f"cancel {cancelled}"))


def test_a_switched_off_operation_is_refused_plainly(engine, as_released, monkeypatch):
    monkeypatch.setattr(REGISTRY.contracts["record_payment"], "planned", "Core")
    swept(chat(engine, scripted(Reply("", [call("record_payment", **PAY)])), "Hooli paid 100"), {"answer"})


def test_a_plugin_the_books_have_not_enabled_is_refused_plainly(engine):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    swept(chat(engine, scripted(Reply("", [call("create_invoice", **INVOICE)])),
               "invoice Acme Corporation for 5 copper wire"))


def test_a_busy_conversation_is_refused_plainly(engine):
    c = TestClient(create_app(engine, scripted(), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    who = c.get("/v1/me").json()
    assert store.claim(cid, "an earlier message", principal_id=who["principal_id"],
                       principal_name=who["name"]) is not None  # another turn holds the conversation
    events = c.post(f"/v1/conversations/{cid}/messages", json={"text": "hello"}).json()["events"]
    assert [e.get("code") for e in events] == ["busy"], events
    swept(events)


# --- a card, approved and declined --------------------------------------------------------------------------------

def test_a_card_its_approval_and_its_email_read_plainly(engine):
    c = TestClient(create_app(engine, scripted(Reply("", [call("create_invoice", customer="Wayne Enterprises", email=True,
                                                                 items=[{"item": "circuit board", "quantity": 2}])])),
                              embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    events = c.post(f"/v1/conversations/{cid}/messages",
                    json={"text": "invoice Wayne Enterprises for 2 circuit boards and email it"}).json()["events"]
    card = next(e for e in events if e["type"] == "card")
    swept(events, {"card"})
    done = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]},
                  params={"conversation_id": cid}).json()["events"]
    assert done[0]["status"] == "committed", done
    swept(done, {"result"})


def test_a_declined_card_reads_plainly(engine):
    c = TestClient(create_app(engine, scripted(Reply("", [call("create_invoice", **INVOICE)])), embedded_owner=True))
    cid = c.post("/v1/conversations").json()["id"]
    [card] = c.post(f"/v1/conversations/{cid}/messages",
                    json={"text": "invoice Acme Corporation for 5 copper wire"}).json()["events"]
    swept(c.post(f"/v1/cards/{card['approval_id']}/decline", params={"conversation_id": cid}).json()["events"],
          {"result"})


def test_a_card_set_aside_by_the_next_change_reads_plainly(engine):
    model = scripted(Reply("", [call("create_invoice", **INVOICE)]), Reply("", [call("record_payment", **PAY)]))
    swept(chat(engine, model, "invoice Acme Corporation for 5 copper wire", "Hooli paid 100"))


# --- a model that misbehaves --------------------------------------------------------------------------------------

def _text_calls() -> Reply:
    r = Reply("")
    r.text_calls = 1
    return r


GARBAGE = {  # name -> (the owner's words, the model's replies)
    "a call written as text": ("invoice Acme Corporation for 5 copper wire", [_text_calls()] * 5),
    "an empty reply": ("how are we doing?", [Reply("")]),
    "an unknown tool": ("invoice Acme Corporation for 5 copper wire", [Reply("", [call("drop_every_table")])] * 5),
    "bad arguments to a write": ("invoice Acme Corporation for 5 copper wire",
                                 [Reply("", [call("create_invoice", customer="Acme Corporation",
                                                  items="5 copper wire")])] * 5),
    "bad arguments to a read": ("who owes us money?", [Reply("", [call("get_metric", metric="happiness")])] * 5),
    "arguments that are not JSON": ("invoice Acme Corporation for 5 copper wire",
                                    [Reply("", [ToolCall("c1", "create_invoice", None, '{"customer": "Acme',
                                                         "the tool arguments were not complete JSON")])] * 5),
    "a record id in the model's words": ("what's new?", [Reply("Your record 3f2b8c1e-9a4d-4c2b-8e7f-1a2b3c4d5e6f "
                                                               "is ready.")]),
    "the model's words hold technical text": ("what's new?", [Reply("KeyError: customer_id is not in sales_invoice "
                                                                    "(see /home/erpclaw/app.py)")]),
}
GARBAGE_XFAIL = {
    "the model's words hold technical text":
        "awaiting the model text screen; "
        "KeyError: customer_id is not in sales_invoice (see /home/erpclaw/app.py); "
        "rules 1, 2, 3, 5 and 6: source location, exception, table, field and path",
}


@pytest.mark.parametrize("name", [pytest.param(n, marks=pytest.mark.xfail(strict=True, raises=DeferredOwnerText, reason=GARBAGE_XFAIL[n]))
                                  if n in GARBAGE_XFAIL else n for n in GARBAGE])
def test_a_model_that_misbehaves_never_puts_technical_text_in_front_of_the_owner(engine, name):
    said, replies = GARBAGE[name]
    events = chat(engine, scripted(*replies), said)
    if name in GARBAGE_XFAIL:
        defer_owner_text([text for event in events for text in owner_strings(event)], [
            ("KeyError: customer_id is not in sales_invoice (see /home/erpclaw/app.py)", [1, 2, 3, 5, 6])])
    swept(events)


# --- each provider failure (Muse, over a stubbed HTTP transport: no network, no key) ------------------------------

def _resp(output: list[dict], status: str = "completed") -> dict:
    return {"id": "resp_1", "object": "response", "created_at": 0, "model": "muse-spark-1.3", "status": status,
            "incomplete_details": None, "output": output, "parallel_tool_calls": False, "tool_choice": "auto",
            "tools": [], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                                   "output_tokens_details": {"reasoning_tokens": 0},
                                   "input_tokens_details": {"cached_tokens": 0}}}


def _error(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {},
                          json={"error": {"message": "internal detail 7f3a", "type": "server_error", "code": "x"}})


def _raise(exc):
    def handler(request):
        raise exc("internal detail 7f3a", request=request)
    return handler


PROVIDER = {  # name -> what the stubbed server does for each request
    "401": [lambda r: _error(401)],
    "429 with a long Retry-After": [lambda r: _error(429, {"retry-after": "3600"})],
    "429 without Retry-After": [lambda r: _error(429)] * 2,
    "500": [lambda r: _error(500)] * 2,
    "a timeout": [_raise(httpx.ReadTimeout)] * 2,
    "a connection error": [_raise(httpx.ConnectError)] * 2,
    "a failed response": [lambda r: httpx.Response(200, json={**_resp([]), "status": "failed",
                                                              "error": {"code": "server_error",
                                                                        "message": "internal detail 7f3a"}})],
}
PROVIDER_XFAIL = {  # filled from the first sweep: name -> the text and the rule it breaks
}


def muse(handlers) -> MuseModel:
    queue = list(handlers)

    def handler(request: httpx.Request) -> httpx.Response:
        h = queue.pop(0) if queue else (lambda r: httpx.Response(200, json=_resp([])))
        return h(request)

    key = "".join(["LL", "M_"]) + str(secrets.randbelow(10**9)) + "_" + secrets.token_hex(16)
    return MuseModel(api_key=key, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                     sleep=lambda s: None, base_url="https://muse.test/v1")


@pytest.mark.parametrize("name", [pytest.param(n, marks=pytest.mark.xfail(strict=True, raises=DeferredOwnerText, reason=PROVIDER_XFAIL[n]))
                                  if n in PROVIDER_XFAIL else n for n in PROVIDER])
def test_each_provider_failure_reads_plainly(engine, name):
    events = chat(engine, muse(PROVIDER[name]), "invoice Acme Corporation for 5 copper wire")
    assert all("7f3a" not in s for e in events for s in owner_strings(e)), events  # the planted marker stays inside
    swept(events, {"refused"})


def test_a_malformed_provider_body_never_shows_its_detail(engine):
    """A 200 whose body is not JSON: no handler expects it, so the route fails; nothing of it reaches the owner. The
    error body's own shape belongs to the route sweep, a later part."""
    model = muse([lambda r: httpx.Response(200, content=b"<html>internal detail 7f3a</html>")])
    c = TestClient(create_app(engine, model, embedded_owner=True), raise_server_exceptions=False)
    cid = c.post("/v1/conversations").json()["id"]
    r = c.post(f"/v1/conversations/{cid}/messages", json={"text": "invoice Acme Corporation for 5 copper wire"})
    shown = r.text if r.status_code >= 400 else json.dumps(r.json())
    assert "7f3a" not in shown, shown
    if r.status_code == 200:
        swept(r.json()["events"])


@pytest.mark.xfail(strict=True, raises=DeferredOwnerText, reason="awaiting plain words for the approval read-contract refusal; "
                   "get_metric is not a write with a declared read set.; rule 5: contract name")
def test_proposing_a_read_checks_the_actual_refusal_text(engine):
    from erpclaw_core import approvals
    from erpclaw_core.common import Refusal

    with pytest.raises(Refusal) as caught:
        approvals.propose(engine, REGISTRY.contracts["get_metric"], {"metric": "sales"})
    refusal = caught.value
    assert refusal.code == "not_a_write"
    defer_owner_text([refusal.message], [("get_metric is not a write with a declared read set.", [5])],
                     code=refusal.code)


# One runtime case per construction, including the helper that returns a refusal
# for ensure_enabled to raise. Lines make additions and removals fail closed.
PLUGIN_REFUSALS = {
    535: ("plugin_not_enabled", "The stock plugin is not enabled in these books.", ()),
    737: ("service_input", "Something went wrong inside your books' software, so nothing was changed.", ()),
    742: ("service_input", "Something went wrong inside your books' software, so nothing was changed.", ()),
    747: ("service_input", "Something went wrong inside your books' software, so nothing was changed.", ()),
    774: ("unknown_currency", "These books do not have the following currencies, so nothing was changed: XYZ.", ()),
    812: ("undeclared_service", "Something went wrong inside your books' software, so nothing was changed.", ()),
    814: ("plugin_not_enabled", "The stock plugin is not enabled in these books.", ()),
    818: ("service_outside_commit", "Something went wrong inside your books' software, so nothing was changed.", ()),
    829: ("service_output", "Something went wrong inside your books' software, so nothing was changed.", ()),
    832: ("service_output", "Something went wrong inside your books' software, so nothing was changed.", ()),
    851: ("plugin_installed", "The stock plugin is already installed in these books.", ()),
    854: ("plugin_dependency", "The stock plugin needs sales installed and enabled in these books first.", ()),
    860: ("message_connector_installed", "These books already send messages through email_folder; disable it first.", ()),
    864: ("plugin_core_revision", "The stock plugin needs these books at core revision 0015 or later; "
          "they are at no core revision.", ()),
    920: ("no_plugin", "This engine has no plugin called absent.", ()),
    939: ("plugin_not_enabled", "The stock plugin is not enabled in these books.", ()),
    942: ("plugin_has_dependents", "The stock plugin cannot be disabled while sales depends on it.", ()),
    956: ("plugin_not_installed", "The stock plugin is not installed in these books.", ()),
    959: ("plugin_has_dependents", "The stock plugin cannot be uninstalled while sales depends on it.", ()),
    965: ("plugin_has_postings", "The sales plugin cannot be uninstalled: posted documents in these books "
          "depend on it. It can be disabled instead.", ()),
}
PLUGIN_PATH = "packages/erpclaw-core/src/erpclaw_core/plugins.py"


def _plugin_parameters():
    for line, (code, _, _) in PLUGIN_REFUSALS.items():
        if line in (812, 818, 829):
            continue  # retained individually below
        yield pytest.param(line, id=f"{line}-{code}")


def test_every_plugin_refusal_site_has_a_runtime_case():
    sites = {node.lineno for path, node in refusal_calls() if path == ROOT / PLUGIN_PATH}
    assert sites == set(PLUGIN_REFUSALS), (sites - PLUGIN_REFUSALS.keys(), PLUGIN_REFUSALS.keys() - sites)
    assert all(set(rules) <= {2, 5} for _, _, rules in PLUGIN_REFUSALS.values())
    family = [(line, *case) for line, case in PLUGIN_REFUSALS.items() if case[2]]
    print(f"Closed plugin family: {len(family)} sites; {len(sites) - len(family)} passing sites")
    for line, code, text, rules in family:
        print(f"{PLUGIN_PATH}:{line} | {code} | {text} | rules {rules}")


def _reach_plugin_refusal(line, engine, monkeypatch):
    """Execute the original boundary; doubles change its inputs, never its refusal."""
    from dataclasses import replace
    from types import SimpleNamespace

    from erpclaw_core import migrate, plugins
    from erpclaw_core.common import company_ctx
    from erpclaw_core.contracts import Money

    with engine.connect() as conn:
        ctx = company_ctx(conn)
        if line == 535:
            monkeypatch.setattr(plugins, "enabled_plugins", lambda conn: set())
            return plugins.ensure_enabled(conn, "stock")
        if line == 737:
            return plugins.call_service(ctx, "assistant", "stock.list_warehouses", [])
        if line == 742:
            return plugins.call_service(ctx, "sales", "stock.preview_issue", {})
        if line == 747:
            # The real service model coerces this nested bool to int; the
            # boundary must refuse it before dispatching the stock read.
            return plugins.call_service(ctx, "sales", "stock.preview_return", {
                "voucher_type": "sales_invoice", "voucher": "INV-2026-00001",
                "posting_date": "2026-10-26", "original": {"voucher": "INV-2026-00001", "line": True},
                "qty": "1"})
        if line == 774:
            return plugins._known_currencies(ctx, Money(value="1", currency="XYZ"),
                                             "The input to stock.list_warehouses")
        if line == 812:
            return plugins.call_service(SimpleNamespace(extra={}), "sales", "stock.list_warehouses", {})
        if line == 814:
            monkeypatch.delitem(REGISTRY.services, "stock.list_warehouses")
            return plugins.call_service(SimpleNamespace(extra={}), "assistant", "stock.list_warehouses", {})
        if line == 818:
            def must_not_run(ctx, inp):
                pytest.fail("a write service ran without an approved commit")
            service = REGISTRY.services["stock.issue"]
            monkeypatch.setitem(REGISTRY.services, "stock.issue", replace(service, fn=must_not_run))
            return plugins.call_service(ctx, "sales", "stock.issue", {})
        if line in (829, 832):
            service = REGISTRY.services["stock.list_warehouses"]
            original = service.output_model.model_dump

            def dump(self, *args, **kwargs):
                if kwargs.get("mode") == "json":
                    if line == 829:
                        raise TypeError("".join(('erpclaw-planted-secret-', '0a1b2c3d')))
                    return {"warehouses": set()}
                return original(self, *args, **kwargs)

            monkeypatch.setattr(service.output_model, "model_dump", dump)
            monkeypatch.setitem(REGISTRY.services, "stock.list_warehouses",
                                replace(service, fn=lambda ctx, inp: {"warehouses": []}))
            return plugins.call_service(ctx, "assistant", "stock.list_warehouses", {})
        if line == 851:
            return plugins._install_checks(conn, REGISTRY.plugins["stock"])
        if line == 854:
            plugin = REGISTRY.plugins["stock"]
            plugin = replace(plugin, manifest=plugin.manifest.model_copy(update={"depends": {"sales": ">=0.0.1"}}))
            monkeypatch.setattr(plugins, "_install_rows", lambda conn: {})
            return plugins._install_checks(conn, plugin)
        if line == 860:
            monkeypatch.setattr(plugins, "_install_rows", lambda conn: {"email_folder": {"status": "installed"}})
            return plugins._install_checks(conn, REGISTRY.plugins["email"])
        if line == 864:
            monkeypatch.setattr(plugins, "_install_rows", lambda conn: {})
            monkeypatch.setattr(migrate, "applied_core_revision", lambda conn: None)
            return plugins._install_checks(conn, REGISTRY.plugins["stock"])
        if line == 920:
            return plugins.install_plugin(engine, "absent")
        if line in (939, 942, 956, 959):
            monkeypatch.setattr(plugins, "check_books", lambda conn: None)
            rows = {}
            if line in (942, 959):
                rows = {"stock": {"status": "installed"},
                        "sales": {"status": "installed" if line == 942 else "disabled"}}
                sales = REGISTRY.plugins["sales"]
                monkeypatch.setitem(REGISTRY.plugins, "sales", replace(
                    sales, manifest=sales.manifest.model_copy(update={"depends": {"stock": ">=0.0.1"}})))
            monkeypatch.setattr(plugins, "_install_rows", lambda conn: rows)
            action = plugins.disable_plugin if line in (939, 942) else plugins.uninstall_plugin
            return action(conn, "stock")
        if line == 965:
            # Demo sales invoices supply real postings; no posting lookup is doubled.
            return plugins.uninstall_plugin(conn, "sales")
    pytest.fail(f"no runtime route for plugin refusal {line}")


def _check_plugin_refusal(engine, monkeypatch, line):
    from erpclaw_core.common import Refusal

    code, text, _ = PLUGIN_REFUSALS[line]
    with pytest.raises(Refusal) as caught:
        _reach_plugin_refusal(line, engine, monkeypatch)
    refusal = caught.value
    where, raised_line = refusal_location(refusal)
    # ensure_enabled raises the refusal constructed by _not_enabled.
    assert (where, raised_line) == (PLUGIN_PATH, 543 if line == 535 else line)
    assert (refusal.code, refusal.message) == (code, text)
    assert "".join(('erpclaw-planted-secret-', '0a1b2c3d')) not in refusal.message
    bad = owner_text_violations(refusal.message, code=refusal.code)
    assert not bad, (where, raised_line, code, text, bad)


@pytest.mark.parametrize("line", list(_plugin_parameters()))
def test_every_plugin_refusal_checks_its_actual_owner_text(engine, monkeypatch, line):
    _check_plugin_refusal(engine, monkeypatch, line)


def test_a_service_serializer_failure_checks_the_actual_refusal_text(engine, monkeypatch):
    _check_plugin_refusal(engine, monkeypatch, 829)


def test_an_undeclared_service_checks_the_actual_refusal_at_its_raising_site(engine, monkeypatch):
    _check_plugin_refusal(engine, monkeypatch, 812)


def test_a_write_service_outside_commit_checks_the_actual_refusal_at_its_raising_site(engine, monkeypatch):
    _check_plugin_refusal(engine, monkeypatch, 818)


# Each file refusal is executed through its original function. The marker
# variants exercise every owner-text branch that can reach the same raise.
FILE_PATH = "packages/erpclaw-core/src/erpclaw_core/files.py"
FILE_DATABASE = "owner_text_books"
FILE_REFUSALS = {
    "home": (126, "files_home_missing", "ERPClaw's storage folder isn't set up yet, so the file could not be saved.", ()),
    "other-books": (225, "files_not_these_books", "The file could not be saved. Please ask for help restoring these books' files; nothing was changed.", ()),
    "missing-root": (225, "files_not_these_books", "The file could not be saved. Please ask for help restoring these books' files; nothing was changed.", ()),
    "missing-marker": (225, "files_not_these_books", "The file could not be saved. Please ask for help restoring these books' files; nothing was changed.", ()),
    "renamed": (225, "files_not_these_books", "The file could not be saved. Please ask for help restoring these books' files; nothing was changed.", ()),
    "render-mapping": (280, "render_inputs_shape", 'The file could not be saved. Please try making it again; nothing was changed.', ()),
    "render-key": (285, "render_inputs_shape", 'The file could not be saved. Please try making it again; nothing was changed.', ()),
    "render-value": (290, "render_inputs_shape", 'The file could not be saved. Please try making it again; nothing was changed.', ()),
    "render-nested": (290, "render_inputs_shape", 'The file could not be saved. Please try making it again; nothing was changed.', ()),
    "bytes": (301, "file_not_bytes", 'The file could not be saved. Please try making it again; nothing was changed.', ()),
    "type": (304, "file_type_refused", 'Only PDF files can be saved. Please choose a PDF file; nothing was changed.', ()),
    "size": (307, "file_size_refused", 'A stored file holds at least one byte and at most 10 MB.', ()),
    "document": (311, "file_document", 'The file could not be saved. Please choose the document it belongs to; nothing was changed.', ()),
    "save": (332, "file_not_saved", 'The file could not be saved safely.', ()),
    "row": (356, "file_not_found", 'No such stored file.', ()),
    "lost": (368, "file_altered", 'The stored file has been altered or lost.', ()),
    "altered": (371, "file_altered", 'The stored file has been altered or lost.', ()),
}


def test_file_refusal_expectations_are_fixed_owner_sentences():
    for _, code, text, rules in FILE_REFUSALS.values():
        assert not rules and not owner_text_violations(text, code=code)


def test_every_file_refusal_site_has_a_runtime_case():
    sites = {node.lineno for path, node in refusal_calls() if path == ROOT / FILE_PATH}
    assert sites == {entry[0] for entry in FILE_REFUSALS.values()}
    for case, (line, code, text, rules) in FILE_REFUSALS.items():
        print(f"{FILE_PATH}:{line} | {code} | {text} | rules {rules} | {case}")


def _reach_file_refusal(case, engine, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from erpclaw_core import files
    from erpclaw_core.common import company_ctx

    if case == "home":
        monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path / "absent" / "home"))
        return files._home_ready()
    if case.startswith("render-"):
        value = {"render-mapping": [], "render-key": {2: "title"},
                 "render-value": {"title": 2}, "render-nested": {"header": {"title": 2}}}[case]
        return files._render_inputs(value)
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        if case in ("other-books", "missing-root", "missing-marker", "renamed"):
            root = files._ensure_root(conn)
            marker = root / files.ROOT_MARKER
            doc = json.loads(marker.read_text())
            if case == "other-books":
                doc["books_id"] = "other-books"
            elif case == "renamed":
                doc["database"] = "previous_books"
            marker.write_text(json.dumps(doc))
            if case == "missing-marker":
                monkeypatch.setattr(files, "read_marker", lambda root: None)
            if case == "missing-root":
                monkeypatch.setattr(type(root), "is_dir", lambda self: False if self == root else True)
            return files._ensure_root(conn)
        if case == "row":
            return files.row(ctx, "absent")
        data, media, document = b"file", "application/pdf", ("sales_invoice", "invoice")
        if case == "bytes": data = "file"
        if case == "type": media = "text/plain"
        if case == "size": data = b""
        if case == "document": document = None
        if case == "save":
            def fail(*args): raise OSError("".join(('erpclaw-planted-secret-', '0a1b2c3d')))
            monkeypatch.setattr(files, "_write_durably", fail)
        if case in ("lost", "altered"):
            fid = files.put(ctx, data, media, document)
            row = files.row(ctx, fid)
            path = files.file_path(files.root_dir(conn), ctx.company_id, row["sha256"])
            if case == "lost": path.unlink()
            else: path.write_bytes(b"changed")
            return files.get(ctx, fid)
        return files.put(ctx, data, media, document)


@pytest.mark.parametrize("case", list(FILE_REFUSALS))
def test_every_file_refusal_checks_its_actual_owner_text(engine, monkeypatch, tmp_path, case):
    from erpclaw_core import files
    from erpclaw_core.common import Refusal
    from owner_text import violation_rules

    line, code, text, rules = FILE_REFUSALS[case]
    monkeypatch.setattr(files, "database_name", lambda conn: FILE_DATABASE)
    with pytest.raises(Refusal) as caught:
        _reach_file_refusal(case, engine, monkeypatch, tmp_path)
    refusal = caught.value
    assert refusal_location(refusal) == (FILE_PATH, line)
    assert (refusal.code, refusal.message) == (code, text)
    actual = violation_rules(refusal.message, code)
    assert actual == list(rules), (text, actual, rules)
    if actual:
        raise DeferredOwnerText((FILE_PATH, line, code, text, actual))


# Reuse the feature scenarios' original calls and recording boundaries. This
# inventory checks the production Refusals they construct as well as the
# feature's own assertions; it never fabricates a Refusal to claim coverage.
FEATURE_MODULES = (
    "test_ledger.py", "test_ledger_checks.py", "test_year_end.py",
    "test_integration_kernel.py", "test_journal_csv.py", "test_sales.py", "test_payment.py",
    "test_own_books.py", "unit/test_own_books_inputs.py", "unit/test_stock_onboarding.py",
    "test_outbox_core.py", "test_outbox_attachments.py", "test_users_session_cli.py",
    "test_card_bounds.py", "test_result_amount_kind.py", "test_hosted_guards.py",
    "test_turn_deadline.py", "test_date_source.py", "test_payment_resolve.py",
    "test_supplier_payments_unit.py", "test_supplier_payment_commit_unit.py",
    "test_supplier_owed_unit.py", "test_supplier_payment_reversal_unit.py",
    "unit/test_payable_items.py",
    "stock/test_stock_rules.py", "stock/test_stock_setup_unit.py",
    "stock/test_stock_authority.py", "stock/test_setup_names_unit.py",
    "stock/test_opening_stock_unit.py", "stock/test_write_off_unit.py",
    "stock/test_stock_counts_unit.py", "stock/test_cancellation_unit.py",
    "stock/test_goods_received_unit.py", "stock/test_reverse_goods_received_unit.py",
    "sales_stock/test_sales_stock_rules.py", "sales_stock/test_sales_stock_returns.py",
)


EXTRA_FEATURES = {
    "test_approval_row.py": {"test_a_fence_belongs_to_one_company"},
    "test_approval_set_aside.py": {"test_a_stale_token_is_refused_and_the_card_stays",
                                  "test_a_card_of_another_conversation_is_never_set_aside"},
    "test_reversal.py": {"test_payment_refusals_and_questions", "test_an_invoice_already_cancelled_is_refused"},
    "test_api_session.py": {"test_throttle_eleventh_failure_engine_wide_and_resets_at_minute_boundary",
                            "test_bearer_on_exchange_is_refused_before_spending_code",
                            "test_session_routes_are_host_only_with_the_frozen_404"},
    "test_outgoing_account.py": {"test_unmet_settlement_is_asked_before_account"},
    "test_turn_not_done_questions.py": {"test_a_date_a_rate_and_a_memo_are_asked_one_at_a_time_then_one_card"},
    "test_cash_account.py": {"test_fresh_books_refuse_cash_replace_helper_with_bank_lookup",
                             "test_two_live_cash_refuse_with_both_names_choose_first_live",
                             "test_existing_cash_refuses_proposal_remove_duplicate_plan"},
    "test_email_sender_settings.py": {"test_missing_sender_uses_existing_refusal",
                                      "test_direct_commit_requires_live_approval",
                                      "test_owner_only_even_with_all_existing_grants_and_forged_actor"},
    "test_invoice_cancel_credits.py": {"test_mixed_invoice_with_standing_credit_refuses_before_card"},
}


def _feature_module(filename):
    import importlib.util
    import sys

    name = "owner_inventory_" + filename.replace("/", "_").removesuffix(".py")
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        sys.path.insert(0, str((ROOT / "tests" / filename).parent))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
    return sys.modules[name]


def _feature_cases():
    import inspect
    from itertools import product

    out = {}
    for filename in (*FEATURE_MODULES, *EXTRA_FEATURES):
        module = _feature_module(filename)
        for name, fn in vars(module).items():
            if not name.startswith("test_") or not inspect.isfunction(fn) or fn.__module__ != module.__name__:
                continue
            if filename in EXTRA_FEATURES and name not in EXTRA_FEATURES[filename]:
                continue
            groups = []
            for mark in getattr(fn, "pytestmark", []):
                if mark.name == "parametrize":
                    names, values = mark.args[:2]
                    names = [n.strip() for n in names.split(",")] if isinstance(names, str) else names
                    rows = []
                    for value in values:
                        if hasattr(value, "marks") and hasattr(value, "values"): value = value.values
                        elif len(names) == 1: value = (value,)
                        rows.append(dict(zip(names, value)))
                    groups.append(rows)
            for index, combination in enumerate(product(*groups)):
                values = {k: v for row in combination for k, v in row.items()}
                case = f"{filename.removesuffix('.py')}/{name.removeprefix('test_')}/{index}"
                out[case] = (module, fn, values)
    return out


FEATURE_SELECTION = (
    'sales_stock/test_sales_stock_returns/a_credit_for_an_item_on_two_lines_asks_which_line/0',
    'sales_stock/test_sales_stock_rules/a_credit_beyond_what_the_invoice_still_owes_is_refused_no_refund/0',
    'sales_stock/test_sales_stock_rules/over_credit_is_refused_with_both_figures_and_the_limit_is_accepted/0',
    'stock/test_cancellation_unit/additional_exact_mirror_comparison_catches_wrong_counter/0',
    'stock/test_cancellation_unit/each_pair_must_be_latest/0',
    'stock/test_cancellation_unit/exact_company_owned_number_only/2',
    'stock/test_cancellation_unit/excluded_kinds_never_reach_generic_reverse/0',
    'stock/test_cancellation_unit/excluded_kinds_never_reach_generic_reverse/1',
    'stock/test_cancellation_unit/original_reversal_refusals_before_effects/1',
    'stock/test_cancellation_unit/original_reversal_refusals_before_effects/4',
    'stock/test_cancellation_unit/owner_date_and_actual_period_rules/0',
    'stock/test_cancellation_unit/owner_date_and_actual_period_rules/2',
    'stock/test_cancellation_unit/owner_date_and_actual_period_rules/3',
    'stock/test_cancellation_unit/real_ledger_preflight_refuses_before_stock_writes/1',
    'stock/test_cancellation_unit/real_ledger_preflight_refuses_before_stock_writes/2',
    'stock/test_cancellation_unit/real_ledger_preflight_refuses_before_stock_writes/3',
    'stock/test_cancellation_unit/valued_missing_posting_refuses_even_offsetting_net/1',
    'stock/test_goods_received_unit/bad_resolved_request_refuses_before_effects/2',
    'stock/test_goods_received_unit/company_record_roles_are_checked/0',
    'stock/test_goods_received_unit/company_record_roles_are_checked/3',
    'stock/test_goods_received_unit/company_record_roles_are_checked/4',
    'stock/test_goods_received_unit/company_record_roles_are_checked/5',
    'stock/test_goods_received_unit/date_and_complete_read_set/0',
    'stock/test_goods_received_unit/receipt_destination_defaults_ignore_existing_quantity/0',
    'stock/test_goods_received_unit/receipt_value_limits_are_typed/2',
    'stock/test_goods_received_unit/still_owed_cannot_also_choose_bank/0',
    'stock/test_opening_stock_unit/books_start_period_refusal_is_actionable/0',
    'stock/test_opening_stock_unit/duplicate_pairs_refused_even_after_name_normalisation/0',
    'stock/test_opening_stock_unit/ever_moved_pair_refused_even_with_no_stock_left/1',
    'stock/test_opening_stock_unit/oversized_opening_values_are_typed_refusals_before_writes/1',
    'stock/test_reverse_goods_received_unit/company_scoped_stored_fact_lookup/0',
    'stock/test_reverse_goods_received_unit/company_scoped_stored_fact_lookup/1',
    'stock/test_reverse_goods_received_unit/company_scoped_stored_fact_lookup/4',
    'stock/test_reverse_goods_received_unit/invalid_document_id/4',
    'stock/test_setup_names_unit/refusal_has_the_original_value_and_exact_public_shape/9',
    'stock/test_stock_counts_unit/all_unchanged_has_original_no_card_and_no_role_requirement/0',
    'stock/test_stock_counts_unit/duplicate_pair_after_name_resolution_has_original_sentence/0',
    'stock/test_stock_counts_unit/empty_pair_missing_rate_is_scoped_and_names_stored_unit/0',
    'stock/test_stock_counts_unit/gain_rounding_to_zero_is_refused/1',
    'stock/test_stock_counts_unit/negative_count_has_original_refusal/2',
    'stock/test_stock_counts_unit/oversized_gain_is_typed_before_writes/0',
    'stock/test_stock_counts_unit/registered_contract_stays_planned/0',
    'stock/test_stock_rules/rule_1_a_warehouse_of_another_company_is_refused_and_its_own_is_taken/0',
    'stock/test_stock_setup_unit/duplicate_warehouse_precedes_account_and_default_checks/0',
    'stock/test_stock_setup_unit/existing_stock_item_refuses_before_unknown_default/0',
    'stock/test_stock_setup_unit/existing_stock_item_refuses_before_unknown_default/1',
    'stock/test_stock_setup_unit/method_is_never_folded_or_defaulted_and_refuses_before_item_read/3',
    'stock/test_stock_setup_unit/second_default_refuses_after_account_validation/0',
    'stock/test_stock_setup_unit/stock1_movement_refusal_retains_warehouse_reference/0',
    'stock/test_write_off_unit/invalid_quantity_never_coerced/8',
    'stock/test_write_off_unit/near_names_never_silently_selected/2',
    'stock/test_write_off_unit/nonstock_item_refuses/0',
    'stock/test_write_off_unit/nowhere_held_asks_with_all_warehouses/0',
    'stock/test_write_off_unit/oversized_engine_cost_is_typed_before_writes/0',
    'stock/test_write_off_unit/zero_stock_cannot_be_written_off/0',
    'test_card_bounds/a_card_with_every_field_at_its_maximum_exceeds_the_whole_card_cap/0',
    'test_date_source/a_card_whose_operation_now_takes_no_date_is_refused/0',
    'test_date_source/a_date_read_from_the_owner_s_words_is_stated_with_its_phrase/0',
    'test_date_source/an_invoice_with_a_date_written_is_stated_as_its_default_beside_its_notice/0',
    'test_date_source/the_digest_covers_the_date_source_and_approval_reuses_the_stored_one/0',
    'test_hosted_guards/a_refusal_from_the_hosted_model_finishes_its_turn_and_the_next_message_goes_on/0',
    'test_hosted_guards/muse_scans_before_sending_too/0',
    'test_integration_kernel/accounting_mode_cannot_change_after_a_posting/0',
    'test_integration_kernel/an_unmapped_account_refuses_the_posting_and_writes_nothing/0',
    'test_integration_kernel/inbound_staging_dedupes_per_record_and_never_overwrites/0',
    'test_integration_kernel/payloads_refuse_binary_floats_anywhere/2',
    'test_journal_csv/bad_period_leaves_no_file/3',
    'test_journal_csv/company_selection_and_existing_destination/0',
    'test_journal_csv/t5_current_principal_and_both_grants/4',
    'test_ledger/03_each_check_refuses_and_writes_nothing/0',
    'test_ledger/03_each_check_refuses_and_writes_nothing/4',
    'test_ledger/03_each_check_refuses_and_writes_nothing/8',
    'test_ledger/08_reversal_mirrors_and_never_touches_the_original/0',
    'test_ledger_checks/check_post_refuses_a_missing_period_a_closing_voucher_and_a_duplicate_only_with_its_id/0',
    'test_outbox_attachments/a_refused_attachment_queues_nothing/0',
    'test_outbox_attachments/a_sha256_other_than_the_approved_one_is_refused_at_enqueue/0',
    'test_outbox_attachments/another_companys_file_id_is_refused_at_enqueue/0',
    'test_outbox_core/enqueue_refuses_a_header_injection_or_anything_but_one_plain_address/7',
    'test_outbox_core/enqueue_refuses_a_line_break_in_the_sender_snapshot/0',
    'test_outbox_core/send_again_refuses_a_message_that_was_accepted/0',
    'test_outbox_core/the_sender_comes_from_core_and_without_a_filler_only_emailing_is_refused/0',
    'test_own_books/a_missing_unit_or_price_is_asked_never_defaulted/0',
    'test_own_books/choosing_the_existing_customer_adds_no_customer_and_no_card/0',
    'test_own_books/own_books_over_books_that_already_hold_a_company_is_refused/0',
    'test_own_books/set_up_company_is_refused_on_the_demo_books/0',
    'test_own_books/while_pending_propose_refuses_every_operation_but_set_up_company_and_writes_nothing/4',
    'test_payment/a_named_invoice_that_is_not_open_is_refused_with_its_own_message/2',
    'test_payment/a_payment_dated_before_an_invoice_is_not_applied_to_it/0',
    'test_payment_resolve/a_payment_amount_is_read_whole_or_asked_never_in_part/0',
    'test_payment_resolve/an_unrecognised_phrase_after_a_cue_asks/3',
    'test_payment_resolve/coordinated_methods_ask_one_detail_at_a_time/2',
    'test_payment_resolve/each_payer_is_resolved_from_its_own_phrase_never_from_another_customer_in_the_request/0',
    'test_payment_resolve/every_reference_or_note_phrase_is_used_or_asked/1',
    'test_payment_resolve/oldest_first_needs_the_owner_s_words/0',
    'test_payment_resolve/the_invoice_the_owner_named_is_kept_whatever_the_model_passed/0',
    'test_sales/12_cancel_reverses_and_paid_invoice_refused/0',
    'test_supplier_owed_unit/duplicate_item_is_not_summed_twice/0',
    'test_supplier_owed_unit/exact_cap_is_complete_and_over_cap_has_no_partial_claim/0',
    'test_supplier_payment_commit_unit/missing_approval_refuses/0',
    'test_supplier_payment_commit_unit/real_core_row_and_digest_binding/8',
    'test_supplier_payment_commit_unit/replay_is_left_to_core_not_reexecuted/1',
    'test_supplier_payment_reversal_unit/inconsistent_payment_header_refuses/4',
    'test_supplier_payment_reversal_unit/reason_bound/1',
    'test_supplier_payments_unit/all_future_debt_precedes_above_total_refusal/0',
    'test_supplier_payments_unit/no_open_items_is_nothing_owed/0',
    'test_supplier_payments_unit/original_account_change_is_not_silently_remapped/0',
    'test_supplier_payments_unit/original_supplier_line_is_exact/5',
    'test_supplier_payments_unit/overpayment_refusal_order_uses_all_debt_before_eligible_debt/0',
    'test_supplier_payments_unit/overpayment_refusal_order_uses_all_debt_before_eligible_debt/1',
    'test_supplier_payments_unit/payment_date_is_exact_and_not_future/0',
    'test_supplier_payments_unit/payment_date_is_exact_and_not_future/2',
    'test_supplier_payments_unit/resolved_bank_identity_is_not_name_or_cross_company_fallback/3',
    'test_supplier_payments_unit/resolved_id_boundary/2',
    'test_supplier_payments_unit/reversed_source_is_never_payable/0',
    'test_supplier_payments_unit/source_lines_cannot_be_silently_truncated/0',
    'test_turn_deadline/money_name_correction_timeout_tracks_the_books_card_status/0',
    'test_turn_deadline/per_call_timeout_inside_budget_keeps_model_unavailable/1',
    'test_year_end/a_closing_entry_is_never_reversed/0',
    'test_year_end/a_second_close_is_refused/0',
    'test_year_end/an_opening_entry_carries_balance_sheet_balances_only/0',
    'test_year_end/an_opening_entry_is_dated_in_the_first_year/0',
    'test_year_end/close_needs_a_next_year/0',
    'test_year_end/close_needs_every_period_locked/0',
    'test_year_end/close_needs_exactly_one_proper_retained_earnings_account/2',
    'test_year_end/close_refuses_a_trial_balance_that_does_not_balance/0',
    'test_year_end/close_refuses_books_whose_chain_does_not_verify/0',
    'test_year_end/closed_is_terminal/0',
    'test_year_end/lock_through_locks_in_order_and_only_at_a_period_end/0',
    'test_year_end/lock_through_refuses_a_gap/0',
    'test_year_end/opening_entries_come_first_on_both_paths/0',
    'test_year_end/the_private_closing_path_checks_its_target/3',
    'test_year_end/years_close_in_order/0',
    'unit/test_payable_items/amount_domain_refuses_before_insert/9',
    'unit/test_payable_items/conditional_close_conflict_never_emits_success_audit/0',
    'unit/test_payable_items/other_sources_cannot_touch_payable_party_lines/2',
    'unit/test_payable_items/touched_paid_or_reversed_delivery_cannot_close/1',
    'unit/test_stock_onboarding/kind_change_does_not_silently_ignore_stock_only_choices/0',
    'unit/test_stock_onboarding/model_invented_choices_are_discarded_and_actual_owner_choices_bind_only_their_write/0',
    'unit/test_stock_onboarding/near_name_protection_still_precedes_stock_setup/0',
    'unit/test_stock_onboarding/near_name_protection_still_precedes_stock_setup/1',
    'unit/test_stock_onboarding/original_account_choice_fixture_binds_number_and_passes_company_scoped_id/0',
    'unit/test_stock_onboarding/price_stays_grounded_in_owner_digits_with_new_stock_choices/0',
    'unit/test_stock_onboarding/unknown_account_number_never_reaches_stock_writer/0',
)

_ALL_FEATURE_CASES = _feature_cases()
assert set(FEATURE_SELECTION) <= _ALL_FEATURE_CASES.keys(), "a selected refusal scenario disappeared"
FEATURE_CASES = {case: value for case, value in _ALL_FEATURE_CASES.items()
                 if case in FEATURE_SELECTION or case.split("/")[0] + ".py" in EXTRA_FEATURES}


def _run_feature_scenario(request, module, fn, values):
    import inspect
    from contextlib import ExitStack

    with ExitStack() as stack:
        cache = dict(values)

        def argument(name):
            if name in cache: return cache[name]
            fixture = getattr(module, name, None)
            if fixture is not None and hasattr(fixture, "__wrapped__") and hasattr(fixture, "_fixture_function"):
                fixture = fixture.__wrapped__
                result = fixture(**{key: argument(key) for key in inspect.signature(fixture).parameters})
                if inspect.isgenerator(result):
                    generator = result
                    result = next(generator)
                    def finish():
                        try: next(generator)
                        except StopIteration: return
                        pytest.fail("a scenario fixture yielded more than once")
                    stack.callback(finish)
            else:
                result = request.getfixturevalue(name)
            cache[name] = result
            return result

        for name, fixture in vars(module).items():
            marker = getattr(fixture, "_fixture_function_marker", None) if hasattr(fixture, "__wrapped__") else None
            if marker is not None and marker.autouse:
                argument(name)
        for mark in getattr(fn, "pytestmark", []):
            if mark.name == "usefixtures":
                for name in mark.args: argument(name)
        fn(**{key: argument(key) for key in inspect.signature(fn).parameters})


from owner_text import check_inventory, pending_mark


@pytest.mark.parametrize("case", [pytest.param(case, id=case, marks=pending_mark(case)) for case in FEATURE_CASES])
def test_remaining_refusals_check_actual_text_through_feature_scenarios(request, case):
    module, fn, values = FEATURE_CASES[case]
    _run_feature_scenario(request, module, fn, values)
    entries = list({(e["file"], e["line"], e["code"], e["text"]): e
                    for e in request.node.__dict__.get("owner_refusals", [])}.values())
    print("OWNER_INVENTORY " + json.dumps({"case": case, "entries": entries}))
    # Ruled families and earlier individually ruled failures are checked by
    # their dedicated runtime cases, with exact rules and strict xfails.
    from owner_text import violation_rules
    entries = [e for e in entries if not (
        e["file"] in (PLUGIN_PATH, FILE_PATH) and set(violation_rules(e["text"], e["code"])) <= {2, 5})
        and not (e["code"] == "not_a_write" and e["line"] == 205
                 and e["text"] == "get_metric is not a write with a declared read set.") ]
    check_inventory(entries, case)


class InventoryRows:
    """A read-only result double for boundaries that refuse before any write."""
    def __init__(self, rows=()): self.rows = list(rows)
    def execute(self, statement, *args):
        assert statement.is_select
        return self
    def mappings(self): return self
    def scalars(self): return self
    def all(self): return self.rows
    def first(self): return self.rows[0] if self.rows else None
    def scalar(self): return self.first()
    def scalar_one(self): return self.first()
    def __iter__(self): return iter(self.rows)


DIRECT_CODES = {
    "bad_permission", "principal_exists", "bad_session_end_reason", "bad_date_order",
    "no_owner", "no_principal", "company_ambiguous", "no_company", "no_area", "bad_mode",
    "no_account", "no_year", "model_input_invalid", "supplier_exists", "supplier_near_ambiguous",
    "setup_name_missing", "setup_currency_missing", "setup_fy_start_missing", "setup_date_order_missing",
    "amount_name", "answer_amounts", "card_amounts", "answer_interrupted", "no_conversation",
    "capability_mismatch", "date_source_invalid", "payable_needs_party", "receivable_needs_party",
    "address_domain", "no_fiscal_year", "no_message", "no_original_issue", "not_company_item", "not_stock_item",
    "lines_differ", "not_a_draft", "receivable_account", "item_price_missing", "warehouse_account_fixed", "issue_reversed",
    "credit_before_invoice", "invoice_line_unknown", "item_not_on_invoice", "item_not_on_order", "no_card", "not_found",
    "nothing_to_credit", "nothing_to_deliver", "nothing_to_invoice", "order_draft", "order_unknown", "over_delivery", "return_exceeds_issue",
}
from erpclaw_core.models import ACCOUNT_TYPES
ROLE_CODES = {f"{prefix}{role}{suffix}": (role, prefix == "several_")
              for role in ACCOUNT_TYPES for prefix, suffix in (("no_", "_account"), ("several_", "_accounts"))}
MASTER_CODES = {f"{what}_{ending}": (what, ending) for what in ("customer", "supplier", "item")
                for ending in ("missing", "unknown", "ambiguous")}
from owner_text import resolver_occurrence_kinds
from erpclaw_assistant import resolve as resolver
# Occurrence kind -> actual question code, not a naming convention.
# Date reaches resolve_date's ambiguous-date question; the other kinds use _unclaimed.
# Sites below are in packages/erpclaw-assistant/src/erpclaw_assistant/resolve.py:
# account calls _unclaimed at :3531; _unclaimed raises at :3332 for every kind
# below (item also has candidate questions at :3327 and :3330).
OCCURRENCE_CODES = {
    "account": "account_ambiguous",
    "customer": "customer_ambiguous",
    "date": "date_ambiguous",
    "document": "document_ambiguous",
    "draft": "draft_ambiguous",
    "email": "email_ambiguous",
    "figure": "figure_ambiguous",
    "item": "item_ambiguous",
    "memo": "memo_ambiguous",
    "method": "method_ambiguous",
    "number": "number_ambiguous",
    # The supplier payment contracts' rule: for pay_from/paid_from, two or more
    # distinct account phrases raise the one-detail question with account_ambiguous.
    # _unclaimed :3331 normalises paid_from to account before raising at :3332.
    # _outgoing_account :2701 passes account_ambiguous to _one_detail (:2596)
    # and raises it at :2704; its nested ask (:2651-2655) supplies the same
    # code for a missing account, raised at :2662 and :2709.
    "paid_from": "account_ambiguous",
    "rate": "rate_ambiguous",
    "supplier": "supplier_ambiguous",
    "warehouse": "warehouse_ambiguous",
}
# The incoming account reader, resolve_args :2990 and :2998-2999, also asks
# with account_ambiguous. So does bank_account at
# plugins/erpclaw-sales/src/erpclaw_sales/service.py:276.
assert set(OCCURRENCE_CODES) == resolver_occurrence_kinds(), "an occurrence kind needs an explicit code mapping"
DOCUMENT_CODES = {f"{mark['what']}_{ending}": (mark, name, ending)
                  for contract in REGISTRY.contracts.values() for name, mark in resolver.marks(contract.input_model).items()
                  if mark["kind"] == "document" for ending in ("missing", "ambiguous")}
DIRECT_CODES |= ROLE_CODES.keys() | MASTER_CODES.keys() | DOCUMENT_CODES.keys()
# Parameterise by occurrence as well as code so shared codes keep every kind.
BOUNDARY_CASES = ([(code, None) for code in sorted(DIRECT_CODES)]
                  + [(code, kind) for kind, code in sorted(OCCURRENCE_CODES.items())])
DIRECT_CODES |= set(OCCURRENCE_CODES.values())


def _reach_direct_refusal(code, monkeypatch, occurrence_kind=None):
    from datetime import date
    from types import SimpleNamespace
    from erpclaw_core import administration, common, core_contracts, integration, ledger, masters, principals
    from erpclaw_assistant import muse

    ctx = common.Ctx(InventoryRows(), "company", "Example Books", "USD", date(2026, 10, 26))
    if occurrence_kind == "date":
        return resolver.resolve_date("paid 3/4", ctx.today, resolver.Session(), "date")
    if occurrence_kind is not None:
        examples = {"item": "Copper Wire", "customer": "Acme", "supplier": "Copperline", "account": "Bank",
                    "document": "INV-2026-00001", "figure": "5", "number": "five",
                    "rate": "at 20", "memo": "memo freight", "email": "owner@example.test", "draft": "draft",
                    "warehouse": "Main Warehouse", "method": "cash", "paid_from": "Bank"}
        return resolver._unclaimed(resolver.Session(), occurrence_kind,
                                   resolver.Occurrence(occurrence_kind, 0, 1, examples[occurrence_kind]), [], [])
    if code == "not_found":
        from erpclaw_api import session
        dependencies = []
        def route(*args, **kwargs):
            dependencies.extend(kwargs.get("dependencies", []))
            return lambda fn: fn
        class EngineDouble: pass
        engine = EngineDouble()
        session.register_session(SimpleNamespace(post=route, delete=route), engine, run_id=None)
        return dependencies[0].dependency(SimpleNamespace(scope={}))
    if code == "no_card":
        from erpclaw_core import approvals
        return approvals._card(ctx.conn, "absent")
    if code == "return_exceeds_issue":
        from decimal import Decimal
        from erpclaw_stock import service
        monkeypatch.setattr(service, "_pair", lambda *args, **kwargs: object())
        monkeypatch.setattr(service, "returned_against", lambda *args: (Decimal(0), Decimal(0)))
        return service._return_move(ctx, {"reverses_entry_id": None, "id": "issue", "item_id": "item",
                                          "warehouse_id": "main", "qty": Decimal(1)}, ctx.today, "2", lock=False)
    if code in {"credit_before_invoice", "invoice_line_unknown", "item_not_on_invoice", "item_not_on_order",
                "nothing_to_credit", "nothing_to_deliver", "nothing_to_invoice", "order_draft", "order_unknown", "over_delivery"}:
        from datetime import timedelta
        from decimal import Decimal
        import erpclaw_sales
        from erpclaw_sales import stock_sales as sales
        if code == "order_unknown": return sales.find_order(ctx, "SO-2026-09999")
        if code == "order_draft":
            monkeypatch.setattr(erpclaw_sales, "find_customer", lambda *args: {"id": "customer"})
            monkeypatch.setattr(sales, "find_order", lambda *args: {"customer_id": "customer"})
            return erpclaw_sales._resolve_invoice(ctx, SimpleNamespace(customer="Acme", order="SO-2026-00001", submit=False))
        item = {"id": "item", "name": "Copper Wire"}
        other = {"id": "other", "name": "Consulting"}
        line = {"id": "line", "item_id": "item", "quantity": Decimal(1)}
        order = {"number": "SO-2026-00001"}
        state = SimpleNamespace(lines=[line], deliveries=[], invoiced_of={},
                                delivered={"line": Decimal(1) if code == "nothing_to_deliver" else Decimal(0)})
        monkeypatch.setattr(sales, "order_state", lambda *args: state)
        monkeypatch.setattr(sales, "items_by_id", lambda *args: {"item": item})
        if code == "nothing_to_invoice": return sales.plan_order_invoice(ctx, order)
        if code in ("item_not_on_order", "over_delivery", "nothing_to_deliver"):
            asked = None if code == "nothing_to_deliver" else [(other if code == "item_not_on_order" else item, Decimal(2), None)]
            return sales.plan_delivery(ctx, order, asked, ctx.today)
        inv = {"number": "INV-2026-00001", "status": "submitted", "posting_date": ctx.today}
        monkeypatch.setattr(sales, "invoice_lines_numbered", lambda *args: [(1, line)])
        monkeypatch.setattr(sales, "credited", lambda *args: {"line": (Decimal(1), Decimal(28))})
        if code == "credit_before_invoice": return sales.plan_credit(ctx, inv, None, ctx.today - timedelta(days=1))
        asked = None if code == "nothing_to_credit" else [(other if code == "item_not_on_invoice" else item, Decimal(1), 2)]
        return sales.plan_credit(ctx, inv, asked, ctx.today)
    if code in {"no_fiscal_year", "no_original_issue", "not_company_item", "not_stock_item", "issue_reversed"}:
        from erpclaw_stock import opening_stock, service
        if code == "no_fiscal_year": return opening_stock._books_start(ctx)
        if code == "no_original_issue": return service.original_issue(ctx, "INV-2026-00001", 1)
        if code == "not_company_item": return service.company_item(ctx, "absent")
        if code == "not_stock_item": return service.stock_item(ctx, "absent")
        return service._return_move(ctx, {"reverses_entry_id": "reversal"}, ctx.today, "1", lock=False)
    if code in ("address_domain", "no_message"):
        from erpclaw_core import outbox
        if code == "address_domain": return outbox._one_address("owner@" + "a" * 64 + ".example", "recipient")
        monkeypatch.setattr(outbox, "PLANNED", None)
        return outbox.send_again(ctx, "absent")
    if code in ("lines_differ", "not_a_draft", "receivable_account"):
        from decimal import Decimal
        from erpclaw_sales import service
        if code == "receivable_account": return service.receivable_account(ctx)
        monkeypatch.setattr(ledger, "open_period", lambda *args: {"status": "open"})
        return service.check_draft(ctx, {"posting_date": ctx.today, "company_id": ctx.company_id, "status": "draft" if code == "lines_differ" else "submitted",
                                         "number": "INV-2026-00001", "id": "invoice", "total": Decimal("10")})
    if code == "item_price_missing":
        return core_contracts._item_plan(ctx, core_contracts.AddItem(name="Consulting", kind="service", unit="hour"))
    if code == "warehouse_account_fixed":
        monkeypatch.setattr(core_contracts, "_stock_setup_call", lambda *args: {"warehouses": [{"warehouse": "main"}]})
        return core_contracts._item_stock_plan(ctx, core_contracts.AddItem(
            name="Copper Wire", kind="stock", valuation_method="fifo", inventory_account="1300"))
    if code in ("payable_needs_party", "receivable_needs_party"):

        role = code.removesuffix("_needs_party")
        ctx.conn = InventoryRows([{"is_group": False, "frozen": False, "account_type": role}])
        return ledger._check_lines(ctx, [ledger.Line("account"), ledger.Line("account")])
    if code in ("amount_name", "answer_amounts", "card_amounts"):
        from erpclaw_core import contracts
        if code == "amount_name":
            return contracts.check_amount_names(REGISTRY.contracts["get_metric"],
                                                [contracts.Amount("absent", None, "1.00", "USD")])
        if code == "answer_amounts":
            return contracts.check_answer(REGISTRY.contracts["get_metric"], contracts.Result(
                "No figures.", amounts=[contracts.Amount("value", None, "1.00", "USD")]))
        card = {"title": "Example", "footer": "", "lines": [], "books": [], "warnings": [],
                "amounts": [{"where": "lines", "index": 0, "label": "", "kind": "amount", "value": "1.00", "currency": "USD"}]}
        return contracts.check_card_amounts(card)
    if code == "answer_interrupted":
        from erpclaw_assistant.graph import _reply_dict
        return _reply_dict(Reply("Partial answer", finish="length"))
    if code == "no_conversation":
        return store.claim("absent", "hello", principal_id="owner", principal_name="owner", create=False)
    if code == "capability_mismatch":
        from erpclaw_core import catalogue
        monkeypatch.setattr(catalogue, "_synonym_score", lambda *args: 1)
        entry = catalogue.Entry("op.pay_supplier", "operation", "supplier payment", "", (), "installed",
                                overrides=("op.create_invoice",))
        monkeypatch.setattr(catalogue, "named_by_synonym", lambda *args: [(entry, 1)])
        return catalogue.planned_instead(None, "create_invoice", "pay the supplier")
    if code == "date_source_invalid":
        from erpclaw_core import approvals
        return approvals.check_date_source(ctx, REGISTRY.contracts["create_invoice"], SimpleNamespace(), {}, "")
    if code in ROLE_CODES:
        role, several = ROLE_CODES[code]
        if several: ctx.conn = InventoryRows([{"frozen": False}, {"frozen": False}])
        return ledger.account_by_type(ctx, role)
    if code in MASTER_CODES:
        what, ending = MASTER_CODES[code]
        rows = [{"name": "Acme"}, {"name": "Acme"}] if ending == "ambiguous" else []
        return masters._match(rows, "" if ending == "missing" else "ACME", what)
    if code in DOCUMENT_CODES:
        mark, name, ending = DOCUMENT_CODES[code]
        phrase = "" if ending == "missing" else f"{mark['prefix']}-2026-00001 or {mark['prefix']}-2026-00002"
        return resolver._document(ctx, mark, name, "required", {}, phrase, resolver.Session())
    if code == "bad_permission": return administration._check(["absent"])
    if code == "principal_exists": return administration.add_principal(InventoryRows([("owner",)]), "owner", [], [])
    if code == "bad_session_end_reason": return administration.end_sessions(None, "owner", "absent", "owner")
    if code == "bad_date_order": return administration.set_date_order(None, "company", "absent")
    if code == "no_owner": return principals.owner(ctx.conn)
    if code == "no_principal": return principals.by_id(ctx.conn, "absent")
    if code == "company_ambiguous": return common.company_ctx(InventoryRows([{}, {}]))
    if code == "no_company": return common.company_ctx(ctx.conn)
    if code == "no_area": return integration.system_of_record(ctx, "absent")
    if code == "bad_mode": return integration.set_system_of_record(ctx, "accounting", "absent")
    if code == "no_account": return ledger.account_by_number(ctx, "9999")
    if code == "no_year":
        monkeypatch.setattr(ledger, "_lock_head", lambda ctx: None)
        return ledger.close_year(ctx, "absent")
    if code == "model_input_invalid":
        tool = REGISTRY.contracts["create_invoice"].tool_schema()
        tool["function"]["name"] += ".extra.part"
        return muse._tool(tool)
    if code in ("supplier_exists", "supplier_near_ambiguous"):
        monkeypatch.setattr(core_contracts, "suppliers", lambda ctx: [{"name": "Acme", "city": "Chicago"}])
        name = "Acme" if code == "supplier_exists" else "Acmee"
        return core_contracts._supplier_plan(ctx, core_contracts.AddSupplier(name=name))
    if code.startswith("setup_"):
        ctx.conn = InventoryRows(["pending"])
        values = {"name": "Example Books", "currency": "USD", "fiscal_year_start_month": 1, "date_order": "MDY"}
        field = {"setup_name_missing": "name", "setup_currency_missing": "currency",
                 "setup_fy_start_missing": "fiscal_year_start_month", "setup_date_order_missing": "date_order"}[code]
        values[field] = None
        return core_contracts._setup_plan(ctx, core_contracts.SetUpCompany(**values))
    pytest.fail(f"no direct refusal route for {code}")


MODEL_INPUT_REFUSAL = {
    "text": "The assistant asked for something it can't do, so nothing was saved.",
    "rules": [],
    "file": "packages/erpclaw-assistant/src/erpclaw_assistant/muse.py",
    "line": 62,
    "code": "model_input_invalid",
    "case": "boundary/model_input_invalid",
}


def _boundary_marks(code):
    return pending_mark("boundary/" + code)


# Keep every occurrence as its own case even when kinds share a code or a
# master-record boundary already checks that code. All use the same text rules.
@pytest.mark.parametrize("code, occurrence_kind", [
    pytest.param(code, kind, id="boundary/" + code + ("/" + kind if kind else ""), marks=_boundary_marks(code))
    for code, kind in BOUNDARY_CASES
])
def test_refusal_boundaries_with_resolved_inputs_check_actual_owner_text(request, monkeypatch, code, occurrence_kind):
    from erpclaw_core.common import Refusal
    from erpclaw_api.app import Refused

    with pytest.raises((Refusal, Refused)) as caught:
        _reach_direct_refusal(code, monkeypatch, occurrence_kind)
    refusal = caught.value.refusal if isinstance(caught.value, Refused) else caught.value
    assert refusal.code == code
    entries = list({(e["file"], e["line"], e["code"], e["text"]): e
                    for e in request.node.__dict__["owner_refusals"]}.values())
    assert entries and entries[-1]["code"] == code
    if occurrence_kind == "date":
        from datetime import date

        with pytest.raises(Refusal) as date_question:
            resolver.resolve_date("paid 3/4", date(2026, 10, 26), resolver.Session(), "date")
        with pytest.raises(Refusal) as unclaimed_question:
            resolver._unclaimed(resolver.Session(), "date", resolver.Occurrence("date", 0, 1, "tomorrow"), [], [])
        assert refusal.message == date_question.value.message
        assert refusal.message != unclaimed_question.value.message
    if code == MODEL_INPUT_REFUSAL["code"]:
        assert [{k: v for k, v in entry.items() if k != "event"} for entry in entries] == [
            {k: v for k, v in MODEL_INPUT_REFUSAL.items() if k not in ("rules", "case")}]
    check_inventory(entries, "boundary/" + code)
