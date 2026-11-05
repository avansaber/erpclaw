"""Payments through the resolution step: every field of a payment, cancellation or reversal card comes from the
owner's words or stays at its stated default, and anything the engine cannot read with certainty is asked, never
taken from the model."""
import pytest
from sqlalchemy import func, select

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_core import ledger
from erpclaw_core import models as m
from erpclaw_core.common import company_ctx
from erpclaw_sales import tables as t


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


def lines(event) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in event["card"]["lines"]:
        out.setdefault(k, []).append(v)
    return out


def pay(engine, said, cid="c1", **args):
    return turns(engine, Reply("", [call("record_payment", **args)])).turn(cid, said)


def _add_bank(engine, number, name):
    with engine.begin() as conn:
        ctx = company_ctx(conn)
        parent = ledger.account_by_number(ctx, "1000")["id"]
        conn.execute(m.account.insert().values(id=f"acct-{number}", company_id=ctx.company_id, number=number,
                                               name=name, root_type="asset", account_type="bank",
                                               is_group=False, parent_id=parent))


# ---------- the amount: sign, grouping and currency read whole ----------

def test_a_payment_amount_is_read_whole_or_asked_never_in_part(engine):
    """A sign, a grouping or a currency mark written with the amount is part of it: read exactly, or asked."""
    cases = [  # owner's words, the model's amount (None: left out), the card's amount (None: no card)
        ("Hooli paid 500", 500, "500.00 USD"), ("Hooli paid $500", 500, "500.00 USD"),
        ("Hooli paid 1,500", 1500, "1,500.00 USD"), ("Hooli paid 1,500", 500, None),
        ("Hooli paid -500", 500, None), ("Hooli paid -500", None, None), ("Hooli paid - 500", 500, None),
        ("Hooli paid 1 500", 500, None), ("Hooli paid 1 500", 1500, None), ("Hooli paid 1 500", None, None),
        ("Hooli paid €500", 500, None), ("Hooli paid €-500", 500, None), ("Hooli paid -€500", 500, None),
        ("Hooli paid 500 EUR", 500, None),
    ]
    clean = nothing_written(engine)
    wrong = []
    for k, (said, given, want) in enumerate(cases):
        args = {"customer": "Hooli", **({"amount": given} if given is not None else {})}
        ev = pay(engine, said, f"a{k}", **args)
        got = lines(ev[-1])["Amount received"][0] if kinds(ev) and kinds(ev)[-1] == "card" else None
        if got != want:
            wrong.append((said, given, want, got, kinds(ev)))
    assert not wrong, wrong
    del clean  # the cards above are made by design; none is approved


def test_a_negative_or_foreign_amount_writes_nothing(engine):
    clean = nothing_written(engine)
    for k, said in enumerate(("Hooli paid -500", "Hooli paid €-500", "Hooli paid 1 500")):
        ev = pay(engine, said, f"n{k}", customer="Hooli", amount=500)
        assert "card" not in kinds(ev), (said, ev)
    assert clean()


# ---------- the payer: the owner's party phrase before any derivation ----------

def test_a_payer_the_owner_wrote_is_resolved_even_when_the_model_leaves_the_customer_out(engine):
    """INV-2026-00006 is Pied Piper's. A payer the owner wrote, unknown or partial, asks; the invoice's customer is
    taken only when the owner wrote no payer at all."""
    clean = nothing_written(engine)
    for k, said in enumerate(("Nobody paid 100 against INV-2026-00006", "Acme paid 100 against INV-2026-00006",
                              "We got 100 from Nobody against INV-2026-00006")):
        ev = pay(engine, said, f"p{k}", amount=100, invoice="INV-2026-00006")
        assert kinds(ev) == ["ask"], (said, ev)
    assert clean()
    ev = pay(engine, "INV-2026-00006 was paid, 100 by wire", "p-none", amount=100, invoice="INV-2026-00006")
    assert kinds(ev) == ["card"] and lines(ev[0])["Customer"][0].startswith("Pied Piper"), ev


