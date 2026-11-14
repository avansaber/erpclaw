"""Pure cancellation orchestration over explicit in-memory read/write boundaries.
Real stock reversal, ledger mirror/preflight, authority/freshness and card code;
no database, schema, transaction/concurrency or runtime qualification.
"""
import copy
import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from approval_recording import RecordedApproval
from pydantic import ValidationError
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList, Null
from sqlalchemy.sql.dml import Insert, Update, Delete

from erpclaw_core import approvals, ledger
from erpclaw_core.common import Ctx, Refusal
from erpclaw_core.contracts import check_answer, check_card_amounts
from erpclaw_core.plugins import REGISTRY
from erpclaw_stock import MANIFEST, PLUGIN, cancellation as o, service as s, tables as t
from erpclaw_stock.valuation import Group

D = Decimal
DAY = dt.date(2026, 5, 31)
TODAY = dt.date(2026, 11, 4)


def inputs(document="SW-2026-00001", when=DAY):
    return o.CancelStockMovement(document=document, date=when)


class Rows:
    def __init__(self, rows, *, mapping=False): self.rows, self.mapping = rows, mapping
    def mappings(self): return Rows(self.rows, mapping=True)
    def __iter__(self): return iter(self.all())
    def all(self): return [dict(r) if self.mapping else SimpleNamespace(**r) for r in self.rows]
    def first(self): return self.all()[0] if self.rows else None
    def one(self):
        assert len(self.rows) == 1
        return self.first()
    def one_or_none(self):
        assert len(self.rows) <= 1
        return self.first()
    def scalars(self): return [next(iter(r.values())) for r in self.rows]
    def scalar(self): return self.scalars()[0] if self.rows else None
    def scalar_one(self):
        assert len(self.rows) == 1
        return self.scalar()
    def scalar_one_or_none(self):
        assert len(self.rows) <= 1
        return self.scalar()


def value(expr, row):
    if isinstance(expr, BindParameter): return expr.value
    if isinstance(expr, Null): return None
    return row[expr.name]


def matches(expr, row):
    if isinstance(expr, BooleanClauseList):
        assert expr.operator is operators.and_
        return all(matches(x, row) for x in expr.clauses)
    a, b = value(expr.left, row), value(expr.right, row)
    op = expr.operator
    if op is operators.in_op: return a in b
    if op is operators.not_in_op: return a not in b
    if op is operators.is_: return a is b
    if op is operators.is_not: return a is not b
    assert op in (operators.eq, operators.ne, operators.lt, operators.le, operators.gt, operators.ge), op
    return op(a, b)


class MemoryBoundary:
    """Only the finite SQLAlchemy row operations used by these pure units.
    This is a test read adapter, not an SQL engine or a transaction simulator.
    """
    def __init__(self):
        self.tables, self.events, self.queries, self.inserts = {}, [], [], []
        self.replay = None
    def execute(self, query):
        self.queries.append(query)
        if isinstance(query, Delete): raise AssertionError("No original row may be deleted")
        if isinstance(query, Insert):
            params = query.compile().params
            self.events.append("insert:" + query.table.name)
            self.inserts.append((query.table.name, copy.deepcopy(params)))
            self.tables.setdefault(query.table.name, []).append(dict(params))
            return Rows([])
        if isinstance(query, Update):
            assert query.table.name == "stock_head", "Only existing head mutation is allowed"
            self.events.append("update:stock_head")
            for row in self.tables[query.table.name]:
                if all(matches(c, row) for c in query._where_criteria): row.update(query.compile().params | {})
            return Rows([])
        if "JOIN approval" in str(query):
            self.replay_query = query
            return Rows([] if self.replay is None else [{"result_json": self.replay}])
        tables = query.get_final_froms()
        assert len(tables) == 1 and hasattr(tables[0], "name"), str(query)
        name = tables[0].name
        assert name in self.tables, "Unexpected read table " + name
        rows = [dict(row) for row in self.tables[name] if all(matches(c, row) for c in query._where_criteria)]
        for order in reversed(query._order_by_clauses): rows.sort(key=lambda row: row[order.name])
        if query._limit_clause is not None: rows = rows[:query._limit_clause.value]
        cols = list(query.selected_columns)
        return Rows([{col.name: row.get(col.name) for col in cols} for row in rows])


