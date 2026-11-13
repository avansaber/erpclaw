from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from test_catalogue import find_then
from test_own_books import own_books

FIRST = "hi we need to bill a customer in EUR but our books are in USD. never did foreign currency, we're still moving off our old desktop system. can you walk me through invoicing in euros and handling the exchange gain?"
FOLLOW = "can you do the invoices and payments in another currency thing? I need to bill in EUR but our books are in USD"
SEARCH = "invoices and payments in another currency"
COUNT = "ok forget the bom / work orders stuff for now, can you just do the first part - set up the three separate companies and consolidate the books together?"
EXPLAIN = "i mean three companies — need three separate legal companies set up"
EXPLAIN_AGAIN = "i already told you - the three is for three companies"


def test_foreign_invoice_followups(engine, as_released):
    a = Assistant(engine, ScriptedModel(find_then('none', SEARCH) * 3))
    turns = [a.turn('c1', said) for said in (FIRST, FOLLOW, FOLLOW + ' This is foreign currency.')]
    for events in turns:
        text = ' '.join(e.get('text', '') for e in events)
        assert 'i can draft invoices' in text.lower(), turns
        assert 'one of those' not in text.lower() and 'not in erpclaw yet' not in text.lower(), turns
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
