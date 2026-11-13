"""Who may write stock. A stock write runs inside an approved card's commit (the flag core sets, naming an approval
that exists) or through the explicit Trusted path of an internal caller, which sets no flag. The flag with no approval
behind it is refused: the stale backstop fails closed. The Trusted path is not reachable from another plugin, the
assistant or the API: the services take no authority, and their inputs refuse any field they do not declare."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from erpclaw_core import approvals
from erpclaw_core.common import Refusal, company_ctx, new_id
from erpclaw_core.manifest import parse_use
from erpclaw_core.plugins import REGISTRY, call_service
from erpclaw_core.setup import create_company
from erpclaw_stock import service as stock
from erpclaw_stock import tables as t
from erpclaw_stock.service import Trusted

from stock_fixtures import CONSUMER, consumer
import stock_consumer as planted
from test_stock_commits import books, receive, stocked_books  # noqa: F401  (the demo books fixture and its helpers)
from service_plugins import register  # tests/ is on the path

WHEN = date(2026, 3, 12)


@pytest.fixture
def stocked(engine, monkeypatch):
    register(monkeypatch, consumer())
    cid = create_company(engine, f"Authority Co {new_id()[:8]}")
    trusted = Trusted("the authority tests' books")
    with engine.begin() as conn:
        ctx = company_ctx(conn, company_id=cid)
        wh = stock.create_warehouse(ctx, "Main", default=True)
        from erpclaw_core import models as m
        item = new_id()
        conn.execute(m.item.insert().values(id=item, company_id=cid, name="Bearing", unit="unit", standard_rate="1"))
        stock.make_stock_item(ctx, item, "moving_average")
        stock.receive(ctx, "pr-0", item, wh, "10", "2.00", date(2026, 3, 2), trusted=trusted)
        trusted.verify(ctx)
    return {"engine": engine, "company": cid, "item": item, "warehouse": wh}


def _rows(engine) -> tuple:
    with engine.connect() as conn:
        return tuple(conn.execute(select(func.count()).select_from(tbl)).scalar() for tbl in t.TABLES)


def _issue_input(s) -> dict:
    return {"voucher_type": "sales_invoice", "voucher": f"inv-{new_id()[:8]}", "posting_date": WHEN.isoformat(),
            "lines": [{"item": s["item"], "warehouse": s["warehouse"], "qty": "1"}]}


def test_the_commit_flag_with_no_approval_behind_it_is_refused(stocked):
    before = _rows(stocked["engine"])
    with stocked["engine"].begin() as conn:
        ctx = company_ctx(conn, company_id=stocked["company"])
        ctx.extra["commit"] = new_id()  # set by hand: no approval row has this id
        with pytest.raises(Refusal) as caught:
            call_service(ctx, CONSUMER, "stock.issue", _issue_input(stocked))
        assert caught.value.code == "service_outside_commit"
        with pytest.raises(Refusal) as caught:  # the module's own function, called directly, fails closed too
            stock.issue(ctx, "sales_invoice", "inv-x", WHEN, [{"item": stocked["item"],
                                                               "warehouse": stocked["warehouse"], "qty": "1"}])
        assert caught.value.code == "not_approved"
    assert _rows(stocked["engine"]) == before


def test_no_flag_and_no_trusted_authority_is_refused(stocked):
    with stocked["engine"].begin() as conn:
        ctx = company_ctx(conn, company_id=stocked["company"])
        with pytest.raises(Refusal) as caught:
            call_service(ctx, CONSUMER, "stock.issue", _issue_input(stocked))
        assert caught.value.code == "service_outside_commit"  # core's own rule for a write service
        for write in (lambda: stock.issue(ctx, "sales_invoice", "inv-y", WHEN, [
                          {"item": stocked["item"], "warehouse": stocked["warehouse"], "qty": "1"}]),
                      lambda: stock.receive(ctx, "pr-y", stocked["item"], stocked["warehouse"], "1", "1.00", WHEN),
                      lambda: stock.reverse_movements(ctx, "stock_receipt", "pr-0", WHEN, "stock_receipt_reversal",
                                                      "pr-0-rev")):
            with pytest.raises(Refusal) as caught:
                write()
            assert caught.value.code == "not_approved"


def test_the_trusted_path_writes_and_runs_its_comparison(stocked):
    trusted = Trusted("an internal job in this test")
    with stocked["engine"].begin() as conn:
        ctx = company_ctx(conn, company_id=stocked["company"])
        out = stock.issue(ctx, "sales_invoice", "inv-t", WHEN, [{"item": stocked["item"],
                                                                 "warehouse": stocked["warehouse"], "qty": "1"}],
                          trusted=trusted)
        assert out["lines"] == [{"cost": "2.00", "currency": "USD"}] and not ctx.checks  # nothing registered on the context
        assert len(trusted.checks) == 1
        with pytest.raises(Refusal) as caught:  # its posting was never made: the comparison refuses
            trusted.verify(ctx)
        assert caught.value.code == "stock_posting_mismatch"
        conn.rollback()


def test_the_trusted_path_never_runs_under_an_approval_flag_and_states_its_reason(stocked):
    with pytest.raises(ValueError):
        Trusted("")
    with stocked["engine"].begin() as conn:
        ctx = company_ctx(conn, company_id=stocked["company"])
        ctx.extra["commit"] = new_id()
        with pytest.raises(ValueError):
            stock.issue(ctx, "sales_invoice", "inv-z", WHEN, [{"item": stocked["item"],
                                                               "warehouse": stocked["warehouse"], "qty": "1"}],
                        trusted=Trusted("mixed with a flag"))
        conn.rollback()


def test_the_trusted_path_cannot_be_reached_through_a_service():
    """The services are the only way in from another plugin, the assistant or the API: none takes an authority, and
    an input naming one is refused before the service runs."""
    services = {k: v for k, v in REGISTRY.services.items() if k.startswith("stock.")}
    assert set(services) == {
        "stock.stock_on_hand", "stock.choose_warehouse", "stock.preview_issue", "stock.issue",
        "stock.preview_return", "stock.return_in", "stock.preview_reverse", "stock.reverse_movements",
        "stock.list_warehouses", "stock.preview_create_warehouse", "stock.create_warehouse",
        "stock.preview_make_stock_item", "stock.make_stock_item",
    }
    for name, sv in services.items():
        fields = set(sv.input_model.model_fields)
        assert not fields & {"trusted", "authority", "commit"}, name
        assert sv.input_model.model_config.get("extra") == "forbid", name


def test_an_input_naming_an_authority_is_refused_before_the_service_runs(stocked):
    before = _rows(stocked["engine"])
    with stocked["engine"].begin() as conn:
        ctx = company_ctx(conn, company_id=stocked["company"])
        with pytest.raises(Refusal) as caught:
            call_service(ctx, CONSUMER, "stock.preview_issue", {**_issue_input(stocked), "trusted": "yes"})
        assert caught.value.code == "service_input"
    assert _rows(stocked["engine"]) == before


# ---------------------------------------------------------------- the flag must name the commit running now

def _flagged_issue(engine, approval_id: str, company_id: str | None = None, **setup) -> Refusal:
    """A stock issue under a hand-set flag naming `approval_id`, after `setup` (status changes) in that transaction."""
    from erpclaw_core import models as m
    from sqlalchemy import update

    with engine.begin() as conn:
        if setup:
            conn.execute(update(m.approval).where(m.approval.c.id == approval_id).values(**setup))
        ctx = company_ctx(conn, company_id=company_id)
        wh = conn.execute(select(t.warehouse.c.id).where(t.warehouse.c.company_id == ctx.company_id)).scalars().first()
        item = conn.execute(select(t.stock_item.c.item_id).where(t.stock_item.c.company_id == ctx.company_id)
                            ).scalars().first()
        ctx.extra["commit"] = approval_id
        with pytest.raises(Refusal) as caught:
            stock.issue(ctx, "planted_sale", f"inv-{new_id()[:8]}", WHEN, [{"item": item, "warehouse": wh,
                                                                            "qty": "1"}])
        conn.rollback()
    return caught.value


@pytest.fixture
def demo(books):  # noqa: F811  (the demo books with stock, from the commit-path tests)
    receive(books, "10", "10.00")
    return books


def _card(demo, **over):
    from test_stock_commits import DAY
    args = dict(customer_id=demo["customer"], item_id=demo["item"], warehouse_id=demo["warehouse"], qty="1",
                price="10.00", voucher=f"inv-{new_id()[:8]}", posting_date=DAY.isoformat())
    return approvals.propose(demo["engine"], REGISTRY.contracts["stock_sale"], {**args, **over})


def test_a_flag_naming_a_pending_card_is_refused(demo):
    card = _card(demo)
    assert _flagged_issue(demo["engine"], card.approval_id).code == "not_approved"


def test_a_flag_naming_a_declined_card_is_refused(demo):
    card = _card(demo)
    approvals.decline(demo["engine"], card.approval_id)
    assert _flagged_issue(demo["engine"], card.approval_id).code == "not_approved"


def test_a_flag_naming_an_expired_card_is_refused(demo):
    card = _card(demo)
    assert _flagged_issue(demo["engine"], card.approval_id, status="expired").code == "not_approved"


def test_a_flag_naming_an_already_committed_card_is_refused(demo):
    card = _card(demo)
    assert approvals.approve(demo["engine"], card.approval_id, card.digest).status == "committed"
    assert _flagged_issue(demo["engine"], card.approval_id).code == "not_approved"


def test_a_flag_naming_another_operations_card_is_refused_even_while_claimed(demo, monkeypatch):
    """A card of the sales plugin, made to declare no stock service (it declares them as optional uses), claimed in
    this very transaction: its commit may not write stock."""
    from dataclasses import replace

    card = approvals.propose(demo["engine"], REGISTRY.contracts["create_invoice"], {
        "customer": "Acme Corporation", "items": [{"item": "copper wire", "quantity": 1}]})
    sales = REGISTRY.plugins["sales"]
    monkeypatch.setitem(REGISTRY.plugins, "sales", replace(sales, manifest=sales.manifest.model_copy(update={"uses": []})))
    assert _flagged_issue(demo["engine"], card.approval_id, status="consumed").code == "not_approved"


def test_the_card_being_committed_is_accepted(demo):
    """The same check passes inside a real commit: the planted sale's own card."""
    out = approvals.approve(demo["engine"], *(lambda c: (c.approval_id, c.digest))(_card(demo)))
    assert out.status == "committed", out.text


