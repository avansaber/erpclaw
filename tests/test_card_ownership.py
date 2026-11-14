"""Card and conversation ownership at the authenticated and core boundaries."""
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import update

from erpclaw_assistant import graph, store
from erpclaw_assistant.model import Reply
from erpclaw_core import administration as admin, approvals, models as m, principals as pr
from erpclaw_core.common import Refusal, now
from erpclaw_core.plugins import REGISTRY
from host_client import books, host_world
from test_api_approval_security import (approval_world, signed, message_signed, INVOICE, approve,
                                        conversation, error, invoice_effect, pending, result)

UNKNOWN = "00000000-0000-0000-0000-000000000000"


def test_owner_read_does_not_wait_for_writer(message_signed):
    world, _, alice = message_signed
    cid = conversation(alice)
    owner = principal_id(world, alice)
    locked = threading.Event()

    def write_while_locked():
        conn = store._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            locked.set()
            time.sleep(5)
            conn.execute("UPDATE conversation SET owner_principal_id = ? WHERE id = ?", (owner, cid))
            conn.execute("COMMIT")
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write_while_locked)
        assert locked.wait(5)
        for target, expected in ((UNKNOWN, None), (cid, owner)):
            start = time.monotonic()
            assert store.conversation_owner(target) == expected
            assert time.monotonic() - start < 1
        writer.result()
    assert store.conversation_owner(cid) == owner


def test_claim_rechecks_owner_after_read(message_signed):
    world, server, alice = message_signed
    peer = peer_for(world, server)
    cid = conversation(alice)
    owner = principal_id(world, alice)
    assert store.conversation_owner(cid) == owner
    with store._tx() as conn:
        conn.execute("UPDATE conversation SET owner_principal_id = ? WHERE id = ?",
                     (principal_id(world, peer), cid))
    with pytest.raises(Refusal) as refusal:
        store.claim(cid, "hello", principal_id=owner, principal_name="test", create=False)
    assert refusal.value.code == "no_conversation"
    conn = store._open()
    try:
        assert conn.execute("SELECT COUNT(*) FROM request_message WHERE conversation_id = ?", (cid,)).fetchone()[0] == 0
        assert conn.execute("SELECT claimed_by FROM conversation WHERE id = ?", (cid,)).fetchone()[0] is None
    finally:
        conn.close()


def no_effect(before, after, principal_id):
    before, after = deepcopy(before), deepcopy(after)
    keys = [key for key, row in before["principal_session"].items() if row["principal_id"] == principal_id]
    assert len(keys) == 1
    for snapshot in (before, after):
        row = snapshot["principal_session"][keys[0]]
        row.pop("last_seen_at")
        row.pop("idle_expires_at")
    assert before == after


def peer_for(world, server):
    with world.owner.begin() as conn:
        admin.add_principal(conn, "peer", ["view", "draft", "post", "approve"], [world.companies[0]])
    return world.client(server, "peer")


def frozen(response):
    assert response.status == 404
    assert response.json() == {"code": "no_conversation", "detail": "That conversation was not found."}


def principal_id(world, client):
    digest = pr.session_digest(client.secret)
    return next(row["principal_id"] for row in books(world.owner)["principal_session"].values()
                if row["secret_sha256"] == digest)


def open_history(world, client, cid):
    claim = store.claim(cid, "hello", principal_id=principal_id(world, client), principal_name="test")
    assert isinstance(claim, store.Claim)
    request = store.new_request(claim)
    assert request is not None
    assert store.assign(claim, request["id"])
    store.release(claim)


@pytest.mark.parametrize("route", ["approve", "decline", "view"])
def test_peer_card_routes_are_concealed(signed, route):
    world, server, alice = signed
    peer = peer_for(world, server)
    _, card = pending(world, alice)
    key = books(world.owner)["approval"][(card["approval_id"],)]["operation_key"]
    pid = principal_id(world, peer)
    before = books(world.owner)
    if route == "approve":
        response = approve(peer, card)
    elif route == "decline":
        response = peer.request("POST", f"/v1/cards/{card['approval_id']}/decline", json={})
    else:
        response = peer.request("GET", f"/v1/operations/{key}")
    after = books(world.owner)
    no_effect(before, after, pid)
    frozen(response)


