"""Pure delivery reversal: real stock/ledger/payable helpers over recording objects.

No SQL engine, database, network, native host, contract registration or conftest.
"""
from datetime import date, datetime
from decimal import Decimal
import dataclasses
import json
from types import SimpleNamespace

import pytest
from approval_recording import RecordedApproval
from sqlalchemy.sql.dml import Insert, Update

from erpclaw_core import approvals, ledger, payables
from erpclaw_core.common import Refusal
from erpclaw_core.contracts import check_card_amounts
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import PLUGIN, reverse_goods_received as r, service as s
from erpclaw_stock.valuation import Group

D = Decimal
ORIGINAL = date(2026, 4, 5)
DAY = date(2026, 4, 6)


def request(**changes):
    data = dict(voucher_id="gr", reversal_date=DAY)
    data.update(changes)
    return r.ReverseGoodsReceived(**data)


class Rows:
    def __init__(self, rows=(), rowcount=1): self.rows, self.rowcount = list(rows), rowcount
    def mappings(self): return self
    def scalars(self): return self
    def first(self): return self.rows[0] if self.rows else None
    def one(self): assert len(self.rows) == 1; return self.rows[0]
    def scalar_one(self): return self.one()
    def scalar_one_or_none(self):
        value = self.first()
        return value.contract if hasattr(value, "contract") else value
    def all(self): return self.rows
    def __iter__(self): return iter(self.rows)


