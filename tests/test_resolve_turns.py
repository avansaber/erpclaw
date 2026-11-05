"""The resolution step in the assistant's turn: a write's fields come from the owner's words before validation, a
question ends the turn with its codes, a part that would change the money written gets the planned reply, and the
parts not done are said before the card. Payments, cancelling and reversing are on only behind it."""
from decimal import Decimal

import pytest

from sqlalchemy import func, select

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx
from erpclaw_core.plugins import REGISTRY
from erpclaw_sales import service as s
from erpclaw_sales import tables as t

# For now a reply makes one change: replies with several writes ask the
# owner for one change at a time (test_a_second_change_in_one_message_asks_for_one_at_a_time). These tests state how
# several writes resolve and draft (one record per call, one card per reply, scoped answers) and come back on with
# multi-write replies.
MULTI_WRITE = pytest.mark.skip(reason="one change per reply until several writes per reply come back")

SWITCHED_ON = ("record_payment", "cancel_invoice", "reverse_payment")


def turns(engine, *replies):
    return Assistant(engine, ScriptedModel(list(replies)))


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def nothing_written(engine):
    before = count(engine, m.approval), count(engine, t.invoice), count(engine, t.payment)
    return lambda: (count(engine, m.approval), count(engine, t.invoice), count(engine, t.payment)) == before


def kinds(events) -> list[str]:
    return [e["type"] for e in events]


def paid(engine, customer: str, amount: str) -> str:
    """A payment in the books, by the service (no card): its number."""
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == customer)).mappings().one())
        r = s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal(amount), ctx.today, oldest_first=True))
        return conn.execute(select(t.payment.c.number).where(t.payment.c.id == r["id"])).scalar_one()


# ---------- switched on, behind the step ----------

def test_an_amount_the_owner_never_wrote_is_asked_not_carded(engine):
    """The guard for switching payments on: without the step, the model's 500 would make a card."""
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("record_payment", customer="Hooli", amount=500)]))
    ev = a.turn("c1", "Hooli paid us")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "amount_missing"
    assert clean()


def test_an_amount_the_owner_wrote_makes_the_payment_card(engine):
    a = turns(engine, Reply("", [call("record_payment", customer="Hooli", amount=500)]))
    ev = a.turn("c1", "Hooli paid 500")
    assert kinds(ev) == ["card"]


def test_a_cancel_needs_the_invoice_number_the_owner_wrote(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("cancel_invoice", invoice="INV-2026-00003")]))
    ev = a.turn("c1", "cancel that invoice")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "invoice_missing"
    assert clean()


def test_a_cancel_of_the_named_invoice_makes_its_card(engine):
    a = turns(engine, Reply("", [call("cancel_invoice", invoice="INV-2026-00003")]))
    assert kinds(a.turn("c1", "cancel INV-2026-00003, it was a mistake")) == ["card"]


def test_a_reversal_takes_the_payment_the_owner_wrote_never_the_model_s(engine):
    number = paid(engine, "Hooli", "100")
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("reverse_payment", payment=number)]))
    ev = a.turn("c1", "reverse the payment, it bounced")  # no number written: the model's is not used
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "payment_missing"
    assert clean()
    a = turns(engine, Reply("", [call("reverse_payment", payment=number)]))
    assert kinds(a.turn("c2", f"reverse {number}, it bounced")) == ["card"]


# ---------- questions ----------

def test_a_question_carries_its_id_and_codes_never_a_reference(engine):
    a = turns(engine, Reply("", [call("create_invoice", customer="Acme", items=[{"item": "copper wire",
                                                                                  "quantity": 5}])]))
    ev = a.turn("c1", "invoice Acme for 5 copper wire")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Acme Corporation", "someone else"]
    data = ev[0]["data"]
    assert data["question_id"] == "q1" and data["choices"] == [{"code": "q1-1", "label": "Acme Corporation"},
                                                           {"code": "q1-2", "label": "someone else"}]


def test_question_codes_are_never_reused_across_turns(engine):
    ask = Reply("", [call("create_invoice", customer="Acme", items=[{"item": "copper wire", "quantity": 5}])])
    a = turns(engine, ask, ask)
    first = a.turn("c1", "invoice Acme for 5 copper wire")
    second = a.turn("c1", "invoice Acme for 5 copper wire")
    assert (first[0]["data"]["question_id"], second[0]["data"]["question_id"]) == ("q1", "q2")


@MULTI_WRITE
def test_a_question_ends_the_reply_nothing_after_it_runs(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("create_invoice", customer="Acme", items=[{"item": "copper wire",
                                                                                  "quantity": 5}]),
                                 call("create_invoice", customer="Hooli", items=[{"item": "copper wire",
                                                                                   "quantity": 5}])]))
    ev = a.turn("c1", "invoice Acme for 5 copper wire and Hooli for 5 copper wire")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Hooli", "Acme Corporation"]
    assert clean()


def test_an_item_the_model_took_from_other_words_is_asked_never_the_one_name_found(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("create_invoice", customer="Hooli", items=[
        {"item": "copper wire", "quantity": 5}, {"item": "circuit", "quantity": 3}])]))
    ev = a.turn("c1", "invoice Hooli for 5 copper wire and 3 circuit")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Copper Wire", "Circuit Board"]
    assert ev[0]["data"]["field"] == "create_invoice#0:items.1.item"
    assert clean()


def test_a_model_argument_the_owner_never_wrote_does_not_unseat_the_name_found(engine):
    a = turns(engine, Reply("", [call("create_invoice", customer="Initech", items=[
        {"item": "copper wire", "quantity": 5}])]))
    ev = a.turn("c1", "invoice Hooli for 5 copper wire")  # Initech is not in the owner's words
    assert kinds(ev) == ["card"]
    assert any(k == "Customer" and v.startswith("Hooli") for k, v in ev[0]["card"]["lines"])