def test_unknown_and_foreign_card_and_key_are_byte_identical(signed):
    world, server, alice = signed
    peer = peer_for(world, server)
    cid, card = pending(world, alice)
    key = books(world.owner)["approval"][(card["approval_id"],)]["operation_key"]
    pid = principal_id(world, peer)
    open_history(world, alice, cid)
    before = books(world.owner)
    history = store.history_page(cid)
    serial = store.serial(cid)
    responses = [approve(peer, {"approval_id": UNKNOWN, "digest": card["digest"]}), approve(peer, card),
                 peer.request("POST", f"/v1/cards/{UNKNOWN}/decline", json={}),
                 peer.request("POST", f"/v1/cards/{card['approval_id']}/decline", json={}),
                 peer.request("GET", f"/v1/operations/{UNKNOWN}"), peer.request("GET", f"/v1/operations/{key}")]
    no_effect(before, books(world.owner), pid)
    assert store.history_page(cid) == history
    assert store.serial(cid) == serial
    for response in responses:
        frozen(response)
    assert all((r.status, r.body, r.raw_headers) == (responses[0].status, responses[0].body, responses[0].raw_headers)
               for r in responses[1:])


def test_operation_read_back_ignores_stray_conversation_id(signed):
    world, server, alice = signed
    peer = peer_for(world, server)
    cid, card = pending(world, alice)
    foreign = conversation(peer)
    open_history(world, alice, cid)
    open_history(world, peer, foreign)
    key = books(world.owner)["approval"][(card["approval_id"],)]["operation_key"]
    before = books(world.owner)
    histories = {c: (store.history_page(c), store.serial(c)) for c in (cid, foreign)}
    path = f"/v1/operations/{key}"
    responses = [alice.request("GET", path)] + [
        alice.request("GET", f"{path}?conversation_id={c}") for c in (foreign, UNKNOWN)
    ]
    no_effect(before, books(world.owner), principal_id(world, alice))
    assert {c: (store.history_page(c), store.serial(c)) for c in (cid, foreign)} == histories
    assert responses[0].status == 200
    assert all((r.status, r.body, r.raw_headers) ==
               (responses[0].status, responses[0].body, responses[0].raw_headers) for r in responses[1:])


def test_foreign_proposal_does_not_supersede_or_disclose(signed):
    world, server, alice = signed
    peer = peer_for(world, server)
    cid, card = pending(world, alice)
    pid = principal_id(world, peer)
    with world.owner.connect() as conn:
        who = pr.by_id(conn, pid)
    before = books(world.owner)
    with pytest.raises(Refusal) as refusal:
        approvals.propose(world.runtime, REGISTRY.contracts["create_invoice"], INVOICE, cid,
                          principal=who, company_id=world.companies[0], ownership_check=lambda c: store.conversation_owner(c) == pid)
    assert refusal.value.code == "no_conversation"
    assert refusal.value.message == "That conversation was not found."
    assert books(world.owner) == before
    assert before["approval"][(card["approval_id"],)]["status"] == "pending"


def test_foreign_retry_never_creates_a_fresh_card(signed):
    world, server, alice = signed
    peer = peer_for(world, server)
    _, card = pending(world, alice)
    pid = principal_id(world, peer)
    before = books(world.owner)
    responses = [approve(peer, card), approve(peer, card)]
    no_effect(before, books(world.owner), pid)
    for response in responses:
        frozen(response)
    assert responses[0].body == responses[1].body


def test_unbound_card_uses_company_grants(signed):
    world, server, alice = signed
    bob = world.client(server, "bob")
    proposed = approvals.propose(world.runtime, REGISTRY.contracts["create_invoice"], INVOICE,
                                 principal=world.principals["alice"], company_id=world.companies[0])
    card, = approvals.card_events(proposed)
    before = books(world.owner)
    error(approve(bob, card), 403, "forbidden")
    no_effect(before, books(world.owner), principal_id(world, bob))
    result(approve(alice, card), "committed")
    invoice_effect(world, before, books(world.owner), card)


