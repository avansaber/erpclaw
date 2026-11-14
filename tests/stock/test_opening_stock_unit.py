"""Pure opening-stock units: no engine, SQL connection, network or conftest.

Read boundaries use explicit Python objects. These verify producer decisions and
orchestration; they do not claim database transactions/concurrency are qualified.
"""
import copy
import datetime as dt
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from approval_recording import RecordedApproval
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.dml import Insert

from erpclaw_core import approvals, ledger, masters
from erpclaw_core.common import Refusal
from erpclaw_core.contracts import check_card_amounts, check_answer
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import MANIFEST, PLUGIN, opening_stock as o, service as s, tables as t

D = Decimal
START = dt.date(2026, 1, 1)


def inputs(**changes):
    data = {"lines": [{"item": "Copper Wire", "warehouse": "Main Warehouse", "quantity": "5", "rate": "2.50"}]}
    data.update(changes)
    return o.RecordOpeningStock.model_validate(data)


@pytest.mark.parametrize("field", ["quantity", "rate"])
@pytest.mark.parametrize("bad", [True, 1.5, "0", "-1", "1e2", "NaN", "Infinity", "1.00001", "100000000000000"])
def test_invalid_owner_numbers_are_never_coerced(field, bad):
    line = {"item": "Copper Wire", "warehouse": "Main Warehouse", "quantity": "5", "rate": "2.50"}
    line[field] = bad
    with pytest.raises((Refusal, ValidationError)):
        o.OpeningLine.model_validate(line)


def test_rate_is_required_and_not_selling_price_default():
    with pytest.raises(ValidationError):
        o.RecordOpeningStock.model_validate({"lines": [{"item": "Copper Wire", "warehouse": "Main Warehouse", "quantity": "5"}]})
    assert inputs().lines[0].rate == D("2.50")


@pytest.mark.parametrize("data", [{"lines": []}, {"lines": [dict(item="Copper Wire", warehouse="", quantity=1, rate=1)]}])
def test_missing_lines_or_warehouse_refused(data):
    with pytest.raises(ValidationError):
        o.RecordOpeningStock.model_validate(data)


class ReadResult:
    def __init__(self, first=None, scalar=None):
        self.value, self.scalar = first, scalar
    def first(self): return self.value
    def mappings(self): return self
    def scalar_one(self): return self.scalar
    def scalar_one_or_none(self): return self.scalar
    def scalars(self): return iter(self.scalar or [])


class Boundaries:
    def __init__(self):
        self.events, self.inserts = [], []
        self.moved = None
        self.card_status = "consumed"
        self.card_contract = o.NAME
        self.existing_outcome = None
        self.read_set = []
        self.mapping_ids = ["map-inventory", "map-equity"]
        self.mapping_queries = []
    def execute(self, stmt):
        if isinstance(stmt, Insert):
            values = stmt.compile().params
            self.events.append("insert:" + stmt.table.name)
            self.inserts.append((stmt.table.name, values))
            return ReadResult()
        names = [c.name for c in stmt.selected_columns]
        tables = str(stmt)
        if "approval.contract" in tables:
            return ReadResult(SimpleNamespace(status=self.card_status, contract=self.card_contract))
        if "operation_outcome.key" in tables:
            return ReadResult(self.existing_outcome)
        if names == ["read_set_json"]:
            return ReadResult(scalar=json.dumps([[table, key, "digest"] for table, key in self.read_set]))
        if "stock_ledger_entry.id" in tables:
            return ReadResult(self.moved)
        if "account_map.id" in tables:
            self.mapping_queries.append(stmt)
            return ReadResult(scalar=self.mapping_ids)
        raise AssertionError("Unexpected read boundary: " + str(stmt))