def test_a_field_the_model_left_out_is_asked_never_sent_back_to_it(engine):
    model = ScriptedModel([Reply("", [call("create_invoice", customer="Hooli", items=[{"item": "copper wire"}])])])
    ev = Assistant(engine, model).turn("c1", "invoice Hooli for copper wire")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "quantity_missing"
    assert len(model.seen) == 1  # the engine asked; the model was not asked to fill it


# ---------- the planned reply and the parts not done ----------

def test_sales_tax_gets_the_planned_reply_with_its_capability_and_no_card(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("create_invoice", customer="Hooli", items=[{"item": "copper wire",
                                                                                   "quantity": 5}])]))
    ev = a.turn("c1", "invoice Hooli for 5 copper wire and add 8% sales tax")
    assert ev == [{"type": "answer", "outcome": "c", "capability": "planned.tax_engine", "currency": "USD",
                   "text": "Sales tax isn't available yet, so I haven't drafted this invoice."}]
    assert clean()


def test_emailing_is_said_before_the_card_whatever_the_model_s_boolean(engine):
    for emailed in (True, False):
        a = turns(engine, Reply("", [call("create_invoice", customer="Hooli", email=emailed,
                                          items=[{"item": "copper wire", "quantity": 5}])]))
        ev = a.turn(f"c{emailed}", "invoice Hooli for 5 copper wire and email it to them")
        assert kinds(ev) == ["not_done", "card"], emailed
        assert ev[0]["part"] == "email" and ev[0]["capability"] == "planned.messaging"


def test_a_canonical_name_reaches_the_card_whatever_cut_the_model_passed(engine):
    a = turns(engine, Reply("", [call("create_invoice", customer="Wire", items=[{"item": "Copper",
                                                                                  "quantity": 5}])]))
    ev = a.turn("c1", "invoice Hooli for 5 Copper Wire")
    assert kinds(ev) == ["card"]
    assert any(k == "Customer" and v.startswith("Hooli") for k, v in ev[0]["card"]["lines"])


def test_another_currency_is_refused_naming_its_capability(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("record_payment", customer="Hooli", amount=500)]))
    ev = a.turn("c1", "Hooli paid 500 euros")
    assert kinds(ev) == ["refused"] and ev[0]["code"] == "foreign_currency"
    assert ev[0]["capability"] == "planned.multi_currency" and "capability" not in ev[0]["data"]
    assert clean()


def test_a_stated_rate_is_said_naming_its_capability(engine):
    a = turns(engine, *[Reply("", [call("create_invoice", customer="Hooli", items=[{"item": "copper wire",
                                                                                     "quantity": 5}])])] * 2)
    ev = a.turn("c1", "invoice Hooli for 5 copper wire at 25 each")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "rate_choice_missing", ev  # asked first
    ev = a.turn("c1", "Use the list price")
    assert kinds(ev) == ["not_done", "card"], ev
    assert ev[0]["part"] == "rate" and ev[0]["capability"] == "planned.invoice_fields"


# ---------- every id the step names is in the catalogue ----------

def test_every_capability_the_step_names_is_a_catalogue_entry(engine):
    from erpclaw_assistant import resolve
    from erpclaw_core import catalogue as cat

    named = set(resolve.NOT_DONE.values()) | {"planned.tax_engine", "planned.discounts", "planned.payment_terms",
                                               "planned.stock", "planned.multi_currency"}
    with engine.connect() as conn:
        ids = {e.id for e in cat.catalogue(conn)}
    assert named <= ids, named - ids


def test_a_currency_request_finds_multi_currency_and_a_rates_import_finds_the_csv_connector(engine):
    from erpclaw_core import catalogue as cat

    def first_planned(request: str) -> str:
        with engine.connect() as conn:
            return next(e.id for e in cat.find_capability(conn, request) if e.state == "planned")

    assert first_planned("record a payment in another currency") == "planned.multi_currency"
    assert first_planned("can I bill in a foreign currency") == "planned.multi_currency"
    assert first_planned("import exchange rates from a csv") == "planned.connector.csv"
    with engine.connect() as conn:  # an exchange-rate question surfaces the currency entries before invoice fields
        ids = [e.id for e in cat.find_capability(conn, "what's the exchange rate for GBP") if e.state == "planned"]
    assert ids[:2] == ["planned.connector.csv", "planned.multi_currency"], ids


def test_a_name_the_owner_wrote_that_is_no_record_is_unknown_quoting_only_the_owner_s_words(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("create_invoice", customer="Nobody At All",
                                      items=[{"item": "copper wire", "quantity": 1}])]))
    [ev] = a.turn("c1", "invoice Nobody for 1 copper wire")
    assert ev["type"] == "ask" and ev["code"] == "customer_unknown"
    assert "Nobody" in ev["text"] and "At All" not in ev["text"]
    a = turns(engine, Reply("", [call("create_invoice", customer="Nobody At All",
                                      items=[{"item": "copper wire", "quantity": 1}])]))
    [ev] = a.turn("c2", "invoice them for 1 copper wire")  # the model's name is none of the owner's words
    assert ev["code"] == "customer_missing"
    assert clean()


# ---------- one field across the calls of one reply ----------

def _invoice(customer, *lines):
    return call("create_invoice", customer=customer, items=[{"item": i, "quantity": q} for i, q in lines])


def _customer(card) -> str:
    return next(v for k, v in card["card"]["lines"] if k == "Customer")


