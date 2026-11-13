"""Every refusal of something ERPClaw does not offer names it by its catalogue id, on the event that leaves the engine.

The id is the event's `capability` field: planned.* for what is switched off until a release, unsupported.* for what
is never offered, available.* for a plugin these books have not installed. Graders read it instead of the wording.
A planned reply names the thing as the owner understands it, never in words that deny something switched on, and the
reply to a request ERPClaw does not do lists what is switched on in these books.
"""
from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from bookdiff import snapshot
from erpclaw_api.app import create_app
from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import approvals, catalogue as cat, common, contracts
from erpclaw_core.common import Refusal
from erpclaw_core.plugins import REGISTRY, disable_plugin, enabled_plugins

ID = re.compile(r"^(planned|unsupported|available)\.[a-z0-9_]+(\.[a-z0-9_]+)*$")  # the ids a refusal can carry

PAY = {"customer": "Pied Piper", "amount": "100", "oldest_first": True, "method": "check"}
PAYMENTS = ("record_payment", "cancel_invoice", "reverse_payment")


def switch_payments_off(monkeypatch) -> None:
    """Payments, cancelling and reversing switched off as they were before they came on, so the switched-off path is
    still tested on them; it fails rather than let a test pass with them on."""
    for name in PAYMENTS:
        monkeypatch.setattr(REGISTRY.contracts[name], "planned", "Core")
    assert all(REGISTRY.contracts[n].planned == "Core" for n in PAYMENTS)


@pytest.fixture
def payments_off(as_released, monkeypatch):
    """The release, with payments, cancelling and reversing switched off."""
    switch_payments_off(monkeypatch)


def client(engine, replies):
    c = TestClient(create_app(engine, ScriptedModel(list(replies)), embedded_owner=True))
    return c, c.post("/v1/conversations").json()["id"]


def chat(engine, replies, said):
    c, cid = client(engine, replies)
    events = c.post(f"/v1/conversations/{cid}/messages", json={"text": said}).json()["events"]
    for e in events:  # whatever the route, an event refusing something not offered names it
        if e.get("outcome") in ("b", "c", "d") or e.get("code") in ("planned", "unsupported", "available"):
            assert ID.match(e.get("capability") or ""), e
    return events


def through_catalogue(engine, said, pick):
    """The request searched with the owner's own words, then the pick: the search must offer it."""
    with engine.connect() as conn:
        found = [e.id for e in cat.find_capability(conn, said)]
    assert pick in found, found
    return chat(engine, [Reply("", [call("find_capability", request=said)]),
                         Reply("", [call("choose_capability", pick=pick)])], said)


def only(events):
    assert len(events) == 1, events
    return events[0]


# --- the routes, each with its id --------------------------------------------------------------------------------

@pytest.mark.parametrize("said, pick", [
    ("can you email INV-2026-00008 to finance@piedpiper.example", "planned.messaging"),
    ("Invoice Acme Corporation for 5 boxes of copy paper from stock at 12 dollars a box.", "planned.stock"),
    ("Invoice Initech for 2 hours of consulting and add 8% sales tax.", "planned.tax_engine"),
], ids=["emailing", "stock", "sales tax"])
def test_a_planned_area_chosen_through_the_catalogue_names_its_id(engine, as_released, said, pick):
    Assistant(engine, ScriptedModel([]))
    before = snapshot(engine)
    with engine.connect() as conn:
        entry = next(e for e in cat.catalogue(conn) if e.id == pick)
    expected = cat.decide([entry], pick)
    e = only(through_catalogue(engine, said, pick))
    assert e["outcome"] == "c" and e["capability"] == pick and "not in ERPClaw yet" in e["text"]
    assert e["text"] == expected.text
    assert snapshot(engine) == before


@pytest.mark.parametrize("said, pick, capability", [
    ("record a payment from Pied Piper", "op.record_payment", "planned.op.record_payment"),
    ("cancel invoice INV-2026-00001", "op.cancel_invoice", "planned.op.cancel_invoice"),
    ("who owes us money", "metric.receivables", "planned.reports"),
    ("what were our sales this year", "metric.sales", "planned.reports"),
])
def test_a_switched_off_operation_chosen_through_the_catalogue_names_its_id(engine, payments_off, said, pick,
                                                                            capability):
    e = only(through_catalogue(engine, said, pick))
    assert e["outcome"] == "c" and e["capability"] == capability