@pytest.fixture
def world(monkeypatch):
    conn = Boundaries()
    ctx = SimpleNamespace(conn=conn, company_id="co", company_name="Test Books", currency="USD",
                          today=dt.date(2026, 11, 4), extra={"commit": "approval"})
    item = {"id": "item", "name": "Copper Wire", "kind": "stock", "unit": "m", "price": D("999")}
    wh = {"id": "wh", "name": "Main Warehouse", "account_id": "inventory", "company_id": "co"}
    stock = {"id": "stock-item", "item_id": "item", "method": "fifo"}
    inventory = {"id": "inventory", "number": "1300", "name": "Inventory", "frozen": False}
    counter = {"id": "equity", "number": "3900", "name": "Opening Balance Equity", "frozen": False}
    year = {"id": "fy", "start_date": START}
    period = {"id": "period", "status": "open", "fiscal_year_id": "fy"}
    monkeypatch.setattr(o, "_books_start", lambda ctx: (year, period))
    monkeypatch.setattr(masters, "find_item", lambda ctx, said: masters._match([item], said, "item", plural=True))
    monkeypatch.setattr(s, "warehouses", lambda ctx: [wh])
    monkeypatch.setattr(s, "head", lambda *a, **kw: None)
    monkeypatch.setattr(s, "_pair", lambda *a, **kw: s.Pair(copy.deepcopy(stock), copy.deepcopy(wh),
                        "inventory", None, D(0), D(0)))
    monkeypatch.setattr(s, "_inventory_account_row", lambda *a: inventory)
    def role(ctx, name):
        assert name == "opening_balance"
        return counter
    monkeypatch.setattr(ledger, "account_by_type", role)
    monkeypatch.setattr(o.integration, "system_of_record", lambda ctx, area: ("native", None))
    def no_native_mapping(*args):
        raise AssertionError("Native accounting must not request external account codes")
    monkeypatch.setattr(o.integration, "account_codes", no_native_mapping)
    def check(ctx, when, kind, vid, lines):
        assert when == START and kind == o.VOUCHER
        conn.events.append("preflight")
        return lines
    monkeypatch.setattr(ledger, "check_post", check)
    monkeypatch.setattr(REGISTRY, "contract_plugin", {o.NAME: "stock"})
    monkeypatch.setattr(approvals, "_row_digest", lambda *a: "digest")
    authority = RecordedApproval(ctx, lambda: SimpleNamespace(status=conn.card_status, contract=conn.card_contract, company_id="co"), lambda: conn.existing_outcome)
    authority.wrap(monkeypatch, o, "commit_opening")
    return SimpleNamespace(ctx=ctx,authority=authority, conn=conn, item=item, wh=wh, stock=stock, year=year, period=period,
                           inventory=inventory, counter=counter)


def test_preview_is_dated_at_books_start_and_shows_actual_values(world):
    inp = inputs(date=dt.date(2026, 3, 15))
    card = o.preview_opening(world.ctx, inp).card
    lines = dict(card.lines)
    assert lines["Date"] == "Opening stock dated 1 Jan 2026 (when these books start)"
    assert lines["Date you supplied"].startswith("You said 15 Mar 2026;")
    assert lines["Copper Wire"] == "5 m in Main Warehouse at 2.50 USD per m: 12.50 USD"
    assert lines["Total"] == "12.50 USD"
    assert card.books == ["1300 Inventory: debit 12.50 USD", "3900 Opening Balance Equity: credit 12.50 USD"]
    assert world.conn.inserts == [] and world.conn.events == ["preflight"]
    check_card_amounts(card.as_dict())


@pytest.mark.parametrize("owner_date", [None, START])
def test_matching_or_absent_owner_date_has_no_discrepancy(world, owner_date):
    assert "Date you supplied" not in dict(o.preview_opening(world.ctx, inputs(date=owner_date)).card.lines)


def test_real_receipt_valuation_and_fifo_move_are_reused(world):
    p = o.plan_opening(world.ctx, inputs(lines=[dict(item="Copper Wire", warehouse="Main Warehouse", quantity="12.5", rate="4.25")]))
    assert p.moves[0].qty == D("12.5") and p.moves[0].value == D("53.13")
    assert p.moves[0].layer is True and p.moves[0].qty_after == D("12.5")
    assert [(ln.account_id, ln.debit, ln.credit) for ln in p.lines] == [("inventory", D("53.13"), D(0)), ("equity", D(0), D("53.13"))]