@MULTI_WRITE
def test_two_customers_named_with_and_draft_one_card_and_say_the_other_whatever_cut_the_model_passed(engine):
    """One card per reply: each call resolves to its own customer; the first in the owner's order is
    drafted, and the other is said, never dropped, naming both by their stored names."""
    from erpclaw_core import approvals

    said = "invoice Acme Corporation for 5 copper wire and Hooli for 5 copper wire"
    for first, second in (("Acme Corporation", "Hooli"), ("Acme", "Hooli")):
        a = turns(engine, Reply("", [_invoice(first, ("copper wire", 5)), _invoice(second, ("copper wire", 5))]))
        ev = a.turn(f"c-{first}", said)
        assert kinds(ev) == ["card", "not_done"], (first, ev)
        assert _customer(ev[0]).startswith("Acme Corporation")
        assert ev[1]["part"] == "one_at_a_time" and ev[1]["capability"] == "planned.several_changes"
        assert ev[1]["text"] == ("One change at a time: I've drafted the invoice for Acme Corporation. Ask again for "
                                 "the invoice for Hooli once you've approved or declined it.")
        assert approvals.approve(engine, ev[0]["approval_id"], ev[0]["digest"]).status == "committed"


def test_fewer_calls_than_customers_named_asks(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [_invoice("Acme Corporation", ("copper wire", 5))]))
    ev = a.turn("c1", "invoice Acme Corporation and Hooli for 5 copper wire")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Acme Corporation", "Hooli"]
    assert clean()


@MULTI_WRITE
def test_two_calls_taking_the_same_customer_named_once_asks(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5)), _invoice("Hooli", ("copper wire", 5))]))
    ev = a.turn("c1", "invoice Acme Corporation for 5 copper wire and Hooli for 5 copper wire")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Acme Corporation", "Hooli"]
    assert clean()


@MULTI_WRITE
def test_either_or_between_customers_asks_with_one_call_or_two(engine):
    said = "invoice either Acme Corporation or Hooli for 5 copper wire"
    for calls in ([_invoice("Acme Corporation", ("copper wire", 5))],
                  [_invoice("Acme Corporation", ("copper wire", 5)), _invoice("Hooli", ("copper wire", 5))]):
        clean = nothing_written(engine)
        ev = turns(engine, Reply("", calls)).turn(f"c{len(calls)}", said)
        assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Acme Corporation", "Hooli"], ev
        assert clean()


def test_either_or_between_items_asks(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5))]))
    ev = a.turn("c1", "invoice Hooli for 5 copper wire or circuit board")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Circuit Board", "Copper Wire"]
    assert ev[0]["data"]["field"] == "create_invoice#0:items.0.item"
    assert clean()


def test_an_item_written_twice_may_take_two_lines_but_a_line_the_model_doubled_asks(engine):
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5), ("circuit board", 3), ("copper wire", 2))]))
    assert kinds(a.turn("c1", "invoice Hooli for 5 copper wire, 3 circuit board and 2 copper wire")) == ["card"]
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5), ("copper wire", 3))]))
    ev = a.turn("c2", "invoice Hooli for 5 copper wire and 3 circuit board")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Circuit Board", "Copper Wire"]
    assert clean()


def test_a_single_record_request_is_unaffected(engine):
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5), ("circuit board", 3))]))
    ev = a.turn("c1", "invoice Hooli for 5 copper wire and 3 circuit board")
    assert kinds(ev) == ["card"] and _customer(ev[0]).startswith("Hooli")


def test_or_asks_even_when_the_name_before_it_is_found_bounded(engine):
    """Only the "or" test stops these cards: Hooli and Circuit Board are bounded, the names next to "or" are not."""
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5))]))
    ev = a.turn("c1", "invoice Hooli for 5 copper wire or Acme Corporation for 5 copper wire")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Acme Corporation", "Hooli"]
    a = turns(engine, Reply("", [_invoice("Hooli", ("circuit board", 3))]))
    ev = a.turn("c2", "invoice Hooli for 5 copper wire or 3 circuit board")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Circuit Board", "Copper Wire"]
    assert clean()


@MULTI_WRITE
def test_a_record_taken_by_more_calls_than_the_owner_wrote_it_asks(engine):
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [_invoice("Acme Corporation", ("copper wire", 5)), _invoice("Hooli", ("copper wire", 5)),
                                 _invoice("Hooli", ("copper wire", 5))]))
    ev = a.turn("c1", "invoice Acme Corporation for 5 copper wire and Hooli for 5 copper wire")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Acme Corporation", "Hooli"]
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5), ("circuit board", 3), ("copper wire", 2))]))
    ev = a.turn("c2", "invoice Hooli for 5 copper wire and 3 circuit board")
    assert kinds(ev) == ["ask"] and ev[0]["options"] == ["Circuit Board", "Copper Wire"]
    assert clean()


@MULTI_WRITE
def test_two_invoices_for_one_customer_ask_with_one_mention_or_two(engine):
    """Customers are strict: one invoice with two lines, or two invoices? The owner has not said."""
    clean = nothing_written(engine)
    for said in ("invoice Acme Corporation for 5 copper wire and Acme Corporation for 3 circuit board",
                 "invoice Acme Corporation for 5 copper wire, Acme Corporation is our biggest client"):
        a = turns(engine, Reply("", [_invoice("Acme Corporation", ("copper wire", 5)),
                                     _invoice("Acme Corporation", ("circuit board", 3))]))
        ev = a.turn(f"c{len(said)}", said)
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "customer_ambiguous", ev
        assert "more than one change" in ev[0]["text"]
    assert clean()


@MULTI_WRITE
def test_different_operations_may_take_the_same_customer_one_card_at_a_time(engine):
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5)),
                                 call("record_payment", customer="Hooli", amount=500)]))
    ev = a.turn("c1", "invoice Hooli for 5 copper wire, and Hooli paid 500")
    assert kinds(ev) == ["card", "not_done"], ev
    assert any(k == "Item" for k, _ in ev[0]["card"]["lines"])  # the invoice: the owner asked it first
    assert "the payment from Hooli" in ev[1]["text"]