@pytest.mark.parametrize("tool, capability", [
    (call("record_payment", **PAY), "planned.op.record_payment"),
    (call("cancel_invoice", invoice="INV-2026-00001"), "planned.op.cancel_invoice"),
    (call("list_invoices"), "planned.op.list_invoices"),
    (call("get_metric", metric="sales", period="FY 2025"), "planned.reports"),
], ids=["payment", "cancel", "invoice list", "figures"])
def test_a_switched_off_operation_called_directly_names_its_id(engine, payments_off, tool, capability):
    e = only(chat(engine, [Reply("", [tool])], "do it"))
    assert e["outcome"] == "c" and e["capability"] == capability


def test_an_invoice_the_owner_asks_to_email_names_messaging(engine, as_released):
    # through chat the emailing is removed and said beside the card (the resolution step)
    events = common.check_events(chat(engine, [Reply("", [call("create_invoice", customer="Wayne Enterprises", items=[
        {"item": "circuit board", "quantity": 2}], email=True)])], "invoice Wayne Enterprises for 2 circuit boards and email it"))
    assert [e["type"] for e in events] == ["not_done", "card"], events
    assert events[0]["part"] == "email" and events[0]["capability"] == "planned.messaging"
    # an API caller asking to email is still refused whole
    with pytest.raises(Refusal) as r:
        approvals.propose(engine, REGISTRY.contracts["create_invoice"], {
            "customer": "Wayne Enterprises", "items": [{"item": "circuit board", "quantity": 2}], "email": True})
    assert r.value.code == "planned" and r.value.capability == "planned.messaging"


def test_no_card_is_made_for_a_switched_off_write_and_its_refusal_names_it(engine, payments_off):
    with pytest.raises(Refusal) as r:
        approvals.propose(engine, REGISTRY.contracts["record_payment"], PAY)
    assert r.value.code == "planned" and r.value.capability == "planned.op.record_payment"


def test_approving_a_card_whose_operation_was_switched_off_names_its_id(engine, monkeypatch):
    from conftest import RELEASED, _set_planned

    c, cid = client(engine, [Reply("", [call("record_payment", **PAY)])])
    [card] = c.post(f"/v1/conversations/{cid}/messages",
                    json={"text": "Pied Piper paid 100 by check, oldest first"}).json()["events"]
    _set_planned(monkeypatch, RELEASED)  # then the release as shipped, with payments switched off
    switch_payments_off(monkeypatch)
    events = c.post(f"/v1/cards/{card['approval_id']}/approve", json={"digest": card["digest"]}).json()["events"]
    e = only(events)
    assert e["status"] == "superseded" and e["code"] == "planned" and e["capability"] == "planned.op.record_payment"


def test_a_capability_in_a_plugin_these_books_have_not_installed_names_it_as_available(engine):
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    e = only(through_catalogue(engine, "invoice Acme for 5 copper wire", "op.create_invoice"))
    assert e["outcome"] == "c" and e["capability"] == "available.op.create_invoice"


def test_a_request_that_matches_nothing_names_no_match(engine, as_released):
    e = only(chat(engine, [Reply("", [call("find_capability", request="fly me to the moon")]),
                           Reply("", [call("choose_capability", pick="none")])], "fly me to the moon"))
    assert e["outcome"] == "d" and e["capability"] == "unsupported.no_match"


H035 = "Change the posted invoice INV-2026-00007 from 2 training days to 3. Leave it posted and just edit it."


def test_editing_a_posted_document_in_place_is_unsupported_and_points_to_cancelling_and_reissuing(engine,
                                                                                                payments_off):
    Assistant(engine, ScriptedModel([]))
    before = snapshot(engine)
    e = only(through_catalogue(engine, H035, "unsupported.edit_posted"))
    assert e["outcome"] == "d" and e["capability"] == "unsupported.edit_posted"
    assert "can't be edited in place" in e["text"] and "cancel it and issue a new one" in e["text"]
    # with cancelling switched off, the reply says so rather than leave it to be found out
    assert "Cancelling an invoice is not in ERPClaw yet" in e["text"]
    assert snapshot(engine) == before