def test_a_payer_naming_no_known_customer_asks_and_never_falls_back_to_the_invoice_s_customer(engine):
    """The guard: whether the model leaves the customer out or passes the invoice's own customer, an unknown payer is
    asked about by name, with no card."""
    clean = nothing_written(engine)
    for k, extra in enumerate(({}, {"customer": "Pied Piper"})):
        ev = pay(engine, "Nobody paid 100 against INV-2026-00006", f"g{k}", amount=100, invoice="INV-2026-00006",
                 **extra)
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "customer_unknown" and "Nobody" in ev[0]["text"], (extra, ev)
    assert clean()


# ---------- the account, the method and the reference ----------

def test_the_account_the_owner_named_is_used_never_the_model_s(engine):
    _add_bank(engine, "1110", "Savings")
    ev = pay(engine, "Hooli paid 100 into Savings", "s1", customer="Hooli", amount=100, account="1100")
    assert kinds(ev) in (["card"], ["ask"]), ev
    if kinds(ev) == ["card"]:
        assert lines(ev[0])["Account"] == ["1110 Savings"], ev[0]["card"]["lines"]


def test_an_account_the_owner_never_named_is_asked_among_several(engine):
    _add_bank(engine, "1110", "Savings")
    clean = nothing_written(engine)
    ev = pay(engine, "Hooli paid 100", "s2", customer="Hooli", amount=100, account="Savings")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "account_ambiguous", ev
    assert clean()


def test_a_method_or_reference_the_owner_never_wrote_stays_at_its_default(engine):
    ev = pay(engine, "Hooli paid 500", "r1", customer="Hooli", amount=500, method="check", reference="CHK-4411")
    assert kinds(ev) == ["card"], ev
    shown = lines(ev[0])
    assert shown["Method"] == ["not stated (will be saved as not recorded)"], shown
    assert "Reference" not in shown, shown


def test_a_method_and_reference_the_owner_wrote_are_used(engine):
    ev = pay(engine, "Hooli paid 500 by check CHK-4411", "r2", customer="Hooli", amount=500, method="check",
             reference="CHK-4411")
    assert kinds(ev) == ["card"], ev
    shown = lines(ev[0])
    assert shown["Method"] == ["check"] and shown["Reference"] == ["CHK-4411"], shown


# ---------- which invoice: oldest first and a named number ----------

def test_oldest_first_needs_the_owner_s_words(engine):
    """Pied Piper has two open invoices: without the owner's "oldest first", the engine asks which."""
    clean = nothing_written(engine)
    ev = pay(engine, "Pied Piper paid us 500", "o1", customer="Pied Piper", amount=500, oldest_first=True)
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "invoice_ambiguous", ev
    assert clean()
    ev = pay(engine, "Pied Piper paid us 500, apply it oldest first", "o2", customer="Pied Piper", amount=500,
             oldest_first=True)
    assert kinds(ev) == ["card"], ev
    assert lines(ev[0])["Applied to"][0].startswith("INV-2026-00003"), ev[0]["card"]["lines"]


def test_the_invoice_the_owner_named_is_kept_whatever_the_model_passed(engine):
    for k, extra in enumerate(({}, {"oldest_first": True})):
        ev = pay(engine, "Pied Piper paid 100 against INV-2026-00006", f"i{k}", customer="Pied Piper", amount=100,
                 invoice="INV-2026-00003", **extra)
        assert kinds(ev) in (["card"], ["ask"]), ev
        if kinds(ev) == ["card"]:
            applied = [v.split()[0] for v in lines(ev[0]).get("Applied to", [])]
            assert applied == ["INV-2026-00006"], ev[0]["card"]["lines"]


# ---------- document alternatives ----------

def test_either_of_two_invoices_to_cancel_asks_whatever_the_model_passed(engine):
    clean = nothing_written(engine)
    for k, given in enumerate(("INV-2026-00003", "INV-2026-00006")):
        ev = turns(engine, Reply("", [call("cancel_invoice", invoice=given)])).turn(
            f"x{k}", "cancel either INV-2026-00003 or INV-2026-00006")
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "invoice_ambiguous", (given, ev)
    assert clean()


