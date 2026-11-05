"""Every date expression the owner writes is read as a date or asked about, never dropped: an invoice (dated today)
says so beside its card, and a payment's date is the day the owner meant, or the engine asks or refuses."""
from __future__ import annotations

import pytest

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call

TODAY = "26 Oct 2026"  # the suite's pinned day (a Monday)
OTHER_DAY = ["tomorrow", "yesterday", "Friday", "on Friday", "last week", "next week", "end of month", "EOM",
             "in 2 days", "the 5th", "on the 5th"]
SAME_DAY = ["tonight", "this morning"]
INVOICE = call("create_invoice", customer="Hooli", items=[{"item": "copper wire", "quantity": 5}])
PAYMENT = call("record_payment", customer="Hooli", amount=100)


def kinds(events) -> list[str]:
    return [e["type"] for e in events]


def turn_answering_item_confirms(engine, cid: str, said: str, reply: Reply) -> list[dict]:
    """One request, with any single-candidate confirm answered ("Do you mean Copper Wire?", "Do you mean Hooli?"),
    so the date is what decides."""
    a = Assistant(engine, ScriptedModel([reply] * 4))
    ev = a.turn(cid, said)
    for _ in range(3):
        choices = ev[0].get("data", {}).get("choices", []) if kinds(ev) == ["ask"] else []
        if not (ev and ev[0].get("code") in ("item_ambiguous", "customer_ambiguous") and len(choices) == 2
                and choices[1]["label"] == "someone else"):
            return ev
        ev = a.turn(cid, choices[0]["label"], choice=choices[0]["code"])  # the owner answers by its code
    return ev


def placements(phrase: str) -> list[str]:
    return [f"{phrase}, invoice Hooli for 5 copper wire", f"invoice Hooli for 5 copper wire {phrase}"]


def date_said_or_asked(ev) -> bool:
    return kinds(ev) == ["ask"] or any(e["type"] == "not_done" and e.get("part") == "date" for e in ev)


@pytest.mark.parametrize("phrase", OTHER_DAY)
def test_an_invoice_with_another_day_written_says_it_is_dated_today_or_asks(engine, phrase):
    wrong = []
    for k, said in enumerate(placements(phrase)):
        ev = turn_answering_item_confirms(engine, f"i{k}", said, Reply("", [INVOICE]))
        if not date_said_or_asked(ev):
            wrong.append((said, [(e["type"], e.get("code") or e.get("part")) for e in ev]))
    assert not wrong, wrong


@pytest.mark.parametrize("phrase", OTHER_DAY)
def test_another_day_alone_in_a_second_message_is_never_dropped(engine, phrase):
    a = Assistant(engine, ScriptedModel([Reply("", [INVOICE]), Reply("", [INVOICE])]))
    assert kinds(a.turn("s", "invoice Hooli for 5 copper wire")) == ["card"]
    ev = a.turn("s", phrase)
    assert date_said_or_asked(ev) or "card" not in kinds(ev), ev


@pytest.mark.parametrize("phrase", OTHER_DAY)
def test_a_payment_is_never_dated_today_for_another_day(engine, phrase):
    wrong = []
    for k, said in enumerate([f"{phrase}, Hooli paid 100", f"Hooli paid 100 {phrase}"]):
        ev = turn_answering_item_confirms(engine, f"p{k}", said, Reply("", [PAYMENT]))
        if kinds(ev) == ["card"]:
            dated = next(v for kk, v in ev[0]["card"]["lines"] if kk == "Date")
            if dated == f"Received {TODAY}":
                wrong.append((said, dated))
    assert not wrong, wrong


@pytest.mark.parametrize("phrase", SAME_DAY)
def test_a_same_day_word_restates_today(engine, phrase):
    ev = turn_answering_item_confirms(engine, "t", f"Hooli paid 100 {phrase}", Reply("", [PAYMENT]))
    assert kinds(ev) == ["ask"] or (kinds(ev) == ["card"] and
                                    next(v for k, v in ev[0]["card"]["lines"] if k == "Date") == f"Received {TODAY}"), ev


