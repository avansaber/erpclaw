"""Genuine pure GR boundaries; recording objects only, no engine/SQL/network."""
import dataclasses
from datetime import date
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from approval_recording import RecordedApproval
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.dml import Insert, Update

from erpclaw_core import approvals, ledger, payables
from erpclaw_core.common import Refusal
from erpclaw_core.contracts import check_card_amounts
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import MANIFEST, PLUGIN, goods_received as g, service as s, tables as t

D = Decimal
DAY = date(2026,4,5)
REAL_CHECK_POST = ledger.check_post
REAL_FIFO_GROUPS = s.fifo_groups


def request(settlement="paid_now", **changes):
    data = dict(supplier_id="supplier", posting_date=DAY, settlement=settlement,
                bank_account_id="bank" if settlement == "paid_now" else None,
                lines=(g.ReceiptLine("item", "100", "7.25"),))
    data.update(changes)
    return g.GoodsReceived(**data)


class Rows:
    def __init__(self, rows=()): self.rows = list(rows)
    def mappings(self): return self
    def scalars(self): return self
    def first(self): return self.rows[0] if self.rows else None
    def scalar_one_or_none(self):
        value = self.first()
        return value.contract if hasattr(value, "contract") else value
    def scalar_one(self): return self.first()
    def all(self): return self.rows
    def __iter__(self): return iter(self.rows)


class Boundary:
    def __init__(self):
        self.card = SimpleNamespace(status="consumed", contract=g.NAME, company_id="co")
        self.outcome, self.replay = None, None
        self.read_set, self.queries, self.writes, self.events = [], [], [], []
        self.supplier = dict(id="supplier", company_id="co", kind="supplier", name="Copperline Metals")
        self.items = {"item":dict(id="item", company_id="co", kind="stock", name="Copper Wire", unit="m", price=D(999))}
        self.stock = dict(id="stock-item", item_id="item", company_id="co", method="fifo", default_warehouse_id="wh")
        self.warehouses = {"wh":dict(id="wh", company_id="co", name="Main Warehouse", account_id="inventory", is_default=False)}
        self.accounts = {
            "inventory":dict(id="inventory", company_id="co", number="1300", name="Inventory", account_type="stock", is_group=False, frozen=False),
            "bank":dict(id="bank", company_id="co", number="1100", name="Bank", account_type="bank", is_group=False, frozen=False),
            "ap":dict(id="ap", company_id="co", number="2100", name="Accounts Payable", account_type="payable", is_group=False, frozen=False)}
        self.heads, self.groups, self.owed = {}, {}, []
        self.period = dict(id="period", fiscal_year_id="fy", status="open")
    def execute(self, stmt):
        if isinstance(stmt,(Insert,Update)):
            assert isinstance(stmt,Insert), "Forward receipt cannot update via this recording boundary"
            self.writes.append(stmt)
            self.events.append("insert:"+stmt.table.name)
            return Rows()
        self.queries.append(stmt)
        table = next(iter(stmt.selected_columns)).table.name
        fields = [c.name for c in stmt.selected_columns]
        q = stmt.compile().params
        if table == "approval":
            if fields == ["read_set_json"]: return Rows([json.dumps([[a,b,"same"] for a,b in self.read_set])])
            if fields == ["contract"]: return Rows([] if self.card is None else [SimpleNamespace(contract=self.card.contract)])
            return Rows([] if self.card is None else [self.card])
        if table == "operation_outcome":
            value = self.replay if fields == ["result_json"] else self.outcome
            return Rows([] if value is None else [value])
        assert q.get("company_id_1") == "co", (table,q)
        if table == "party": row = self.supplier if self.supplier and q["id_1"] == self.supplier["id"] else None
        elif table == "item": row = self.items.get(q["id_1"])
        elif table == "stock_item": row = self.stock if q["item_id_1"] == self.stock["item_id"] else None
        elif table == "account":
            if isinstance(q["id_1"], list):
                return Rows([(key,) for key in q["id_1"] if self.accounts[key]["account_type"]==q["account_type_1"]])
            row = self.accounts.get(q["id_1"])
        elif table == "posting": return Rows()
        elif table == "stock_warehouse":
            if "id_1" not in q: return Rows(self.warehouses.values())
            row = self.warehouses.get(q["id_1"])
        elif table == "stock_head": row = self.heads.get((q["item_id_1"],q["warehouse_id_1"]))
        elif table == "supplier_payable": return Rows(self.owed)
        elif table == "account_map": return Rows(["map-"+a for a in q["account_id_1"]])
        else: raise AssertionError((table,q))
        return Rows([] if row is None else [row])


