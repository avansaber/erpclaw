"""Invoice fields must never silently change the owner's stated money or date."""
from decimal import Decimal

from sqlalchemy import func, select
import pytest

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import models as m
from erpclaw_sales import tables as sales


def _invoice():
    return call("create_invoice", customer="Acme Corporation", items=[{"item": "copper wire", "quantity": 4}])


def _counts(engine):
    with engine.connect() as conn:
        return tuple(conn.execute(select(func.count()).select_from(t)).scalar_one()
                     for t in (m.approval, sales.invoice, m.posting))


def _safe(events):
    return bool(events) and events[-1]["type"] in {"ask", "refused"} and not any(
        e["type"] == "card" for e in events)



def _check_omission_path(engine, text, omission, require_omission=False):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply('', [_invoice()])] * 4))

    def turn(answer):
        events = a.turn('c1', answer)
        assert _safe(events), events
        assert _counts(engine) == before, events
        return events

    events = turn(text)
    if events[-1].get('code') == 'item_ambiguous':
        events = turn(events[-1]['choices'][0]['label'])
    choices = [choice['label'] for choice in events[-1].get('choices', [])
               if 'leave out' in choice['label'].lower()
               and omission in choice['label'].lower()]
    if require_omission:
        assert choices, events
    if choices:
        events = turn(choices[0])
    assert events[-1]['type'] == 'refused' or (
        events[-1]['type'] == 'ask' and events[-1].get('code') in {
            'rate_choice_missing', 'date_unknown', 'date_ambiguous', 'date_which', 'date_choice_missing'
        }
    ), events


def test_purchase_order_omission_preserves_rate(engine):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply('', [_invoice()])] * 5))
    events = a.turn('c1', 'Invoice Acme Corporation for 4 copper wire, PO 123, 30 each')
    if events[-1].get('code') == 'item_ambiguous':
        assert _counts(engine) == before
        events = a.turn('c1', events[-1]['choices'][0]['label'])
    if events[-1].get('code') == 'memo_choice_missing':
        assert _counts(engine) == before
        choice = next(c['label'] for c in events[-1]['choices'] if c['label'].lower().startswith('leave out'))
        events = a.turn('c1', choice)
        assert any(e['type'] == 'not_done' and e.get('part') == 'memo' for e in events), events
    card = next(e['card'] for e in events if e['type'] == 'card')
    assert any('@ 30.00 = 120.00' in v for k, v in card['lines'] if k == 'Item')
    assert _counts(engine)[1:] == before[1:]


def test_foreign_rate_is_not_dropped(engine):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])]))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire at 28 each in euros")
    assert _safe(events) and events[-1]["code"] in {"foreign_currency", "rate_choice_missing"}, events
    assert _counts(engine) == before


def test_dollar_rate_is_not_dropped_in_euro_books():
    from conftest import drop_everything, empty_books
    from erpclaw_core.common import company_ctx
    from erpclaw_core.masters import create_item, create_party
    from erpclaw_core.setup import create_company

    eng = empty_books()
    try:
        create_company(eng, "Euro Trading", "EUR")
        with eng.begin() as conn:
            ctx = company_ctx(conn)
            create_party(ctx, "customer", "Acme Corporation", "acme@example.com", "Springfield")
            create_item(ctx, "Copper Wire", Decimal("28"))
        before = _counts(eng)
        a = Assistant(eng, ScriptedModel([Reply("", [_invoice()])]))
        events = a.turn("c1", "Invoice Acme Corporation for 4 Copper Wire at 28 each in dollars")
        assert _safe(events) and events[-1]["code"] == "foreign_currency", events
        assert events[-1]["text"] == ("Your books are in EUR, so a payment in dollars can't be recorded: the exchange "
                                      "difference is not something ERPClaw can book yet.")
        assert _counts(eng) == before
    finally:
        drop_everything(eng)
        eng.dispose()


@pytest.mark.parametrize("rate", ["at 28 euros each", "at 28 EUR each", "at 28 each €", "at EUR 28 each",
                                  "EUR28each", "€28each", "at 28 each with 7 mentioned in euros"])
