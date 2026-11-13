"""A payment and its invoice reference are one requested change."""
import pytest
from sqlalchemy import func, select

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_assistant.resolve_args import write_intents
from erpclaw_sales import tables


SINGLE = [
    ("Record a 400.00 payment from Pied Piper on INV-2026-00008.", "Pied Piper", 400, "INV-2026-00008"),
    ("Record 250 from Wayne Enterprises against INV-2026-00007.", "Wayne Enterprises", 250, "INV-2026-00007"),
    ("Pied Piper paid 400 on invoice INV-2026-00008.", "Pied Piper", 400, "INV-2026-00008"),
    ("Record a payment of 400 from Pied Piper for invoice 8.", "Pied Piper", 400, "8"),
    ("Log a 400 payment from Pied Piper on INV-2026-00008 by bank transfer.", "Pied Piper", 400, "INV-2026-00008"),
]


@pytest.mark.parametrize("said,customer,amount,invoice", SINGLE)
def test_single_payment_reaches_payment_resolution(engine, said, customer, amount, invoice):
    with engine.connect() as conn:
        before = [conn.execute(select(func.count()).select_from(t)).scalar_one()
                  for t in (tables.payment, tables.invoice, tables.allocation)]
    model = ScriptedModel([Reply("", [call("record_payment", customer=customer, amount=amount, invoice=invoice)])])
    events = Assistant(engine, model).turn("single-payment", said)
    assert events and events[-1]["type"] in ("card", "ask"), events
    assert all(e.get("code") != "changes_ambiguous" for e in events), events
    if events[-1]["type"] == "card":
        assert events[-1]["card"]["title"] == "Record a customer payment"
        assert dict(events[-1]["card"]["lines"])["Amount received"] == f"{amount:,.2f} USD"
    with engine.connect() as conn:
        assert [conn.execute(select(func.count()).select_from(t)).scalar_one()
                for t in (tables.payment, tables.invoice, tables.allocation)] == before


@pytest.mark.parametrize("said", [
    "Record a 400 payment from Pied Piper and cancel INV-2026-00008.",
    "Record 400 from Pied Piper and invoice Hooli for one website audit.",
])
def test_two_changes_still_ask(engine, said):
    model = ScriptedModel([Reply("", [call("record_payment", customer="Pied Piper", amount=400)])])
    events = Assistant(engine, model).turn("two-changes", said)
    assert len(events) == 1 and events[0]["code"] == "changes_ambiguous", events
    assert write_intents(said) > 1


def test_invoice_number_is_not_the_payment_amount(engine):
    said = "Pied Piper paid 100 against invoice 6"
    model = ScriptedModel([Reply("", [call("record_payment", customer="Pied Piper", amount=6, invoice="6")])])
    events = Assistant(engine, model).turn("amount-decoy", said)
    assert events[-1]["type"] == "ask" and events[-1]["code"] == "amount_missing", events


@pytest.mark.parametrize("said", [
    "Record the payment paid by Pied Piper on invoice 8",
    "Log the payment paid by Pied Piper on invoice 8",
    "Log a 400 payment from Pied Piper on INV-2026-00008 by bank transfer.",
    "Pied Piper paid 400 for invoice 8",
    "Record a payment from Pied Piper invoice 8",
    "Record 250 from Wayne Enterprises against invoice 7",
    "Record a payment of 400 from Pied Piper for invoice 8",
])
def test_payment_words_and_numbered_document_count_once(said):
    assert write_intents(said) == 1


@pytest.mark.parametrize('separator', ['; ', '. ', ' - '])
def test_punctuation_separates_independent_payment_actions(engine, separator):
    said = f'Record a payment from Pied Piper{separator}pay invoice 8'
    model = ScriptedModel([Reply('', [call('record_payment', customer='Pied Piper', invoice='8')])])
    events = Assistant(engine, model).turn('punctuation-two-changes', said)
    assert write_intents(said) > 1
    assert len(events) == 1 and events[0]['code'] == 'changes_ambiguous', events
    assert events[0]['text'] == 'One change at a time, please: send each change as its own message.'