# ---------------------------------------------------------------- the card's plugin declares the stock service

def _declaring(engine, monkeypatch, declares: str) -> dict:
    """The demo books with stock, the planted sale's plugin declaring the stock services as `declares`."""
    books = stocked_books(engine, monkeypatch, planted.plugin(declares))
    receive(books, "10", "10.00")
    return books


def test_a_card_whose_plugin_declares_stock_only_as_optional_uses_commits(engine, monkeypatch):
    """An optional use ("stock.issue?") declares the service as a plain one does: the approved sale writes stock."""
    demo = _declaring(engine, monkeypatch, "optional")
    assert all(parse_use(u)[1] for u in REGISTRY.plugins[planted.NAME].manifest.uses)
    card = _card(demo)
    out = approvals.approve(demo["engine"], card.approval_id, card.digest)
    assert out.status == "committed", out.text
    with demo["engine"].connect() as conn:
        assert conn.execute(select(func.count()).select_from(t.entry).where(t.entry.c.direction == "out")).scalar() == 1


def test_a_card_whose_plugin_declares_no_stock_service_is_refused_while_claimed(engine, monkeypatch):
    """The same planted sale's card, its plugin then declaring stock in neither form: refused inside its own commit.
    (Such a plugin cannot preview the sale at all, so the card is made while it still declares the services.)"""
    demo = _declaring(engine, monkeypatch, "plain")
    card = _card(demo)
    monkeypatch.setitem(REGISTRY.plugins, planted.NAME, planted.plugin("none"))
    assert _flagged_issue(demo["engine"], card.approval_id, status="consumed").code == "not_approved"