@pytest.mark.parametrize("model_rate", [False, True])
def test_foreign_rate_forms_never_preview_in_books_currency(engine, rate, model_rate):
    before = _counts(engine)
    line = {"item": "copper wire", "quantity": 4}
    if model_rate:
        line["rate"] = 28
    action = call("create_invoice", customer="Acme Corporation", items=[line])
    a = Assistant(engine, ScriptedModel([Reply("", [action])] * 2))
    events = a.turn("c1", f"Invoice Acme Corporation for 4 Copper Wire {rate}")
    if events[-1].get("code") == "item_ambiguous":
        events = a.turn("c1", events[-1]["choices"][0]["label"])
    assert _safe(events) and events[-1]["code"] in {"foreign_currency", "rate_choice_missing"}, events
    assert _counts(engine) == before


@pytest.mark.parametrize("extra", ["", ', memo "shipping details"'])
def test_foreign_rate_after_purchase_order_never_previews(engine, extra):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 2))
    events = a.turn("c1", "Invoice Acme Corporation for 4 Copper Wire, PO 123, 30 each in euros" + extra)
    if events[-1].get("code") == "item_ambiguous":
        events = a.turn("c1", events[-1]["choices"][0]["label"])
    assert _safe(events) and events[-1]["code"] in {"foreign_currency", "rate_choice_missing"}, events
    assert _counts(engine) == before


def test_negated_date_cannot_replace_a_stated_date(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()]), Reply("", [_invoice()])]))
    a.turn("c1", "Invoice Acme Corporation for 4 copper wire dated yesterday")
    before = _counts(engine)
    events = a.turn("c1", "not today")
    assert _safe(events), events
    assert _counts(engine) == before


def test_future_invoice_day_is_asked(engine):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])]))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire dated tomorrow")
    assert len(events) == 1 and events[0]["type"] == "ask" and events[0]["code"] == "date_choice_missing", events
    assert events[0]["text"] == "An invoice can't be dated in the future. Shall I date it today, or leave it?"
    assert [c["label"] for c in events[0]["choices"]] == ["Date it today", "Leave it"]
    assert _counts(engine) == before


@pytest.mark.parametrize("answer", ["Date it today", "Leave it"])
def test_future_invoice_choice_can_be_answered(engine, answer):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 2))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire dated tomorrow")
    assert len(events) == 1 and events[0]["code"] == "date_choice_missing", events
    choice = next(c for c in events[0]["choices"] if c["label"] == answer)
    events = a.turn("c1", answer, choice=choice["code"])
    if answer == "Date it today":
        assert any(e["type"] == "card" for e in events), events
    else:
        assert len(events) == 1 and events[0]["type"] == "answer", events
        assert not any(e["type"] == "card" for e in events)


def test_future_submit_invoice_date_is_asked(engine):
    action = call("submit_invoice", invoice="INV-2026-00008")
    a = Assistant(engine, ScriptedModel([Reply("", [action])]))
    events = a.turn("c1", "Submit invoice INV-2026-00008 dated tomorrow")
    assert len(events) == 1 and events[0]["type"] == "ask", events
    assert events[0]["code"] == "date_choice_missing", events
    assert events[0]["text"] == "An invoice can't be dated in the future. Shall I date it today, or leave it?"
    assert [c["label"] for c in events[0]["choices"]] == ["Date it today", "Leave it"]


@pytest.mark.parametrize("extra", ["", ', memo "check the date"'])
def test_month_end_after_a_purchase_order_never_defaults_to_today(engine, extra):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 5))
    events = a.turn("c1", "Invoice Acme Corporation for 4 Copper Wire, PO 123, dated end of month" + extra)
    if events[0].get("code") == "item_ambiguous":
        events = a.turn("c1", events[0]["choices"][0]["label"])
    assert events[0]["code"] == "memo_date_choice_missing", events
    choice = next(c for c in events[0]["choices"] if c["label"] == "The invoice date")
    events = a.turn("c1", "The invoice date", choice=choice["code"])
    if events[0].get("code") == "item_ambiguous":
        events = a.turn("c1", events[0]["choices"][0]["label"])
    assert len(events) == 1 and events[0]["type"] == "ask" and events[0]["code"] == "date_choice_missing", events
    assert events[0]["text"] == "An invoice can't be dated in the future. Shall I date it today, or leave it?"
    assert [c["label"] for c in events[0]["choices"]] == ["Date it today", "Leave it"]
    assert _counts(engine) == before