class Recording:
    def __init__(self):
        self.queries, self.writes, self.events, self.read_set = [], [], [], []
        self.card = SimpleNamespace(status="consumed", contract=r.NAME, company_id="co")
        self.outcome, self.replay, self.close_rows = None, None, 1
        self.voucher = dict(id="gr", company_id="co", kind="goods_received", number="GR-2026-00001", posting_date=ORIGINAL)
        self.header = dict(id="header", company_id="co", voucher_id="gr", supplier_id="supplier", settlement="still_owed", account_id="ap", number="GR-2026-00001")
        self.supplier = dict(id="supplier", company_id="co", kind="supplier", name="Copperline Metals")
        self.item = dict(id="item", company_id="co", kind="stock", name="Copper Wire", unit="m")
        self.stock = dict(id="stock-item", item_id="item", company_id="co", method="moving_average")
        self.warehouse = dict(id="wh", company_id="co", name="Main", account_id="inventory")
        self.accounts = {
            "inventory":dict(id="inventory", company_id="co", number="1300", name="Inventory", account_type="stock", is_group=False, frozen=False),
            "ap":dict(id="ap", company_id="co", number="2100", name="Payable", account_type="payable", is_group=False, frozen=False),
            "bank":dict(id="bank", company_id="co", number="1100", name="Bank", account_type="bank", is_group=False, frozen=False)}
        self.head = dict(id="head", company_id="co", item_id="item", warehouse_id="wh", qty=D(200), value=D(1450), last_seq=1, last_date=ORIGINAL)
        self.entries = [dict(id="entry", company_id="co", item_id="item", warehouse_id="wh", direction="in", qty=D(200), value=D(1450), seq=1, line_no=1, against_entry_id=None, reverses_entry_id=None, voucher_type=r.VOUCHER, voucher_id="gr", posting_date=ORIGINAL)]
        self.layers = {"layer":dict(id="layer",company_id="co",entry_id="entry",group_id="layer",qty=D(200),value=D(1450))}
        self.takes, self.stock_reversed, self.posting_reversed = [], False, False
        self.posting = dict(id="posting", company_id="co", voucher_type=r.VOUCHER, voucher_id="gr", posting_date=ORIGINAL, reverses_posting_id=None, seq=9)
        self.lines = [dict(id="le1",posting_id="posting",company_id="co",account_id="inventory",debit=D(1450),credit=D(0),party_id=None,line_no=1),
                      dict(id="le2",posting_id="posting",company_id="co",account_id="ap",debit=D(0),credit=D(1450),party_id="supplier",line_no=2)]
        self.payable = dict(id="payable",company_id="co",supplier_id="supplier",source_type=r.VOUCHER,source_id="gr",source_number="GR-2026-00001",currency="USD",amount=D(1450),outstanding=D(1450),status="open")
        self.period = dict(id="period",fiscal_year_id="year",status="open")
    def execute(self, stmt):
        q = stmt.compile().params
        if isinstance(stmt, (Insert, Update)):
            self.writes.append(stmt); self.events.append("write:" + stmt.table.name)
            if isinstance(stmt, Update) and stmt.table.name == "supplier_payable":
                assert q["company_id_1"] == "co" and q["id_1"] == self.payable["id"]
                assert q["status_1"] == "open" and q["outstanding_1"] == self.payable["outstanding"]
                if self.close_rows: self.payable.update(status=q["status"],outstanding=q["outstanding"])
                return Rows(rowcount=self.close_rows)
            if isinstance(stmt, Update) and stmt.table.name == "stock_head":
                assert q["id_1"] == self.head["id"]
                self.head.update({key:q[key] for key in ("qty","value","last_seq","last_date")})
                self.head["qty"] = D(self.head["qty"])
            if isinstance(stmt, Insert) and stmt.table.name == "stock_layer_take": self.takes.append(q)
            return Rows()
        self.queries.append(stmt)
        fields = [c.name for c in stmt.selected_columns]
        table = next(iter(stmt.selected_columns)).table.name
        if table == "approval":
            if fields == ["read_set_json"]: return Rows([json.dumps([[a,b,"same"] for a,b in self.read_set])])
            if fields == ["contract"]: return Rows([] if self.card is None else [SimpleNamespace(contract=self.card.contract)])
            return Rows([] if self.card is None else [self.card])
        if table == "operation_outcome":
            value = self.replay if fields == ["result_json"] else self.outcome
            return Rows([] if value is None else [value])
        if table == "stock_voucher": row = self.voucher if q["id_1"] == "gr" else None
        elif table == "stock_goods_received": row = self.header if q["voucher_id_1"] == "gr" else None
        elif table == "party":
            row = self.supplier if self.supplier and q["id_1"] == self.supplier["id"] else None
            if fields == ["name"]: return Rows([row["name"]] if row and row["company_id"] == q["company_id_1"] else [])
        elif table == "item": row = self.item if q["id_1"] == self.item["id"] else None
        elif table == "stock_item": row = self.stock
        elif table == "stock_warehouse": row = self.warehouse
        elif table == "account": row = self.accounts.get(q["id_1"])
        elif table == "stock_head":
            if stmt._for_update_arg is not None: self.events.append("lock:stock_head")
            row = self.head
        elif table == "stock_ledger_entry":
            if "reverses_entry_id_1" in q: return Rows([("reverse",)] if self.stock_reversed else [])
            assert q["company_id_1"] == "co" and q["voucher_type_1"] == r.VOUCHER and q["voucher_id_1"] == "gr"
            return Rows(self.entries)
        elif table == "stock_layer":
            row = self.layers.get(q["id_1"]) if "id_1" in q else next((x for x in self.layers.values() if x["entry_id"] == q["entry_id_1"]),None)
            return Rows([row] if row else [])
        elif table == "posting":
            if "reverses_posting_id_1" in q: return Rows([("reversed",)] if self.posting_reversed else [])
            row = self.posting
        elif table == "ledger_entry":
            assert q["posting_id_1"] == "posting"
            return Rows([x["id"] for x in self.lines] if fields == ["id"] else self.lines)
        elif table == "supplier_payable":
            if stmt._for_update_arg is not None: self.events.append("lock:supplier_payable")
            assert q["source_type_1"] == r.VOUCHER and q["source_id_1"] == "gr"
            row = self.payable
        elif table == "account_map":
            assert q["company_id_1"] == "co"
            return Rows(["map-" + key for key in q["account_id_1"]])
        else: raise AssertionError((table,q))
        assert q.get("company_id_1") == "co", (table,q)
        return Rows([row] if row is not None and row["company_id"] == q["company_id_1"] else [])