def entry(eid="entry", item="wire", direction="out", qty="4", amount="8.00", seq=3, line=1):
    return dict(id=eid, company_id="co", item_id=item, warehouse_id="wh", currency="USD", posting_date=DAY,
                seq=seq, direction=direction, qty=D(qty), value=D(amount), qty_after=D(6), value_after=D(12),
                voucher_type="stock_write_off", voucher_id="voucher", line_no=line,
                against_entry_id=None, reverses_entry_id=None, created_at=None)


def account(aid, number, name, role):
    return dict(id=aid, company_id="co", number=number, name=name, account_type=role, is_group=False, frozen=False)


@pytest.fixture
def world(monkeypatch):
    db = MemoryBoundary()
    voucher = dict(id="voucher",company_id="co",kind="write_off",number="SW-2026-00001",posting_date=DAY,
                   reason="damage",created_at=None)
    posting = dict(id="posting",company_id="co",seq=7,posting_date=DAY,voucher_type="stock_write_off",
                   voucher_id="voucher",reverses_posting_id=None,ledger="general")
    db.tables = {
        "stock_voucher": [voucher], "stock_voucher_line": [], "stock_ledger_entry": [entry()],
        "posting": [posting],
        "ledger_entry": [dict(id="le1",company_id="co",posting_id="posting",line_no=1,account_id="writeoff",debit=D(8),credit=D(0),party_id=None),
                         dict(id="le2",company_id="co",posting_id="posting",line_no=2,account_id="inv",debit=D(0),credit=D(8),party_id=None)],
        "account": [account("inv","1300","Inventory","stock"),account("writeoff","5210","Original write-off","stock_write_off"),
                    account("opening","3100","Original opening balance","opening_balance"),account("adjust","5200","Original adjustment","stock_adjustment")],
        "item": [dict(id="wire",company_id="co",name="Copper Wire",unit="Meter",kind="stock")],
        "stock_item": [dict(id="stock-wire",company_id="co",item_id="wire",method="moving_average",default_warehouse_id="wh")],
        "stock_warehouse": [dict(id="wh",company_id="co",name="Main Warehouse",account_id="inv",position=1)],
        "stock_head": [dict(id="head-wire",company_id="co",item_id="wire",warehouse_id="wh",currency="USD",last_seq=3,
                            qty=D(6),value=D(12),last_date=DAY)],
        "stock_layer": [], "stock_layer_take": [], "party": [], "account_map": [], "fiscal_year": [],
        "fiscal_period": [dict(id="period",company_id="co",fiscal_year_id="fy",start_date=dt.date(2026,1,1),
                               end_date=dt.date(2026,12,31),status="open")],
        "approval": [dict(id="approval",company_id="co",status="consumed",contract=o.NAME,read_set_json="[]")],
        "operation_outcome": [],
    }
    ctx = Ctx(db,"co","Test Books","USD",TODAY,extra={"commit":"approval"})
    groups = {"wire": [Group("g",D(6),D(12))]}
    monkeypatch.setattr(s,"fifo_groups",lambda ctx,iid,wid: copy.deepcopy(groups[iid]))
    monkeypatch.setattr(o.integration,"system_of_record",lambda ctx,area:("native",None))
    def unmapped(*args): raise AssertionError("Unexpected external mapping")
    monkeypatch.setattr(o.integration,"account_codes",unmapped)
    monkeypatch.setattr(REGISTRY,"contract_plugin",{o.NAME:"stock"})
    monkeypatch.setattr(approvals,"_row_digest",lambda *args:"digest")
    # Real ledger.reverse and post/check_post still run; the append/hash-chain
    # storage boundary is explicit and does not claim real transaction coverage.
    posted = []
    def post_storage(ctx,when,vtype,vid,lines,**kw):
        db.events.append("ledger:append")
        assert kw["reverses"] == "posting" and kw["closing"] is None
        posted.append((when,vtype,vid,copy.deepcopy(lines),kw))
        db.tables["posting"].append(dict(id="mirror",company_id="co",seq=8,posting_date=when,voucher_type=vtype,
                                         voucher_id=vid,reverses_posting_id="posting",ledger="general"))
        for n,line in enumerate(lines,1):
            db.tables["ledger_entry"].append(dict(id="mirror-"+str(n),company_id="co",posting_id="mirror",line_no=n,
                account_id=line.account_id,debit=line.debit,credit=line.credit,party_id=line.party_id))
        return SimpleNamespace(id="mirror")
    monkeypatch.setattr(ledger,"_post",post_storage)
    def no_role(*args): raise AssertionError("Cancellation never resolves a current counter/COGS role")
    monkeypatch.setattr(ledger,"account_by_type",no_role)
    monkeypatch.setattr(s,"role_account",no_role)
    monkeypatch.setattr(s,"_contribution",no_role)
    authority = RecordedApproval(ctx, lambda: db.tables["approval"][0] if db.tables["approval"] else None, lambda: next(iter(db.tables["operation_outcome"]), None))
    authority.wrap(monkeypatch, o, "commit_cancellation")
    return SimpleNamespace(ctx=ctx,authority=authority,db=db,voucher=voucher,posting=posting,groups=groups,posted=posted)


