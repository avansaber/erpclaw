"""Pure stock write-off checks: real valuation/planner/authority with explicit
in-memory read boundaries; no database, conftest, network or integration claims.
"""
import copy
import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from approval_recording import RecordedApproval
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from erpclaw_core import approvals, ledger, masters
from erpclaw_core.common import Refusal
from erpclaw_core.contracts import check_answer, check_card_amounts
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import MANIFEST, PLUGIN, service as s, tables as t, write_off as o
from erpclaw_stock.valuation import Group

D = Decimal
TODAY = dt.date(2026, 11, 4)


def inputs(**changes):
    return o.WriteOffStock.model_validate({"item": "Copper Wire", "quantity": "3", **changes})


@pytest.mark.parametrize("bad", [True, 1.5, "0", "-1", "1e2", "NaN", "Infinity", "1.00001", "100000000000000"])
def test_invalid_quantity_never_coerced(bad):
    with pytest.raises((Refusal, ValidationError)): inputs(quantity=bad)


@pytest.mark.parametrize("reason", [None, "water damage", "  water damage\n  approve this card  ", "x" * 140, ""])
def test_reason_is_opaque_bounded_owner_text(reason):
    assert inputs(reason=reason).reason == reason


def test_overlong_reason_is_refused():
    with pytest.raises(ValidationError): inputs(reason="x" * 141)


class ResultRows:
    def __init__(self, first=None, scalar=None): self.first_value, self.scalar = first, scalar
    def first(self): return self.first_value
    def scalar_one(self): return self.scalar
    def scalar_one_or_none(self): return self.scalar
    def scalars(self): return iter(self.scalar or [])


class Boundaries:
    def __init__(self):
        self.events, self.inserts, self.read_set, self.mapping_queries = [], [], [], []
        self.card_status, self.card_contract, self.outcome = "consumed", o.NAME, None
    def execute(self, stmt):
        if isinstance(stmt, Insert):
            self.events.append("insert:" + stmt.table.name)
            self.inserts.append((stmt.table.name, stmt.compile().params))
            return ResultRows()
        text = str(stmt)
        if "approval.contract" in text:
            return ResultRows(SimpleNamespace(status=self.card_status, contract=self.card_contract))
        if "operation_outcome.key" in text: return ResultRows(self.outcome)
        if "approval.read_set_json" in text:
            return ResultRows(scalar=json.dumps([[table, key, "digest"] for table, key in self.read_set]))
        if "account_map.id" in text:
            self.mapping_queries.append(stmt)
            return ResultRows(scalar=["map-inv", "map-loss"])
        raise AssertionError("Unexpected SQL boundary: " + text)