@pytest.fixture
def world(monkeypatch):
    c = Recording()
    ctx = SimpleNamespace(conn=c,company_id="co",company_name="Test Books",currency="USD",today=DAY,actor="owner",extra={"commit":"approval"})
    monkeypatch.setattr(REGISTRY,"contract_plugin",{r.NAME:"stock","record_opening_stock":"stock","other":"core"})
    def period(ctx,day,**kw):
        if c.period["status"] != "open": raise Refusal("Period is closed.","period_closed")
        return c.period
    monkeypatch.setattr(ledger,"open_period",period)
    def check(ctx,day,kind,vid,lines):
        assert kind == r.VOUCHER + ":reversal" and vid == "gr"
        assert sum(x.debit for x in lines) == sum(x.credit for x in lines)
        c.events.append("ledger-preflight")
        return lines
    monkeypatch.setattr(ledger,"check_post",check)
    monkeypatch.setattr(r.integration,"system_of_record",lambda *a:("native",None))
    monkeypatch.setattr(approvals,"_row_digest",lambda *a:"same")
    monkeypatch.setattr(s,"fifo_groups",lambda *a:[Group("layer",D(c.head["qty"]),c.head["value"])])
    authority = RecordedApproval(ctx, lambda: c.card, lambda: c.outcome)
    authority.wrap(monkeypatch, r, "commit_reverse_goods_received")
    authority.wrap(monkeypatch, s, "reverse_movements")
    return SimpleNamespace(ctx=ctx,authority=authority,c=c)


def paid(world):
    c = world.c
    c.header.update(settlement="paid_now",account_id="bank")
    c.entries[0].update(qty=D(100),value=D(725))
    c.head.update(qty=D(100),value=D(725))
    c.lines[0]["debit"] = D(725)
    c.lines[1].update(account_id="bank",credit=D(725),party_id=None)
    c.payable = None


def tuples(lines): return [(x.account_id,x.debit,x.credit,x.party_id) for x in lines]


def test_si7_untouched_delivery_exact_mirror_and_card(world):
    plan = r.plan_reverse_goods_received(world.ctx,request())
    assert plan.total == D(1450)
    assert tuples(plan.mirror) == [("inventory",D(0),D(1450),None),("ap",D(1450),D(0),"supplier")]
    assert [(m.direction,m.qty,m.value,m.qty_after,m.value_after) for m in plan.moves] == [("out",D(200),D(1450),D(0),D(0))]
    card = r.preview_reverse_goods_received(world.ctx,request()).card
    assert "Copperline Metals" in dict(card.lines)["Delivery"]
    assert dict(card.lines)["Delivery payable"] == "1,450.00 USD outstanding becomes 0.00 USD; status reversed."
    assert "(Copperline Metals)" in card.books[1]
    assert not any("COGS" in text for text in card.books)
    check_card_amounts(card.as_dict())
    required = {("stock_voucher","gr"),("stock_goods_received","header"),("stock_ledger_entry","entry"),("stock_head","head"),("posting","posting"),("ledger_entry","le1"),("ledger_entry","le2"),("supplier_payable","payable"),("party","supplier"),("fiscal_period","period"),("fiscal_year","year")}
    assert required <= set(plan.read_set) and plan.read_set == sorted(set(plan.read_set))
    assert world.c.writes == []


def test_si1_paid_now_reverses_bank_without_payable_read(world):
    paid(world)
    plan = r.plan_reverse_goods_received(world.ctx,request())
    assert plan.payable is None and plan.total == D(725)
    assert tuples(plan.mirror)[-1] == ("bank",D(725),D(0),None)
    assert not any("supplier_payable" in str(q) for q in world.c.queries)
    card = r.preview_reverse_goods_received(world.ctx,request()).card
    assert "Delivery payable" not in dict(card.lines)
    check_card_amounts(card.as_dict())