def test_a_date_like_word_the_table_does_not_know_asks(engine):
    """The fallback: a date-like word no rule reads is asked about, never dropped."""
    for k, said in enumerate(["invoice Hooli for 5 copper wire fortnightly", "Hooli paid 100 the day after"]):
        reply = Reply("", [INVOICE if said.startswith("invoice") else PAYMENT])
        ev = turn_answering_item_confirms(engine, f"u{k}", said, reply)
        assert kinds(ev) == ["ask"] or date_said_or_asked(ev), (said, ev)


# ---------- exact readings, the longest phrase first; everything else asks, on every operation ----------

READ = [("yesterday", "25 Oct 2026"), ("last night", "25 Oct 2026"), ("day before yesterday", "24 Oct 2026"),
        ("the day before yesterday", "24 Oct 2026"), ("Friday", "23 Oct 2026"), ("on Friday", "23 Oct 2026"),
        ("tonight", TODAY), ("this morning", TODAY), ("this afternoon", TODAY)]
FUTURE = ["tomorrow", "day after tomorrow", "EOM", "end of month"]  # read, and after today
UNREAD = ["next week", "last week", "next quarter", "at month end", "month-end", "EOY", "next business day",
          "in a week", "fortnightly", "the day after", "weekend", "end of the week", "mid-month", "EOD",
          "end of year", "last quarter", "in a month"]


@pytest.mark.parametrize("phrase, day", READ)
def test_a_payment_is_dated_exactly_the_day_the_owner_wrote(engine, phrase, day):
    wrong = []
    for k, said in enumerate([f"{phrase}, Hooli paid 100", f"Hooli paid 100 {phrase}"]):
        ev = turn_answering_item_confirms(engine, f"x{k}", said, Reply("", [PAYMENT]))
        got = next((v for kk, v in ev[0]["card"]["lines"] if kk == "Date"), None) if kinds(ev) == ["card"] else ev
        figure_next_to_amount = phrase[0].isdigit() and said.endswith(phrase)  # "paid 100 3 days ago": asked, safe
        if got != f"Received {day}" and not (figure_next_to_amount and kinds(ev) == ["ask"]):
            wrong.append((said, got))
    assert not wrong, wrong


@pytest.mark.parametrize("phrase", FUTURE)
def test_a_payment_dated_after_today_is_never_carded(engine, phrase):
    for k, said in enumerate([f"{phrase}, Hooli paid 100", f"Hooli paid 100 {phrase}"]):
        ev = turn_answering_item_confirms(engine, f"f{k}", said, Reply("", [PAYMENT]))
        assert "card" not in kinds(ev), (said, ev)


def _reversal(engine):
    from decimal import Decimal

    from sqlalchemy import select

    from erpclaw_core import models as m
    from erpclaw_core.common import company_ctx
    from erpclaw_sales import service as s
    from erpclaw_sales import tables as t

    with engine.begin() as conn:
        ctx = company_ctx(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == "Hooli")).mappings().one())
        r = s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal("100"), ctx.today, oldest_first=True))
        return conn.execute(select(t.payment.c.number).where(t.payment.c.id == r["id"])).scalar_one()


@pytest.mark.parametrize("phrase", UNREAD)
def test_an_unread_date_asks_on_every_operation(engine, phrase):
    number = _reversal(engine)
    cases = [  # the owner's words with the date before and after, and the model's call (no date)
        ("Hooli paid 100", PAYMENT),
        (f"reverse {number}", call("reverse_payment", payment=number)),
        ("submit INV-2026-00008", call("submit_invoice", invoice="INV-2026-00008")),
        ("cancel INV-2026-00003", call("cancel_invoice", invoice="INV-2026-00003")),
        ("invoice Hooli for 5 copper wire", INVOICE),
    ]
    wrong = []
    for k, (said, model_call) in enumerate(cases):
        for j, text in enumerate([f"{phrase}, {said}", f"{said} {phrase}"]):
            ev = turn_answering_item_confirms(engine, f"u{k}{j}", text, Reply("", [model_call]))
            ok = kinds(ev) == ["ask"] or (model_call is INVOICE and date_said_or_asked(ev))
            if not ok:
                wrong.append((text, [(e["type"], e.get("code") or e.get("part")) for e in ev]))
    assert not wrong, wrong


# ---------- every calendar word read or asked; a unit or a rate is no date ----------

