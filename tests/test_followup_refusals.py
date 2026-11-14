from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import catalogue as cat
from erpclaw_core.plugins import REGISTRY
from test_catalogue import find_then
from test_own_books import own_books

FIRST = "hi we need to bill a customer in EUR but our books are in USD. never did foreign currency, we're still moving off our old desktop system. can you walk me through invoicing in euros and handling the exchange gain?"
FOLLOW = "can you do the invoices and payments in another currency thing? I need to bill in EUR but our books are in USD"
SEARCH = "invoices and payments in another currency"
COUNT = "ok forget the bom / work orders stuff for now, can you just do the first part - set up the three separate companies and consolidate the books together?"
EXPLAIN = "i mean three companies — need three separate legal companies set up"
EXPLAIN_AGAIN = "i already told you - the three is for three companies"


def test_foreign_invoice_followups(engine, as_released):
    a = Assistant(engine, ScriptedModel(find_then('none', SEARCH) * 2))
    turns = [a.turn('c1', said) for said in (FIRST, FOLLOW)]
    for events in turns:
        text = ' '.join(e.get('text', '') for e in events)
        assert 'i can draft invoices' in text.lower() and 'payment' in text.lower(), turns
        assert 'another currency' in text.lower() and 'not in erpclaw yet' in text.lower(), turns
        assert events[0]['capability'] == 'unsupported.no_match'


def test_company_count_explanation(engine):
    own_books(engine)
    reply = Reply('', [call('set_up_company')])
    a = Assistant(engine, ScriptedModel([reply, reply, reply]))
    turns = [a.turn('c1', said) for said in (COUNT, EXPLAIN, EXPLAIN_AGAIN)]
    for events in turns:
        text = ' '.join(e.get('text', '') for e in events)
        assert 'one at a time' in text.lower(), turns
        assert not any(e.get('code') == 'number_ambiguous' for e in events), turns
        assert sum(e.get('type') == 'ask' for e in events) <= 1, turns


def test_customer_record_count_keeps_other_figure_unplaced(engine, as_released):
    a = Assistant(engine, ScriptedModel([Reply('', [call('add_customer')])]))
    events = a.turn('c1', 'add three customers and what about 500?')
    assert any(e.get('code') == 'number_ambiguous' and 'three' in e.get('text', '').lower() for e in events), events


def test_shared_invoice_word_does_not_name_planned_list(engine, as_released):
    said = 'do invoices and bill a customer'
    a = Assistant(engine, ScriptedModel(find_then('none', SEARCH)))
    events = a.turn('shared', said)
    assert events[0]['capability'] == 'unsupported.no_match'
    assert events[0]['text'] == "I didn't do anything with that. I can draft invoices."


def test_followup_selects_unavailable_entry_named_by_owner(engine, as_released, monkeypatch):
    find = cat.find_capability

    def invoice_list_first(conn, request):
        found = find(conn, request)
        entries = {e.id: e for e in cat.catalogue(conn)}
        return [entries['op.list_invoices']] + [e for e in found if e.id != 'op.list_invoices']

    monkeypatch.setattr(cat, 'find_capability', invoice_list_first)
    a = Assistant(engine, ScriptedModel(find_then('none', SEARCH) * 2))
    a.turn('c1', FIRST)
    events = a.turn('c1', FOLLOW + ' This is foreign currency.')
    text = ' '.join(e.get('text', '') for e in events).lower()
    assert events[0]['capability'] == 'unsupported.no_match'
    assert 'another currency' in text
    assert 'invoice list is not' not in text


def test_unrelated_searched_entry_is_not_named(engine, as_released, monkeypatch):
    find = cat.find_capability

    def invoice_list_first(conn, request):
        found = find(conn, request)
        entries = {e.id: e for e in cat.catalogue(conn)}
        return [entries['op.list_invoices']] + [e for e in found if e.id != 'op.list_invoices']

    monkeypatch.setattr(cat, 'find_capability', invoice_list_first)
    said = 'charge Acme Corporation for 5 copper wire'
    a = Assistant(engine, ScriptedModel(find_then('none', said)))
    events = a.turn('c1', said)
    assert events[0]['capability'] == 'unsupported.no_match'
    assert events[0]['text'] == "I didn't do anything with that. I can draft invoices."


def test_compound_company_count_is_not_partially_blanked(engine):
    own_books(engine)
    a = Assistant(engine, ScriptedModel([Reply('', [call('set_up_company')])]))
    events = a.turn('c1', 'set up twenty three separate companies')
    assert not any(e.get('code') in ('number_ambiguous', 'figure_ambiguous') for e in events), events
    assert any('one at a time' in e.get('text', '').lower() for e in events), events