@pytest.mark.parametrize("change,code",[(dict(outstanding=D(850)),"payable_touched"),(dict(status="paid",outstanding=D(0)),"payable_touched"),(dict(status="reversed",outstanding=D(0)),"payable_reversed")])
def test_touched_or_ended_item_refuses_no_card_or_effect(world,change,code):
    world.c.payable.update(change)
    with pytest.raises(Refusal) as exc: r.preview_reverse_goods_received(world.ctx,request())
    assert exc.value.code == code and world.c.writes == []
    if code == "payable_touched": assert str(exc.value) == "GR-2026-00001 has a payment against it; reverse the payment first."


def test_missing_owed_item_never_defaults_to_paid(world):
    world.c.payable = None
    with pytest.raises(Refusal) as exc: r.plan_reverse_goods_received(world.ctx,request())
    assert exc.value.code == "no_payable" and world.c.writes == []


@pytest.mark.parametrize("where,change",[("payable",dict(supplier_id="other")),("payable",dict(amount=D(1400),outstanding=D(1400))),("payable",dict(currency="JPY")),("payable",dict(source_number="GR-other")),("header",dict(settlement="unknown")),("header",dict(number="GR-other")),("header",dict(account_id="bank")),("posting",dict(posting_date=DAY))])
def test_inconsistent_stored_facts_refuse_before_effect(world,where,change):
    getattr(world.c,where).update(change)
    with pytest.raises(Refusal) as exc: r.plan_reverse_goods_received(world.ctx,request())
    assert exc.value.code == "goods_received_inconsistent" and world.c.writes == []


@pytest.mark.parametrize("bad",[None,"",True," gr","x"*37])
def test_invalid_document_id(world,bad):
    with pytest.raises(Refusal) as exc: r.plan_reverse_goods_received(world.ctx,request(voucher_id=bad))
    assert exc.value.code == "goods_received_input" and world.c.queries == []


@pytest.mark.parametrize("bad",[None,"2026-04-06",datetime(2026,4,6)])
def test_invalid_reversal_date(world,bad):
    with pytest.raises(Refusal) as exc: r.plan_reverse_goods_received(world.ctx,request(reversal_date=bad))
    assert exc.value.code == "goods_received_input"


@pytest.mark.parametrize("where",["voucher","header","supplier","posting","payable"])
def test_company_scoped_stored_fact_lookup(world,where):
    getattr(world.c,where)["company_id"] = "other"
    with pytest.raises(Refusal): r.plan_reverse_goods_received(world.ctx,request())
    assert world.c.writes == []


@pytest.mark.parametrize("case,code",[("kind","goods_received_missing"),("header","goods_received_inconsistent"),("stock_reversed","already_reversed"),("posting_reversed","already_reversed"),("later_stock","not_latest"),("before","backdated"),("closed","period_closed")])
def test_original_stock_and_ledger_refusals(world,case,code):
    req = request()
    if case == "kind": world.c.voucher["kind"] = "opening"
    elif case == "header": world.c.header = None
    elif case == "later_stock": world.c.head["last_seq"] += 1
    elif case == "before": req = request(reversal_date=date(2026,4,4))
    elif case == "closed": world.c.period["status"] = "closed"
    else: setattr(world.c,case,True)
    with pytest.raises(Refusal) as exc: r.plan_reverse_goods_received(world.ctx,req)
    assert exc.value.code == code and world.c.writes == []


def test_actual_card_digest_binds_destination_and_codes(world,monkeypatch):
    contract = SimpleNamespace(preview=r.preview_reverse_goods_received)
    first = approvals._preview(world.ctx,contract,request())
    monkeypatch.setattr(r.integration,"system_of_record",lambda *a:("external","target"))
    monkeypatch.setattr(r.integration,"account_codes",lambda ctx,system,ids:{key:"EXT-"+key for key in ids})
    second = approvals._preview(world.ctx,contract,request())
    assert approvals.digest(first) != approvals.digest(second)
    monkeypatch.setattr(r.integration,"account_codes",lambda ctx,system,ids:{key:"NEW-"+key for key in ids})
    third = approvals._preview(world.ctx,contract,request())
    assert approvals.digest(second) != approvals.digest(third)
    assert {("account_map","map-inventory"),("account_map","map-ap")} <= set(r.reverse_goods_received_read_set(world.ctx,request()))