def approve(world, inp=None):
    inp = inp or inputs()
    rows = o.cancellation_read_set(world.ctx, inp)
    world.db.tables["approval"][0]["read_set_json"] = json.dumps([[t,k,"digest"] for t,k in rows])
    world.db.events.clear()
    return inp


def original_facts(world):
    return copy.deepcopy({name:world.db.tables[name] for name in ["stock_voucher","stock_voucher_line"]})


def k8(world):
    world.voucher.update(kind="count",number="SC-2026-00001")
    world.posting["voucher_type"]="stock_count"
    entries=[entry("bolt-entry","bolt","in","2","10",line=1),entry("fuse-entry","fuse","out","5","10",line=2)]
    for e in entries:e["voucher_type"]="stock_count"
    world.db.tables["stock_ledger_entry"]=entries
    world.db.tables["item"]=[dict(id="bolt",company_id="co",name="Hex Bolt",unit="Each",kind="stock"),
                              dict(id="fuse",company_id="co",name="Fuse",unit="Each",kind="stock")]
    world.db.tables["stock_item"]=[dict(id="stock-"+i,company_id="co",item_id=i,method=m,default_warehouse_id="wh")
                                   for i,m in [("bolt","moving_average"),("fuse","fifo")]]
    world.db.tables["stock_head"]=[dict(id="head-"+i,company_id="co",item_id=i,warehouse_id="wh",currency="USD",
        last_seq=3,qty=D(q),value=D(v),last_date=DAY) for i,q,v in [("bolt",102,510),("fuse",45,90)]]
    world.groups["fuse"]=[Group("fuse-group",D(45),D(90))]
    world.db.tables["stock_layer_take"]=[dict(id="take",company_id="co",entry_id="fuse-entry",group_id="fuse-group",
        qty=D(5),value=D(10),kind="take",layer_id=None,currency="USD")]
    gross=[("inv",10,0),("adjust",0,10),("adjust",10,0),("inv",0,10)]
    world.db.tables["ledger_entry"]=[dict(id="le"+str(n),company_id="co",posting_id="posting",line_no=n,
        account_id=a,debit=D(d),credit=D(c),party_id=None) for n,(a,d,c) in enumerate(gross,1)]
    return inputs("SC-2026-00001")


def test_date_is_required_and_not_defaulted():
    with pytest.raises(ValidationError):o.CancelStockMovement(document="SW-2026-00001")
    with pytest.raises(ValidationError):inputs("")


@pytest.mark.parametrize("kind,vtype,prefix",[("opening","stock_opening","OS"),("count","stock_count","SC"),("write_off","stock_write_off","SW")])
def test_kind_controls_identity_not_prefix(world,kind,vtype,prefix):
    world.voucher.update(kind=kind,number="OS-2026-00999")
    world.posting["voucher_type"]=vtype
    world.db.tables["stock_ledger_entry"][0]["voucher_type"]=vtype
    plan=o.plan_cancellation(world.ctx,inputs("OS-2026-00999"))
    assert plan.voucher_type==vtype
    assert [ln.account_id for ln in plan.lines]==["writeoff","inv"]
    assert world.db.events==[]


@pytest.mark.parametrize("change",["unknown","foreign","case"])
def test_exact_company_owned_number_only(world,change):
    inp=inputs("SW-2026-99999" if change=="unknown" else "sw-2026-00001" if change=="case" else "SW-2026-00001")
    if change=="foreign":world.voucher["company_id"]="other"
    with pytest.raises(Refusal,match="no stock document") as exc:o.plan_cancellation(world.ctx,inp)
    assert exc.value.code=="no_stock_document" and world.db.events==[]


@pytest.mark.parametrize("kind,code",[("goods_received","goods_received_reversal_required"),("transfer","stock_cancel_kind")])
def test_excluded_kinds_never_reach_generic_reverse(world,kind,code,monkeypatch):
    world.voucher["kind"]=kind
    monkeypatch.setattr(s,"_reverse_moves",lambda *a,**k:pytest.fail("No excluded-kind reversal"))
    with pytest.raises(Refusal) as exc:o.plan_cancellation(world.ctx,inputs())
    assert exc.value.code==code and world.db.events==[]