READ_MORE = [("a week ago", "19 Oct 2026"), ("2 weeks ago", "12 Oct 2026"), ("two weeks ago", "12 Oct 2026"),
             ("Friday before last", "16 Oct 2026"), ("3 days ago", "23 Oct 2026")]
FUTURE_MORE = ["Friday after next"]
UNREAD_MORE = ["a month ago", "at the end of September", "last September", "September", "the previous day",
               "year-end", "a year ago", "Friday after the meeting", "the following week"]


@pytest.mark.parametrize("phrase, day", READ_MORE)
def test_a_relative_day_is_read_exactly(engine, phrase, day):
    test_a_payment_is_dated_exactly_the_day_the_owner_wrote(engine, phrase, day)


@pytest.mark.parametrize("phrase", FUTURE_MORE)
def test_a_relative_day_after_today_is_never_carded(engine, phrase):
    test_a_payment_dated_after_today_is_never_carded(engine, phrase)


@pytest.mark.parametrize("phrase", UNREAD_MORE)
def test_every_calendar_word_unread_asks_on_every_operation(engine, phrase):
    test_an_unread_date_asks_on_every_operation(engine, phrase)


@pytest.fixture
def services(engine):
    """The honesty set's own books: consulting by the hour, a support retainer by the month."""
    import conftest
    from erpclaw_cli.evals.seeds import services_co

    conftest.drop_everything(engine)
    services_co(engine)
    return engine


@pytest.mark.parametrize("said, item, qty", [
    ("Invoice Initech for two months of support retainer", "support retainer", 2),
    ("Invoice Acme Corporation for 4 hours of consulting at 150 an hour", "consulting", 4),
    ("Invoice Initech for one support retainer", "support retainer", 1),
])
def test_a_unit_or_a_rate_is_no_date(services, said, item, qty):
    customer = "Initech" if "Initech" in said else "Acme Corporation"
    a = Assistant(services, ScriptedModel([Reply("", [call("create_invoice", customer=customer,
                                                           items=[{"item": item, "quantity": qty}])])]))
    ev = a.turn("unit", said)
    assert "card" in kinds(ev) and not any(e.get("part") == "date" for e in ev), ev


def test_may_and_march_alone_are_ordinary_words(engine):
    a = Assistant(engine, ScriptedModel([Reply("", [INVOICE])]))
    ev = a.turn("may", "invoice Hooli for 5 copper wire, you may march on")
    assert not any(e.get("code") == "date_unknown" or e.get("part") == "date" for e in ev), ev


# ---------- a calendar word inside a stored name is the name's, never a date ----------

@pytest.fixture
def named(engine):
    from decimal import Decimal

    from erpclaw_core.common import company_ctx
    from erpclaw_core.masters import create_item, create_party

    with engine.begin() as conn:
        ctx = company_ctx(conn)
        for name in ("May & Co", "September Labs"):
            create_party(ctx, "customer", name)
        for name in ("Monthly Retainer", "Year-End Review", "Weekend Support"):
            create_item(ctx, name, Decimal("100.00"))
    return engine


@pytest.mark.parametrize("customer, item, said", [
    ("Hooli", "monthly retainer", "invoice Hooli for 1 monthly retainer"),
    ("May & Co", "copper wire", "invoice May & Co for 5 copper wire"),
    ("Hooli", "year-end review", "invoice Hooli for 1 Year-End Review"),
    ("Hooli", "weekend support", "invoice Hooli for 1 weekend support"),
    ("September Labs", "copper wire", "invoice September Labs for 5 copper wire"),
])
def test_a_calendar_word_inside_a_stored_name_is_no_date(named, customer, item, said):
    a = Assistant(named, ScriptedModel([Reply("", [call("create_invoice", customer=customer,
                                                        items=[{"item": item, "quantity": 1 if "1 " in said else 5}])])]))
    ev = a.turn("nm", said)
    assert "card" in kinds(ev) and not any(e.get("part") == "date" or e.get("code") == "date_unknown" for e in ev), ev


def test_a_payer_named_for_a_month_is_no_date(named):
    a = Assistant(named, ScriptedModel([Reply("", [call("record_payment", customer="September Labs", amount=100)])]))
    ev = a.turn("pm", "September Labs paid 100")
    assert kinds(ev) == ["card"] and next(v for k, v in ev[0]["card"]["lines"] if k == "Date") == f"Received {TODAY}", ev