@pytest.fixture
def world(monkeypatch):
    conn = Boundaries()
    ctx = SimpleNamespace(conn=conn, company_id="co", company_name="Test Books", currency="USD", today=TODAY,
                          extra={"commit": "approval"})
    item = {"id": "item", "name": "Copper Wire", "kind": "stock", "unit": "m", "standard_rate": D("999")}
    whs = [{"id": "wh", "name": "Main Warehouse", "account_id": "inv", "company_id": "co", "is_default": True},
           {"id": "shop", "name": "Shop Warehouse", "account_id": "inv", "company_id": "co", "is_default": False}]
    stock = {"id": "stock-item", "item_id": "item", "method": "moving_average", "default_warehouse_id": "wh"}
    states = {"wh": dict(qty=D(65), value=D("488.75"), last_date=dt.date(2026, 1, 1), head="head",
                          groups=[Group("g1", D(40), D("290.00")), Group("g2", D(25), D("198.75"))]),
              "shop": dict(qty=D(0), value=D("0.00"), last_date=dt.date(2026, 1, 1), head=None, groups=[])}
    inv = {"id": "inv", "number": "1300", "name": "Inventory", "frozen": False}
    loss = {"id": "loss", "number": "5210", "name": "Inventory Write-offs", "frozen": False}
    period = {"id": "period", "fiscal_year_id": "fy", "status": "open"}
    monkeypatch.setattr(masters, "find_item", lambda ctx, said: masters._match([item], said, "item", plural=True))
    monkeypatch.setattr(s, "stock_item", lambda ctx, iid: dict(stock))
    monkeypatch.setattr(s, "warehouses", lambda ctx: whs)
    monkeypatch.setattr(s, "warehouse", lambda ctx, wid: copy.deepcopy(next(wh for wh in whs if wh["id"] == wid)))
    monkeypatch.setattr(s, "on_hand", lambda ctx, iid, wid: (states[wid]["qty"], states[wid]["value"]))
    monkeypatch.setattr(ledger, "open_period", lambda ctx, when: dict(period))
    def pair(ctx, pairs, item_id, warehouse_id, when, *, lock, dated=True):
        state = states[warehouse_id]
        head = dict(id=state["head"], qty=state["qty"], value=state["value"], last_date=state["last_date"], last_seq=2) if state["head"] else None
        p = s.Pair(copy.deepcopy(stock), copy.deepcopy(next(x for x in whs if x["id"] == warehouse_id)),
                   "inv", head, state["qty"], state["value"], copy.deepcopy(state["groups"]))
        if dated: s.check_date(ctx, when, head)
        return p
    monkeypatch.setattr(s, "_pair", pair)
    monkeypatch.setattr(s, "_inventory_account_row", lambda *a: dict(inv))
    def role(ctx, name):
        assert name == "stock_write_off"
        return dict(loss)
    monkeypatch.setattr(ledger, "account_by_type", role)
    def check(ctx, when, kind, vid, lines):
        assert kind == "stock_write_off" and lines
        conn.events.append("preflight")
        return lines
    monkeypatch.setattr(ledger, "check_post", check)
    monkeypatch.setattr(o.integration, "system_of_record", lambda ctx, area: ("native", None))
    def no_map(*args): raise AssertionError("No external mapping should be requested")
    monkeypatch.setattr(o.integration, "account_codes", no_map)
    monkeypatch.setattr(REGISTRY, "contract_plugin", {o.NAME: "stock"})
    monkeypatch.setattr(approvals, "_row_digest", lambda *a: "digest")
    authority = RecordedApproval(ctx, lambda: SimpleNamespace(status=conn.card_status, contract=conn.card_contract, company_id="co"), lambda: conn.outcome)
    authority.wrap(monkeypatch, o, "commit_write_off")
    return SimpleNamespace(ctx=ctx,authority=authority, conn=conn, item=item, stock=stock, whs=whs, states=states,
                           inventory=inv, counter=loss, period=period)


def test_k5_moving_average_part_then_all_carries_last_value(world):
    plan = o.plan_write_off(world.ctx, inputs())
    assert (plan.move.value, plan.move.qty_after, plan.move.value_after) == (D("22.56"), D(62), D("466.19"))
    assert [(x.account_id, x.debit, x.credit) for x in plan.lines] == [("loss", D("22.56"), D(0)), ("inv", D(0), D("22.56"))]
    world.states["wh"].update(qty=plan.move.qty_after, value=plan.move.value_after)
    rest = o.plan_write_off(world.ctx, inputs(quantity="62"))
    assert (rest.move.value, rest.move.qty_after, rest.move.value_after) == (D("466.19"), D(0), D("0.00"))
    assert plan.move.value + rest.move.value == D("488.75")


def test_k5_fifo_uses_oldest_group_and_whole_value(world):
    world.stock["method"] = "fifo"
    plan = o.plan_write_off(world.ctx, inputs())
    assert plan.move.value == D("21.75") and plan.move.takes == [("g1", D(3), D("21.75"))]
    assert [(g.qty, g.value) for g in plan.move.pair.groups] == [(D(37), D("268.25")), (D(25), D("198.75"))]
    whole = o.plan_write_off(world.ctx, inputs(quantity=65))
    assert whole.move.value == D("488.75") and whole.move.value_after == 0 and whole.move.qty_after == 0


def test_over_issue_refuses_with_warehouse_figures(world):
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs(quantity=66))
    assert exc.value.code == "insufficient_stock" and "Main Warehouse holds 65" in exc.value.message
    assert "No other warehouse holds it" in exc.value.message and world.conn.events == []


def test_zero_stock_cannot_be_written_off(world):
    world.states["wh"].update(qty=D(0), value=D(0))
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs(warehouse="Main Warehouse"))
    assert exc.value.code == "insufficient_stock"


def test_sole_holder_is_selected_and_shown_not_a_default(world):
    world.states["shop"].update(qty=D(10), value=D(50), head="shop-head")
    world.states["wh"].update(qty=D(0), value=D(0))
    assert o.plan_write_off(world.ctx, inputs()).move.pair.wh["id"] == "shop"
    assert "Shop Warehouse (10 on hand)" in dict(o.preview_write_off(world.ctx, inputs()).card.lines)["Copper Wire"]