def test_writeoff_mirrors_original_accounts_not_current_roles(world):
    # The original role can change; the immutable posting still determines it.
    world.db.tables["account"][1]["account_type"]="expense"
    plan=o.plan_cancellation(world.ctx,inputs())
    assert [(x.account_id,x.debit,x.credit) for x in plan.lines]==[("writeoff",D(0),D(8)),("inv",D(8),D(0))]
    mv=plan.moves[0]
    assert (mv.direction,mv.qty,mv.value,mv.qty_after,mv.value_after,mv.reverses)==("in",D(4),D(8),D(10),D(20),"entry")
    assert mv.original==world.db.tables["stock_ledger_entry"][0]


def test_opening_fifo_removes_its_original_layer(world):
    world.voucher.update(kind="opening",number="OS-2026-00001")
    world.posting["voucher_type"]="stock_opening"
    e=world.db.tables["stock_ledger_entry"][0];e.update(direction="in",voucher_type="stock_opening")
    world.db.tables["stock_item"][0]["method"]="fifo"
    world.db.tables["stock_head"][0].update(qty=D(4),value=D(8))
    world.groups["wire"]=[Group("g",D(4),D(8))]
    world.db.tables["stock_layer"]=[dict(id="layer",entry_id="entry",group_id="g",qty=D(4),value=D(8))]
    mv=o.plan_cancellation(world.ctx,inputs("OS-2026-00001")).moves[0]
    assert (mv.direction,mv.qty_after,mv.value_after,mv.take_layer,mv.takes)==("out",D(0),D(0),"layer",[])
    assert [(g.qty,g.value) for g in mv.pair.groups]==[(D(0),D(0))]


def test_k8_four_gross_lines_fifo_giveback_and_actual_card(world):
    inp=k8(world);plan=o.plan_cancellation(world.ctx,inp)
    assert [(x.account_id,x.debit,x.credit) for x in plan.lines]==[("inv",D(0),D(10)),("adjust",D(10),D(0)),
        ("adjust",D(0),D(10)),("inv",D(10),D(0))]
    assert [(mv.qty_after,mv.value_after) for mv in plan.moves]==[(D(100),D(500)),(D(50),D(100))]
    assert plan.moves[1].give_backs==[("fuse-group",D(5),D(10))]
    assert [(g.qty,g.value) for g in plan.moves[1].pair.groups]==[(D(50),D(100))]
    card=approvals._preview(world.ctx,o.CONTRACT,inp)
    assert len(card["books"])==4
    check_card_amounts(card)


@pytest.mark.parametrize("offset",[False,True])
def test_valued_missing_posting_refuses_even_offsetting_net(world,offset):
    inp=k8(world) if offset else inputs()
    world.db.tables["posting"].clear()
    with pytest.raises(Refusal) as exc:o.plan_cancellation(world.ctx,inp)
    assert exc.value.code=="stock_posting_missing" and world.db.events==[]


def test_true_zero_cost_no_posting_still_reverses_stock(world,monkeypatch):
    world.db.tables["posting"].clear();world.db.tables["ledger_entry"].clear()
    world.db.tables["stock_ledger_entry"][0]["value"]=D(0)
    world.db.tables["stock_head"][0]["value"]=D(0)
    monkeypatch.setattr(ledger,"reverse",lambda *a:pytest.fail("No empty posting"))
    plan=o.plan_cancellation(world.ctx,inputs());assert not plan.lines
    card=approvals._preview(world.ctx,o.CONTRACT,inputs());assert dict(card["lines"])["Accounting destination"]=="No ledger posting"
    inp=approve(world);o.commit_cancellation(world.ctx,inp,"key")
    assert len(world.db.tables["stock_ledger_entry"])==2 and not world.posted
    for check in world.ctx.checks.values():check(world.ctx)


@pytest.mark.parametrize("case,code",[("stock_repeated","already_reversed"),("posting_repeated","already_reversed"),
    ("later","not_latest"),("noncontiguous","not_latest"),("empty","nothing_to_reverse")])