@pytest.fixture
def world(monkeypatch):
    c=Boundary()
    ctx=SimpleNamespace(conn=c,company_id="co",company_name="Test Books",currency="USD",today=DAY,
                        actor="owner",extra={"commit":"approval"})
    monkeypatch.setattr(REGISTRY,"contract_plugin",{g.NAME:"stock","record_opening_stock":"stock","other":"core"})
    def period(ctx,day,**kw): return c.period
    monkeypatch.setattr(ledger,"open_period",period)
    def role(ctx,role):
        assert role == "payable"
        return c.accounts["ap"]
    monkeypatch.setattr(ledger,"account_by_type",role)
    def check(ctx,day,kind,vid,lines):
        assert kind == g.VOUCHER and day == DAY
        assert sum(x.debit for x in lines) == sum(x.credit for x in lines)
        assert all(x.account_id != "srnb" for x in lines)
        c.events.append("preflight")
        return lines
    monkeypatch.setattr(ledger,"check_post",check)
    monkeypatch.setattr(g.integration,"system_of_record",lambda *a:("native",None))
    monkeypatch.setattr(approvals,"_row_digest",lambda *a:"same")
    monkeypatch.setattr(s,"fifo_groups",lambda ctx,item,wh:list(c.groups.get((item,wh),[])))
    authority = RecordedApproval(ctx, lambda: c.card, lambda: c.outcome)
    authority.wrap(monkeypatch, g, "commit_goods_received")
    return SimpleNamespace(ctx=ctx,authority=authority,c=c)


def lines(plan): return [(x.account_id,x.debit,x.credit,x.party_id) for x in plan.posting]


def test_si1_paid_receipt_card_and_amount_without_payable(world):
    plan=g.plan_goods_received(world.ctx,request())
    assert plan.total==D("725.00") and plan.owed_after is None
    assert lines(plan)==[("inventory",D("725.00"),D(0),None),("bank",D(0),D("725.00"),None)]
    card=g.preview_goods_received(world.ctx,request()).card
    assert dict(card.lines)["Settlement"]=="Paid from Bank (1100)"
    assert "into Main Warehouse (0 on hand now)" in dict(card.lines)["Copper Wire"]
    assert dict(card.lines)["Total"]=="725.00 USD" and "Total owed" not in dict(card.lines)
    check_card_amounts(card.as_dict())
    assert world.c.writes==[]


def test_si2_owed_receipt_and_actual_outstanding_card(world):
    world.c.owed=[dict(id="old",source_number="GR-2026-00000",posting_date=DAY,amount=D(50),outstanding=D(50),status="open")]
    req=request("still_owed",lines=(g.ReceiptLine("item",200,"7.25"),))
    plan=g.plan_goods_received(world.ctx,req)
    assert plan.total==D("1450.00") and plan.owed_after==D("1500.00")
    assert lines(plan)[-1]==("ap",D(0),D("1450.00"),"supplier")
    assert ("supplier_payable","old") in plan.read_set
    card=g.preview_goods_received(world.ctx,req).card
    assert dict(card.lines)["Total owed"]=="Copperline Metals will be owed 1,500.00 USD in total."
    assert "(Copperline Metals)" in card.books[-1]
    check_card_amounts(card.as_dict())


@pytest.mark.parametrize("field",["quantity","rate"])
@pytest.mark.parametrize("bad",[True,1.5,"0","-1","1e2","NaN","1.00001","100000000000000"])
def test_bad_owner_figures_refuse_before_writes(world,field,bad):
    values=dict(item_id="item",quantity=100,rate="7.25");values[field]=bad
    with pytest.raises(Refusal):g.plan_goods_received(world.ctx,request(lines=(g.ReceiptLine(**values),)))
    assert world.c.writes==[]