def prepare_commit(world,monkeypatch):
    c = world.c
    c.read_set = r.reverse_goods_received_read_set(world.ctx,request())
    c.events.clear(); c.queries.clear()
    def post(ctx,day,kind,vid,lines,**kw):
        assert kind == r.VOUCHER+":reversal" and vid == "gr" and kw["_reverses"] == "posting"
        assert tuples(lines) == [(x["account_id"],x["credit"],x["debit"],x["party_id"]) for x in c.lines]
        c.events.append("ledger-write")
    monkeypatch.setattr(ledger,"post",post)


@pytest.mark.parametrize("settlement",["still_owed","paid_now"])
@pytest.mark.parametrize("method",["moving_average","fifo"])
def test_commit_real_stock_entries_ledger_mirror_and_payable_close(world,monkeypatch,settlement,method):
    if settlement == "paid_now": paid(world)
    world.c.stock["method"] = method
    world.c.layers["layer"].update(qty=world.c.head["qty"],value=world.c.head["value"])
    prepare_commit(world,monkeypatch)
    result = r.commit_reverse_goods_received(world.ctx,request(),"key")
    c = world.c
    assert c.head["qty"] == 0 and c.head["value"] == 0 and c.head["last_seq"] == 2
    entries = [q.compile().params for q in c.writes if q.table.name == "stock_ledger_entry"]
    assert len(entries) == 1 and entries[0]["reverses_entry_id"] == "entry"
    assert entries[0]["voucher_type"] == r.VOUCHER+":reversal" and entries[0]["voucher_id"] == "gr"
    assert c.events.index("ledger-preflight") < c.events.index("write:stock_ledger_entry") < c.events.index("ledger-write")
    if settlement == "still_owed":
        assert c.events.index("lock:stock_head") < c.events.index("lock:supplier_payable") < c.events.index("write:stock_ledger_entry")
        assert c.payable["status"] == "reversed" and c.payable["outstanding"] == 0
        assert c.events.count("write:supplier_payable") == 1 and c.events.count("write:audit_log") == 1
    else: assert not any("supplier_payable" in x for x in c.events)
    if method == "fifo":
        assert len(c.takes) == 1 and c.takes[0]["layer_id"] == "layer" and c.takes[0]["kind"] == "take"
    assert result.data["number"] == "GR-2026-00001" and result.data["voucher_id"] == "gr"
    assert all(q.table.name not in ("stock_voucher","stock_goods_received") for q in c.writes)


def test_repeated_pair_reverses_original_contiguous_run_descending(world,monkeypatch):
    c = world.c
    c.entries[0].update(qty=D(100),value=D(725))
    c.entries.append({**c.entries[0],"id":"entry2","seq":2,"line_no":2})
    c.head["last_seq"] = 2
    c.stock["method"] = "fifo"
    c.layers["layer"].update(qty=D(100),value=D(725))
    c.layers["layer2"] = {**c.layers["layer"],"id":"layer2","entry_id":"entry2"}
    prepare_commit(world,monkeypatch)
    r.commit_reverse_goods_received(world.ctx,request(),"key")
    entries = [q.compile().params for q in c.writes if q.table.name == "stock_ledger_entry"]
    assert [(x["reverses_entry_id"],x["seq"],x["qty_after"],x["value_after"]) for x in entries] == [("entry2",3,"100",D(725)),("entry",4,"0",D(0))]
    assert [x["layer_id"] for x in c.takes] == ["layer2","layer"]


@pytest.mark.parametrize("case",["missing","pending","completed","foreign","other_card","absent_registry"])
def test_actual_authority_requires_own_inflight_stock_contract(world,case):
    if case == "missing": world.authority.enabled = False
    elif case == "pending": world.c.card.status = "pending"
    elif case == "completed": world.c.outcome = "key"
    elif case == "foreign": world.c.card.company_id = "other"
    elif case == "other_card": world.c.card.contract = "record_opening_stock"
    elif case == "absent_registry": REGISTRY.contract_plugin.pop(r.NAME)
    with pytest.raises(Refusal) as exc: r.commit_reverse_goods_received(world.ctx,request(),"key")
    assert exc.value.code == "not_approved" and world.c.writes == [] and "ledger-preflight" not in world.c.events