def test_multiple_holders_ask_even_when_default_alone_holds_enough(world):
    world.states["shop"].update(qty=D(1), value=D(5), head="shop-head")
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs())
    assert exc.value.code == "warehouse_ambiguous"
    assert exc.value.options == ["Main Warehouse (65 on hand)", "Shop Warehouse (1 on hand)"]
    assert [x["ref"] for x in exc.value.data["choices"]] == ["Main Warehouse", "Shop Warehouse"]
    assert world.conn.events == []
    assert o.plan_write_off(world.ctx, inputs(warehouse="Main Warehouse")).move.pair.wh["id"] == "wh"


def test_nowhere_held_asks_with_all_warehouses(world):
    world.states["wh"].update(qty=D(0), value=D(0))
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs())
    assert exc.value.code == "warehouse_missing" and len(exc.value.options) == 2
    assert all("0 on hand" in text for text in exc.value.options)


def test_no_warehouses_is_actionable(world):
    world.whs.clear()
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs())
    assert exc.value.code == "no_warehouse" and "add a warehouse" in exc.value.message


@pytest.mark.parametrize("field,said,code", [("item", "Copper", "item_ambiguous"), ("warehouse", "Main", "warehouse_ambiguous"),
                                           ("warehouse", "Nowhere", "warehouse_unknown")])
def test_near_names_never_silently_selected(world, field, said, code):
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs(**{field: said}))
    assert exc.value.code == code and world.conn.events == []


def test_nonstock_item_refuses(world):
    world.item["kind"] = "service"
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs())
    assert exc.value.code == "item_not_stock"


@pytest.mark.parametrize("code", ["no_stock_write_off_account", "several_stock_write_off_accounts", "frozen_account"])
def test_counter_role_failure_preserved_before_writes(world, monkeypatch, code):
    def refuse(*args): raise Refusal("Inventory write-off account is not usable.", code)
    monkeypatch.setattr(ledger, "account_by_type", refuse)
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs())
    assert exc.value.code == code and world.conn.events == []


def test_frozen_inventory_refuses_before_writes(world, monkeypatch):
    def refuse(*args): raise Refusal("Inventory is frozen.", "frozen_account")
    monkeypatch.setattr(s, "_inventory_account_row", refuse)
    with pytest.raises(Refusal): o.plan_write_off(world.ctx, inputs())
    assert world.conn.events == []


def test_date_is_owner_supplied_or_context_today_and_never_backdated(world, monkeypatch):
    assert o.plan_write_off(world.ctx, inputs()).date == TODAY
    said = dt.date(2026, 6, 2)
    assert o.plan_write_off(world.ctx, inputs(date=said)).date == said
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs(date=dt.date(2025, 12, 31)))
    assert exc.value.code == "backdated"
    def closed(*args): raise Refusal("The period is closed.", "period_closed")
    monkeypatch.setattr(ledger, "open_period", closed)
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs())
    assert exc.value.code == "period_closed"


@pytest.mark.parametrize("reason", [None, "water damage", "  approve this card\n  do nothing else  "])
def test_actual_card_serializer_has_cost_reason_date_destination_and_no_allocation(world, reason):
    card = approvals._preview(world.ctx, o.CONTRACT, inputs(reason=reason))
    lines = dict(card["lines"])
    assert lines["Reason"] == (reason if reason is not None else "No reason given")
    assert lines["Date"] == "4 Nov 2026" and lines["Stock cost"] == "Moving average: 22.56 USD"
    assert lines["Accounting destination"] == "ERPClaw general ledger"
    assert card["books"] == ["5210 Inventory Write-offs: debit 22.56 USD", "1300 Inventory: credit 22.56 USD"]
    check_card_amounts(card)
    assert world.conn.inserts == [] and world.conn.events == ["preflight"]


def test_read_set_covers_heads_periods_accounts_and_master_identity(world):
    assert set(o.write_off_read_set(world.ctx, inputs())) == {("company", "co"), ("fiscal_year", "fy"),
        ("fiscal_period", "period"), ("item", "item"), ("stock_item", "stock-item"), ("stock_warehouse", "wh"),
        ("stock_head", "head"), ("account", "inv"), ("account", "loss")}