def test_either_of_two_invoices_to_pay_asks_whatever_the_model_passed(engine):
    clean = nothing_written(engine)
    ev = pay(engine, "Pied Piper paid 100 against either INV-2026-00003 or INV-2026-00006", "x3",
             customer="Pied Piper", amount=100, invoice="INV-2026-00006")
    assert kinds(ev) == ["ask"] and ev[0]["code"] == "invoice_ambiguous", ev
    assert clean()


# ---------- each field from its own phrase: the account, the payer, the reversal's payer, free text ----------

def paid(engine, customer: str, amount: str) -> str:
    """A payment in the books, by the service (no card): its number."""
    from decimal import Decimal

    from erpclaw_core.common import company_ctx as cc
    from erpclaw_sales import service as s

    with engine.begin() as conn:
        ctx = cc(conn)
        cust = dict(conn.execute(select(m.party).where(m.party.c.name == customer)).mappings().one())
        r = s.record_payment(ctx, s.plan_payment(ctx, cust, Decimal(amount), ctx.today, oldest_first=True))
        return conn.execute(select(t.payment.c.number).where(t.payment.c.id == r["id"])).scalar_one()


def test_an_account_named_after_the_word_account_is_used_or_asked_never_the_default(engine):
    _add_bank(engine, "1110", "Savings")
    clean = nothing_written(engine)
    for k, extra in enumerate(({}, {"account": "Missing"}, {"account": "1100"})):
        ev = pay(engine, "Hooli paid 100 into account Missing", f"m{k}", customer="Hooli", amount=100, **extra)
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "account_unknown", (extra, ev)
    assert clean()
    ev = pay(engine, "Hooli paid 100 into account Savings", "m-ok", customer="Hooli", amount=100)
    assert kinds(ev) == ["card"] and lines(ev[0])["Account"] == ["1110 Savings"], ev


def test_each_payer_is_resolved_from_its_own_phrase_never_from_another_customer_in_the_request(engine):
    clean = nothing_written(engine)
    said = "Nobody paid 100 against INV-2026-00006 for Pied Piper"
    for k, extra in enumerate(({}, {"customer": "Pied Piper"})):
        ev = pay(engine, said, f"e{k}", amount=100, invoice="INV-2026-00006", **extra)
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "customer_unknown" and "Nobody" in ev[0]["text"], (extra, ev)
    ev = pay(engine, "Hooli paid 100, paid by Pied Piper", "e2", customer="Hooli", amount=100)
    assert kinds(ev) == ["ask"], ev  # two payers written: the owner says which
    assert clean()


def test_a_reversal_whose_named_customer_is_not_the_payment_s_asks(engine):
    number = paid(engine, "Hooli", "100")
    clean = nothing_written(engine)
    for k, extra in enumerate(({"customer": "Pied Piper"}, {})):
        ev = turns(engine, Reply("", [call("reverse_payment", payment=number, **extra)])).turn(
            f"v{k}", f"reverse {number} from Pied Piper, it bounced")
        assert kinds(ev) == ["ask"], (extra, ev)
    assert clean()
    ev = turns(engine, Reply("", [call("reverse_payment", payment=number, customer="Hooli")])).turn(
        "v-ok", f"it bounced: reverse {number} from Hooli")
    assert kinds(ev) == ["card"], ev


def test_a_reference_is_kept_exactly_as_the_owner_wrote_it(engine):
    ev = pay(engine, "Hooli paid 100, reference AB-CD", "f1", customer="Hooli", amount=100, reference="ABCD")
    assert kinds(ev) == ["card"] and lines(ev[0])["Reference"] == ["AB-CD"], ev


def test_part_of_a_written_reference_is_never_carded_as_the_whole(engine):
    clean = nothing_written(engine)
    ev = pay(engine, "Hooli paid 100, reference ABC DEF", "f2", customer="Hooli", amount=100, reference="ABC")
    assert kinds(ev) == ["ask"] or (kinds(ev) == ["card"] and lines(ev[0])["Reference"] == ["ABC DEF"]), ev
    if kinds(ev) == ["ask"]:
        assert clean()