def test_missing_approved_read_set_refuses_before_effects(world):
    with pytest.raises(Refusal) as exc: r.commit_reverse_goods_received(world.ctx,request(),"key")
    assert exc.value.code == "stale" and world.c.writes == []


def test_conditional_close_failure_is_preserved(world,monkeypatch):
    prepare_commit(world,monkeypatch); world.c.close_rows = 0
    with pytest.raises(Refusal) as exc: r.commit_reverse_goods_received(world.ctx,request(),"key")
    assert exc.value.code == "conflict"
    assert "write:audit_log" not in world.c.events
    # Recording objects do not roll back: real enclosing-savepoint rollback is a deferred DB gate.


@pytest.mark.parametrize("settlement",["paid_now","still_owed"])
@pytest.mark.parametrize("entrypoint",["preview","commit"])
def test_generic_gr_paths_route_delivery_before_stock_effects(world,monkeypatch,settlement,entrypoint):
    world.c.header["settlement"] = settlement
    monkeypatch.setattr(s,"_reverse_moves",lambda *a,**k:pytest.fail("generic GR reached stock reversal"))
    with pytest.raises(Refusal) as exc:
        if entrypoint == "preview": s.preview_reverse(world.ctx,r.VOUCHER,"gr",DAY)
        else: s.reverse_movements(world.ctx,r.VOUCHER,"gr",DAY,"other","id")
    assert exc.value.code == "goods_received_reversal_required"
    assert exc.value.data["points_to"] == r.NAME and world.c.writes == []
    if settlement == "still_owed": assert str(exc.value) == "GR-2026-00001 is still owed to Copperline Metals; reverse it as a delivery."


def test_generic_foreign_delivery_reveals_no_other_company_facts(world):
    world.c.header["company_id"] = "other"
    with pytest.raises(Refusal) as exc: s.preview_reverse(world.ctx,r.VOUCHER,"gr",DAY)
    assert str(exc.value) == "Goods received must be reversed as a delivery."


@pytest.mark.parametrize("kind",["stock_receipt","sales_invoice","sales_credit_note"])
def test_non_gr_service_boundary_unchanged(world,monkeypatch,kind):
    monkeypatch.setattr(s,"_reverse_moves",lambda *a,**k:[])
    monkeypatch.setattr(s,"check_fresh",lambda *a,**k:None)
    monkeypatch.setattr(s,"contribute",lambda *a,**k:None)
    assert s.preview_reverse(world.ctx,kind,"id",DAY) == {"mirror_entries":[],"contribution":[],"read_set":[]}
    assert s.reverse_movements(world.ctx,kind,"id",DAY,kind+":reversal","id") == {"contribution":[],"entry_ids":[]}
    assert not any("stock_goods_received" in str(q) for q in world.c.queries)


def test_exact_committed_company_contract_scoped_replay(world):
    assert r.find_reverse_goods_received(world.ctx,"key") is None
    world.c.replay = json.dumps({"text":"Reversed GR","data":{"number":"GR-1"},"figures":["1.00"],"amounts":[{"what":"value","of":"GR-1","value":"1.00","currency":"USD"}]})
    result = r.find_reverse_goods_received(world.ctx,"key")
    assert result.data == {"number":"GR-1"} and result.amounts[0].value == "1.00"
    q = world.c.queries[-1].compile().params
    assert q == {"company_id_1":"co","key_1":"key","status_1":"committed","contract_1":r.NAME}


def test_backend_dataclass_has_public_contract_adapter():
    assert r.NAME in [contract.name for contract in PLUGIN.contracts]
    assert r.CONTRACT.input_model is r.ReverseGoodsReceivedInput
    assert dataclasses.is_dataclass(r.ReverseGoodsReceived)