def test_a_date_beside_a_name_with_a_calendar_word_still_asks(named):
    a = Assistant(named, ScriptedModel([Reply("", [call("create_invoice", customer="Hooli",
                                                        items=[{"item": "monthly retainer", "quantity": 1}])])]))
    ev = a.turn("nd", "invoice Hooli for 1 monthly retainer last September")
    assert kinds(ev) == ["ask"] or any(e.get("part") == "date" for e in ev), ev


# ---------- complete consumption: a reading takes every touching calendar or number word, or asks ----------

READ_WHOLE = [("twenty-one days ago", "05 Oct 2026"), ("twenty one days ago", "05 Oct 2026"),
              ("eleven days ago", "15 Oct 2026"), ("next Friday", None), ("last Friday", "23 Oct 2026")]
ASK_PARTIAL = ["half a week ago", "Friday next", "Friday last", "a week and a half ago", "one and a half weeks ago",
               "last May", "at the end of March", "early March", "in May", "last spring", "at Christmas",
               "this summer", "over Easter", "last winter"]


@pytest.mark.parametrize("phrase, day", READ_WHOLE)
def test_a_reading_consumes_its_whole_phrase(engine, phrase, day):
    if day is None:  # read, and after today: never carded
        test_a_payment_dated_after_today_is_never_carded(engine, phrase)
    else:
        test_a_payment_is_dated_exactly_the_day_the_owner_wrote(engine, phrase, day)


@pytest.mark.parametrize("phrase", ASK_PARTIAL)
def test_a_reading_with_a_touching_word_left_over_asks(engine, phrase):
    test_an_unread_date_asks_on_every_operation(engine, phrase)


def test_may_and_march_unqualified_stay_ordinary_but_a_name_holding_them_is_the_name(named):
    a = Assistant(named, ScriptedModel([Reply("", [call("create_invoice", customer="May & Co",
                                                        items=[{"item": "copper wire", "quantity": 5}])])]))
    ev = a.turn("mc", "invoice May & Co for 5 copper wire")
    assert "card" in kinds(ev) and not any(e.get("part") == "date" or e.get("code") == "date_unknown" for e in ev), ev


# ---------- a qualifier beside a month and day asks; the month and day alone reads ----------

QUALIFIED_DAY = ["next November 3", "next 25 October", "last November 3", "this 25 October", "November 3 next",
                 "coming November 3", "previous 25 October"]


@pytest.mark.parametrize("phrase", QUALIFIED_DAY)
def test_a_qualified_month_and_day_asks_on_a_payment(engine, phrase):
    for k, said in enumerate([f"{phrase}, Hooli paid 100", f"Hooli paid 100 {phrase}"]):
        ev = turn_answering_item_confirms(engine, f"q{k}", said, Reply("", [PAYMENT]))
        assert kinds(ev) == ["ask"], (said, ev)


@pytest.mark.parametrize("phrase", ["next November 3", "next 25 October"])
def test_a_qualified_month_and_day_on_an_invoice_is_never_read(engine, phrase):
    ev = turn_answering_item_confirms(engine, "qi", f"{phrase} invoice Hooli for 5 copper wire", Reply("", [INVOICE]))
    cards = [e for e in ev if e["type"] == "card"]
    assert kinds(ev) == ["ask"] or (date_said_or_asked(ev) and cards and cards[0]["card"].get("date_source", {
        "default": True}) == {"default": True}), ev  # never a phrase read from it


@pytest.mark.parametrize("phrase", ["next November 3", "next 25 October"])
def test_submitting_a_draft_with_a_qualified_month_and_day_writes_no_date(engine, phrase):
    ev = turn_answering_item_confirms(engine, "qs", f"submit INV-2026-00008 {phrase}",
                                      Reply("", [call("submit_invoice", invoice="INV-2026-00008")]))
    assert kinds(ev) == ["ask"], ev


def test_a_month_and_day_alone_still_reads_exactly(engine):
    ev = turn_answering_item_confirms(engine, "md", "Hooli paid 100 November 3", Reply("", [PAYMENT]))
    assert kinds(ev) == ["card"], ev
    assert next(v for k, v in ev[0]["card"]["lines"] if k == "Date") == "Received 03 Nov 2025"  # the last 3 November