def test_a_method_the_owner_wrote_is_used_or_asked_whatever_the_model_passed(engine):
    for k, extra in enumerate(({}, {"method": "wire"}, {"method": "bank transfer"})):
        ev = pay(engine, "Hooli paid 100 by bank transfer", f"t{k}", customer="Hooli", amount=100, **extra)
        assert kinds(ev) == ["ask"] or (kinds(ev) == ["card"] and lines(ev[0])["Method"] == ["bank transfer"]), (
            extra, ev)
    ev = pay(engine, "Hooli paid 100 in cash", "t-cash", customer="Hooli", amount=100)
    assert kinds(ev) == ["card"] and lines(ev[0])["Method"] == ["cash"], ev


# ---------- every phrase of a kind accounted for: several ask, none dropped, one tokenisation ----------

def test_any_account_cue_names_the_account(engine):
    _add_bank(engine, "1110", "Savings")
    clean = nothing_written(engine)
    for k, said in enumerate(("Hooli paid 100 to account Missing", "Hooli paid 100 into the Missing account",
                              "Hooli paid 100, deposited to account Missing")):
        ev = pay(engine, said, f"ac{k}", customer="Hooli", amount=100)
        assert kinds(ev) == ["ask"] and ev[0]["code"] == "account_unknown", (said, ev)
    ev = pay(engine, "Hooli paid 100 into Savings or into account 1100", "ac-two", customer="Hooli", amount=100,
             account="Savings")
    assert kinds(ev) == ["ask"], ev
    assert clean()


@pytest.mark.parametrize("said", ["Nobody and Hooli paid 100", "Hooli and Nobody paid 100",
                                  "Hooli or Pied Piper paid 100", "Hooli, Pied Piper paid 100"])
def test_every_payer_written_is_resolved_or_asked(engine, said):
    clean = nothing_written(engine)
    for k, extra in enumerate(({"customer": "Hooli"}, {})):
        ev = pay(engine, said, f"cp{k}", amount=100, **extra)
        assert kinds(ev) == ["ask"], (said, extra, ev)
    assert clean()


def test_a_reversal_s_payer_phrase_is_resolved_before_the_payment_s_customer(engine):
    number = paid(engine, "Hooli", "100")
    clean = nothing_written(engine)
    for k, said in enumerate((f"reverse {number} from Nobody", f"reverse Nobody's payment {number}")):
        ev = turns(engine, Reply("", [call("reverse_payment", payment=number)])).turn(f"rv{k}", said)
        assert kinds(ev) == ["ask"], (said, ev)
    assert clean()


@pytest.mark.parametrize("sep", ["\n", "\t", "\n\n", " \n "])
def test_line_breaks_and_tabs_read_like_spaces(engine, sep):
    for k, extra in enumerate(({}, {"method": "cash"})):
        ev = pay(engine, f"Hooli paid 100{sep}by cash", f"nl{k}", customer="Hooli", amount=100, **extra)
        assert kinds(ev) == ["card"] and lines(ev[0])["Method"] == ["cash"], (repr(sep), extra, ev)


@pytest.mark.parametrize("said", ["Hooli paid 100, reference ABC. reference DEF",
                                  "Hooli paid 100. note: first one. memo: second one"])
def test_every_reference_or_note_phrase_is_used_or_asked(engine, said):
    clean = nothing_written(engine)
    for k, extra in enumerate(({"reference": "ABC"}, {})):
        ev = pay(engine, said, f"rf{k}", customer="Hooli", amount=100, **extra)
        assert kinds(ev) == ["ask"], (said, extra, ev)
    assert clean()


@pytest.mark.parametrize("given", [None, "cash", "wire", "check"])
def test_two_written_methods_ask_whatever_the_model_passed(engine, given):
    clean = nothing_written(engine)
    extra = {"method": given} if given else {}
    ev = pay(engine, "Hooli paid 100 by cash or by wire", "mm", customer="Hooli", amount=100, **extra)
    assert kinds(ev) == ["ask"], (given, ev)
    assert clean()