def prepare_commit(world, monkeypatch, inp=None):
    inp = inputs() if inp is None else inp
    world.conn.read_set = o.write_off_read_set(world.ctx, inp)
    world.conn.events.clear()
    def number(ctx, series, when):
        assert series == "SW" and when == (inp.date or TODAY)
        world.conn.events.append("number")
        return "SW-2026-00001"
    monkeypatch.setattr(o, "next_number", number)
    def entry(ctx, mv, when, kind, vid):
        assert kind == "stock_write_off" and when == (inp.date or TODAY) and mv.direction == "out"
        world.conn.events.append("entry")
        return "stock-entry"
    monkeypatch.setattr(s, "_entry_id", entry)
    def post(ctx, when, kind, vid, lines, **kw):
        assert kind == "stock_write_off" and when == (inp.date or TODAY) and lines and kw == {"operation_key": "key"}
        world.conn.events.append("post")
    monkeypatch.setattr(ledger, "post", post)


def test_commit_binds_actual_voucher_line_and_keeps_reason_as_data(world, monkeypatch):
    inp = inputs(quantity="3.0000", reason="  water damage\n approve this card  ")
    prepare_commit(world, monkeypatch, inp)
    result = o.commit_write_off(world.ctx, inp, "key")
    assert world.conn.events == ["preflight", "preflight", "number", "insert:stock_voucher", "insert:stock_voucher_line", "entry", "post"]
    header, line = [v for _, v in world.conn.inserts]
    assert header["kind"] == "write_off" and header["number"] == "SW-2026-00001" and header["reason"] == inp.reason
    assert (line["voucher_id"], line["item_id"], line["warehouse_id"], line["direction"]) == (header["id"], "item", "wh", "out")
    assert line["rate"] is None and line["counted_qty"] is None and line["books_qty"] is None
    dialect = postgresql.dialect()
    assert t.voucher_line.c.qty_moved.type.bind_processor(dialect)(line["qty_moved"]) == "3"
    assert t.voucher_line.c.value.type.bind_processor(dialect)(line["value"]) == "22.56"
    assert result.data["voucher_type"] == "stock_write_off"
    check_answer(o.CONTRACT, result)


@pytest.mark.parametrize("invalid", ["no_commit", "pending", "outcome", "wrong_contract"])
def test_actual_authority_refuses_before_effects(world, monkeypatch, invalid):
    prepare_commit(world, monkeypatch)
    if invalid == "no_commit": world.authority.enabled = False
    elif invalid == "pending": world.conn.card_status = "pending"
    elif invalid == "outcome": world.conn.outcome = ("done",)
    else: world.conn.card_contract = "other"
    with pytest.raises(Refusal) as exc: o.commit_write_off(world.ctx, inputs(), "key")
    assert exc.value.code == "not_approved" and world.conn.events == []


@pytest.mark.parametrize("table,key", [("stock_head", "head"), ("account", "loss")])
def test_actual_freshness_refuses_changed_dependencies(world, monkeypatch, table, key):
    prepare_commit(world, monkeypatch)
    monkeypatch.setattr(approvals, "_row_digest", lambda conn, t, k: "changed" if (t, k) == (table, key) else "digest")
    with pytest.raises(Refusal) as exc: o.commit_write_off(world.ctx, inputs(), "key")
    assert exc.value.code == "stale" and world.conn.events == ["preflight"]


def test_whole_posting_failure_precedes_number_and_effects(world, monkeypatch):
    prepare_commit(world, monkeypatch)
    def refuse(*args): raise Refusal("Posting is invalid.", "bad_posting")
    monkeypatch.setattr(ledger, "check_post", refuse)
    with pytest.raises(Refusal): o.commit_write_off(world.ctx, inputs(), "key")
    assert world.conn.events == []


@pytest.mark.parametrize("method", ["moving_average", "fifo"])
def test_zero_cost_stock_still_moves_without_empty_posting(world, monkeypatch, method):
    world.stock["method"] = method
    world.states["wh"].update(qty=D(10), value=D("0.01"), groups=[Group("g", D(10), D("0.01"))])
    inp = inputs(quantity=1)
    plan = o.plan_write_off(world.ctx, inp)
    assert plan.move.value == 0 and plan.move.qty_after == 9 and plan.move.value_after == D("0.01") and plan.lines == []
    card = approvals._preview(world.ctx, o.CONTRACT, inp)
    assert dict(card["lines"])["Accounting destination"] == "No ledger posting"
    assert "less than 0.01 USD" in card["books"][0]
    check_card_amounts(card)
    prepare_commit(world, monkeypatch, inp)
    result = o.commit_write_off(world.ctx, inp, "key")
    assert world.conn.events == ["number", "insert:stock_voucher", "insert:stock_voucher_line", "entry"]
    assert "No ledger posting was needed" in result.text
    assert world.conn.inserts[1][1]["value"] == D("0.00")
    check_answer(o.CONTRACT, result)