@pytest.mark.parametrize("case",["zero","individual","aggregate"])
def test_receipt_value_limits_are_typed(world,case):
    if case=="zero": entries=(g.ReceiptLine("item","0.0001","0.0001"),)
    elif case=="individual":entries=(g.ReceiptLine("item","99999999999999","1000"),)
    else:entries=(g.ReceiptLine("item","99999999999999","9"),)*2
    with pytest.raises(Refusal) as exc:g.plan_goods_received(world.ctx,request(lines=entries))
    assert exc.value.code==("receipt_below_minor_unit" if case=="zero" else "goods_received_value")
    assert world.c.writes==[] and world.c.events==[]


@pytest.mark.parametrize("currency,expected",[("USD","53.13"),("JPY","53"),("KWD","53.125")])
def test_existing_receipt_valuation_at_company_scale(world,currency,expected):
    world.ctx.currency=currency
    plan=g.plan_goods_received(world.ctx,request(lines=(g.ReceiptLine("item","12.5","4.25"),)))
    assert plan.total==D(expected)


@pytest.mark.parametrize("change,code",[(dict(supplier_id="foreign"),"bad_party"),(dict(settlement="maybe"),"goods_received_settlement"),
    (dict(bank_account_id=None),"account_missing"),(dict(bank_account_id="foreign"),"account_not_bank"),
    (dict(lines=()),"goods_received_input"),(dict(posting_date=None),"goods_received_input")])
def test_bad_resolved_request_refuses_before_effects(world,change,code):
    with pytest.raises(Refusal) as exc:g.plan_goods_received(world.ctx,request(**change))
    assert exc.value.code==code and world.c.writes==[]


def test_still_owed_cannot_also_choose_bank(world):
    with pytest.raises(Refusal) as exc:g.plan_goods_received(world.ctx,request("still_owed",bank_account_id="bank"))
    assert exc.value.code=="goods_received_settlement" and world.c.writes==[]


@pytest.mark.parametrize("target,field,value,code",[("supplier","kind","customer","bad_party"),
    ("item","kind","service","item_not_stock"),("bank","account_type","stock","account_not_bank"),
    ("bank","is_group",True,"account_not_bank"),("bank","frozen",True,"account_frozen"),
    ("inventory","frozen",True,"frozen_account")])
def test_company_record_roles_are_checked(world,target,field,value,code):
    row=world.c.supplier if target=="supplier" else world.c.items["item"] if target=="item" else world.c.accounts[target]
    row[field]=value
    with pytest.raises(Refusal) as exc:g.plan_goods_received(world.ctx,request())
    assert exc.value.code==code and world.c.writes==[]


def test_missing_payable_role_remains_typed(world,monkeypatch):
    def absent(*args):raise Refusal("No accounts payable account.","no_payable_account")
    monkeypatch.setattr(ledger,"account_by_type",absent)
    with pytest.raises(Refusal) as exc:g.plan_goods_received(world.ctx,request("still_owed"))
    assert exc.value.code=="no_payable_account" and world.c.writes==[]


def test_receipt_destination_defaults_ignore_existing_quantity(world,monkeypatch):
    def forbidden(*a,**kw):raise AssertionError("Issue selector must not run for receipt")
    monkeypatch.setattr(s,"choose_warehouse",forbidden)
    other=dict(world.c.warehouses["wh"],id="wh2",name="Shop Warehouse",is_default=True)
    world.c.warehouses["wh2"]=other
    assert g.plan_goods_received(world.ctx,request()).lines[0].warehouse["id"]=="wh"
    world.c.stock["default_warehouse_id"]=None
    assert g.plan_goods_received(world.ctx,request()).lines[0].warehouse["id"]=="wh2"
    assert g.plan_goods_received(world.ctx,request(lines=(g.ReceiptLine("item",1,1,"wh"),))).lines[0].warehouse["id"]=="wh"
    other["is_default"]=False
    with pytest.raises(Refusal) as exc:g.plan_goods_received(world.ctx,request())
    assert exc.value.code=="warehouse_ambiguous"
    world.c.warehouses.clear()
    with pytest.raises(Refusal) as exc:g.plan_goods_received(world.ctx,request())
    assert exc.value.code=="no_warehouse"


