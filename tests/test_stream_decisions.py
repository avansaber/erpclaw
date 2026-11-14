"""Conversation stream and card read enforce the same ownership boundary."""
from host_client import host_world
from test_api_approval_security import approval_world, signed, pending
from test_card_ownership import UNKNOWN, peer_for


def test_card_read_is_owner_only(signed):
    world, server, owner = signed
    peer = peer_for(world, server)
    _, card = pending(world, owner)
    aid = card['approval_id']
    response = owner.request('GET', f'/v1/cards/{aid}')
    assert response.status == 200
    assert response.json()['status'] == 'pending'
    assert response.json()['digest'] == card['digest']
    assert 'replaces' not in response.json()
    foreign = peer.request('GET', f'/v1/cards/{aid}')
    unknown = peer.request('GET', f'/v1/cards/{UNKNOWN}')
    assert foreign.status == unknown.status == 404
    assert foreign.body == unknown.body
    assert foreign.json()['code'] == 'no_conversation'


def test_stream_replay_refuses_foreign_and_unknown(signed):
    world, server, owner = signed
    peer = peer_for(world, server)
    cid, _ = pending(world, owner)
    for suffix, headers in (('', None), ('?after=0', None), ('', {'Last-Event-ID': '0'})):
        foreign = peer.request('GET', f'/v1/conversations/{cid}/stream{suffix}', headers=headers)
        unknown = peer.request('GET', f'/v1/conversations/{UNKNOWN}/stream{suffix}', headers=headers)
        assert foreign.status == unknown.status == 404
        assert foreign.body == unknown.body
        assert foreign.json()['code'] == 'no_conversation'

import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event

from erpclaw_assistant import store
from erpclaw_core import approvals
from erpclaw_core.plugins import REGISTRY
from test_api_approval_security import INVOICE
from test_api_approval_security import approve, conversation


def watch(client, cid):
    """Open the response before the action; a reader records transient decisions."""
    connection, response = client._open('GET', f'/v1/conversations/{cid}/stream?after=0')
    assert response.status == 200
    received = queue.Queue()
    def read():
        try:
            kind = None
            for raw in response:
                line = raw.decode().strip()
                if line.startswith('event:'):
                    kind = line[6:].strip()
                elif line.startswith('data:') and kind == 'decision':
                    received.put(json.loads(line[5:].strip()))
        except (OSError, ValueError):
            pass
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    response._watch_thread = thread
    return connection, response, received


def close_watch(connection, response):
    connection.shutdown(2)
    response._watch_thread.join(timeout=5)
    response.close()
    connection.close()


@pytest.mark.parametrize('action,status', [('approve', 'consumed'), ('decline', 'declined')])
def test_decision_matches_card_and_contains_no_preview(signed, action, status):
    world, _, owner = signed
    cid, card = pending(world, owner)
    conn, response, received = watch(owner, cid)
    try:
        if action == 'approve':
            assert approve(owner, card).status == 200
        else:
            assert owner.request('POST', f"/v1/cards/{card['approval_id']}/decline", json={}).status == 200
        decision = received.get(timeout=5)
        assert set(decision) == {'type', 'approval_id', 'status', 'at'}
        assert decision['approval_id'] == card['approval_id']
        assert decision['status'] == owner.request('GET', f"/v1/cards/{card['approval_id']}").json()['status'] == status
        assert decision['type'] == 'decision'
        assert json.dumps(card['card']) not in json.dumps(decision)
        assert received.empty()
    finally:
        close_watch(conn, response)