def test_original_reversal_refusals_before_effects(world,case,code):
    if case=="stock_repeated":
        extra=entry("mirror");extra.update(voucher_type="stock_write_off:reversal",reverses_entry_id="entry")
        world.db.tables["stock_ledger_entry"].append(extra)
    if case=="posting_repeated":world.db.tables["posting"].append(dict(world.posting,id="mirror",voucher_type="stock_write_off:reversal",reverses_posting_id="posting"))
    if case=="later":world.db.tables["stock_head"][0]["last_seq"]=4
    if case=="noncontiguous":world.db.tables["stock_ledger_entry"].append(entry("earlier",seq=1,line=2))
    if case=="empty":world.db.tables["stock_ledger_entry"].clear()
    with pytest.raises(Refusal) as exc:o.plan_cancellation(world.ctx,inputs())
    assert exc.value.code==code and world.db.events==[]
    if code=="not_latest":assert "new count" in exc.value.message and "credit note" not in exc.value.message and "points_to" not in exc.value.data


def test_each_pair_must_be_latest(world):
    inp=k8(world);world.db.tables["stock_head"][1]["last_seq"]=4
    with pytest.raises(Refusal) as exc:o.plan_cancellation(world.ctx,inp)
    assert exc.value.code=="not_latest" and exc.value.data["item"]=="fuse"


def test_contiguous_same_pair_is_reversed_in_descending_order(world):
    world.db.tables["stock_ledger_entry"]=[entry("first",qty="2",amount="4",seq=2,line=1),entry("last",qty="2",amount="4",seq=3,line=2)]
    moves=o.plan_cancellation(world.ctx,inputs()).moves
    assert [mv.reverses for mv in moves]==["last","first"]
    assert [mv.qty_after for mv in moves]==[D(8),D(10)]


@pytest.mark.parametrize("case,code",[("before_original","reversal_before_original"),("before_latest","backdated"),
                                    ("closed","period_closed"),("outside_period","no_period")])
def test_owner_date_and_actual_period_rules(world,case,code):
    when=DAY
    if case=="before_original":when=DAY-dt.timedelta(days=1)
    if case=="before_latest":world.db.tables["stock_head"][0]["last_date"]=DAY+dt.timedelta(days=1)
    if case=="closed":world.db.tables["fiscal_period"][0]["status"]="locked"
    if case=="outside_period":when=dt.date(2027,1,1)
    with pytest.raises(Refusal) as exc:o.plan_cancellation(world.ctx,inputs(when=when))
    assert exc.value.code==code and not world.db.events


@pytest.mark.parametrize("case,code",[("frozen","frozen_account"),("group","group_account"),("foreign","foreign_account"),("unbalanced","unbalanced")])
def test_real_ledger_preflight_refuses_before_stock_writes(world,case,code):
    if case=="frozen":world.db.tables["account"][1]["frozen"]=True
    if case=="group":world.db.tables["account"][1]["is_group"]=True
    if case=="foreign":world.db.tables["account"][1]["company_id"]="other"
    if case=="unbalanced":world.db.tables["ledger_entry"][0]["debit"]=D(9)
    with pytest.raises(Refusal) as exc:o.commit_cancellation(world.ctx,inputs(),"key")
    assert exc.value.code==code and not world.db.events


@pytest.mark.parametrize("case",["no_flag","pending","wrong_owner","outcome"])
def test_actual_authority_refuses_before_effects(world,case):
    if case=="no_flag":world.authority.enabled = False
    if case=="pending":world.db.tables["approval"][0]["status"]="pending"
    if case=="wrong_owner":world.db.tables["approval"][0]["contract"]="other"
    if case=="outcome":world.db.tables["operation_outcome"].append(dict(approval_id="approval",key="old"))
    with pytest.raises(Refusal) as exc:o.commit_cancellation(world.ctx,inputs(),"key")
    assert exc.value.code=="not_approved" and not world.db.events


@pytest.mark.parametrize("row",[("stock_head","head-wire"),("stock_ledger_entry","entry"),("posting","posting"),("account","writeoff")])
def test_actual_readset_freshness_before_any_write(world,row,monkeypatch):
    inp=approve(world)
    monkeypatch.setattr(approvals,"_row_digest",lambda conn,t,k:"changed" if (t,k)==row else "digest")
    with pytest.raises(Refusal) as exc:o.commit_cancellation(world.ctx,inp,"key")
    assert exc.value.code=="stale" and not world.db.events