def test_repeated_pairs_keep_each_rate_and_line_in_one_posting(world):
    req=request(lines=(g.ReceiptLine("item",100,"7.25"),g.ReceiptLine("item",40,"7.50")))
    plan=g.plan_goods_received(world.ctx,req)
    assert [x.value for x in plan.lines]==[D("725.00"),D("300.00")]
    assert plan.total==D("1025.00") and len(plan.posting)==2
    assert len([x for x in g.preview_goods_received(world.ctx,req).card.lines if x[0]=="Copper Wire"])==2


def test_multiple_inventory_accounts_are_summed_separately(world):
    world.c.warehouses["wh2"]=dict(world.c.warehouses["wh"],id="wh2",name="Shop",account_id="inventory2")
    world.c.accounts["inventory2"]=dict(world.c.accounts["inventory"],id="inventory2",number="1310")
    plan=g.plan_goods_received(world.ctx,request(lines=(g.ReceiptLine("item",100,"7.25"),g.ReceiptLine("item",40,"7.50","wh2"))))
    assert [(x.account_id,x.debit,x.credit) for x in plan.posting]==[("inventory",D(725),D(0)),("inventory2",D(300),D(0)),("bank",D(0),D(1025))]


def test_date_and_complete_read_set(world):
    world.c.heads[("item","wh")]=dict(id="head",qty=D(2),value=D(14),last_date=DAY,last_seq=1)
    plan=g.plan_goods_received(world.ctx,request())
    assert set(plan.read_set)=={("company","co"),("party","supplier"),("fiscal_period","period"),("fiscal_year","fy"),
        ("account","bank"),("account","inventory"),("item","item"),("stock_item","stock-item"),("stock_warehouse","wh"),("stock_head","head")}
    with pytest.raises(Refusal) as exc:g.plan_goods_received(world.ctx,request(posting_date=date(2026,4,4)))
    assert exc.value.code=="backdated"


def test_actual_card_changes_for_destination_codes_and_owed_total(world,monkeypatch):
    req=request("still_owed")
    native=g.preview_goods_received(world.ctx,req).card.as_dict()
    monkeypatch.setattr(g.integration,"system_of_record",lambda *a:("external","ERP One"))
    codes={"inventory":"I1","ap":"A1"}
    monkeypatch.setattr(g.integration,"account_codes",lambda ctx,system,ids:{k:codes[k] for k in ids})
    external=g.preview_goods_received(world.ctx,req).card.as_dict()
    assert {row for row in g.goods_received_read_set(world.ctx,req) if row[0]=="account_map"}=={("account_map","map-inventory"),("account_map","map-ap")}
    codes["ap"]="A2";changed=g.preview_goods_received(world.ctx,req).card.as_dict()
    world.c.owed=[dict(id="old",source_number="GR-old",posting_date=DAY,outstanding=D(50))]
    owing=g.preview_goods_received(world.ctx,req).card.as_dict()
    monkeypatch.setattr(g.integration,"system_of_record",lambda *a:("external","ERP Two"))
    destination=g.preview_goods_received(world.ctx,req).card.as_dict()
    cards=[native,external,changed,owing,destination]
    assert len({approvals.digest(card) for card in cards})==5
    for card in cards:check_card_amounts(card)


def prepare_commit(world,monkeypatch,req):
    c=world.c;c.read_set=g.goods_received_read_set(world.ctx,req);c.events.clear()
    def number(ctx,series,day):
        assert series=="GR" and day==DAY;c.events.append("number");return "GR-2026-00001"
    monkeypatch.setattr(g,"next_number",number)
    moves=[]
    def entry(ctx,mv,day,kind,vid):
        c.events.append("stock_entry");moves.append(mv)
        key=(mv.pair.item["item_id"],mv.pair.wh["id"])
        c.heads[key]=dict(id="head",qty=mv.qty_after,value=mv.value_after,last_date=day,last_seq=len(moves))
        if mv.layer:
            groups=c.groups.setdefault(key,[])
            if mv.merge_into:
                c.groups[key]=[s.Group(x.id,x.qty+mv.qty,x.value+mv.value) if x.id==mv.merge_into else x for x in groups]
            else:groups.append(s.Group("group"+str(len(moves)),mv.qty,mv.value))
    monkeypatch.setattr(s,"_entry_id",entry)
    def post(ctx,day,kind,vid,posting,**kw):
        assert kw=={"operation_key":"operation"};c.events.append("post")
    monkeypatch.setattr(ledger,"post",post)
    return moves