@MULTI_WRITE
def test_a_payment_asked_beside_a_new_invoice_is_carded_against_the_books_as_they_stand(engine):
    """One card per reply: the payment asked first is drafted alone, against the invoices open now; approving it posts
    exactly what its card showed. The invoice asked beside it is said, not drafted."""
    from erpclaw_core import approvals

    said = "Hooli paid 500, and invoice Hooli for 5 copper wire"
    replies = Reply("", [_invoice("Hooli", ("copper wire", 5)), call("record_payment", customer="Hooli", amount=500)])
    ev = turns(engine, replies).turn("c1", said)
    assert kinds(ev) == ["card", "not_done"] and "the invoice for Hooli" in ev[1]["text"], ev
    pay = ev[0]
    assert any(k == "Amount received" for k, _ in pay["card"]["lines"]), pay["card"]["lines"]
    shown = [v for k, v in pay["card"]["lines"] if k == "Applied to"]
    credit = [v for k, v in pay["card"]["lines"] if k == "Credit left"]
    out = approvals.approve(engine, pay["approval_id"], pay["digest"])
    assert out.status == "committed", out.text
    with engine.connect() as conn:
        row = conn.execute(select(t.payment).where(t.payment.c.operation_key == out.operation_key)).mappings().one()
        alloc = conn.execute(select(t.invoice.c.number, t.allocation.c.amount).join(
            t.invoice, t.invoice.c.id == t.allocation.c.invoice_id).where(
            t.allocation.c.payment_id == row["id"])).all()
    assert sorted((n, f"{a:,.2f}") for n, a in alloc) == sorted(
        (v.split()[0], v.split("(")[1].split()[0]) for v in shown), (shown, alloc)
    assert [f"{row['unallocated']:,.2f}"] == [c.split()[0] for c in credit] or (not credit and row["unallocated"] == 0)


# ---------- references, lines, strictness, shapes ----------

def test_a_customer_named_by_email_is_never_replaced_by_the_invoice_s_payer(engine):
    """INV-2026-00006 is Pied Piper's; the owner named Hooli by email, so the engine asks, never a Pied
    Piper payment."""
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("record_payment", customer="payables@hooli.example", amount=100,
                                      invoice="INV-2026-00006")]))
    ev = a.turn("c1", "payables@hooli.example paid 100 against invoice 6")
    assert "card" not in kinds(ev), ev
    assert clean()


def test_a_line_the_model_doubled_for_the_one_item_named_asks(engine):
    """one item written once never takes two lines."""
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5), ("copper wire", 5))]))
    ev = a.turn("c1", "invoice Hooli for 5 copper wire")
    assert kinds(ev) == ["ask"], ev
    assert clean()


@MULTI_WRITE
def test_two_invoices_for_one_customer_ask_whether_omitted_or_named_by_email(engine):
    """customers are strict per operation, whatever the calls give for the customer."""
    for said, customer in (("invoice Hooli for 5 copper wire and 3 circuit board", None),
                           ("invoice payables@hooli.example for 5 copper wire and 3 circuit board",
                            "payables@hooli.example")):
        clean = nothing_written(engine)
        first, second = ({"items": [{"item": i, "quantity": q}]} for i, q in (("copper wire", 5), ("circuit board", 3)))
        if customer:
            first["customer"] = second["customer"] = customer
        a = turns(engine, Reply("", [call("create_invoice", **first), call("create_invoice", **second)]))
        ev = a.turn(f"c{len(said)}", said)
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "customer_ambiguous", (said, ev)
        assert clean()


def test_malformed_lines_go_back_to_the_model_and_never_raise(engine):
    """a lines value that is not a list of objects is left to validation."""
    for items in (5, "copper wire", [5], ["copper wire"]):
        clean = nothing_written(engine)
        a = turns(engine, Reply("", [call("create_invoice", customer="Hooli", items=items)]))
        ev = a.turn(f"c{items!r}", "invoice Hooli for 5 copper wire")
        assert "card" not in kinds(ev), (items, ev)
        assert clean()



def test_a_question_for_any_write_in_the_reply_makes_no_card(engine):
    """Questions before cards: call 1 resolves, call 2 is ambiguous; the reply asks and drafts nothing."""
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5)), _invoice("Acme", ("circuit board", 3))]))
    ev = a.turn("c1", "invoice Hooli for 5 copper wire and Acme for 3 circuit board")
    assert kinds(ev) == ["ask"], ev
    assert clean()


@MULTI_WRITE
def test_the_owner_s_order_decides_which_card_is_drafted_not_the_model_s(engine):
    a = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5)), _invoice("Acme Corporation", ("copper wire", 5))]))
    ev = a.turn("c1", "invoice Acme Corporation for 5 copper wire and Hooli for 5 copper wire")
    assert kinds(ev) == ["card", "not_done"] and _customer(ev[0]).startswith("Acme Corporation"), ev
    assert "the invoice for Hooli" in ev[1]["text"]


@MULTI_WRITE
def test_every_card_a_reply_returns_can_be_approved(engine):
    from erpclaw_core import approvals

    for said, calls in (
            ("invoice Acme Corporation for 5 copper wire and Hooli for 5 copper wire",
             [_invoice("Acme Corporation", ("copper wire", 5)), _invoice("Hooli", ("copper wire", 5))]),
            ("invoice Hooli for 5 copper wire, and Hooli paid 500",
             [_invoice("Hooli", ("copper wire", 5)), call("record_payment", customer="Hooli", amount=500)])):
        ev = turns(engine, Reply("", calls)).turn(f"c{len(said)}", said)
        cards = [e for e in ev if e["type"] == "card"]
        assert cards and all(approvals.approve(engine, c["approval_id"], c["digest"]).status == "committed"
                             for c in cards), ev