@pytest.mark.parametrize("route", ["approve", "decline"])
def test_unbound_card_rejects_unowned_targets(signed, route):
    world, server, alice = signed
    peer = peer_for(world, server)
    foreign = conversation(peer)
    ownerless = conversation(peer)
    open_history(world, peer, foreign)
    open_history(world, peer, ownerless)
    with store._tx() as conn:
        conn.execute("UPDATE conversation SET owner_principal_id = NULL WHERE id = ?", (ownerless,))
    proposed = approvals.propose(world.runtime, REGISTRY.contracts["create_invoice"], INVOICE,
                                 principal=world.principals["alice"], company_id=world.companies[0])
    card, = approvals.card_events(proposed)
    before = books(world.owner)
    history = store.history_page(foreign)
    ownerless_history = store.history_page(ownerless)
    body = {"digest": card["digest"]} if route == "approve" else {}
    responses = [alice.request("POST", f"/v1/cards/{card['approval_id']}/{route}?conversation_id={cid}", json=body)
                 for cid in (foreign, UNKNOWN, ownerless)]
    no_effect(before, books(world.owner), principal_id(world, alice))
    assert store.history_page(foreign) == history
    assert store.history_page(ownerless) == ownerless_history
    for response in responses:
        frozen(response)
    assert len({response.body for response in responses}) == 1


@pytest.mark.parametrize("route", ["approve", "decline"])
@pytest.mark.parametrize("with_conversation", [True, False])
def test_unbound_card_accepts_own_or_no_target(signed, route, with_conversation):
    world, _, alice = signed
    cid = conversation(alice) if with_conversation else None
    if cid:
        open_history(world, alice, cid)
    proposed = approvals.propose(world.runtime, REGISTRY.contracts["create_invoice"], INVOICE,
                                 principal=world.principals["alice"], company_id=world.companies[0])
    card, = approvals.card_events(proposed)
    before = books(world.owner)
    history = store.history_page(cid) if cid else None
    body = {"digest": card["digest"]} if route == "approve" else {}
    query = f"?conversation_id={cid}" if cid else ""
    response = alice.request("POST", f"/v1/cards/{card['approval_id']}/{route}{query}", json=body)
    result(response, "committed" if route == "approve" else "declined")
    if route == "approve":
        invoice_effect(world, before, books(world.owner), card)
    else:
        assert books(world.owner)["approval"][(card["approval_id"],)]["status"] == "declined"
    if cid:
        assert store.history_page(cid) != history


def test_owner_can_approve_own_proposal(signed):
    world, _, alice = signed
    _, card = pending(world, alice)
    before = books(world.owner)
    result(approve(alice, card), "committed")
    invoice_effect(world, before, books(world.owner), card)



def test_committed_card_replay_preserves_ownership(signed):
    world, server, alice = signed
    peer = peer_for(world, server)
    _, card = pending(world, alice)
    committed = result(approve(alice, card), "committed")
    before = books(world.owner)
    assert result(approve(alice, card), "committed") == committed
    no_effect(before, books(world.owner), principal_id(world, alice))
    before = books(world.owner)
    frozen(approve(peer, card))
    no_effect(before, books(world.owner), principal_id(world, peer))


def test_superseded_card_replay_returns_committed_replacement_to_owner(signed):
    world, server, alice = signed
    peer = peer_for(world, server)
    _, card = pending(world, alice)
    with world.owner.begin() as conn:
        conn.execute(update(m.item).where(m.item.c.name == "Copper Wire").values(standard_rate="29.00"))
    response = approve(alice, card)
    response.require_status(200)
    fresh, = [event for event in response.json()["events"] if event["type"] == "card"]
    committed = result(approve(alice, fresh), "committed")
    before = books(world.owner)
    assert result(approve(alice, card), "committed") == committed
    no_effect(before, books(world.owner), principal_id(world, alice))
    before = books(world.owner)
    frozen(approve(peer, card))
    no_effect(before, books(world.owner), principal_id(world, peer))