@pytest.mark.parametrize("settlement",["paid_now","still_owed"])
def test_commit_header_stock_payable_order_and_typed_result(world,monkeypatch,settlement):
    req=request(settlement,lines=(g.ReceiptLine("item",200,"7.25"),))
    moves=prepare_commit(world,monkeypatch,req)
    result=g.commit_goods_received(world.ctx,req,"operation")
    expected=["preflight","preflight","number","insert:stock_voucher","insert:stock_goods_received","stock_entry"]
    if settlement=="still_owed":expected.append("insert:supplier_payable")
    assert world.c.events==expected+["post"]
    header=world.c.writes[1].compile().params
    assert header["supplier_id"]=="supplier" and header["company_id"]=="co" and header["settlement"]==settlement
    assert header["account_id"]==("bank" if settlement=="paid_now" else "ap")
    assert header["number"]=="GR-2026-00001" and header["voucher_id"]==world.c.writes[0].compile().params["id"]
    assert not any(x.table.name=="stock_voucher_line" for x in world.c.writes)
    if settlement=="still_owed":
        item=world.c.writes[-1].compile().params
        assert item["amount"]==item["outstanding"]==D(1450) and item["source_id"]==header["voucher_id"]
    assert moves[0].qty==D(200) and moves[0].value==D(1450)
    assert result.amounts[0].value=="1450.00" and result.data["voucher_type"]==g.VOUCHER


@pytest.mark.parametrize("method",["fifo","moving_average"])
def test_actual_receipt_machinery_handles_repeated_pairs_sequentially(world,monkeypatch,method):
    world.c.stock["method"]=method
    req=request(lines=(g.ReceiptLine("item",100,"7.25"),g.ReceiptLine("item",40,"7.25"),g.ReceiptLine("item",10,"7.50")))
    moves=prepare_commit(world,monkeypatch,req)
    g.commit_goods_received(world.ctx,req,"operation")
    assert [x.line_no for x in moves]==[1,2,3]
    assert [x.qty_after for x in moves]==[D(100),D(140),D(150)]
    assert world.c.heads[("item","wh")]["value"]==D(1090)
    if method=="fifo":
        assert [x.merge_into for x in moves]==[None,"group1",None]
        assert [(x.qty,x.value) for x in world.c.groups[("item","wh")]]==[(D(140),D(1015)),(D(10),D(75))]
    else:assert not world.c.groups


@pytest.mark.parametrize("bad",["missing_commit","no_card","pending","outcome","company","other_card","unregistered"])
def test_commit_cannot_bypass_missing_public_contract_or_real_authority(world,monkeypatch,bad):
    if bad=="missing_commit":world.authority.enabled = False
    elif bad=="no_card":world.c.card=None
    elif bad=="pending":world.c.card.status="pending"
    elif bad=="outcome":world.c.outcome=("done",)
    elif bad=="company":world.c.card.company_id="elsewhere"
    elif bad=="other_card":world.c.card.contract="record_opening_stock"
    else:monkeypatch.setattr(REGISTRY,"contract_plugin",{})
    with pytest.raises(Refusal) as exc:g.commit_goods_received(world.ctx,request(),"operation")
    assert exc.value.code=="not_approved" and world.c.writes==[] and world.c.events==[]