def test_zero_rounded_value_refused_before_any_writes(world):
    with pytest.raises(Refusal, match="worth less"):
        o.plan_opening(world.ctx, inputs(lines=[dict(item="Copper Wire", warehouse="Main Warehouse", quantity="0.0001", rate="0.0001")]))
    assert world.conn.events == []


def test_duplicate_pairs_refused_even_after_name_normalisation(world):
    with pytest.raises(Refusal) as exc:
        o.plan_opening(world.ctx, inputs(lines=[dict(item="Copper Wire", warehouse="Main Warehouse", quantity=1, rate=2),
                                              dict(item="copper wire", warehouse="main warehouse", quantity=2, rate=3)]))
    assert exc.value.code == "duplicate_stock_pair" and world.conn.events == []
    assert exc.value.message == "Copper Wire in Main Warehouse appears twice in that opening stock; give one figure for it."


def test_service_item_refused(world):
    world.item["kind"] = "service"
    with pytest.raises(Refusal) as exc: o.plan_opening(world.ctx, inputs())
    assert exc.value.code == "item_not_stock"


@pytest.mark.parametrize("field,said,code", [("item", "Copper", "item_ambiguous"), ("warehouse", "Main", "warehouse_ambiguous"),
                                           ("warehouse", "Nowhere", "warehouse_unknown")])
def test_near_names_are_not_silently_selected(world, field, said, code):
    data = inputs().model_dump(); data["lines"][0][field] = said
    with pytest.raises(Refusal) as exc: o.plan_opening(world.ctx, o.RecordOpeningStock.model_validate(data))
    assert exc.value.code == code and world.conn.events == []


@pytest.mark.parametrize("history", ["entry", "empty_head"])
def test_ever_moved_pair_refused_even_with_no_stock_left(world, monkeypatch, history):
    if history == "entry": world.conn.moved = ("old-entry",)
    else: monkeypatch.setattr(s, "head", lambda *a, **kw: {"qty": D(0), "value": D(0)})
    with pytest.raises(Refusal) as exc: o.plan_opening(world.ctx, inputs())
    assert exc.value.code == "opening_stock_moved" and "record a count" in exc.value.message
    assert world.conn.events == []


def test_read_set_covers_books_date_accounts_and_stock_identity(world):
    assert set(o.opening_read_set(world.ctx, inputs())) == {("company", "co"), ("fiscal_year", "fy"),
        ("fiscal_period", "period"), ("account", "equity"), ("account", "inventory"), ("item", "item"),
        ("stock_item", "stock-item"), ("stock_warehouse", "wh")}


@pytest.mark.parametrize("code", ["no_opening_balance_account", "several_opening_balance_accounts", "frozen_account"])
def test_counter_role_failure_never_auto_creates(world, monkeypatch, code):
    def refuse(*args): raise Refusal("Account is not usable.", code)
    monkeypatch.setattr(ledger, "account_by_type", refuse)
    with pytest.raises(Refusal) as exc: o.plan_opening(world.ctx, inputs())
    assert exc.value.code == code and world.conn.events == []


def test_inventory_failure_stops_before_any_write(world, monkeypatch):
    def refuse(*args): raise Refusal("Inventory is frozen.", "frozen_account")
    monkeypatch.setattr(s, "_inventory_account_row", refuse)
    with pytest.raises(Refusal): o.plan_opening(world.ctx, inputs())
    assert world.conn.events == []


def prepare_commit(world, monkeypatch):
    world.conn.read_set = o.opening_read_set(world.ctx, inputs())
    world.conn.events.clear()
    def number(ctx, series, date):
        assert series == "OS" and date == START
        world.conn.events.append("number")
        return "OS-2026-00003"
    monkeypatch.setattr(o, "next_number", number)
    def entry(ctx, mv, when, kind, vid):
        world.conn.events.append("stock_entry")
        assert when == START and kind == "stock_opening" and mv.line_no == 1
        return "entry"
    monkeypatch.setattr(s, "_entry_id", entry)
    def post(ctx, when, kind, vid, lines, **kw):
        world.conn.events.append("post")
        assert when == START and kind == "stock_opening" and kw == {"operation_key": "key"}
        assert [(x.account_id, x.debit, x.credit) for x in lines] == [("inventory", D("12.50"), D(0)), ("equity", D(0), D("12.50"))]
    monkeypatch.setattr(ledger, "post", post)