def test_a_several_changes_request_does_not_outrank_the_sales_operations_in_search(engine):
    from erpclaw_core import catalogue as cat

    with engine.connect() as conn:
        found = [e.id for e in cat.find_capability(conn, "invoice all of them for one hour each")]
    assert found and found[0] != "planned.several_changes", found



# ---------- number words ----------

def test_a_name_followed_by_a_number_word_is_bounded_and_the_number_words_are_the_quantity(engine):
    """P5: "hooli" is the whole stored name; "five hundred" is the quantity."""
    a = turns(engine, Reply("", [call("create_invoice", customer="hooli", items=[{"item": "copper wire",
                                                                                   "quantity": 500}])]))
    ev = a.turn("c1", "bill hooli five hundred copper wire")
    assert kinds(ev) == ["card"], ev
    assert _customer(ev[0]).startswith("Hooli")
    assert any(k == "Item" and v.startswith("500 x Copper Wire") for k, v in ev[0]["card"]["lines"])


def test_a_stored_name_that_continues_with_the_number_word_still_asks(engine):
    """With "Hooli Five" on file, "hooli five hundred" could be either customer: the engine asks."""
    from erpclaw_core.masters import create_party

    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", "Hooli Five")
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("create_invoice", customer="hooli", items=[{"item": "copper wire",
                                                                                   "quantity": 500}])]))
    ev = a.turn("c1", "bill hooli five hundred copper wire")
    assert kinds(ev) == ["ask"] and set(ev[0]["options"]) >= {"Hooli", "Hooli Five"}, ev
    assert clean()


def test_compound_number_words_read_as_one_quantity():
    from erpclaw_assistant.resolve import number_words
    from decimal import Decimal as D

    assert number_words("five hundred".split()) == D(500)
    assert number_words("two thousand three hundred and".split()[:4]) == D(2300)
    assert number_words("twenty five".split()) == D(25)
    assert number_words("a dozen".split()) == D(12)
    assert number_words("two dozen".split()) == D(24)
    assert number_words("five widgets".split()) is None


# ---------- a contract's own question has the step's shape ----------

def _answer(assistant, cid: str, choice: dict) -> list[dict]:
    """The owner answers by the choice's code, as a client sends it, with its label as the text."""
    return assistant.turn(cid, choice["label"], choice=choice["code"])


def test_a_payment_question_carries_codes_and_an_answer_by_code_settles_the_card(engine):
    said = "Pied Piper paid us 500 via wire"
    pay = call("record_payment", customer="Pied Piper", amount=500, method="wire")
    for pick, applied in ((1, ["INV-2026-00006"]), (2, ["INV-2026-00003"])):  # the second invoice; oldest first
        a = turns(engine, Reply("", [pay]), Reply("", [pay]))
        [ask] = a.turn(f"c{pick}", said)
        assert ask["type"] == "ask" and ask["code"] == "invoice_ambiguous", ask
        choices = ask["data"]["choices"]
        assert [c["label"] for c in choices][-1] == "oldest first" and all("ref" not in c for c in choices)
        assert ask["data"]["question_id"] and all(c["code"].startswith(ask["data"]["question_id"]) for c in choices)
        [card] = _answer(a, f"c{pick}", choices[1] if pick == 1 else choices[-1])
        assert card["type"] == "card", card
        shown = [v.split()[0] for k, v in card["card"]["lines"] if k == "Applied to"]
        assert shown[:1] == applied, card["card"]["lines"]


# ---------- numbers, payers, previews, choices ----------

def test_a_decimal_quantity_is_read_as_written_whether_the_model_gives_it_or_not(engine):
    """A decimal: "1.5" is one and a half, never 15, and a sign written against a quantity is kept."""
    for given in (None, 1.5):
        line = {"item": "copper wire"} if given is None else {"item": "copper wire", "quantity": given}
        a = turns(engine, Reply("", [call("create_invoice", customer="Hooli", items=[line])]))
        ev = a.turn(f"c{given}", "invoice Hooli for 1.5 copper wire")
        assert kinds(ev) == ["card"], (given, ev)
        assert any(k == "Item" and v.startswith("1.5 x Copper Wire") for k, v in ev[0]["card"]["lines"]), ev
    clean = nothing_written(engine)
    a = turns(engine, Reply("", [call("create_invoice", customer="Hooli", items=[{"item": "copper wire"}])]))
    ev = a.turn("c-neg", "invoice Hooli for -5 copper wire")
    assert "card" not in kinds(ev), ev
    assert clean()


def _quantity_events(engine, cid, said, given):
    line = {"item": "copper wire"} if given is None else {"item": "copper wire", "quantity": given}
    return turns(engine, Reply("", [call("create_invoice", customer="Hooli", items=[line])])).turn(cid, said)


def _carded_quantity(ev) -> str | None:
    if kinds(ev) != ["card"]:
        return None
    return next(v.split(" x ")[0] for k, v in ev[0]["card"]["lines"] if k == "Item")