def test_foreign_and_unknown_messages_with_multiple_companies(signed):
    world, server, alice = signed
    peer = peer_for(world, server)
    cid = conversation(alice)
    pid = principal_id(world, peer)
    before = books(world.owner)
    serial = store.serial(cid)
    responses = [peer.request("POST", f"/v1/conversations/{c}/messages", json={"text": "hello"}) for c in (cid, UNKNOWN)]
    no_effect(before, books(world.owner), pid)
    assert store.serial(cid) == serial
    for response in responses:
        frozen(response)
    assert responses[0].body == responses[1].body


def test_foreign_and_unknown_messages_single_company(message_signed):
    world, server, alice = message_signed
    peer = peer_for(world, server)
    cid = conversation(alice)
    pid = principal_id(world, peer)
    before = books(world.owner)
    serial = store.serial(cid)
    responses = [peer.request("POST", f"/v1/conversations/{c}/messages", json={"text": "hello"}) for c in (cid, UNKNOWN)]
    no_effect(before, books(world.owner), pid)
    assert store.serial(cid) == serial
    for response in responses:
        frozen(response)
    assert responses[0].body == responses[1].body


def test_message_grant_refusal_leaves_empty_conversation_unclaimed(message_signed):
    world, _, alice = message_signed
    cid = conversation(alice)
    with world.owner.begin() as conn:
        admin.revoke(conn, "alice", ["view"], world.companies[0])
    pid = principal_id(world, alice)
    before = books(world.owner)
    history = store.history_page(cid)
    serial = store.serial(cid)
    lease = store.live_turn(cid)
    assert lease["claimed_by"] is None and lease["claim_until"] is None
    error(alice.request("POST", f"/v1/conversations/{cid}/messages", json={"text": "hello"}), 403, "forbidden")
    no_effect(before, books(world.owner), pid)
    assert store.history_page(cid) == history
    assert store.serial(cid) == serial
    assert store.live_turn(cid) == lease


def test_message_grant_refusal_does_not_claim_or_replay(message_signed):
    world, server, alice = message_signed
    cid = conversation(alice)
    world.model.replies.append(Reply("Hello."))
    body = {"text": "hello", "client_message_id": "one"}
    assert alice.request("POST", f"/v1/conversations/{cid}/messages", json=body).status == 200
    with world.owner.begin() as conn:
        admin.revoke(conn, "alice", ["view"], world.companies[0])
    pid = principal_id(world, alice)
    before = books(world.owner)
    serial = store.serial(cid)
    for request_body in (body, {"text": "another", "client_message_id": "two"}):
        error(alice.request("POST", f"/v1/conversations/{cid}/messages", json=request_body), 403, "forbidden")
    no_effect(before, books(world.owner), pid)
    assert store.serial(cid) == serial
    assert not store.live_turn(cid)["live"]


def test_foreign_refusal_only_refreshes_callers_session(signed):
    world, server, alice = signed
    peer = peer_for(world, server)
    _, card = pending(world, alice)
    caller = principal_id(world, peer)
    owner = principal_id(world, alice)
    old = now() - timedelta(minutes=5)
    with world.owner.begin() as conn:
        conn.execute(update(m.principal_session).where(m.principal_session.c.principal_id.in_([caller, owner]))
                     .values(last_seen_at=old))
    before = books(world.owner)
    response = approve(peer, card)
    after = books(world.owner)
    no_effect(before, after, caller)
    owner_before = [r for r in before["principal_session"].values() if r["principal_id"] == owner]
    owner_after = [r for r in after["principal_session"].values() if r["principal_id"] == owner]
    assert owner_before == owner_after
    caller_before = next(r for r in before["principal_session"].values() if r["principal_id"] == caller)
    caller_after = next(r for r in after["principal_session"].values() if r["principal_id"] == caller)
    assert caller_after["last_seen_at"] != caller_before["last_seen_at"]
    assert caller_after["idle_expires_at"] != caller_before["idle_expires_at"]
    frozen(response)