def test_commit_uses_real_authority_and_freshness_before_number_and_effects(world, monkeypatch):
    prepare_commit(world, monkeypatch)
    result = o.commit_opening(world.ctx, inputs(date=dt.date(2026, 5, 1)), "key")
    assert world.conn.events == ["preflight", "preflight", "number", "insert:stock_voucher", "insert:stock_voucher_line", "stock_entry", "post"]
    header, line = [data for _, data in world.conn.inserts]
    assert header["kind"] == "opening" and header["posting_date"] == START
    assert header["number"] == "OS-2026-00003"
    assert line["voucher_id"] == header["id"] and line["company_id"] == "co"
    assert (line["qty_moved"], line["rate"], line["value"]) == (D(5), D("2.50"), D("12.50"))
    assert line["counted_qty"] is None and line["books_qty"] is None
    assert result.data["voucher_type"] == "stock_opening"
    check_answer(o.CONTRACT, result)


@pytest.mark.parametrize("invalid", ["no_commit", "pending", "outcome", "wrong_contract"])
def test_actual_authority_refuses_without_effects(world, monkeypatch, invalid):
    prepare_commit(world, monkeypatch)
    if invalid == "no_commit": world.authority.enabled = False
    elif invalid == "pending": world.conn.card_status = "pending"
    elif invalid == "outcome": world.conn.existing_outcome = ("done",)
    else: world.conn.card_contract = "some_other_contract"
    with pytest.raises(Refusal) as exc: o.commit_opening(world.ctx, inputs(), "key")
    assert exc.value.code == "not_approved" and world.conn.events == []


def test_actual_freshness_refuses_changed_account_before_first_effect(world, monkeypatch):
    prepare_commit(world, monkeypatch)
    monkeypatch.setattr(approvals, "_row_digest", lambda conn, table, key: "changed" if (table, key) == ("account", "inventory") else "digest")
    with pytest.raises(Refusal) as exc: o.commit_opening(world.ctx, inputs(), "key")
    assert exc.value.code == "stale" and world.conn.events == ["preflight"]


def test_whole_posting_failure_stops_number_allocation(world, monkeypatch):
    prepare_commit(world, monkeypatch)
    def refuse(*args): raise Refusal("Not a valid whole posting.", "bad_posting")
    monkeypatch.setattr(ledger, "check_post", refuse)
    with pytest.raises(Refusal): o.commit_opening(world.ctx, inputs(), "key")
    assert world.conn.events == []


def test_books_start_query_is_company_scoped_and_ordered(monkeypatch):
    queries = []
    def execute(stmt):
        queries.append(stmt)
        return ReadResult({"id": "first", "start_date": START})
    ctx = SimpleNamespace(conn=SimpleNamespace(execute=execute), company_id="co")
    monkeypatch.setattr(ledger, "open_period", lambda ctx, date: {"id": "p", "date": date})
    year, period = o._books_start(ctx)
    assert year["start_date"] == period["date"] == START
    sql = str(queries[0]); assert "fiscal_year.company_id =" in sql and "ORDER BY fiscal_year.start_date" in sql and "LIMIT" in sql
    assert "co" in queries[0].compile().params.values()


@pytest.mark.parametrize("code,expected", [("period_closed", "opening_period_closed"), ("no_period", "no_period")])
def test_books_start_period_refusal_is_actionable(monkeypatch, code, expected):
    ctx = SimpleNamespace(conn=SimpleNamespace(execute=lambda q: ReadResult({"id": "fy", "start_date": START})), company_id="co")
    def refuse(*a): raise Refusal("Period unavailable.", code)
    monkeypatch.setattr(ledger, "open_period", refuse)
    with pytest.raises(Refusal) as exc: o._books_start(ctx)
    assert exc.value.code == expected
    if code == "period_closed": assert "record a count instead" in exc.value.message