def test_numbers_read_exactly_or_ask_never_misread(engine):
    """Numbers: each form reads exactly as written, or the engine asks."""
    cases = [  # owner's words, the model's quantity (None: left out), the card's quantity (None: the engine asks)
        ("invoice Hooli for 1,500 copper wire", None, "1500"), ("invoice Hooli for 1,500 copper wire", 1500, "1500"),
        ("invoice Hooli for 1,500 copper wire", 1, None), ("invoice Hooli for 1,500 copper wire", 500, None),
        ("invoice Hooli for 1 500 copper wire", None, None), ("invoice Hooli for 1 500 copper wire", 500, None),
        ("invoice Hooli for 1 500 copper wire", 1500, None),
        ("invoice Hooli for 5 copper wire at $28", None, "5"), ("invoice Hooli for 5 copper wire at $28", 5, "5"),
        ("invoice Hooli for 5 copper wire at $28", 28, None),
        ("invoice Hooli for .5 copper wire", None, None), ("invoice Hooli for .5 copper wire", 5, None),
        ("invoice Hooli for 2 dozen copper wire", None, "24"), ("invoice Hooli for 2 dozen copper wire", 24, "24"),
        ("invoice Hooli for 2 dozen copper wire", 2, None),
    ]
    clean = nothing_written(engine)
    wrong = []
    for k, (said, given, want) in enumerate(cases):
        got = _carded_quantity(_quantity_events(engine, f"n{k}", said, given))
        if got != want:
            wrong.append((said, given, want, got))
    assert not wrong, wrong
    del clean  # cards are made by design here


def test_a_partial_or_unknown_customer_is_resolved_never_replaced_by_the_invoice_s_payer(engine):
    """INV-2026-00006 is Pied Piper's; a customer the owner wrote, partly or unknown, asks."""
    clean = nothing_written(engine)
    for said, customer in (("Acme paid 100 against INV-2026-00006", "Acme"),
                           ("Acme paid 100 against INV-2026-00006", None),
                           ("Nobody paid 100 against INV-2026-00006", "Nobody")):
        args = {"amount": 100, "invoice": "INV-2026-00006", **({"customer": customer} if customer else {})}
        ev = turns(engine, Reply("", [call("record_payment", **args)])).turn(f"c{customer}", said)
        assert kinds(ev) == ["ask"], (said, customer, ev)
    assert clean()


def test_a_payer_is_still_taken_from_the_invoice_when_the_owner_names_no_customer(engine):
    ev = turns(engine, Reply("", [call("record_payment", amount=100, invoice="INV-2026-00006")])).turn(
        "c1", "INV-2026-00006 was paid, 100 by wire")
    assert kinds(ev) == ["card"] and _customer(ev[0]).startswith("Pied Piper"), ev


@MULTI_WRITE
def test_two_payments_whose_invoices_share_a_customer_ask(engine):
    """Strictness holds for customers taken from the invoices too."""
    clean = nothing_written(engine)
    ev = turns(engine, Reply("", [call("record_payment", amount=100, invoice="INV-2026-00003"),
                                  call("record_payment", amount=100, invoice="INV-2026-00006")])).turn(
        "c1", "100 against INV-2026-00003 and 100 against INV-2026-00006")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "customer_ambiguous", ev
    assert clean()


@MULTI_WRITE
def test_a_question_any_write_s_preview_would_ask_makes_no_card(engine):
    """The payment alone asks which invoice; placed after an invoice it still asks, no card."""
    clean = nothing_written(engine)
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5)),
                                  call("record_payment", customer="Pied Piper", amount=500)])).turn(
        "c1", "invoice Hooli for 5 copper wire, and Pied Piper paid us 500")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "invoice_ambiguous", ev
    assert clean()


def test_a_reversal_answered_by_code_reverses_the_chosen_payment(engine):
    """The choice binds the payment's number, never its description."""
    first, second = paid(engine, "Hooli", "100"), paid(engine, "Hooli", "200")
    said = "reverse the payment from Hooli"
    rev = call("reverse_payment", customer="Hooli")
    a = turns(engine, Reply("", [rev]), Reply("", [rev]))
    [ask] = a.turn("c1", said)
    assert ask["type"] == "ask" and ask["code"] == "payment_ambiguous" and ask["data"].get("question_id"), ask
    pick = next(c for c in ask["data"]["choices"] if c["label"].startswith(first))
    [card] = _answer(a, "c1", pick)
    assert card["type"] == "card" and any(first in v for _, v in card["card"]["lines"]), card
    assert not any(second in v for _, v in card["card"]["lines"])


def test_a_document_question_raised_while_resolving_carries_codes(engine):
    """A number several payments end in asks with codes and binds the chosen one."""
    made = []
    while "PAY-2026-00003" not in made and len(made) < 5:  # with the demo's PAY-2025-00003, two payments end in 3
        made.append(paid(engine, "Hooli", "10"))
    assert "PAY-2026-00003" in made, made
    said = "reverse payment 3"
    rev = call("reverse_payment", payment="3")
    a = turns(engine, Reply("", [rev]), Reply("", [rev]))
    [ask] = a.turn("c1", said)
    assert ask["type"] == "ask" and ask["data"].get("question_id") and ask["data"].get("choices"), ask
    [card] = _answer(a, "c1", next(c for c in ask["data"]["choices"] if c["label"] == "PAY-2026-00003"))
    assert card["type"] == "card", card


# ---------- support and coverage: number phrases, scoped answers, items left out ----------

def test_a_number_phrase_is_read_whole_or_asked_never_in_part(engine):
    """One hundred and five" is 105; words that are not one number ask."""
    assert _carded_quantity(_quantity_events(engine, "w1", "invoice Hooli for one hundred and five copper wire",
                                             None)) == "105"
    assert _carded_quantity(_quantity_events(engine, "w2", "invoice Hooli for two three copper wire", None)) is None