@pytest.mark.parametrize("operation", ["approve", "decline", "read_back", "pending_card"])
def test_core_without_ownership_input_fails_closed(signed, operation):
    world, _, alice = signed
    cid, card = pending(world, alice)
    key = books(world.owner)["approval"][(card["approval_id"],)]["operation_key"]
    before = books(world.owner)
    args = {"approve": (world.runtime, card["approval_id"], card["digest"]),
            "decline": (world.runtime, card["approval_id"]), "read_back": (world.runtime, key),
            "pending_card": (world.runtime, cid)}[operation]
    with pytest.raises(Refusal) as refusal:
        getattr(approvals, operation)(*args)
    assert refusal.value.code == "no_conversation"
    assert refusal.value.message == "That conversation was not found."
    assert books(world.owner) == before


@pytest.mark.parametrize("operation", ["approve", "decline", "read_back", "pending_card", "set_aside"])
def test_core_rejects_other_owner_input(signed, operation):
    world, server, alice = signed
    peer = peer_for(world, server)
    cid, card = pending(world, alice)
    pid = principal_id(world, peer)
    key = books(world.owner)["approval"][(card["approval_id"],)]["operation_key"]
    before = books(world.owner)
    args = {"approve": (world.runtime, card["approval_id"], card["digest"]),
            "decline": (world.runtime, card["approval_id"]), "read_back": (world.runtime, key),
            "pending_card": (world.runtime, cid),
            "set_aside": (world.runtime, cid, secrets.token_urlsafe(24), card["approval_id"], "ignored")}[operation]
    with pytest.raises(Refusal) as refusal:
        getattr(approvals, operation)(*args, ownership_check=lambda c: store.conversation_owner(c) == pid)
    assert refusal.value.code == "no_conversation"
    assert refusal.value.message == "That conversation was not found."
    assert books(world.owner) == before


@pytest.mark.parametrize("operation", ["approve", "decline", "read_back", "pending_card"])
def test_core_accepts_right_owner_input(signed, operation):
    world, _, alice = signed
    cid, card = pending(world, alice)
    pid = principal_id(world, alice)
    key = books(world.owner)["approval"][(card["approval_id"],)]["operation_key"]
    args = {"approve": (world.runtime, card["approval_id"], card["digest"]),
            "decline": (world.runtime, card["approval_id"]), "read_back": (world.runtime, key),
            "pending_card": (world.runtime, cid)}[operation]
    value = getattr(approvals, operation)(*args, ownership_check=lambda c: store.conversation_owner(c) == pid)
    if operation == "pending_card":
        assert value["id"] == card["approval_id"]
    else:
        assert value.status == {"approve": "committed", "decline": "declined", "read_back": "unknown"}[operation]


def test_owned_card_still_requires_post_grant(approval_world):
    world = approval_world
    with world.owner.begin() as conn:
        admin.add_principal(conn, "draft_only", ["view", "draft", "approve"], [world.companies[0]])
    with world.server() as server:
        client = world.client(server, "draft_only")
        _, card = pending(world, client)
        before = books(world.owner)
        error(approve(client, card), 403, "forbidden")
        no_effect(before, books(world.owner), principal_id(world, client))


def test_owned_card_is_invalidated_after_drafter_grant_revoked(signed):
    world, _, alice = signed
    _, card = pending(world, alice)
    with world.owner.begin() as conn:
        admin.revoke(conn, "alice", ["draft"], world.companies[0])
    before = books(world.owner)
    result(approve(alice, card), "invalidated")
    after = books(world.owner)
    assert after["approval"][(card["approval_id"],)]["status"] == "invalidated"
    assert len(after["approval"]) == len(before["approval"])


@pytest.mark.parametrize("operation", ["propose", "set_aside"])
def test_core_proposal_helpers_without_ownership_fail_closed(signed, operation):
    world, _, alice = signed
    cid, card = pending(world, alice)
    before = books(world.owner)
    with pytest.raises(Refusal) as refusal:
        if operation == "propose":
            approvals.propose(world.runtime, REGISTRY.contracts["create_invoice"], INVOICE, cid,
                              principal=world.principals["alice"], company_id=world.companies[0])
        else:
            approvals.set_aside(world.runtime, cid, secrets.token_urlsafe(24), card["approval_id"], "ignored")
    assert refusal.value.code == "no_conversation"
    assert refusal.value.message == "That conversation was not found."
    assert books(world.owner) == before


