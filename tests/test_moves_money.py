"""A write that says it moves no money is checked, not trusted: its own scenario, drafted and approved through the real
card path, leaves the books' money byte-identical. The snapshot comes from the schema: every table of the books with a
money column, and the ledger heads by name. A scenario lives in its plugin's tests (tests/<plugin>/scenarios.py,
NO_MONEY_SCENARIOS), and a write that says it moves no money without one fails here."""
from __future__ import annotations

import dataclasses
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import select, update

from erpclaw_core import approvals, backup as bk, models as m
from erpclaw_core.contracts import Contract, Input, Preview, Result, Shown, WriteContract, moves_money
from erpclaw_core.db import Money, MoneyText
from erpclaw_core.plugins import REGISTRY
from service_plugins import install  # tests/ is on the path
from test_plugins import plug

TESTS = Path(__file__).resolve().parent
# the ledger tables that hold no money column of their own, named, never inferred: a posting's header (its amounts are in
# ledger_entry) and each ledger head (a hash and a count)
LEDGER_NAMED = ("posting", "posting_chain_head", "stock_head")


def money_snapshot(engine) -> dict[str, list[str]]:
    """Every table of these books that has a money column, and each ledger head, all rows in their canonical form."""
    with engine.connect() as conn:
        tables = [t for t in bk.books_tables(conn)
                  if t.name in LEDGER_NAMED or any(isinstance(c.type, (Money, MoneyText)) for c in t.columns)]
        return {t.name: bk._dump(conn, t) for t in tables}


def approve_leaves_money(engine, contract: Contract, args: dict, owner_words: str = "") -> None:
    """Draft the card through the real path, approve it, and require the books' money to be unchanged."""
    before = money_snapshot(engine)
    assert before["posting"] and before["ledger_entry"], "an empty ledger cannot show a write left it unchanged"
    card = approvals.propose(engine, contract, args, owner_words=owner_words or None)
    assert card.status == "card", (card.status, card.text)
    assert card.card["amounts"] == [], f"{contract.name} says it moves no money, and its card shows amounts"
    out = approvals.approve(engine, card.approval_id, card.digest)
    assert out.status == "committed", (out.status, out.text)
    after = money_snapshot(engine)
    changed = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
    assert not changed, f"{contract.name} says it moves no money, and it changed {changed}"


def _scenarios() -> dict:
    found = {}
    for path in sorted(TESTS.glob("*/scenarios.py")):
        spec = importlib.util.spec_from_file_location(f"scenarios_{path.parent.name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        found.update(getattr(module, "NO_MONEY_SCENARIOS", {}))
    return found


NO_MONEY = sorted(c.name for c in REGISTRY.contracts.values() if c.effect != "read" and not moves_money(c)) \
    if REGISTRY.contracts else []


def test_every_write_that_moves_no_money_has_its_scenario():
    assert set(NO_MONEY) <= set(_scenarios()), "a write that says it moves no money needs NO_MONEY_SCENARIOS"


@pytest.mark.parametrize("name", NO_MONEY)
def test_a_write_that_says_it_moves_no_money_leaves_the_money_unchanged(request, engine, name):
    args, words = _scenarios()[name](request, engine)
    approve_leaves_money(engine, REGISTRY.contracts[name], args, words)


def test_the_snapshot_holds_the_ledger_its_documents_and_the_heads(engine):
    names = set(money_snapshot(engine))
    assert {"posting", "ledger_entry", "posting_chain_head", "sales_invoice"} <= names


# --- planted writes that say they move no money --------------------------------------------------------------------

class _Act(Input):
    act: Literal["nothing", "post", "head"]


def _preview(ctx, inp) -> Preview:
    sh = Shown(ctx.currency)
    sh.title("Planted change")
    return Preview(sh.card(), "t")


def _commit(ctx, inp: _Act, key: str) -> Result:
    if inp.act == "post":
        from erpclaw_core import ledger

        a, b = ctx.conn.execute(select(m.account.c.id).where(  # two plain accounts: no party needed
            m.account.c.company_id == ctx.company_id, m.account.c.is_group.is_(False),
            m.account.c.account_type.not_in(("receivable", "payable"))).order_by(m.account.c.number).limit(2)).scalars()
        ledger.post(ctx, ctx.today, "journal", key,
                    [ledger.Line(a, debit=Decimal("1")), ledger.Line(b, credit=Decimal("1"))])
    elif inp.act == "head":
        ctx.conn.execute(update(m.posting_chain_head).where(m.posting_chain_head.c.company_id == ctx.company_id)
                         .values(last_hash="0" * 64))
    return Result("Done.")


PLANTED = WriteContract("nomoney_change", "nomoney", "write", "Planted.", _Act, preview=_preview, commit=_commit,
                        find_by_operation_key=lambda ctx, key: None, read_set=lambda ctx, inp: [],
                        moves_money=False)


@pytest.fixture
def planted(engine, monkeypatch):
    install(monkeypatch, engine, plug("nomoney", contracts=[PLANTED]))
    return REGISTRY.contracts["nomoney_change"]


def test_a_write_that_changes_nothing_passes(engine, planted):
    approve_leaves_money(engine, planted, {"act": "nothing"})


@pytest.mark.parametrize("act, table", [("post", "posting"), ("head", "posting_chain_head")])
def test_a_write_that_says_it_moves_no_money_and_writes_the_ledger_fails(engine, planted, act, table):
    with pytest.raises(AssertionError, match=table):
        approve_leaves_money(engine, planted, {"act": act})


def test_a_write_that_says_it_moves_no_money_and_issues_stock_fails(engine, monkeypatch):
    sys.path.insert(0, str(TESTS / "stock"))
    import stock_fixtures as sf

    install(monkeypatch, engine, sf.consumer())
    real = REGISTRY.contracts[sf.FIXTURE_WRITE.name]
    dressed = WriteContract(**{f.name: getattr(real, f.name) for f in dataclasses.fields(Contract)},
                            moves_money=False)
    monkeypatch.setitem(REGISTRY.contracts, real.name, dressed)
    case = sf.load(sf.FIXTURES / "demo_books_issue_copper_wire_issue.json")
    books = sf.Books(engine, case)
    with engine.connect() as conn:
        assert {"stock_ledger_entry", "stock_head"} <= {t.name for t in bk.books_tables(conn)}
    before = money_snapshot(engine)
    books.call(case["service"], case["input"], write=True)
    after = money_snapshot(engine)
    assert before["stock_ledger_entry"] != after["stock_ledger_entry"]
    assert "stock_head" in after