def test_replay_is_scoped_to_contract_company_and_exact_key():
    statements = []
    payload = {"text": "Recorded 12.50 USD.", "data": {"voucher_type": "stock_opening"}, "figures": ["12.50"],
               "amounts": [dict(what="value", of="OS-2026-00003", value="12.50", currency="USD")]}
    def execute(stmt):
        statements.append(stmt)
        return ReadResult(scalar=json.dumps(payload))
    ctx = SimpleNamespace(conn=SimpleNamespace(execute=execute), company_id="co")
    result = o.find_opening(ctx, "key")
    params = statements[0].compile().params
    assert set(params.values()) == {"co", "key", "committed", o.NAME}
    assert "JOIN approval" in str(statements[0])
    assert result.data == payload["data"] and result.figures == ["12.50"] and result.amounts[0].value == "12.50"
    ctx.conn.execute = lambda q: ReadResult()
    assert o.find_opening(ctx, "missing") is None


def migration():
    path = Path(o.__file__).parent / "migrations/versions/stock_0002_stock_vouchers.py"
    spec = importlib.util.spec_from_file_location("opening_stock_migration_unit", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_frozen_migration_matches_declared_tables_without_database():
    rev = migration()
    assert (rev.revision, rev.down_revision, rev.depends_on, rev.branch_labels) == ("stock_0002", "stock_0001", None, None)
    assert rev.creates == ("stock_voucher", "stock_voucher_line")
    ddl = [str(CreateTable(table).compile(dialect=postgresql.dialect())).strip() for table in (t.voucher, t.voucher_line)]
    assert list(rev.DDL) == ["\n".join(line.rstrip() for line in sql.splitlines()) for sql in ddl]
    assert {table.name for table in t.TABLES} == set(MANIFEST.owned_tables)
    for table in (t.voucher, t.voucher_line):
        assert table.info["owner"] == "stock" and table.info["runtime"].insert
        assert not table.info["runtime"].update and not table.info["runtime"].delete
    references = [tuple(f.parent.name for f in fk.elements) for fk in t.voucher_line.foreign_key_constraints if len(fk.elements) > 1]
    assert all(columns[0] == "company_id" for columns in references) and len(references) == 3


def test_migration_triggers_are_append_only_and_schema_qualified(monkeypatch):
    rev = migration(); statements = []
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), execute=lambda q: ReadResult(scalar='"book"'))
    monkeypatch.setattr(rev.op, "get_bind", lambda: bind)
    monkeypatch.setattr(rev.op, "execute", statements.append)
    rev.upgrade()
    assert statements[:2] == list(rev.DDL)
    for table in rev.creates:
        triggers = [s for s in statements[2:] if f'ON "book".{table} ' in s]
        assert len(triggers) == 2
        assert any("BEFORE UPDATE OR DELETE" in s for s in triggers)
        assert any("BEFORE TRUNCATE" in s and "FOR EACH STATEMENT" in s for s in triggers)
        assert all('FUNCTION "book".erpclaw_stock_refuse()' in s for s in triggers)
    bind.dialect.name = "sqlite"
    with pytest.raises(RuntimeError): rev.upgrade()
    with pytest.raises(NotImplementedError): rev.downgrade()


def test_contract_is_registered_but_planned_and_versions_are_consistent(as_released):
    assert o.CONTRACT in PLUGIN.contracts
    assert o.CONTRACT.planned == "Core" and o.CONTRACT.moves_money is True
    assert o.CONTRACT.effect == "write" and o.CONTRACT.plugin == "stock"
    assert MANIFEST.contracts[o.NAME] == "write" and "stock_opening" in MANIFEST.voucher_types
    assert MANIFEST.version == "0.1.3" and MANIFEST.after_core_revision == "0015"
    with pytest.raises(Refusal) as exc: approvals._write_contract(o.CONTRACT)
    assert exc.value.code == "planned" and exc.value.capability == "planned.op.record_opening_stock"