def test_core_propose_accepts_right_owner_input(signed):
    world, _, alice = signed
    cid = conversation(alice)
    pid = principal_id(world, alice)
    proposed = approvals.propose(world.runtime, REGISTRY.contracts["create_invoice"], INVOICE, cid,
                                 principal=world.principals["alice"], company_id=world.companies[0],
                                 ownership_check=lambda c: store.conversation_owner(c) == pid)
    assert proposed.status == "card"


def test_core_card_without_recorded_owner_fails_closed(signed):
    world, _, alice = signed
    cid, card = pending(world, alice)
    pid = principal_id(world, alice)
    with store._tx() as conn:
        conn.execute("UPDATE conversation SET owner_principal_id = NULL WHERE id = ?", (cid,))
    before = books(world.owner)
    with pytest.raises(Refusal) as refusal:
        approvals.approve(world.runtime, card["approval_id"], card["digest"],
                          ownership_check=lambda c: store.conversation_owner(c) == pid)
    assert (refusal.value.code, refusal.value.message) == ("no_conversation", "That conversation was not found.")
    assert books(world.owner) == before


def test_direct_turn_claim_uses_books_owner(message_signed, monkeypatch):
    world, _, _ = message_signed
    with world.owner.connect() as conn:
        owner_id = pr.owner(conn)["id"]
    seen = []
    original_claim = store.claim

    def claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        if isinstance(result, store.Claim):
            seen.append((result.principal_id, result.owns(result.conversation_id)))
        return result

    monkeypatch.setattr(store, "claim", claim)
    world.model.replies.append(Reply("Hello."))
    graph.Assistant(world.runtime, world.model).turn("direct-owner", "hello")
    assert seen == [(owner_id, True)]


def test_message_uses_authenticated_principal_for_claim_and_card_checks(message_signed, monkeypatch):
    world, server, alice = message_signed
    peer = peer_for(world, server)
    cid, card = pending(world, peer)
    peer_id = principal_id(world, peer)
    owner_id = principal_id(world, alice)
    owner_cid = conversation(alice)
    seen = []
    original_turn = graph.Assistant.turn
    original_claim = store.claim
    original_pending = approvals.pending_card

    def turn(*args, **kwargs):
        seen.append(("turn", kwargs["principal"]["id"]))
        return original_turn(*args, **kwargs)

    def claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        if isinstance(result, store.Claim):
            seen.append(("claim", result.principal_id, result.owns(cid)))
        return result

    def pending_card(*args, **kwargs):
        check = kwargs["ownership_check"]
        seen.append(("card", check(cid), check(owner_cid)))
        return original_pending(*args, **kwargs)

    monkeypatch.setattr(graph.Assistant, "turn", turn)
    monkeypatch.setattr(store, "claim", claim)
    monkeypatch.setattr(approvals, "pending_card", pending_card)
    world.model.replies.append(Reply("Hello."))
    response = peer.request("POST", f"/v1/conversations/{cid}/messages", json={"text": "hello"})
    assert response.status == 200
    assert peer_id != owner_id
    assert ("turn", peer_id) in seen
    assert ("claim", peer_id, True) in seen
    assert ("card", True, False) in seen


def test_interrupted_answer_keeps_owned_card_and_refuses_foreign_conversation(message_signed):
    from test_api_approval_security import draft

    world, server, alice = message_signed
    peer = peer_for(world, server)
    cid, card = draft(world, alice)
    foreign = conversation(peer)
    world.model.replies.append(Reply("Partial answer", finish="length"))
    response = alice.request("POST", f"/v1/conversations/{cid}/messages", json={"text": "yes approve it"})
    assert response.status == 200
    assert [e["code"] for e in response.json()["events"] if e["type"] == "refused"] == ["answer_interrupted"]
    assert store.open_request(cid)["pending_card"] == card["approval_id"]
    assert books(world.owner)["approval"][(card["approval_id"],)]["status"] == "pending"
    frozen(alice.request("POST", f"/v1/conversations/{foreign}/messages", json={"text": "hello"}))
    frozen(peer.request("POST", f"/v1/conversations/{cid}/messages", json={"text": "hello"}))