def test_decimal_and_invoice_hyphens_are_not_clause_boundaries():
    assert write_intents('Record a 400.00 payment from Pied Piper on INV-2026-00008.') == 1


@pytest.mark.parametrize('separator', ['; ', '. ', ' - ', ' and ', ' and then '])
@pytest.mark.parametrize('lead', ['', 'please ', 'also ', 'now ', 'can you ', 'could you please ', 'then '])
def test_payment_followed_by_another_action_with_lead_in(engine, separator, lead):
    said = f'Record a payment from Pied Piper{separator}{lead}pay invoice 8'
    model = ScriptedModel([Reply('', [call('record_payment', customer='Pied Piper', invoice='8')])])
    events = Assistant(engine, model).turn('payment-lead-in', said)
    assert len(events) == 1 and events[0]['code'] == 'changes_ambiguous', events
    assert events[0]['text'] == 'One change at a time, please: send each change as its own message.'


@pytest.mark.parametrize('said', [
    'Record a payment from Pied Piper; when you have a moment pay invoice 8',
    'Send Hooli; please invoice 8',
])
def test_other_lead_ins_and_write_verbs_through_turn(engine, said):
    model = ScriptedModel([Reply('', [call('record_payment', customer='Pied Piper')])])
    events = Assistant(engine, model).turn('other-lead-in', said)
    assert len(events) == 1 and events[0]['code'] == 'changes_ambiguous', events


@pytest.mark.parametrize('said,operation,args', [
    ('Record a 400.00 payment from Pied Piper on INV-2026-00008.', 'record_payment',
     {'customer': 'Pied Piper', 'amount': 400, 'invoice': 'INV-2026-00008'}),
    ("Record a payment from Pied Piper; it's for invoice 8.", 'record_payment',
     {'customer': 'Pied Piper', 'invoice': '8'}),
    ('Send Wayne an invoice for 2 audits and submit it.', 'create_invoice',
     {'customer': 'Wayne'}),
    ('Invoice Hooli for 5 copper wire, please.', 'create_invoice', {'customer': 'Hooli'}),
])
def test_single_change_controls_through_turn(engine, said, operation, args):
    model = ScriptedModel([Reply('', [call(operation, **args)])])
    events = Assistant(engine, model).turn('single-change-control', said)
    assert write_intents(said) == 1
    assert events and all(e.get('code') != 'changes_ambiguous' for e in events), events


@pytest.mark.parametrize('said', [
    'Record a payment from Pied Piper; please invoice 8 hours of consulting to Hooli',
    'Record a payment from Pied Piper and then please invoice 8 hours of consulting to Hooli',
    'Record a payment from Pied Piper. please invoice 8 hours of consulting to Hooli',
    'Record a payment from Pied Piper; invoice Hooli for 8 hours of consulting',
])
def test_numbered_invoice_action_after_payment_through_turn(engine, said):
    model = ScriptedModel([Reply('', [call('record_payment', customer='Pied Piper')])])
    events = Assistant(engine, model).turn('numbered-invoice-action', said)
    assert len(events) == 1 and events[0]['code'] == 'changes_ambiguous', events


@pytest.mark.parametrize('said', [
    'Hooli paid invoice 8.',
    'Hooli paid invoice INV-2026-00008.',
    'Pied Piper pays invoice 8.',
    'Settle invoice 8 for Pied Piper.',
    'Pay invoice 8 from Pied Piper.',
    'Pied Piper paid 400 on invoice 8.',
    'Record a 400.00 payment from Pied Piper on INV-2026-00008.',
    "Record a payment from Pied Piper; it's for invoice 8.",
    'Record a payment from Pied Piper against invoice 8.',
    'Pied Piper paid 400 on invoice 8.',
])
def test_payment_reference_controls_through_turn(engine, said):
    model = ScriptedModel([Reply('', [call('record_payment', customer='Pied Piper')])])
    events = Assistant(engine, model).turn('payment-reference-control', said)
    assert events and all(e.get('code') != 'changes_ambiguous' for e in events), events