def test_k8_commit_real_entry_writer_and_ledger_reverse_preserve_originals(world):
    inp=k8(world);before=original_facts(world);original=copy.deepcopy(world.db.tables["stock_ledger_entry"])
    approve(world,inp);result=o.commit_cancellation(world.ctx,inp,"key")
    assert original_facts(world)==before and world.db.tables["stock_ledger_entry"][:2]==original
    assert [(e["voucher_type"],e["voucher_id"],e["reverses_entry_id"]) for e in world.db.tables["stock_ledger_entry"][2:]]==[
        ("stock_count:reversal","voucher","bolt-entry"),("stock_count:reversal","voucher","fuse-entry")]
    assert len(world.posted)==1 and len(world.posted[0][3])==4
    assert world.posted[0][1:3]==("stock_count:reversal","voucher")
    assert result.data["number"]=="SC-2026-00001"
    assert all(name not in {"stock_voucher","stock_voucher_line"} for name,_ in world.db.inserts)
    check_answer(o.CONTRACT,result)
    for check in world.ctx.checks.values():check(world.ctx)
    givebacks=[r for r in world.db.tables["stock_layer_take"] if r["kind"]=="give_back"]
    assert len(givebacks)==1 and givebacks[0]["qty"]=="5" and givebacks[0]["value"]==D(10)


def test_additional_exact_mirror_comparison_catches_wrong_counter(world):
    inp=approve(world);o.commit_cancellation(world.ctx,inp,"key")
    mirrored=[r for r in world.db.tables["ledger_entry"] if r["posting_id"]=="mirror"]
    mirrored[0]["account_id"]="adjust"
    with pytest.raises(Refusal) as exc:
        for check in world.ctx.checks.values():check(world.ctx)
    assert exc.value.code=="stock_posting_mismatch"


def test_real_card_binds_mode_target_and_external_codes(world,monkeypatch):
    inp=inputs();native=approvals._preview(world.ctx,o.CONTRACT,inp)
    mode=["external","ERP A"];codes={"inv":"INV-A","writeoff":"EXP-A"}
    monkeypatch.setattr(o.integration,"system_of_record",lambda ctx,area:tuple(mode))
    monkeypatch.setattr(o.integration,"account_codes",lambda ctx,external,ids:{i:codes[i] for i in ids})
    world.db.tables["account_map"]=[dict(id="map-"+aid,company_id="co",external_system="ERP A",account_id=aid) for aid in codes]
    external=approvals._preview(world.ctx,o.CONTRACT,inp)
    assert external!=native and "INV-A" in str(external)
    assert {("account_map","map-inv"),("account_map","map-writeoff")}<=set(o.cancellation_read_set(world.ctx,inp))
    codes["inv"]="INV-B";changed=approvals._preview(world.ctx,o.CONTRACT,inp);assert changed!=external
    mode[1]="ERP B";assert approvals._preview(world.ctx,o.CONTRACT,inp)!=changed
    check_card_amounts(external)


def test_unmapped_external_preflight_stops_before_effects(world,monkeypatch):
    monkeypatch.setattr(o.integration,"system_of_record",lambda *a:("external","ERP"))
    def missing(*a):raise Refusal("Map Inventory first.","unmapped_account")
    monkeypatch.setattr(o.integration,"account_codes",missing)
    with pytest.raises(Refusal) as exc:o.commit_cancellation(world.ctx,inputs(),"key")
    assert exc.value.code=="unmapped_account" and not world.db.events


def test_replay_is_company_key_and_actual_contract_scoped(world):
    assert o.find_cancellation(world.ctx,"key") is None
    world.db.replay=json.dumps(dict(text="Cancelled SW-2026-00001.",data={"number":"SW-2026-00001"},figures=[],amounts=[]))
    out=o.find_cancellation(world.ctx,"key");assert out.data["number"]=="SW-2026-00001"
    params=world.db.replay_query.compile().params
    assert set(params.values())=={"co","key","committed",o.NAME}


def test_planned_contract_registered_without_new_voucher_namespace(as_released):
    assert o.CONTRACT in PLUGIN.contracts and MANIFEST.contracts[o.NAME]=="write"
    assert MANIFEST.voucher_types==["stock_receipt","stock_opening","stock_write_off","stock_count","stock_goods_received"]
    assert o.CONTRACT.planned=="Core"
    with pytest.raises(Refusal) as exc:approvals._write_contract(o.CONTRACT)
    assert exc.value.code=="planned" and exc.value.capability=="planned.op.cancel_stock_movement"



def test_every_readset_row_uses_registered_single_key_table(world):
    from erpclaw_core.db import metadata
    rows=o.cancellation_read_set(world.ctx,inputs())
    assert (t.entry.name,"entry") in rows
    assert all(name in metadata.tables and len(metadata.tables[name].primary_key.columns)==1 for name,_ in rows)