def test_decision_waits_for_outer_commit(signed):
    world, _, owner = signed
    cid, card = pending(world, owner)
    conn, response, received = watch(owner, cid)
    held, release = threading.Event(), threading.Event()
    def before_commit(dbconn):
        if dbconn.info.get('hold_decision'):
            held.set()
            assert release.wait(10)
    def mark(conn_, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('INSERT INTO OPERATION_OUTCOME'):
            conn_.info['hold_decision'] = True
    event.listen(world.runtime, 'before_cursor_execute', mark)
    event.listen(world.runtime, 'commit', before_commit)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(approve, owner, card)
            assert held.wait(10)
            with pytest.raises(queue.Empty):
                received.get(timeout=0.75)
            release.set()
            assert future.result(timeout=15).status == 200
            assert received.get(timeout=5)['status'] == 'consumed'
    finally:
        release.set()
        event.remove(world.runtime, 'before_cursor_execute', mark)
        event.remove(world.runtime, 'commit', before_commit)
        close_watch(conn, response)


def test_rollback_emits_no_decision(signed):
    world, _, owner = signed
    cid, card = pending(world, owner)
    conn, response, received = watch(owner, cid)
    def fail(conn_, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('INSERT INTO OPERATION_OUTCOME'):
            raise RuntimeError('forced rollback')
    event.listen(world.runtime, 'before_cursor_execute', fail)
    try:
        assert approve(owner, card).status == 500
        with pytest.raises(queue.Empty):
            received.get(timeout=0.75)
    finally:
        event.remove(world.runtime, 'before_cursor_execute', fail)
        close_watch(conn, response)


def test_decline_lost_race_is_unknown_and_emits_nothing(signed, monkeypatch):
    world, _, owner = signed
    cid, card = pending(world, owner)
    conn, response, received = watch(owner, cid)
    monkeypatch.setattr(approvals, '_end', lambda *args, **kwargs: False)
    try:
        result = owner.request('POST', f"/v1/cards/{card['approval_id']}/decline", json={})
        assert result.json()['events'][0]['status'] == 'unknown'
        with pytest.raises(queue.Empty):
            received.get(timeout=0.75)
    finally:
        close_watch(conn, response)


def test_replay_is_confined_to_each_conversation(signed):
    world, server, owner = signed
    peer = peer_for(world, server)
    first = conversation(owner)
    second = conversation(peer)
    # Durable store rows are scoped by conversation even when their cursors overlap.
    for cid, who, text in ((first, world.principals['alice']['id'], 'first'),
                           (second, None, 'second')):
        if who is None:
            who = store.conversation_owner(cid)
        claim = store.claim(cid, text, principal_id=who, principal_name='owner', create=False)
        request = store.new_request(claim)
        assert store.assign(claim, request['id'])
        store.add_events(request['id'], [{'type': 'answer', 'text': text}])
        store.release(claim)
    for client, cid, expected, forbidden in ((owner, first, 'first', 'second'), (peer, second, 'second', 'first')):
        stream = client.stream(f'/v1/conversations/{cid}/stream?after=0')
        try:
            found = [next(stream), next(stream)]
            data = ' '.join(e.data for e in found)
            assert expected in data and forbidden not in data
            assert all(e.id is not None for e in found)
        finally:
            stream.close()


def test_replacement_event_keeps_link_but_card_read_omits_it(signed):
    world, _, owner = signed
    cid, old = pending(world, owner)
    proposal = approvals.propose(world.runtime, REGISTRY.contracts['create_invoice'], INVOICE, cid,
                                 principal=world.principals['alice'], company_id=world.companies[0],
                                 ownership_check=lambda c: store.conversation_owner(c) == world.principals['alice']['id'])
    new = approvals.card_events(proposal)[-1]
    assert new['replaces'] == old['approval_id']
    response = owner.request('GET', f"/v1/cards/{new['approval_id']}")
    assert response.status == 200
    assert 'replaces' not in response.json()


@pytest.mark.parametrize('reason', ['sign_out', 'idle_expiry', 'absolute_expiry', 'disabled'])
@pytest.mark.parametrize('publish', [False, True], ids=['idle', 'event'])
def test_stream_closes_when_session_ends(signed, reason, publish, monkeypatch):
    from datetime import timedelta
    from sqlalchemy import update
    from erpclaw_core import models as m
    from erpclaw_core.common import now
    from erpclaw_api import session

    world, _, owner = signed
    cid = conversation(owner)
    conn, response, received = watch(owner, cid)
    pid = world.principals['alice']['id']
    try:
        if reason == 'sign_out':
            assert owner.request('DELETE', '/v1/session').status == 204
        elif reason == 'absolute_expiry':
            future = now() + timedelta(days=8)
            monkeypatch.setattr(session, 'now', lambda: future)
        else:
            with world.owner.begin() as db:
                if reason == 'disabled':
                    db.execute(update(m.principal).where(m.principal.c.id == pid).values(disabled=True))
                else:
                    expired = now() - timedelta(seconds=1)
                    db.execute(update(m.principal_session).where(m.principal_session.c.principal_id == pid)
                               .values(idle_expires_at=expired))
        if publish:
            world.runtime._erpclaw_decision_emit(cid, 'test', 'declined')
        response._watch_thread.join(timeout=3)
        assert not response._watch_thread.is_alive(), 'stream remained open after authorization ended'
        assert received.empty(), 'stream delivered a decision after authorization ended'
    finally:
        close_watch(conn, response)