def test_cash_account_count_matches_accounting_with_blanking_off(engine, as_released, monkeypatch):
    from erpclaw_assistant import graph

    said = 'add two cash accounts with 500 each'
    reply = Reply('', [call('add_cash_account')])
    seen = []
    account = graph.account

    def capture(ctx, text, *args, **kwargs):
        result = account(ctx, text, *args, **kwargs)
        seen.append(text)
        return result

    monkeypatch.setattr(graph, 'account', capture)
    actual = Assistant(engine, ScriptedModel([reply])).turn('actual', said)
    with monkeypatch.context() as patch:
        patch.setattr(graph, '_record_count_spans', lambda text, operation, **kwargs: [])
        baseline = Assistant(engine, ScriptedModel([reply])).turn('baseline', said)
    assert seen == [said, said]
    assert [e['type'] for e in actual] == [e['type'] for e in baseline]
    assert actual[-1]['card']['lines'] == baseline[-1]['card']['lines']


def test_none_names_planned_operation_with_unshared_evidence(engine, as_released):
    said = 'bill a customer and credit note'
    a = Assistant(engine, ScriptedModel(find_then('none', said)))
    events = a.turn('planned-operation', said)
    assert events[0]['capability'] == 'unsupported.no_match'
    assert 'i can draft invoices' in events[0]['text'].lower()
    assert 'credit note' in events[0]['text'].lower()
    assert 'not in erpclaw yet' in events[0]['text'].lower()


def test_none_ignores_plural_shared_with_installed_singular(engine, as_released):
    said = 'invoices and bill a customer'
    a = Assistant(engine, ScriptedModel(find_then('none', SEARCH)))
    events = a.turn('shared-plural', said)
    assert events[0]['capability'] == 'unsupported.no_match'
    assert events[0]['text'] == "I didn't do anything with that. I can draft invoices."


def test_unshared_evidence_uses_scores_and_rejects_ties():
    installed = cat.Entry('op.test', 'operation', '', '', ('invoice',), 'installed')
    first = cat.Entry('planned.first', 'area', '', '', ('invoices', 'foreign currency'), 'planned')
    second = cat.Entry('planned.second', 'area', '', '', ('exchange gain',), 'planned')
    said = 'invoices foreign currency exchange gain'
    assert cat.unshared_unavailable([(installed, 2), (first, 5), (second, 3)], said) == first
    assert cat.unshared_unavailable([(installed, 2), (first, 5), (second, 5)], said) is None


def test_unshared_evidence_folds_other_plural_forms():
    installed = cat.Entry('op.test', 'operation', '', '', ('tax', 'currency'), 'installed')
    planned = cat.Entry('planned.test', 'area', '', '', ('taxes', 'currencies'), 'planned')
    assert cat.unshared_unavailable([(installed, 2), (planned, 4)], 'taxes currencies') is None


def test_unshared_evidence_compares_whole_phrases():
    planned = cat.Entry('planned.test', 'area', '', '', ('foreign currency',), 'planned')
    word = cat.Entry('op.word', 'operation', '', '', ('currency',), 'installed')
    phrase = cat.Entry('op.phrase', 'operation', '', '', ('foreign currencies',), 'installed')
    said = 'foreign currency'
    assert cat.unshared_unavailable([(word, 2), (planned, 2)], said) == planned
    assert cat.unshared_unavailable([(phrase, 3), (planned, 2)], said) is None


def test_unshared_evidence_includes_intent_binding_trigger(engine, as_released):
    said = 'reverse supplier payment to Acme and bill a customer'
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
    bound = next(entry for entry, _ in named if entry.id == 'op.reverse_supplier_payment')
    assert bound.overrides and not bound.synonyms
    assert cat.unshared_unavailable([(entry, score) for entry, score in named
                                     if entry.id in ('op.create_invoice', bound.id)], said) == bound


def test_none_keeps_qualified_payment_phrase(engine, monkeypatch):
    monkeypatch.setattr(REGISTRY.contracts['reverse_supplier_payment'], 'planned', 'later')
    said = 'reverse payment to Acme and show receivables outstanding money owed'
    with engine.connect() as conn:
        named = cat.named_by_synonym(conn, said)
    installed = [entry for entry, _ in named if entry.id == 'op.record_payment']
    reversal = next(entry for entry, _ in named if entry.id == 'op.reverse_supplier_payment')
    assert installed and installed[0].state == 'installed'
    assert reversal.state == 'planned' and reversal.overrides
    reply = cat.decide([reversal], reversal.id).text
    events = Assistant(engine, ScriptedModel(find_then('none', SEARCH))).turn('payment-phrase', said)
    assert events[0]['capability'] == 'unsupported.no_match'
    assert reply in events[0]['text']
