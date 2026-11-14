"""A claim holder reports its own lapse; another turn still sees the holder's status."""
from datetime import timedelta
import inspect
import hashlib
import sqlite3

import pytest

from erpclaw_assistant import graph, resolve_text, store
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import principals


SENTENCE = ("That took too long, so I stopped before finishing your last message. "
            "Please check what is already there, and send it again if it is still missing.")


def snapshot():
    with sqlite3.connect(store.path()) as conn:
        rows = {table: conn.execute(f'SELECT * FROM {table}').fetchall()
                for table in ('conversation', 'request', 'request_message', 'request_event')}
        return hashlib.sha256(repr(rows).encode()).hexdigest()


def lapse(monkeypatch):
    now = store._now
    monkeypatch.setattr(store, '_now', lambda: now() + store.LEASE + timedelta(seconds=1))


def assert_lapsed(events):
    assert len(events) == 1
    assert events[0]['type'] == 'refused'
    assert events[0]['code'] == 'turn_lapsed'
    assert events[0]['text'] == SENTENCE
    assert all(e['type'] != 'card' for e in events)


def invoice():
    return call('create_invoice', customer='Acme Corporation',
                items=[{'item': 'copper wire', 'quantity': 1}])


@pytest.mark.parametrize('phase', ['model', 'propose', 'finalise'])
def test_lapse_at_work_boundary(engine, monkeypatch, phase):
    """Mutations: back_to_busy_stopped, back_to_busy_propose, back_to_busy_finalise."""
    captured = []
    original_renew, original_finalise = store.renew, store.finalise
    def renew(claim):
        stack = {frame.function for frame in inspect.stack()}
        selected = (phase == 'model' and 'model_node' in stack or
                    phase == 'propose' and 'propose' in stack)
        if selected and not captured:
            before = snapshot()
            lapse(monkeypatch)
            result = original_renew(claim)
            captured.append((before, snapshot(), result))
            return result
        return original_renew(claim)
    def finalise(claim, *args, **kwargs):
        if phase == 'finalise' and not captured:
            before = snapshot()
            lapse(monkeypatch)
            result = original_finalise(claim, *args, **kwargs)
            captured.append((before, snapshot(), result))
            return result
        return original_finalise(claim, *args, **kwargs)
    monkeypatch.setattr(store, 'renew', renew)
    monkeypatch.setattr(store, 'finalise', finalise)
    events = graph.Assistant(engine, ScriptedModel([Reply('', [invoice()])])).turn(
        'lapse', 'Invoice Acme Corporation for one copper wire')
    assert captured and captured[0][0] == captured[0][1] and captured[0][2] is False
    assert_lapsed(events)


@pytest.mark.parametrize('stalled', [False, True])
def test_other_turn_keeps_busy_wording(engine, monkeypatch, stalled):
    """Mutations: busy_text_to_lapsed, stalled_to_busy."""
    with engine.connect() as conn:
        owner = principals.owner(conn)
    holder = store.claim('held', 'hello', principal_id=owner['id'], principal_name=owner['name'])
    assert holder
    if stalled:
        now = store._now
        monkeypatch.setattr(store, '_now', lambda: now() + store.STALLED_AFTER + timedelta(seconds=1))
    before = snapshot()
    events = graph.Assistant(engine, ScriptedModel([])).turn('held', 'hello again')
    assert snapshot() == before
    assert events[0]['code'] == 'busy'
    assert events[0]['text'] == graph.say('busy_stopped' if stalled else 'busy')


def test_lapse_sentence_is_language_data(monkeypatch):
    """Mutation: hardwired_sentence; blanket_nothing_saved."""
    assert graph.say('turn_lapsed') == SENTENCE
    assert SENTENCE not in inspect.getsource(graph)
    assert 'nothing was saved' not in SENTENCE.lower() and 'card' not in SENTENCE.lower()
    monkeypatch.setitem(resolve_text.TABLES, 'toy', {**resolve_text.TABLES['en'], 'text': {**resolve_text.TABLES['en']['text'], 'turn_lapsed': 'alternate'}})
    monkeypatch.setattr(resolve_text, 'LANGUAGE', 'toy')
    assert graph.say('turn_lapsed') == 'alternate'


def test_cancel_finalise_lapses_without_further_store_writes(engine, monkeypatch):
    """Mutation: back_to_busy_cancel."""
    assistant = graph.Assistant(engine, ScriptedModel([Reply('', [invoice()])]))
    assert assistant.turn('cancel', 'Invoice Acme Corporation for one copper wire')[0]['type'] == 'card'
    original = store.finalise
    captured = []
    def finalise(claim, *args, **kwargs):
        before = snapshot()
        lapse(monkeypatch)
        result = original(claim, *args, **kwargs)
        captured.append((before, snapshot(), result))
        return result
    monkeypatch.setattr(store, 'finalise', finalise)
    events = assistant.turn('cancel', 'cancel')
    assert captured and captured[0][0] == captured[0][1] and captured[0][2] is False
    assert_lapsed(events)


def test_lapse_before_set_aside_renewal(engine, monkeypatch):
    """Mutation: back_to_busy_preset_aside."""
    assistant = graph.Assistant(engine, ScriptedModel([Reply('', [invoice()]), Reply('Okay, cancelled.')]))
    card = assistant.turn('aside', 'Invoice Acme Corporation for one copper wire')[0]
    original = store.renew
    captured = []
    def renew(claim):
        if '_finish_live' in {frame.function for frame in inspect.stack()} and not captured:
            before = snapshot()
            lapse(monkeypatch)
            result = original(claim)
            captured.append((before, snapshot(), result))
            return result
        return original(claim)
    monkeypatch.setattr(store, 'renew', renew)
    events = assistant.turn('aside', 'cancel that')
    assert captured and captured[0][0] == captured[0][1] and captured[0][2] is False
    assert store.open_request('aside')['pending_card'] == card['approval_id']
    assert_lapsed(events)


def test_approval_committed_during_lapsed_correction_is_reconciled(engine, monkeypatch):
    """Mutation: blanket_nothing_saved; emit_card_on_lapse."""
    from erpclaw_core import approvals
    from ownership_helpers import owner_check

    first = graph.Assistant(engine, ScriptedModel([Reply('', [invoice()])]))
    card = first.turn('race', 'Invoice Acme Corporation for one copper wire')[0]
    request = store.open_request('race')
    class RacingModel:
        def complete(self, messages, tools):
            assert approvals.approve(engine, card['approval_id'], card['digest'],
                                     ownership_check=owner_check(engine)).status == 'committed'
            lapse(monkeypatch)
            return Reply('Okay, cancelled.')
    events = graph.Assistant(engine, RacingModel()).turn('race', 'cancel that')
    assert_lapsed(events)
    graph.Assistant(engine, ScriptedModel([Reply('Hello.')])).turn('race', 'hello')
    assert store.get_request(request['id'])['state'] == 'ended'
    with sqlite3.connect(store.path()) as conn:
        assert conn.execute('SELECT status FROM applied_outcome WHERE approval_id = ?',
                            (card['approval_id'],)).fetchone() == ('consumed',)