@MULTI_WRITE
def test_an_answer_binds_only_the_write_it_was_asked_for(engine):
    """Answering the second invoice's customer question leaves the first invoice's customer."""
    said = "invoice Hooli for 5 copper wire and Acme for 3 circuit board"
    calls = [_invoice("Hooli", ("copper wire", 5)), _invoice("Acme", ("circuit board", 3))]
    a = turns(engine, Reply("", calls), Reply("", calls))
    [ask] = a.turn("c1", said)
    assert ask["type"] == "ask" and "Acme Corporation" in ask["options"], ask
    ev = _answer(a, "c1", next(c for c in ask["data"]["choices"] if c["label"] == "Acme Corporation"))
    assert kinds(ev) == ["card", "not_done"], ev
    assert _customer(ev[0]).startswith("Hooli"), ev[0]["card"]["lines"]


def test_an_item_the_model_left_out_is_asked_never_dropped(engine):
    """The phrase "3 circuit" is an item phrase no line took."""
    clean = nothing_written(engine)
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 2))])).turn(
        "c1", "invoice Hooli for 2 copper wire and 3 circuit")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "item_ambiguous", ev
    assert clean()


def test_a_figure_no_field_took_is_asked_never_dropped(engine):
    """A figure the owner wrote that no field uses and no row answers is asked about."""
    clean = nothing_written(engine)
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5))])).turn(
        "c1", "invoice Hooli for 5 copper wire and 3")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "figure_ambiguous", ev
    assert clean()


def test_figures_in_a_date_or_a_rate_are_answered_by_their_rows_not_asked(engine):
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5))])).turn(
        "crate", "invoice Hooli for 5 copper wire at 30 each")
    # a rate or a date the engine cannot set yet is its own question, never a figure question about its "30" or "15"
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "rate_choice_missing", ev
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5))])).turn(
        "cdate", "invoice Hooli for 5 copper wire on 15 November")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "date_choice_missing", ev


def test_a_draft_only_when_the_owner_asks_for_one(engine):
    """The model's submit=false is not honoured without the owner's draft words."""
    for said, title in (("invoice Hooli for 5 copper wire", "Create and submit a sales invoice"),
                        ("invoice Hooli for 5 copper wire and save it as a draft", None)):
        ev = turns(engine, Reply("", [call("create_invoice", customer="Hooli", submit=False,
                                           items=[{"item": "copper wire", "quantity": 5}])])).turn(f"c{len(said)}", said)
        assert kinds(ev) == ["card"], ev
        assert (ev[0]["card"]["title"] == title) if title else ev[0]["card"]["title"] != "Create and submit a sales invoice"


def test_from_our_office_on_an_invoice_is_no_party_question(engine):
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5))])).turn(
        "c1", "invoice Hooli for 5 copper wire from our Dallas office")
    assert kinds(ev) == ["card"] and _customer(ev[0]).startswith("Hooli"), ev


def test_either_of_two_invoice_numbers_asks_whatever_the_model_passed(engine):
    clean = nothing_written(engine)
    ev = turns(engine, Reply("", [call("submit_invoice", invoice="INV-2026-00008")])).turn(
        "c1", "submit either INV-2026-00008 or INV-2026-00003")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "invoice_ambiguous", ev
    assert clean()


def test_payments_cancelling_and_reversing_stay_off_until_their_own_task(as_released):
    for name in SWITCHED_ON:
        assert REGISTRY.contracts[name].planned, name


# ---------- occurrence accounting: every recognised occurrence claimed or asked ----------

ROUND_9 = [  # owner's words, the model's calls: each must ask and draft nothing
    ("invoice Hooli for 1,5 copper wire", [_invoice("Hooli", ("copper wire", 15))]),
    ("invoice Hooli for 1,5 copper wire", [call("create_invoice", customer="Hooli", items=[{"item": "copper wire"}])]),
    ("invoice Hooli for - 5 copper wire", [_invoice("Hooli", ("copper wire", 5))]),
    ("invoice Hooli for 2 copper wire and one hundred and five", [_invoice("Hooli", ("copper wire", 2))]),
    ("invoice Hooli for 2 copper wire and invoice Acme", [_invoice("Hooli", ("copper wire", 2))]),
    ("submit INV-2026-00008 for Hooli", [call("submit_invoice", invoice="INV-2026-00008")]),
    ("invoice Hooli for 2 copper wire and 3 copper wire", [_invoice("Hooli", ("copper wire", 2))]),
    ("invoice Hooli for 2 copper wire and submit invoice 8", [_invoice("Hooli", ("copper wire", 2))]),
    ("submit INV-2026-00008 with memo PO 12345", [call("submit_invoice", invoice="INV-2026-00008")]),
    ("invoice Hooli for 2 copper wire and draft invoice Acme Corporation for 3 circuit board",
     [_invoice("Hooli", ("copper wire", 2)),
      call("create_invoice", customer="Acme Corporation", submit=False, items=[{"item": "circuit board", "quantity": 3}])]),
]


@pytest.mark.parametrize("said, calls", ROUND_9, ids=[f"r9-{k}" for k in range(len(ROUND_9))])
def test_every_recognised_occurrence_is_claimed_or_asked(engine, said, calls):
    clean = nothing_written(engine)
    ev = turns(engine, Reply("", calls)).turn(f"c{len(said)}", said)
    assert kinds(ev) == ["ask"], (said, ev)
    assert clean()


def test_stop_words_in_a_stored_name_are_never_occurrences(engine):
    """Core words only: "the" and "and" inside a stored name, and in the request, ask nothing."""
    from erpclaw_core.masters import create_party

    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", "The Bits and Bytes Company")
    ev = turns(engine, Reply("", [_invoice("The Bits and Bytes Company", ("copper wire", 5), ("circuit board", 3))])).turn(
        "c1", "invoice The Bits and Bytes Company for 5 copper wire and 3 circuit board")
    assert kinds(ev) == ["card"], ev


def test_today_restates_the_default_and_asks_nothing(engine):
    # "today" is claimed by the date's default: no note, no question ("dated" bounds the item: a date word never does)
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5))])).turn(
        "c1", "invoice Hooli for 5 copper wire, dated today")
    assert kinds(ev) == ["card"], ev