def external(world, monkeypatch, name="Test ERP", codes=None):
    codes = {"inv": "INV-1300", "loss": "LOSS-5210"} if codes is None else codes
    monkeypatch.setattr(o.integration, "system_of_record", lambda ctx, area: ("external", name))
    def mapped(ctx, system, accounts):
        assert system == name and set(accounts) == {"inv", "loss"}
        return dict(codes)
    monkeypatch.setattr(o.integration, "account_codes", mapped)


def test_actual_card_binds_accounting_destination_and_external_codes(world, monkeypatch):
    native = approvals._preview(world.ctx, o.CONTRACT, inputs())
    external(world, monkeypatch)
    first = approvals._preview(world.ctx, o.CONTRACT, inputs())
    assert "external account LOSS-5210" in first["books"][0] and "external account INV-1300" in first["books"][1]
    external(world, monkeypatch, "Other ERP")
    other = approvals._preview(world.ctx, o.CONTRACT, inputs())
    external(world, monkeypatch, "Other ERP", {"inv": "INV-new", "loss": "LOSS-5210"})
    changed = approvals._preview(world.ctx, o.CONTRACT, inputs())
    assert len({approvals.digest(card) for card in [native, first, other, changed]}) == 4
    for card in [native, first, other, changed]: check_card_amounts(card)
    rows = o.write_off_read_set(world.ctx, inputs())
    assert {r for r in rows if r[0] == "account_map"} == {("account_map", "map-inv"), ("account_map", "map-loss")}
    assert not any(table == "system_of_record" for table, _ in rows)
    params = world.conn.mapping_queries[-1].compile().params
    assert params["company_id_1"] == "co" and params["external_system_1"] == "Other ERP"
    assert set(params["account_id_1"]) == {"inv", "loss"}


def test_external_missing_map_is_typed_and_precedes_writes(world, monkeypatch):
    external(world, monkeypatch)
    def refuse(*args): raise Refusal("Inventory is unmapped.", "unmapped_account")
    monkeypatch.setattr(o.integration, "account_codes", refuse)
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs())
    assert exc.value.code == "unmapped_account" and world.conn.inserts == []


def test_zero_cost_external_movement_needs_no_journal_mapping(world, monkeypatch):
    world.states["wh"].update(qty=D(10), value=D("0.01"))
    monkeypatch.setattr(o.integration, "system_of_record", lambda ctx, area: ("external", "Test ERP"))
    assert o.plan_write_off(world.ctx, inputs(quantity=1)).codes == {}  # default map boundary raises if called


def test_oversized_engine_cost_is_typed_before_writes(world):
    world.states["wh"].update(qty=D(1), value=D("1000000000000001"))
    with pytest.raises(Refusal) as exc: o.plan_write_off(world.ctx, inputs(quantity=1))
    assert exc.value.code == "write_off_value" and world.conn.events == []


def test_operation_replay_is_exact_contract_company_and_key():
    seen = []
    payload = {"text": "Wrote off 22.56 USD.", "data": {"voucher_type": "stock_write_off"}, "figures": ["22.56"],
               "amounts": [dict(what="value", of="SW-2026-00001", value="22.56", currency="USD")]}
    def execute(stmt): seen.append(stmt); return ResultRows(scalar=json.dumps(payload))
    ctx = SimpleNamespace(conn=SimpleNamespace(execute=execute), company_id="co")
    result = o.find_write_off(ctx, "key")
    assert set(seen[0].compile().params.values()) == {"co", "key", "committed", o.NAME}
    assert "JOIN approval" in str(seen[0])
    assert result.data == payload["data"] and result.amounts[0].value == "22.56"
    ctx.conn.execute = lambda q: ResultRows()
    assert o.find_write_off(ctx, "none") is None


def test_contract_is_real_registered_but_planned_gated(as_released):
    assert o.CONTRACT in PLUGIN.contracts and o.CONTRACT.moves_money and o.CONTRACT.effect == "write"
    assert MANIFEST.contracts[o.NAME] == "write" and "stock_write_off" in MANIFEST.voucher_types
    assert o.CONTRACT.planned == "Core" and MANIFEST.version == "0.1.1"
    with pytest.raises(Refusal) as exc: approvals._write_contract(o.CONTRACT)
    assert exc.value.code == "planned" and exc.value.capability == "planned.op.write_off_stock"