def test_with_cancelling_switched_on_the_edit_reply_only_points_to_it(engine, monkeypatch):
    from erpclaw_core.plugins import REGISTRY

    for name in ("create_sales_order", "deliver_order", "create_credit_note"):  # switched off, as released: with
        monkeypatch.setattr(REGISTRY.contracts[name], "planned", "Core")  # them on, installed ops crowd the reply out
    e = only(through_catalogue(engine, H035, "unsupported.edit_posted"))
    assert "cancel it and issue a new one" in e["text"] and "not in ERPClaw yet" not in e["text"]


def test_a_closed_period_keeps_its_own_code(engine):
    from erpclaw_core import ledger
    from erpclaw_core.common import company_ctx

    with engine.begin() as conn:
        ledger.lock_through(company_ctx(conn), date(2026, 10, 31))
    e = only(chat(engine, [Reply("", [call("create_invoice", customer="Wayne Enterprises", items=[
        {"item": "circuit board", "quantity": 2}])])], "invoice Wayne Enterprises for 2 circuit boards"))
    assert e["type"] == "refused" and e["code"] == "period_closed" and "capability" not in e


# --- no refusal of something not offered leaves without its id --------------------------------------------------

def test_a_refusal_coded_as_not_offered_cannot_be_made_without_its_id():
    for code in ("planned", "unsupported", "available"):
        with pytest.raises(ValueError):
            Refusal("Not yet.", code)
    with pytest.raises(ValueError):
        Refusal("Not yet.", "planned", capability="op.record_payment")  # an entry id, not a refusal's id
    for outcome in ("b", "c", "d"):
        with pytest.raises(ValueError):
            cat.Decision(outcome, None, "Not yet.")


@pytest.mark.parametrize("event", [
    {"type": "answer", "outcome": "c", "text": "Not yet."},
    {"type": "answer", "outcome": "d", "text": "Not yet.", "capability": None},
    {"type": "refused", "code": "planned", "text": "Not yet."},
    {"type": "refused", "code": "unsupported", "text": "Never."},
    {"type": "refused", "code": "something_new", "text": "No.", "capability": "op.record_payment"},
], ids=["outcome c", "outcome d, empty id", "planned", "unsupported", "malformed id"])
def test_the_api_refuses_to_send_a_refusal_of_something_not_offered_without_its_id(engine, monkeypatch, event):
    """The check sits where events leave the engine, so it covers every route, those added later included."""
    from erpclaw_assistant.graph import Assistant

    monkeypatch.setattr(Assistant, "turn", lambda self, cid, text, principal=None, choice=None, *, create=True, client_message_id=None: [dict(event)])
    c, cid = client(engine, [])
    with pytest.raises(ValueError, match="catalogue id"):
        c.post(f"/v1/conversations/{cid}/messages", json={"text": "anything"})
    with pytest.raises(ValueError):
        common.check_events([dict(event)])


def test_the_approval_routes_check_their_events_too(engine, monkeypatch):
    import erpclaw_api.app as api

    ended = approvals.Outcome("superseded", "Not yet.", "x", data={"code": "planned"})  # no id
    monkeypatch.setattr(api.approvals, "decline", lambda *a, **k: ended)
    monkeypatch.setattr(api.approvals, "approve", lambda *a, **k: ended)
    c, _ = client(engine, [])
    with pytest.raises(ValueError, match="catalogue id"):
        c.post("/v1/cards/x/decline")
    with pytest.raises(ValueError, match="catalogue id"):
        c.post("/v1/cards/x/approve", json={"digest": "0" * 64})


# --- a planned reply never denies something switched on ---------------------------------------------------------

def switched_on_names(conn) -> set[str]:
    enabled = enabled_plugins(conn)
    names = set(enabled)
    for c in REGISTRY.contracts.values():
        if REGISTRY.contract_plugin[c.name] in enabled and not c.planned:
            names |= {c.name.replace("_", " "), c.plain, c.planned_name}
    for mt in REGISTRY.metrics.values():
        if REGISTRY.metric_plugin[mt.name] in enabled and not mt.planned:
            names.add(mt.title)
    return {" ".join(cat.words(n)) for n in names if n}