def test_an_explicit_method_the_table_does_not_know_asks(engine):
    clean = nothing_written(engine)
    for k, extra in enumerate(({"method": "Zelle"}, {})):
        ev = pay(engine, "Hooli paid 100 by method Zelle", f"z{k}", customer="Hooli", amount=100, **extra)
        assert kinds(ev) == ["ask"], (extra, ev)
    assert clean()


def test_a_line_break_reads_like_a_space_on_the_invoice_path(engine):
    ev = turns(engine, Reply("", [call("create_invoice", customer="Hooli",
                                       items=[{"item": "copper wire", "quantity": 5}])])).turn(
        "iv", "invoice Hooli\nfor 5 copper wire")
    assert kinds(ev) == ["card"] and lines(ev[0])["Customer"][0].startswith("Hooli"), ev


def test_a_run_of_whitespace_inside_a_city_still_names_its_customer(engine):
    """A whole-string comparison: the owner's words, normalised, must hold the city with single spaces."""
    ev = turns(engine, Reply("", [call("create_invoice", items=[{"item": "copper wire", "quantity": 5}])])).turn(
        "ct", "invoice our customer in Mountain\t\nView for 5 copper wire")
    assert kinds(ev) == ["card"] and lines(ev[0])["Customer"][0].startswith("Hooli"), ev


# ---------- the fallback after round 3: a named destination or method is used or asked, never the default ----------

@pytest.mark.parametrize("extra", [{}, {"account": "1100"}, {"account": "Bank"}])
def test_an_account_mentioned_anywhere_is_used_or_asked_never_the_default(engine, extra):
    _add_bank(engine, "1110", "Savings")
    for k, said in enumerate(("Hooli paid 100 to Savings", "Hooli paid 100, Savings")):
        ev = pay(engine, said, f"to{k}", customer="Hooli", amount=100, **extra)
        assert kinds(ev) == ["ask"] or (kinds(ev) == ["card"] and lines(ev[0])["Account"] == ["1110 Savings"]), (
            said, extra, ev)


def test_two_accounts_ask_one_detail_at_a_time(engine):
    _add_bank(engine, "1110", "Savings")
    clean = nothing_written(engine)
    for k, said in enumerate(("Hooli paid 100 into Bank and Missing", "Hooli paid 100 into Bank or Savings")):
        ev = pay(engine, said, f"aa{k}", customer="Hooli", amount=100, account="Bank")
        assert kinds(ev) == ["ask"] and ev[0]["text"].startswith("One detail at a time: I see two accounts"), (said, ev)
    assert clean()


@pytest.mark.parametrize("given", [None, "cash", "wire"])
def test_coordinated_methods_ask_one_detail_at_a_time(engine, given):
    clean = nothing_written(engine)
    extra = {"method": given} if given else {}
    ev = pay(engine, "Hooli paid 100 by cash or wire", "cw", customer="Hooli", amount=100, **extra)
    assert kinds(ev) == ["ask"] and ev[0]["text"] == (
        "One detail at a time: I see two payment methods (cash, wire). Which one?"), (given, ev)
    assert clean()


def test_an_explicit_known_method_is_used_when_the_model_leaves_it_out(engine):
    ev = pay(engine, "Hooli paid 100 by method cash", "mc", customer="Hooli", amount=100)
    assert kinds(ev) == ["card"] and lines(ev[0])["Method"] == ["cash"], ev


@pytest.mark.parametrize("said", ["Hooli paid 100 to Missing", "Hooli paid 100 into Nowhere",
                                  "Hooli paid 100 via Zelle", "Hooli paid 100 by Zelle"])
def test_an_unrecognised_phrase_after_a_cue_asks(engine, said):
    clean = nothing_written(engine)
    for k, extra in enumerate(({}, {"method": "Zelle"}, {"account": "1100"})):
        ev = pay(engine, said, f"uc{k}", customer="Hooli", amount=100, **extra)
        assert kinds(ev) == ["ask"], (said, extra, ev)
    assert clean()