def test_stale_or_whole_posting_refusal_precedes_number_and_effects(world,monkeypatch):
    prepare_commit(world,monkeypatch,request())
    monkeypatch.setattr(approvals,"_row_digest",lambda *a:"changed")
    with pytest.raises(Refusal) as exc:g.commit_goods_received(world.ctx,request(),"operation")
    assert exc.value.code=="stale" and world.c.writes==[] and "number" not in world.c.events
    def reject(*a):raise Refusal("Invalid posting.","bad_posting")
    monkeypatch.setattr(ledger,"check_post",reject)
    with pytest.raises(Refusal) as exc:g.commit_goods_received(world.ctx,request(),"operation")
    assert exc.value.code=="bad_posting" and world.c.writes==[]


def test_replay_exact_company_key_and_contract(world):
    world.c.replay=json.dumps(dict(text="Recorded.",data={"number":"GR-2026-00001"},figures=["725.00"],
        amounts=[dict(what="value",of="GR-2026-00001",value="725.00",currency="USD")]))
    result=g.find_goods_received(world.ctx,"operation")
    assert result.amounts[0].value=="725.00"
    assert set(world.c.queries[-1].compile().params.values())=={"co","operation","committed",g.NAME}
    world.c.replay=None;assert g.find_goods_received(world.ctx,"missing") is None


def migration():
    path=Path(g.__file__).parent/"migrations/versions/stock_0003_goods_received.py"
    spec=importlib.util.spec_from_file_location("gr_migration_unit",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_header_frozen_ddl_company_constraints_and_metadata():
    rev=migration();normal=lambda text:re.sub(r"\s+"," ",text.strip())
    assert normal(rev.DDL)==normal(str(CreateTable(t.goods_received).compile(dialect=postgresql.dialect())))
    assert (rev.revision,rev.down_revision,rev.creates)==("stock_0003","stock_0002",("stock_goods_received",))
    assert t.goods_received.info["owner"]=="stock" and t.goods_received.info["runtime"].insert
    assert not t.goods_received.info["runtime"].update and not t.goods_received.info["runtime"].delete
    assert {tuple(x.parent.name for x in fk.elements) for fk in t.goods_received.foreign_key_constraints}=={
        ("company_id","voucher_id"),("company_id","supplier_id"),("company_id","account_id")}
    uniques={tuple(c.name for c in u.columns) for u in t.goods_received.constraints if u.__class__.__name__=="UniqueConstraint"}
    assert uniques=={("company_id","id"),("company_id","voucher_id"),("company_id","number")}
    assert "stock_goods_received" in MANIFEST.owned_tables and g.VOUCHER in MANIFEST.voucher_types
    assert MANIFEST.version=="0.1.3"


def test_migration_uses_only_recorded_ddl_and_schema_qualified_append_guards(monkeypatch):
    rev=migration();statements=[]
    bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),execute=lambda q:Rows(['"book"']))
    monkeypatch.setattr(rev.op,"get_bind",lambda:bind);monkeypatch.setattr(rev.op,"execute",statements.append)
    rev.upgrade()
    assert statements[0]==rev.DDL and len(statements)==3
    assert all('ON "book".stock_goods_received' in q and 'FUNCTION "book".erpclaw_stock_refuse()' in q for q in statements[1:])
    assert "BEFORE UPDATE OR DELETE" in statements[1] and "BEFORE TRUNCATE" in statements[2]
    bind.dialect.name="sqlite"
    with pytest.raises(RuntimeError):rev.upgrade()
    with pytest.raises(NotImplementedError):rev.downgrade()


def test_no_public_contract_input_or_registry_operation():
    assert not hasattr(g,"CONTRACT") and not hasattr(g,"Input") and not hasattr(g,"Trusted")
    assert dataclasses.is_dataclass(g.GoodsReceived) and not hasattr(g.GoodsReceived,"model_json_schema")
    assert g.NAME not in {x.name for x in PLUGIN.contracts} and g.NAME not in MANIFEST.contracts
    assert g.NAME not in REGISTRY.contract_plugin