def two_warehouses(world, monkeypatch):
    other = {"id": "wh2", "name": "Shop Warehouse", "account_id": "inventory2", "company_id": "co"}
    account = {"id": "inventory2", "number": "1310", "name": "Shop Inventory", "frozen": False}
    warehouses = {"wh": world.wh, "wh2": other}
    accounts = {"inventory": world.inventory, "inventory2": account}
    monkeypatch.setattr(s, "warehouses", lambda ctx: list(warehouses.values()))
    def pair(ctx, pairs, item, warehouse, when, **kw):
        wh = warehouses[warehouse]
        return s.Pair(copy.deepcopy(world.stock), copy.deepcopy(wh), wh["account_id"], None, D(0), D(0))
    monkeypatch.setattr(s, "_pair", pair)
    monkeypatch.setattr(s, "_inventory_account_row", lambda ctx, account, name: accounts[account])
    return inputs(lines=[dict(item="Copper Wire", warehouse="Main Warehouse", quantity=5, rate="2.50"),
                         dict(item="Copper Wire", warehouse="Shop Warehouse", quantity=3, rate=4)])


def test_multiple_warehouses_card_binds_each_quantity_rate_value(world, monkeypatch):
    inp = two_warehouses(world, monkeypatch)
    card = o.preview_opening(world.ctx, inp).card
    assert card.lines[1:3] == [("Copper Wire", "5 m in Main Warehouse at 2.50 USD per m: 12.50 USD"),
                               ("Copper Wire", "3 m in Shop Warehouse at 4.00 USD per m: 12.00 USD")]
    assert card.lines[3] == ("Total", "24.50 USD")
    assert set(card.books) == {"1300 Inventory: debit 12.50 USD", "1310 Shop Inventory: debit 12.00 USD",
                               "3900 Opening Balance Equity: credit 24.50 USD"}
    check_card_amounts(card.as_dict())
    assert world.conn.inserts == [] and world.conn.events == ["preflight"]


def test_multiple_warehouses_commit_preserves_line_numbers_and_account_attribution(world, monkeypatch):
    inp = two_warehouses(world, monkeypatch)
    prepare_commit(world, monkeypatch)
    world.conn.read_set = o.opening_read_set(world.ctx, inp)
    world.conn.events.clear()
    moves = []
    def entry(ctx, mv, when, kind, vid):
        moves.append((mv.line_no, mv.pair.wh["id"], mv.qty, mv.value, kind, vid))
        world.conn.events.append("stock_entry")
    monkeypatch.setattr(s, "_entry_id", entry)
    def post(ctx, when, kind, vid, lines, **kw):
        assert [(x.account_id, x.debit, x.credit) for x in lines] == [
            ("inventory", D("12.50"), D(0)), ("inventory2", D("12.00"), D(0)), ("equity", D(0), D("24.50"))]
        world.conn.events.append("post")
    monkeypatch.setattr(ledger, "post", post)
    result = o.commit_opening(world.ctx, inp, "key")
    header = world.conn.inserts[0][1]
    lines = [row for table, row in world.conn.inserts if table == "stock_voucher_line"]
    assert [(x["line_no"], x["warehouse_id"], x["qty_moved"], x["rate"], x["value"]) for x in lines] == [
        (1, "wh", D(5), D("2.50"), D("12.50")), (2, "wh2", D(3), D(4), D("12.00"))]
    assert moves == [(1, "wh", D(5), D("12.50"), "stock_opening", header["id"]),
                     (2, "wh2", D(3), D("12.00"), "stock_opening", header["id"])]
    assert all(x["voucher_id"] == header["id"] for x in lines)
    assert world.conn.events.index("number") > world.conn.events.index("preflight")
    assert world.conn.events[-1] == "post"
    check_answer(o.CONTRACT, result)


def test_voucher_line_binds_actual_producer_values_to_canonical_database_text(world, monkeypatch):
    prepare_commit(world, monkeypatch)
    o.commit_opening(world.ctx, inputs(), "key")
    line = next(values for table, values in world.conn.inserts if table == "stock_voucher_line")
    dialect = postgresql.dialect()
    bound = {name: t.voucher_line.c[name].type.bind_processor(dialect)(line[name])
             for name in ("qty_moved", "rate", "value", "counted_qty", "books_qty")}
    assert bound == {"qty_moved": "5", "rate": "2.5", "value": "12.50", "counted_qty": None, "books_qty": None}


