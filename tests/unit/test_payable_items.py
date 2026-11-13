"""Pure payable-item boundaries: recording objects, no SQL/conftest/network.

These do not prove DB privileges, transactions, locks or managed supplier identity.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import ast
from pathlib import Path

import pytest
from approval_recording import RecordedApproval
from sqlalchemy.sql.dml import Insert, Update

from erpclaw_core import ledger, payables as p
from erpclaw_core.common import Refusal
from erpclaw_core.plugins import REGISTRY

D = Decimal
DAY = date(2026, 4, 5)


class Result:
    def __init__(self, rows=(), rowcount=1):
        self.rows, self.rowcount = list(rows), rowcount
    def first(self): return self.rows[0] if self.rows else None
    def mappings(self): return self
    def __iter__(self): return iter(self.rows)


class Boundary:
    def __init__(self):
        self.card = SimpleNamespace(status="consumed", contract="record_goods_received", company_id="co")
        self.outcome = None
        self.supplier = {"id": "supplier", "company_id": "co", "kind": "supplier", "name": "Copperline Metals"}
        self.item = dict(id="owed", company_id="co", supplier_id="supplier", source_type=p.DELIVERY,
                         source_id="receipt", source_number="GR-2026-00001", posting_date=DAY,
                         amount=D("1450.00"), outstanding=D("1450.00"), status="open")
        self.rows = None
        self.payable_line = True
        self.rowcount = 1
        self.queries, self.writes = [], []
    def execute(self, stmt):
        if isinstance(stmt, (Insert, Update)):
            self.writes.append(stmt)
            return Result(rowcount=self.rowcount)
        self.queries.append(stmt)
        table = next(iter(stmt.selected_columns)).table.name
        if table == "approval": rows = [] if self.card is None else [self.card]
        elif table == "operation_outcome": rows = [] if self.outcome is None else [self.outcome]
        elif table == "party": rows = [] if self.supplier is None else [self.supplier]
        elif table == "account": rows = [("ap",)] if self.payable_line else []
        elif table == "supplier_payable": rows = self.rows if self.rows is not None else ([] if self.item is None else [self.item])
        else: raise AssertionError(f"Unexpected SELECT boundary {table}")
        return Result(rows)


@pytest.fixture
def world(monkeypatch):
    conn = Boundary()
    ctx = SimpleNamespace(conn=conn, company_id="co", company_name="Test Books", currency="USD", today=DAY,
                          actor="owner", extra={"commit": "approval"})
    monkeypatch.setattr(REGISTRY, "contract_plugin", {"record_goods_received": "stock", "core_delivery": "core",
                                                     "other": "email"})
    def account(ctx, role):
        assert role == "payable"
        return {"id": "ap", "number": "2100", "account_type": "payable"}
    monkeypatch.setattr(ledger, "account_by_type", account)
    def locked(ctx, stmt):
        return stmt.with_for_update()
    monkeypatch.setattr(ledger, "for_update", locked)
    authority = RecordedApproval(ctx, lambda: conn.card, lambda: conn.outcome)
    authority.wrap(monkeypatch, p, "open_item")
    authority.wrap(monkeypatch, p, "close_item")
    return SimpleNamespace(ctx=ctx,authority=authority, conn=conn)


def open_item(world, **changes):
    fields = dict(supplier_id="supplier", source_type=p.DELIVERY, source_id="receipt",
                  source_number="GR-2026-00001", posting_date=DAY, amount="1450.00")
    fields.update(changes)
    return p.open_item(world.ctx, **fields)


def params(stmt): return stmt.compile().params


@pytest.mark.parametrize("caller", ["record_goods_received", "core_delivery"])
def test_si2_opens_exact_item_and_returns_supplier_credit(world, caller):
    world.conn.card.contract = caller
    line = open_item(world)
    assert (line.account_id, line.debit, line.credit, line.party_id) == ("ap", D(0), D("1450.00"), "supplier")
    assert len(world.conn.writes) == 1
    stmt = world.conn.writes[0]
    assert isinstance(stmt, Insert) and stmt.table.name == "supplier_payable"
    values = params(stmt)
    assert {k: v for k,v in values.items() if k not in ("id", "created_at")} == dict(
        company_id="co", supplier_id="supplier", source_type=p.DELIVERY, source_id="receipt",
        source_number="GR-2026-00001", posting_date=DAY, currency="USD", amount=D("1450.00"),
        outstanding=D("1450.00"), status="open")
    assert values["id"] and values["created_at"]
    auth = params(world.conn.queries[0])
    assert auth["company_id_1"] == "co" and auth["id_1"] == "approval"
    supplier = world.conn.queries[-1]
    assert params(supplier) == {"company_id_1": "co", "id_1": "supplier"}


@pytest.mark.parametrize("action", ["open", "close"])
@pytest.mark.parametrize("bad", ["no_commit", "no_card", "pending", "outcome", "other_plugin", "unknown_contract", "other_company"])
def test_authority_failures_precede_business_reads_and_writes(world, action, bad):
    if bad == "no_commit": world.authority.enabled = False
    elif bad == "no_card": world.conn.card = None
    elif bad == "pending": world.conn.card.status = "pending"
    elif bad == "outcome": world.conn.outcome = ("already-done",)
    elif bad == "other_plugin": world.conn.card.contract = "other"
    elif bad == "unknown_contract": world.conn.card.contract = "missing"
    elif bad == "other_company": world.conn.card.company_id = "other"
    with pytest.raises(Refusal) as exc:
        if action == "open": open_item(world)
        else: p.close_item(world.ctx, p.DELIVERY, "receipt")
    assert exc.value.code == "not_approved"
    assert world.conn.writes == []
    assert all(next(iter(q.selected_columns)).table.name in {"approval", "operation_outcome"} for q in world.conn.queries)
    if bad == "no_commit": assert world.conn.queries == []


@pytest.mark.parametrize("value", [0, -1, True, 1.5, None, "1e2", "1,450", "NaN", "Infinity", "1.001", "1000000000000000"])
def test_amount_domain_refuses_before_insert(world, value):
    with pytest.raises(Refusal) as exc: open_item(world, amount=value)
    assert exc.value.code == "bad_amount" and world.conn.writes == []


@pytest.mark.parametrize("currency,amount", [("USD", "1450.00"), ("JPY", "1450"), ("KWD", "1450.125")])
def test_amount_uses_actual_company_scale(world, currency, amount):
    world.ctx.currency = currency
    line = open_item(world, amount=amount)
    assert line.credit == D(amount)
    assert params(world.conn.writes[0])["currency"] == currency


@pytest.mark.parametrize("source", ["journal", "opening", p.PAYMENT, p.DELIVERY + p.REVERSAL])
@pytest.mark.parametrize("action", ["open", "check_close", "close"])
def test_item_sources_do_not_expand_to_opening_or_other_vouchers(world, source, action):
    with pytest.raises(Refusal) as exc:
        if action == "open": open_item(world, source_type=source)
        elif action == "check_close": p.check_close_item(world.ctx, source, "receipt")
        else: p.close_item(world.ctx, source, "receipt")
    assert exc.value.code == "payable_source_not_allowed" and world.conn.writes == []


@pytest.mark.parametrize("supplier", [None, {"id": "customer", "kind": "customer"}])
def test_supplier_must_be_present_and_supplier_kind(world, supplier):
    world.conn.supplier = supplier
    with pytest.raises(Refusal) as exc: open_item(world)
    assert exc.value.code == "bad_party" and world.conn.writes == []


def test_missing_role_is_not_created(world, monkeypatch):
    def missing(*args): raise Refusal("No accounts payable account.", "no_payable_account")
    monkeypatch.setattr(ledger, "account_by_type", missing)
    with pytest.raises(Refusal) as exc: open_item(world)
    assert exc.value.code == "no_payable_account" and world.conn.writes == []


@pytest.mark.parametrize("source", p.PAYABLE_SOURCES)
def test_matching_frozen_source_allowlist_needs_no_role_query(world, source, monkeypatch):
    if source == p.OPENING:
        with pytest.raises(Refusal) as exc:
            p.check_payable_source(world.ctx, source, [ledger.Line("ap", credit=D(1), party_id="supplier")])
        assert exc.value.code == "payable_source_not_allowed" and world.conn.writes == []
        assert len(world.conn.queries) == 1
        world.conn.queries.clear()
        world.conn.card.contract = "record_opening_position"
        monkeypatch.setitem(REGISTRY.contract_plugin, "record_opening_position", "core")
        world.authority.wrap(monkeypatch, p, "check_payable_source")
    p.check_payable_source(world.ctx, source, [ledger.Line("ap", credit=D(1), party_id="supplier")])
    assert [next(iter(q.selected_columns)).table.name for q in world.conn.queries] == (
        ["approval"] if source == p.OPENING else [])
    assert world.conn.writes == []


def test_source_allowlist_matches_frozen0015_without_importing_migration():
    path = Path(p.__file__).parent / "migrations/versions/0015_supplier_payables.py"
    tree = ast.parse(path.read_text())
    definition = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "PAYABLE_SOURCES" for t in n.targets))
    assert (p.ITEM_SOURCES == ("stock_goods_received", "opening")
            and p.PAYABLE_SOURCES == ("opening", *ast.literal_eval(definition)))


@pytest.mark.parametrize("source", ["journal", "opening", "stock_receipt"])
def test_other_sources_cannot_touch_payable_party_lines(world, source):
    with pytest.raises(Refusal) as exc:
        p.check_payable_source(world.ctx, source, [ledger.Line("ap", credit=D(1), party_id="supplier")])
    assert exc.value.code == "payable_source_not_allowed" and exc.value.message == p.SOURCE_TEXT
    q = world.conn.queries[0]
    assert params(q) == {"company_id_1": "co", "id_1": ["ap"], "account_type_1": "payable", "param_1": 1}
    assert world.conn.writes == []


def test_unrelated_party_and_no_party_lines_keep_old_behavior(world):
    p.check_payable_source(world.ctx, "journal", [ledger.Line("bank", debit=D(1))])
    assert world.conn.queries == []
    world.conn.payable_line = False
    p.check_payable_source(world.ctx, "journal", [ledger.Line("ar", debit=D(1), party_id="customer")])
    assert len(world.conn.queries) == 1 and world.conn.writes == []


@pytest.mark.parametrize("site", ["preview", "locked"])
def test_both_ledger_boundaries_validate_lines_before_source_check(world, monkeypatch, site):
    events = []
    clean = [ledger.Line("ap", credit=D(1), party_id="supplier")]
    monkeypatch.setattr(ledger, "_lock_head", lambda ctx: events.append("chain") or {})
    monkeypatch.setattr(ledger, "open_period", lambda *a, **kw: events.append("period"))
    monkeypatch.setattr(ledger, "_check_lines", lambda ctx, lines: events.append("clean") or clean)
    def source(ctx, voucher, lines):
        events.append("source")
        assert lines is clean and voucher == "journal"
        raise Refusal("Original source rule.", "payable_source_not_allowed")
    monkeypatch.setattr(p, "check_payable_source", source)
    with pytest.raises(Refusal) as exc:
        if site == "preview": ledger.check_post(world.ctx, DAY, "journal", None, [])
        else: ledger._post(world.ctx, DAY, "journal", "v", [], memo=None, operation_key=None, reverses=None, closing=None)
    assert exc.value.code == "payable_source_not_allowed"
    assert events == (["chain"] if site == "locked" else []) + ["period", "clean", "source"]
    assert world.conn.writes == []


def test_earlier_line_validation_remains_authoritative(world, monkeypatch):
    def invalid(*a): raise Refusal("Wrong company account.", "bad_account")
    def forbidden(*a): raise AssertionError("source guard must follow line validation")
    monkeypatch.setattr(ledger, "open_period", lambda *a: None)
    monkeypatch.setattr(ledger, "_check_lines", invalid)
    monkeypatch.setattr(p, "check_payable_source", forbidden)
    with pytest.raises(Refusal) as exc: ledger.check_post(world.ctx, DAY, "journal", None, [])
    assert exc.value.code == "bad_account"


def test_close_only_untouched_item_with_lock_and_conditional_update(world):
    result = p.close_item(world.ctx, p.DELIVERY, "receipt")
    query = world.conn.queries[-1]
    assert query._for_update_arg is not None
    assert params(query) == {"company_id_1": "co", "source_type_1": p.DELIVERY, "source_id_1": "receipt"}
    assert [q.table.name for q in world.conn.writes] == ["supplier_payable", "audit_log"]
    update = params(world.conn.writes[0])
    assert update == dict(status="reversed", outstanding=D("0.00"), company_id_1="co", id_1="owed",
                          status_1="open", outstanding_1=D("1450.00"))
    assert result["status"] == "reversed" and result["outstanding"] == D("0.00")
    assert world.conn.item["status"] == "open"  # result is a copy


@pytest.mark.parametrize("status,outstanding,code", [("open", "400", "payable_touched"),
    ("paid", "0", "payable_touched"), ("reversed", "0", "payable_reversed")])
def test_touched_paid_or_reversed_delivery_cannot_close(world, status, outstanding, code):
    world.conn.item.update(status=status, outstanding=D(outstanding))
    with pytest.raises(Refusal) as exc: p.close_item(world.ctx, p.DELIVERY, "receipt")
    assert exc.value.code == code and world.conn.writes == []


def test_missing_delivery_refused_and_preview_never_locks_or_writes(world):
    item = p.check_close_item(world.ctx, p.DELIVERY, "receipt")
    assert item == world.conn.item and world.conn.queries[-1]._for_update_arg is None
    assert world.conn.writes == []
    world.conn.item = None
    with pytest.raises(Refusal) as exc: p.check_close_item(world.ctx, p.DELIVERY, "missing")
    assert exc.value.code == "no_payable" and world.conn.writes == []


def test_conditional_close_conflict_never_emits_success_audit(world):
    world.conn.rowcount = 0
    with pytest.raises(Refusal) as exc: p.close_item(world.ctx, p.DELIVERY, "receipt")
    assert exc.value.code == "conflict"
    assert len(world.conn.writes) == 1 and isinstance(world.conn.writes[0], Update)


def test_open_items_is_scoped_ordered_and_filters_payment_date_without_writes(world):
    first = dict(world.conn.item)
    future = dict(first, id="future", source_number="GR-2026-00002", posting_date=date(2026,4,10))
    world.conn.rows = [first, future]
    items = p.open_items(world.ctx, "supplier", date(2026,4,8))
    assert items == [{**first, "number": "GR-2026-00001"}]
    assert len(p.open_items(world.ctx, "supplier")) == 2
    q = world.conn.queries[0]
    assert params(q) == {"company_id_1": "co", "supplier_id_1": "supplier", "status_1": ["open"]}
    assert "ORDER BY supplier_payable.posting_date, supplier_payable.source_number, supplier_payable.opening_sequence, supplier_payable.id" in str(q)
    assert world.conn.writes == []


def test_slice_has_no_contract_registration_supplier_resolution_or_trusted_bypass():
    assert not any(hasattr(p, x) for x in ("CONTRACTS", "Trusted", "find_supplier", "suppliers", "pay_supplier", "supplier_owed"))
