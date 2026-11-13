"""Payments to multi-word parties: every word of the resolved party is placed."""

import pytest
from sqlalchemy import func, select

from erpclaw_assistant.graph import Assistant
from erpclaw_assistant.model import Reply, ScriptedModel, call
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


def pay(engine, said, cid="c1", **args):
    return turns(engine, Reply("", [call("record_payment", **args)])).turn(cid, said)


def business_same(engine, before) -> bool:
    return count(engine, t.invoice) == before[0] and count(engine, t.payment) == before[1]


def test_payment_repeated_party_word_is_placed_not_asked(engine):
    """A word of the resolved party repeated outside its span is placed, never asked about."""
    from erpclaw_core.masters import create_party

    first = "".join(["am", "ber"])
    shared = "".join(["har", "bor"])
    third = "".join(["led", "ger"])
    name = " ".join([first, shared, third]).title()
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", name)
    amount = str(400 + 35)
    said = f"{name} paid {amount} by bank transfer, {shared}"
    before = (count(engine, t.invoice), count(engine, t.payment))
    ev = pay(engine, said, f"c{len(said)}", customer=name, amount=int(amount), method="bank transfer")
    assert kinds(ev) == ["card"], ev
    assert business_same(engine, before), ev
    for e in ev:
        assert e.get("type") != "ask", ev


def _party_case_names():
    a = "".join(["am", "ber"])
    s = "".join(["har", "bor"])
    c = "".join(["led", "ger"])
    d = "".join(["quar", "tz"])
    e = "".join(["mea", "dow"])

    def title(parts):
        return " ".join(parts).title()

    return [
        pytest.param(title([s, a]), "two-first", id="two-first"),
        pytest.param(title([a, s]), "two-last", id="two-last"),
        pytest.param(title([s, a, c]), "three-first", id="three-first"),
        pytest.param(title([a, s, c]), "three-middle", id="three-middle"),
        pytest.param(title([a, c, s]), "three-last", id="three-last"),
        pytest.param(title([s, a, c, d]), "four-first", id="four-first"),
        pytest.param(title([a, s, c, d]), "four-middle", id="four-middle"),
        pytest.param(title([a, c, d, s]), "four-last", id="four-last"),
    ]


@pytest.mark.parametrize("name,slot", _party_case_names())
def test_payment_full_party_name_cards_for_every_length_and_position(engine, name, slot):
    """Every word of a two-, three- or four-word party is placed, wherever the shared word sits."""
    from erpclaw_core.masters import create_party

    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", name)
    amount = str(300 + len(name) + len(slot))
    said = f"{name} paid {amount} by bank transfer"
    before = (count(engine, t.invoice), count(engine, t.payment))
    ev = pay(engine, said, f"c{len(said)}-{slot}", customer=name, amount=int(amount), method="bank transfer")
    assert kinds(ev) == ["card"], (slot, name, ev)
    assert business_same(engine, before), (slot, name, ev)


def test_payment_truly_unplaced_word_still_asks(engine):
    """A word belonging to no resolved party still gets the unplaced-word question."""
    from erpclaw_core.masters import create_party

    payer = " ".join(["".join(["am", "ber"]), "".join(["har", "bor"]), "".join(["led", "ger"])]).title()
    other = " ".join(["".join(["quar", "tz"]), "".join(["mea", "dow"]), "".join(["sig", "nal"])]).title()
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", payer)
        create_party(company_ctx(conn), "customer", other)
    extra = other.split()[0]
    amount = str(500 + len(payer))
    said = f"{payer} paid {amount} by bank transfer, {extra}"
    clean = nothing_written(engine)
    ev = pay(engine, said, f"c{len(said)}", customer=payer, amount=int(amount), method="bank transfer")
    assert kinds(ev) == ["ask"], (said, ev)
    assert ev[0]["code"] == "customer_ambiguous", ev
    assert extra.lower() in ev[0]["text"].lower(), ev
    assert clean(), ev


def test_payment_partial_name_word_is_not_placed(engine):
    """A word matching only part of a name word does not place the party."""
    from erpclaw_core.masters import create_party

    full = "".join(["har", "bor"])
    second = "".join(["led", "ger"])
    name = " ".join([full, second]).title()
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", name)
    part = full[:4]
    assert part != full and part in full
    said = f"{part.title()} {second.title()} paid {600 + len(name)} by bank transfer"
    clean = nothing_written(engine)
    ev = pay(engine, said, f"c{len(said)}", customer=name, amount=600 + len(name), method="bank transfer")
    assert kinds(ev) == ["ask"], (said, ev)
    assert ev[0]["code"] in ("customer_ambiguous", "customer_unknown", "customer_missing"), ev
    assert clean(), ev


def test_payment_shared_word_asks_never_silent_pick(engine):
    """One word shared by two parties asks with both offered, never a silent pick."""
    from erpclaw_core.masters import create_party

    left = "".join(["am", "ber"])
    shared = "".join(["har", "bor"])
    right = "".join(["quar", "tz"])
    first = " ".join([left, shared]).title()
    second = " ".join([right, shared]).title()
    with engine.begin() as conn:
        create_party(company_ctx(conn), "customer", first)
        create_party(company_ctx(conn), "customer", second)
    amount = str(700 + len(first) + len(second))
    said = f"{shared.title()} paid {amount} by bank transfer"
    for k, extra in enumerate(({"customer": first}, {"customer": second}, {})):
        clean = nothing_written(engine)
        ev = pay(engine, said, f"s{k}{len(said)}", amount=int(amount), method="bank transfer", **extra)
        assert kinds(ev) == ["ask"], (extra, ev)
        assert ev[0]["code"] == "customer_ambiguous", (extra, ev)
        assert first in ev[0]["options"] and second in ev[0]["options"], (extra, ev)
        assert clean(), (extra, ev)