def test_past_invoice_day_is_not_silently_defaulted(engine):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])]))
    events = a.turn("c1", "Invoice Acme Corporation for 4 Copper Wire dated yesterday")
    assert _safe(events), events
    assert _counts(engine) == before


def test_memo_content_does_not_create_a_rate_question(engine):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])]))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire, memo 30 each")
    assert _safe(events), events
    assert _counts(engine) == before


@pytest.mark.parametrize("memo", ['"tomorrow"', '"not today"'])
def test_memo_day_does_not_create_an_invoice_date(engine, memo):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])]))
    events = a.turn("c1", f"Invoice Acme Corporation for 4 copper wire, memo {memo}")
    assert len(events) == 1 and events[0]["code"] == "memo_choice_missing", events
    assert _counts(engine) == before


def test_unclosed_memo_quote_does_not_hide_a_later_message_date():
    from erpclaw_assistant.resolve_policy import invoice_date_text
    from erpclaw_assistant.resolve_text import MESSAGE_BREAK

    text = f'memo "tomorrow {MESSAGE_BREAK} dated 2026-11-10"'
    assert "2026-11-10" in invoice_date_text(text)


def test_quoted_memo_does_not_become_a_discount_instruction(engine):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])]))
    events = a.turn("c1", 'Invoice Acme Corporation for 4 Copper Wire, memo "discount 50%"')
    assert not any(e["type"] == "card" for e in events), events
    assert any(e.get("capability") == "planned.discounts" for e in events), events
    assert _counts(engine) == before


def test_memo_email_words_do_not_request_email(engine):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])]))
    events = a.turn("c1", 'Invoice Acme Corporation for 4 Copper Wire, memo "send email tomorrow"')
    assert _safe(events), events
    assert _counts(engine) == before


def test_money_guard_keeps_tax_outside_a_quoted_memo(engine):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])]))
    events = a.turn("c1", 'Invoice Acme Corporation for 4 Copper Wire, memo "discount 50%"; add tax')
    assert any(e.get("capability") in {"planned.tax_engine", "planned.discounts"} for e in events), events
    assert not any(e["type"] == "card" for e in events)
    assert _counts(engine) == before


@pytest.mark.parametrize("memo,terms", [
    ('memo "spring order"', '30 each'),
    ('memo "spring order"', 'dated 3 Nov'),
    ('memo "spring order"', '30 each, dated 3 Nov'),
    ('memo spring order', '30 each'),
], ids=['quoted-rate', 'quoted-date', 'quoted-both', 'unquoted-rate'])
def test_terms_after_memo_survive_omission(engine, memo, terms):
    _check_omission_path(engine, f'Invoice Acme Corporation for 4 copper wire, {memo}, {terms}', 'memo')


@pytest.mark.parametrize('opening,closing,body,terms', [
    ('"', '"', 'spring order. memo: rush', '30 each'),
    ('"', '"', 'spring order. memo: rush', 'dated 3 Nov'),
    ('"', '"', 'note 99 each. memo: rush', '30 each, dated 3 Nov'),
    ("'", "'", 'reference 99 each. memo: rush', '30 each'),
    ("'", "'", 'rush, dated tomorrow. note: 99', 'dated 3 Nov'),
    ("'", "'", 'order 99. memo: dated tomorrow', '30 each, dated 3 Nov'),
    ('“', '”', 'note 99 each. memo: rush', '30 each'),
    ('“', '”', 'rush, dated tomorrow. memo: 99', 'dated 3 Nov'),
    ('“', '”', 'order 99. note: dated tomorrow', '30 each, dated 3 Nov'),
    ('‘', '’', 'reference 99 each. memo: rush', '30 each'),
    ('‘', '’', 'rush, dated tomorrow. note: 99', 'dated 3 Nov'),
    ('‘', '’', 'order 99. memo: dated tomorrow', '30 each, dated 3 Nov'),
], ids=['round2-rate', 'round2-date', 'double-both', 'single-rate', 'single-date', 'single-both',
        'curly-double-rate', 'curly-double-date', 'curly-double-both',
        'curly-single-rate', 'curly-single-date', 'curly-single-both'])