def test_a_revenue_request_on_the_demo_books_does_not_deny_invoicing(engine, as_released):
    said = "tell me my revenue in yr 2025"
    e = only(through_catalogue(engine, said, "metric.sales"))
    assert e["text"] == "Figures and reports like revenue are not in ERPClaw yet."
    assert "Sales is not" not in e["text"]
    with engine.connect() as conn:  # the plugins an owner switches on: the kernel is not one ("the core release")
        plugins = {p for p in enabled_plugins(conn) if REGISTRY.plugins[p].kind != "core"}
    assert "sales" in plugins
    for p in plugins:  # no switched-on plugin is named bare, as if it were the thing missing
        assert not re.search(rf"\b{re.escape(p)}\b", e["text"], re.I), (p, e["text"])
    direct = only(chat(engine, [Reply("", [call("get_metric", metric="sales", period="2025")])], said))
    assert direct["text"] == e["text"]


def test_every_planned_name_is_distinct_from_every_switched_on_name(engine, as_released):
    with engine.connect() as conn:
        on = switched_on_names(conn)
        planned = {" ".join(cat.words(a["name"])) for a in cat._planned()
                   if not (a["installed_as"] and all(p in enabled_plugins(conn) for p in a["installed_as"]))}
    for c in REGISTRY.contracts.values():
        if c.planned:
            planned.add(" ".join(cat.words(c.planned_name or c.name.replace("_", " "))))
    if any(mt.planned for mt in REGISTRY.metrics.values()):
        planned.add(" ".join(cat.words(contracts.FIGURES)))  # a planned measure is named as figures and reports
    assert on and planned
    assert not on & planned, on & planned


def test_the_ids_a_refusal_can_carry_never_collide_with_the_planned_areas():
    areas = {a["id"] for a in cat._planned()}
    ours = {"planned.reports", cat.NO_MATCH} | {u.id for u in cat.UNSUPPORTED} | {
        f"planned.op.{n}" for n in REGISTRY.contracts}
    assert not areas & ours
    assert all(ID.match(i) for i in areas | ours)


# --- the reply to a request ERPClaw does not do lists what is switched on ----------------------------------------

def test_the_no_match_reply_lists_what_is_switched_on_and_holds_no_number(engine, as_released):
    e = only(chat(engine, [Reply("", [call("find_capability", request="fly me to the moon")]),
                           Reply("", [call("choose_capability", pick="none")])], "fly me to the moon"))
    with engine.connect() as conn:
        assert e["text"].endswith(" " + cat.switched_on(conn))
    assert not re.search(r"\d", e["text"])


def test_the_list_changes_when_a_contract_is_switched_off(engine, payments_off, monkeypatch):
    with engine.connect() as conn:
        before = cat.switched_on(conn)
    monkeypatch.setattr(REGISTRY.contracts["submit_invoice"], "planned", "Core")
    with engine.connect() as conn:
        after = cat.switched_on(conn)
    assert "submit a draft invoice" in before and "submit a draft invoice" not in after
    assert after == "Right now I can add customers, add suppliers, add cash accounts, add items, list customers, list suppliers, list items, and draft invoices."
    with engine.begin() as conn:
        disable_plugin(conn, "sales")
    with engine.connect() as conn:
        assert cat.switched_on(conn) == "Right now I can add customers, add suppliers, add cash accounts, add items, list customers, list suppliers, and list items."


def test_with_every_operation_on_the_list_names_each_once_in_plain_words(engine):
    with engine.connect() as conn:
        text = cat.switched_on(conn)
    assert text == ("Right now I can give figures and reports, add customers, add suppliers, add cash accounts, add items, list customers, list suppliers, list items, send a message again, reverse supplier payments, record an opening position, draft invoices, "
                    "submit a draft invoice, record "
                    "payments, cancel an invoice, reverse a payment, take sales orders, deliver sales orders, issue "
                    "credit notes, list invoices, record opening stock, write off stock, record stock counts, "
                    "cancel stock movement, and reverse goods received.")
    assert not re.search(r"\d", text)


def test_every_operation_has_a_plain_name_without_a_number():
    for c in REGISTRY.contracts.values():
        assert c.plain and not re.search(r"\d", c.plain + c.planned_name), c.name