@pytest.mark.parametrize("settlement",["paid_now","still_owed"])
def test_real_whole_posting_and_source_guard_accept_receipt_but_refuse_wrong_source(world,monkeypatch,settlement):
    monkeypatch.setattr(ledger,"check_post",REAL_CHECK_POST)
    calls=[]
    original=payables.check_payable_source
    def guard(ctx,kind,posting):
        calls.append(kind)
        return original(ctx,kind,posting)
    monkeypatch.setattr(payables,"check_payable_source",guard)
    plan=g.plan_goods_received(world.ctx,request(settlement))
    assert calls==[g.VOUCHER]
    assert sum(x.debit for x in plan.posting)==sum(x.credit for x in plan.posting)==D(725)
    if settlement=="still_owed":
        with pytest.raises(Refusal) as exc:REAL_CHECK_POST(world.ctx,DAY,"journal",None,plan.posting)
        assert exc.value.code=="payable_source_not_allowed"
        bad=[*plan.posting[:-1],ledger.Line("ap",credit=D(725))]
        with pytest.raises(Refusal) as exc:REAL_CHECK_POST(world.ctx,DAY,g.VOUCHER,None,bad)
        assert exc.value.code=="payable_needs_party"
    assert world.c.writes==[]


@pytest.mark.parametrize("method",["fifo","moving_average"])
def test_real_stock_entry_and_fifo_head_writers_preserve_repeated_receipt_sequence(world,monkeypatch,method):
    # Recording storage adapter, not a database. Actual receipt, stock entry,
    # FIFO row writer, head writer and FIFO read arithmetic all execute.
    c=world.c;c.stock["method"]=method
    req=request(lines=(g.ReceiptLine("item",100,"7.25"),g.ReceiptLine("item",40,"7.25"),g.ReceiptLine("item",10,"7.50")))
    c.read_set=g.goods_received_read_set(world.ctx,req);c.events.clear()
    saved=c.execute;entry_rows=[];layer_rows=[]
    def execute(stmt):
        if isinstance(stmt,(Insert,Update)) and stmt.table.name in {"stock_ledger_entry","stock_layer","stock_head"}:
            c.writes.append(stmt);values=stmt.compile().params
            if stmt.table.name=="stock_ledger_entry":entry_rows.append(values)
            elif stmt.table.name=="stock_layer":layer_rows.append(values)
            elif isinstance(stmt,Insert):
                c.heads[(values["item_id"],values["warehouse_id"])]=dict(values,qty=D(values["qty"]))
            else:
                head=next(h for h in c.heads.values() if h["id"]==values["id_1"])
                head.update({k:v for k,v in values.items() if k!="id_1"});head["qty"]=D(head["qty"])
            return Rows()
        if not isinstance(stmt,(Insert,Update)):
            table=next(iter(stmt.selected_columns)).table.name
            if table=="stock_layer":
                c.queries.append(stmt)
                return Rows([dict(row,qty=D(row["qty"]),seq=next(x["seq"] for x in entry_rows if x["id"]==row["entry_id"])) for row in layer_rows])
            if table=="stock_layer_take":c.queries.append(stmt);return Rows()
        return saved(stmt)
    monkeypatch.setattr(c,"execute",execute)
    monkeypatch.setattr(s,"fifo_groups",REAL_FIFO_GROUPS)
    monkeypatch.setattr(g,"next_number",lambda *a:"GR-2026-00001")
    monkeypatch.setattr(ledger,"post",lambda *a,**kw:None)
    g.commit_goods_received(world.ctx,req,"operation")
    assert [row["line_no"] for row in entry_rows]==[1,2,3]
    assert [row["seq"] for row in entry_rows]==[1,2,3]
    assert [row["qty_after"] for row in entry_rows]==["100","140","150"]
    assert [row["value_after"] for row in entry_rows]==[D(725),D(1015),D(1090)]
    assert all(row["voucher_type"]==g.VOUCHER for row in entry_rows)
    assert c.heads[("item","wh")]["qty"]==D(150) and c.heads[("item","wh")]["value"]==D(1090)
    if method=="fifo":
        assert len(layer_rows)==3
        assert layer_rows[0]["group_id"]==layer_rows[1]["group_id"]!=layer_rows[2]["group_id"]
        groups=REAL_FIFO_GROUPS(world.ctx,"item","wh")
        assert [(x.qty,x.value) for x in groups]==[(D(140),D(1015)),(D(10),D(75))]
    else:assert layer_rows==[]