def test_quoted_memo_inner_cues_do_not_hide_terms_on_omission(engine, opening, closing, body, terms):
    _check_omission_path(
        engine, f'Invoice Acme Corporation for 4 copper wire, memo {opening}{body}{closing}, {terms}', 'memo'
    )


@pytest.mark.parametrize('opening', ['"', "'", '“', '‘'])
def test_unbalanced_memo_never_hides_terms_on_omission(engine, opening):
    _check_omission_path(
        engine, f'Invoice Acme Corporation for 4 copper wire, memo {opening}spring order, 30 each, dated 3 Nov',
        'memo',
    )


def test_apostrophe_in_memo_does_not_hide_rate_after_omission(engine):
    _check_omission_path(
        engine, "Invoice Acme Corporation for 4 copper wire, memo 'Bob's order. memo: rush', 30 each", 'memo'
    )


def test_memo_email_copy_never_queues_email(engine):
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(m.outbox)).scalar_one()
    a = Assistant(engine, ScriptedModel([Reply('', [_invoice()])]))
    events = a.turn('c1', 'Invoice Acme Corporation for 4 copper wire, memo: email the copy')
    assert _safe(events) or any(e.get('capability') == 'planned.email' for e in events), events
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.outbox)).scalar_one() == before


def test_unquoted_memo_comma_date_requires_clarification(engine):
    a = Assistant(engine, ScriptedModel([Reply('', [_invoice()])] * 4))
    events = a.turn('c1', 'Invoice Acme Corporation for 4 copper wire, memo ship Monday, deliver tomorrow')
    assert len(events) == 1 and events[0].get('code') == 'memo_date_choice_missing', events
    assert [choice['label'] for choice in events[0]['choices']] == ['Part of the memo', 'The invoice date']
    assert not any(event['type'] == 'card' for event in events)


def _pick(a, events, label):
    choice = next(c for c in events[0]["choices"] if c["label"] == label)
    return a.turn("c1", label, choice=choice["code"])


@pytest.mark.parametrize("memo", ["ship Monday, deliver tomorrow", "tomorrow"])
def test_unquoted_memo_date_choice_preserves_existing_memo_flow(engine, memo):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 5))
    events = a.turn("c1", f"Invoice Acme Corporation for 4 copper wire, memo {memo}")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_date_choice_missing")], events
    events = _pick(a, events, "Part of the memo")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_choice_missing")], events
    events = _pick(a, events, "Leave out the memo")
    assert any(e["type"] == "not_done" and e.get("part") == "memo" and
               e.get("capability") == "planned.invoice_fields" for e in events), events
    assert next(e for e in events if e["type"] == "card")["card"]["date_source"] == {"default": True}, events
    assert not any("memo" in str(e).lower() and "stored" in str(e).lower() for e in events), events


def test_unquoted_memo_date_choice_applies_future_date_rules(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 5))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire, memo ship Monday, deliver tomorrow")
    events = _pick(a, events, "The invoice date")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "date_choice_missing")], events
    assert events[0]["text"] == "An invoice can't be dated in the future. Shall I date it today, or leave it?"
    assert [c["label"] for c in events[0]["choices"]] == ["Date it today", "Leave it"]


def test_future_date_before_unquoted_memo_keeps_date_question_first(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 4))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire tomorrow, memo ship Monday")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "date_choice_missing")], events
    assert events[0]["text"] == "An invoice can't be dated in the future. Shall I date it today, or leave it?"
    assert [c["label"] for c in events[0]["choices"]] == ["Date it today", "Leave it"]


@pytest.mark.parametrize("answer, expected", [("Date it today", ("ask", "memo_choice_missing")),
                                             ("Leave it", ("answer", None))])