@pytest.mark.parametrize("oversized", ["individual", "aggregate"])
def test_oversized_opening_values_are_typed_refusals_before_writes(world, monkeypatch, oversized):
    two_warehouses(world, monkeypatch)
    if oversized == "individual":
        rows = [dict(item="Copper Wire", warehouse="Main Warehouse", quantity="99999999999999", rate="1000")]
    else:
        rows = [dict(item="Copper Wire", warehouse=where, quantity="99999999999999", rate="9")
                for where in ("Main Warehouse", "Shop Warehouse")]
    with pytest.raises(Refusal) as exc:
        o.plan_opening(world.ctx, inputs(lines=rows))
    assert exc.value.code == "opening_stock_value"
    assert "quantities and unit costs" in exc.value.message
    assert world.conn.events == [] and world.conn.inserts == []



def external_accounting(world, monkeypatch, system="Test ERP", codes=None):
    codes = {"inventory": "INV-1300", "equity": "EQ-3900"} if codes is None else codes
    monkeypatch.setattr(o.integration, "system_of_record", lambda ctx, area: ("external", system))
    def account_codes(ctx, name, accounts):
        assert name == system and set(accounts) == {"inventory", "equity"}
        return dict(codes)
    monkeypatch.setattr(o.integration, "account_codes", account_codes)


def test_actual_approval_card_binds_native_external_target_and_codes(world, monkeypatch):
    # Exercise the actual stored-card serializer: core discards state_token.
    native = approvals._preview(world.ctx, o.CONTRACT, inputs())
    assert dict(native["lines"])["Accounting destination"] == "ERPClaw general ledger"
    external_accounting(world, monkeypatch)
    external = approvals._preview(world.ctx, o.CONTRACT, inputs())
    assert dict(external["lines"])["Accounting destination"] == "Test ERP: ERPClaw sub-ledger, with the journal staged for this system"
    assert external["books"] == ["1300 Inventory (external account INV-1300): debit 12.50 USD",
                                 "3900 Opening Balance Equity (external account EQ-3900): credit 12.50 USD"]
    external_accounting(world, monkeypatch, system="Other ERP")
    other = approvals._preview(world.ctx, o.CONTRACT, inputs())
    external_accounting(world, monkeypatch, system="Other ERP", codes={"inventory": "INV-new", "equity": "EQ-3900"})
    changed_code = approvals._preview(world.ctx, o.CONTRACT, inputs())
    cards = [native, external, other, changed_code]
    assert len({approvals.digest(card) for card in cards}) == 4
    assert "external account INV-new" in changed_code["books"][0]
    for card in cards: check_card_amounts(card)
    assert world.conn.inserts == []


def test_external_mapping_read_set_uses_existing_single_primary_keys(world, monkeypatch):
    external_accounting(world, monkeypatch)
    rows = o.opening_read_set(world.ctx, inputs())
    assert {row for row in rows if row[0] == "account_map"} == {("account_map", "map-inventory"), ("account_map", "map-equity")}
    assert not any(table == "system_of_record" for table, _ in rows)
    params = world.conn.mapping_queries[-1].compile().params
    assert params["company_id_1"] == "co" and params["external_system_1"] == "Test ERP"
    assert set(params["account_id_1"]) == {"inventory", "equity"}
    assert len(o.m.account_map.primary_key.columns) == 1


def test_external_unmapped_account_refuses_before_card_or_writes(world, monkeypatch):
    external_accounting(world, monkeypatch)
    def unmapped(*args): raise Refusal("Inventory has no Test ERP account mapped yet.", "unmapped_account")
    monkeypatch.setattr(o.integration, "account_codes", unmapped)
    with pytest.raises(Refusal) as exc: approvals._preview(world.ctx, o.CONTRACT, inputs())
    assert exc.value.code == "unmapped_account" and world.conn.inserts == []