def test_the_accounting_carries_the_weight(engine, monkeypatch):
    """With "any unclaimed occurrence asks" removed, several of these requests make cards: the accounting, not a
    case rule, is what asks."""
    from erpclaw_assistant import resolve

    monkeypatch.setattr(resolve, "_unclaimed", lambda *a, **k: None)
    carded = [said for said, calls in ROUND_9[3:9]
              if "card" in kinds(turns(engine, Reply("", calls)).turn(f"w{len(said)}", said))]
    assert len(carded) >= 3, carded


# ---------- quantities, signs, defaults, and one write per reply ----------

ROUND_10 = [  # owner's words, the model's calls: each asks and drafts nothing
    ("invoice Hooli for copper wire with memo PO 12345", [_invoice("Hooli", ("copper wire", 12345))]),
    ("invoice Hooli for copper wire with quantity -5", [_invoice("Hooli", ("copper wire", 5))]),
    ("invoice Hooli at 45 each for 2 copper wire", [_invoice("Hooli", ("copper wire", 2))]),
    ("invoice Hooli for 2 copper wire, memo PO 12345 and submit invoice 8", [_invoice("Hooli", ("copper wire", 2))]),
    ("submit INV-2026-00008 dated today", [call("submit_invoice", invoice="INV-2026-00008")]),
    ("invoice Hooli for 2 copper wire and invoice Acme Corporation for 3 copper wire",  # the quantities swapped
     [_invoice("Hooli", ("copper wire", 3)), _invoice("Acme Corporation", ("copper wire", 2))]),
]


@pytest.mark.parametrize("said, calls", ROUND_10, ids=[f"r10-{k}" for k in range(len(ROUND_10))])
def test_round_10_asks_and_drafts_nothing(engine, said, calls):
    clean = nothing_written(engine)
    ev = turns(engine, Reply("", calls)).turn(f"c{len(said)}", said)
    assert kinds(ev) == ["ask"], (said, ev)
    assert clean()


def test_a_second_change_in_one_message_asks_for_one_at_a_time(engine):
    """The narrow landing: one write per reply; a second write intent asks, even when the model left it out."""
    clean = nothing_written(engine)
    for said, calls in (
            ("invoice Acme Corporation for 5 copper wire and Hooli for 5 copper wire",
             [_invoice("Acme Corporation", ("copper wire", 5)), _invoice("Hooli", ("copper wire", 5))]),
            ("invoice Hooli for 5 copper wire and submit INV-2026-00008", [_invoice("Hooli", ("copper wire", 5))])):
        ev = turns(engine, Reply("", calls)).turn(f"c{len(said)}", said)
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "changes_ambiguous", (said, ev)
    assert clean()


def test_a_multi_line_invoice_is_still_one_change(engine):
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5), ("circuit board", 3))])).turn(
        "c1", "invoice Hooli for 5 copper wire and 3 circuit board")
    assert kinds(ev) == ["card"], ev


@pytest.mark.parametrize("said, calls", [
    ("invoice Acme Corporation for 5 copper wire and Hooli for 5 copper wire",
     [_invoice("Acme Corporation", ("copper wire", 5)), _invoice("Hooli", ("copper wire", 5))]),
    ("invoice Hooli for 5 copper wire, and Hooli paid 500",
     [_invoice("Hooli", ("copper wire", 5)), call("record_payment", customer="Hooli", amount=500)]),
    ("invoice Hooli for 2 copper wire and invoice Acme Corporation for 3 copper wire",
     [_invoice("Hooli", ("copper wire", 2))]),  # the model left the second out
    ("invoice Hooli for 5 copper wire and submit INV-2026-00008", [_invoice("Hooli", ("copper wire", 5))]),
])
def test_several_changes_in_one_message_ask_for_one_at_a_time(engine, said, calls):
    clean = nothing_written(engine)
    ev = turns(engine, Reply("", calls)).turn(f"c{len(said)}", said)
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "changes_ambiguous", (said, ev)
    assert clean()


@pytest.mark.parametrize("said", ["send Wayne Enterprises an invoice for 2 circuit boards",
                                  "raise an invoice for Hooli for 5 copper wire",
                                  "create an invoice for Hooli for 5 copper wire",
                                  "invoice Hooli for 5 copper wire and submit it"])
def test_one_change_said_with_a_verb_and_its_document_is_one_intent(engine, said):
    customer = "Wayne Enterprises" if "Wayne" in said else "Hooli"
    line = ("circuit board", 2) if "Wayne" in said else ("copper wire", 5)
    ev = turns(engine, Reply("", [_invoice(customer, line)])).turn(f"c{len(said)}", said)
    assert "changes_ambiguous" not in [e.get("code") for e in ev], (said, ev)


@pytest.mark.parametrize("said", ["invoice Hooli for 5 copper wire, memo PO 12345 and submit this invoice 8",
                                  "invoice Hooli for 5 copper wire and cancel that invoice 8",
                                  "invoice Hooli for 5 copper wire and submit this INV-2026-00008"])
def test_a_pronoun_naming_another_document_is_a_second_change(engine, said):
    """The confirming review: "this invoice 8" names a document, so it is another change, never the same write."""
    clean = nothing_written(engine)
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5))])).turn(f"c{len(said)}", said)
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "changes_ambiguous", (said, ev)
    assert clean()


def test_submit_this_invoice_without_a_number_is_the_same_write(engine):
    said = "invoice Hooli for 5 copper wire and submit this invoice"
    ev = turns(engine, Reply("", [_invoice("Hooli", ("copper wire", 5))])).turn("c1", said)
    assert "changes_ambiguous" not in [e.get("code") for e in ev], ev