def test_future_memo_date_is_asked_after_prior_future_date_answer(engine, answer, expected):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 5))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire tomorrow, memo next Monday")
    for label, question_code in [("Date it today", "date_choice_missing"),
                                 ("The invoice date", "memo_date_choice_missing")]:
        assert [(e["type"], e.get("code")) for e in events] == [("ask", question_code)], events
        assert _counts(engine) == before
        events = _pick(a, events, label)
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "date_choice_missing")], events
    assert events[0]["text"] == "An invoice can't be dated in the future. Shall I date it today, or leave it?"
    assert [c["label"] for c in events[0]["choices"]] == ["Date it today", "Leave it"]
    assert _counts(engine) == before
    events = _pick(a, events, answer)
    assert [(e["type"], e.get("code")) for e in events] == [expected], events
    assert _counts(engine) == before


def test_quoted_memo_date_never_prompts_as_date(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 4))
    events = a.turn("c1", 'Invoice Acme Corporation for 4 copper wire, memo "ship Monday, deliver tomorrow"')
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_choice_missing")], events


def test_unquoted_memo_date_after_sentence_boundary_is_asked(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 4))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire, memo ship Monday. Deliver tomorrow")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_date_choice_missing")], events


def test_today_before_memo_does_not_hide_the_memo_date_question(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 4))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire today, memo tomorrow")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_date_choice_missing")], events


def test_quoted_memo_after_unquoted_memo_does_not_supply_a_date(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 4))
    events = a.turn("c1", 'Invoice Acme Corporation for 4 copper wire, memo shipment and note "deliver tomorrow"')
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_choice_missing")], events


def test_unquoted_memo_figure_date_remains_a_whole_date(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 4))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire, memo shipment, dated 03.10.2026")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_date_choice_missing")], events
    assert "03.10.2026" in events[0]["text"], events


def test_quoted_memo_with_inner_cue_never_supplies_a_date(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 4))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire, memo “rush and note tomorrow”")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_choice_missing")], events


def test_unquoted_memo_followed_by_date_instruction_requires_a_choice(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 4))
    events = a.turn("c1", "Invoice Acme Corporation for 4 copper wire, memo ship Monday, date it 3 October")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "memo_date_choice_missing")], events
    assert "3 October" in events[0]["text"], events
    events = _pick(a, events, "The invoice date")
    assert [(e["type"], e.get("code")) for e in events] == [("ask", "date_choice_missing")], events


def test_purchase_order_cannot_consume_a_later_rate(engine):
    before = _counts(engine)
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])] * 3))
    first = a.turn("c1", "Invoice Acme Corporation for 4 copper wire, PO 123, 30 each")
    if first[-1].get("code") == "item_ambiguous":
        first = a.turn("c1", first[-1]["choices"][0]["label"])
    assert first and not any(e["type"] == "refused" for e in first), first
    choices = first[-1].get("choices", [])
    omit = next((c["label"] for c in choices if c["label"].lower().startswith("leave out")), None)
    events = a.turn("c1", omit) if omit else first
    card = next((e["card"] for e in events if e["type"] == "card"), None)
    assert card is not None, events
    item_lines = [v for k, v in card["lines"] if k == "Item"]
    assert len(item_lines) == 1 and "4 x " in item_lines[0] and "@ 30.00 = 120.00" in item_lines[0], item_lines
    assert [v for k, v in card["lines"] if k == "Total"] == ["120.00 USD"]
    after = _counts(engine)
    assert after[1:] == before[1:]


@pytest.mark.parametrize("delivery", ["2026-11-30", "3 November 2026"])
def test_numeric_delivery_date_does_not_hide_a_past_invoice_date(engine, delivery):
    a = Assistant(engine, ScriptedModel([Reply("", [_invoice()])]))
    events = a.turn("c1", f"Invoice Acme Corporation for 4 copper wire dated 3 October 2026, to be delivered by {delivery}")
    assert not any(e["type"] == "card" for e in events), events
    assert events[-1].get("code") == "date_choice_missing" or any(
        e.get("part") == "date" and e.get("type") == "not_done" for e in events), events
