"""Pure physical stock counts: real stock arithmetic/planner/authority,
explicit in-memory read boundaries, no conftest/database/network qualification.
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
from erpclaw_stock import MANIFEST, PLUGIN, count_stock as o, service as s, tables as t
from erpclaw_stock.valuation import Group

D = Decimal
TODAY = dt.date(2026, 11, 4)


def row(item="Copper Wire", counted="184", warehouse="Main Warehouse", **extra):
    return dict(item=item, counted=counted, warehouse=warehouse, **extra)


def inputs(*rows, **extra):
    return o.RecordStockCount.model_validate(dict(lines=list(rows) or [row()], **extra))


@pytest.mark.parametrize("bad", [True, 1.5, "1e2", "NaN", "Infinity", "1.00001", "100000000000000"])
def test_invalid_count_never_coerced(bad):
    with pytest.raises((Refusal, ValidationError)): inputs(row(counted=bad))


@pytest.mark.parametrize("negative", ["-1", -1, D("-0.0001")])
def test_negative_count_has_original_refusal(negative):
    with pytest.raises(Refusal) as exc: inputs(row(counted=negative))
    assert exc.value.code == "count_negative" and exc.value.message == "A count can't be below zero."


@pytest.mark.parametrize("zero", [0, "0", "0.0000", D("0.000")])
def test_zero_physical_count_is_valid(zero):
    assert inputs(row(counted=zero)).lines[0].counted == D(0)


def test_optional_rate_has_no_default_and_preserves_exact_domain():
    assert inputs().lines[0].rate is None
    with pytest.raises(Refusal): inputs(row(rate=0))
    assert inputs(row(rate="7.4000")).lines[0].rate == D("7.4")


class ReadRows:
    def __init__(self, first=None, scalar=None): self.first_value, self.scalar = first, scalar
    def first(self): return self.first_value
    def scalar_one(self): return self.scalar
    def scalar_one_or_none(self): return self.scalar
    def scalars(self): return iter(self.scalar or [])


class Boundaries:
    def __init__(self):
        self.events, self.inserts, self.read_set, self.mapping_queries = [], [], [], []
        self.status, self.contract, self.outcome = "consumed", o.NAME, None
    def execute(self, stmt):
        if isinstance(stmt, Insert):
            self.events.append("insert:" + stmt.table.name)
            self.inserts.append((stmt.table.name, stmt.compile().params))
            return ReadRows()
        sql = str(stmt)
        if "approval.contract" in sql: return ReadRows(SimpleNamespace(status=self.status, contract=self.contract))
        if "operation_outcome.key" in sql: return ReadRows(self.outcome)
        if "approval.read_set_json" in sql:
            return ReadRows(scalar=json.dumps([[table, key, "digest"] for table, key in self.read_set]))
        if "account_map.id" in sql:
            self.mapping_queries.append(stmt)
            return ReadRows(scalar=["map-inv", "map-adjust"])
        raise AssertionError("Unexpected SQL boundary: " + sql)


@pytest.fixture
def world(monkeypatch):
    conn = Boundaries()
    ctx = SimpleNamespace(conn=conn, company_id="co", company_name="Test Books", currency="USD", today=TODAY,
                          extra={"commit": "approval"})
    items = {"wire": dict(id="wire", name="Copper Wire", unit="Meter", kind="stock", standard_rate=D("99")),
             "bolt": dict(id="bolt", name="Hex Bolt", unit="Each", kind="stock", standard_rate=D("999")),
             "fuse": dict(id="fuse", name="Fuse", unit="Each", kind="stock", standard_rate=D("999"))}
    whs = [dict(id="wh", name="Main Warehouse", account_id="inv", is_default=True),
           dict(id="shop", name="Shop Warehouse", account_id="inv", is_default=False)]
    stocks = {key: dict(id="stock-"+key, item_id=key, method="moving_average", default_warehouse_id="wh") for key in items}
    stocks["fuse"]["method"] = "fifo"
    states = {
        ("wire", "wh"): dict(qty=D(180), value=D("1995.00"), head="head-wire", last_date=dt.date(2026,1,1),
            groups=[Group("g1",D(100),D("1000.00")),Group("g2",D(50),D("650.00")),Group("g3",D(30),D("345.00"))]),
        ("bolt", "wh"): dict(qty=D(100), value=D("500.00"), head="head-bolt", last_date=dt.date(2026,1,1), groups=[]),
        ("fuse", "wh"): dict(qty=D(50), value=D("100.00"), head="head-fuse", last_date=dt.date(2026,1,1),
            groups=[Group("f1",D(50),D("100.00"))])}
    def state(iid, wid): return states.get((iid,wid), dict(qty=D(0),value=D(0),head=None,last_date=dt.date(2026,1,1),groups=[]))
    inventory = dict(id="inv",number="1300",name="Inventory",frozen=False)
    adjustment = dict(id="adjust",number="5200",name="Stock Adjustment",frozen=False)
    period = dict(id="period",fiscal_year_id="fy",status="open")
    monkeypatch.setattr(masters,"find_item",lambda ctx,said: masters._match(list(items.values()),said,"item",plural=True))
    monkeypatch.setattr(s,"stock_item",lambda ctx,iid: dict(stocks[iid]))
    monkeypatch.setattr(s,"warehouses",lambda ctx: whs)
    monkeypatch.setattr(s,"warehouse",lambda ctx,wid: copy.deepcopy(next(x for x in whs if x["id"]==wid)))
    monkeypatch.setattr(s,"on_hand",lambda ctx,iid,wid: (state(iid,wid)["qty"],state(iid,wid)["value"]))
    monkeypatch.setattr(ledger,"open_period",lambda ctx,when:dict(period))
    def pair(ctx,pairs,iid,wid,when,*,lock,dated=True):
        st=state(iid,wid)
        head=dict(id=st["head"],qty=st["qty"],value=st["value"],last_date=st["last_date"],last_seq=3) if st["head"] else None
        p=s.Pair(copy.deepcopy(stocks[iid]),copy.deepcopy(next(x for x in whs if x["id"]==wid)),
                 "inv",head,st["qty"],st["value"],copy.deepcopy(st["groups"]))
        if dated:s.check_date(ctx,when,head)
        return p
    monkeypatch.setattr(s,"_pair",pair)
    monkeypatch.setattr(s,"_inventory_account_row",lambda *a:dict(inventory))
    def role(ctx,name):
        assert name=="stock_adjustment"
        return dict(adjustment)
    monkeypatch.setattr(ledger,"account_by_type",role)
    def check(ctx,when,kind,vid,lines):
        assert kind=="stock_count" and lines
        conn.events.append("preflight")
        return lines
    monkeypatch.setattr(ledger,"check_post",check)
    monkeypatch.setattr(o.integration,"system_of_record",lambda ctx,area:("native",None))
    def no_map(*args):raise AssertionError("No mapping expected")
    monkeypatch.setattr(o.integration,"account_codes",no_map)
    monkeypatch.setattr(REGISTRY,"contract_plugin",{o.NAME:"stock"})
    monkeypatch.setattr(approvals,"_row_digest",lambda *args:"digest")
    authority = RecordedApproval(ctx, lambda: SimpleNamespace(status=conn.status, contract=conn.contract, company_id="co"), lambda: conn.outcome)
    authority.wrap(monkeypatch, o, "commit_count")
    return SimpleNamespace(ctx=ctx,authority=authority,conn=conn,items=items,whs=whs,stocks=stocks,states=states,state=state,
                           inventory=inventory,adjustment=adjustment,period=period)


@pytest.mark.parametrize("method",["moving_average","fifo"])
def test_k3_gain_at_average_and_no_sale_or_supplied_price(world,method):
    world.stocks["wire"]["method"]=method
    plan=o.plan_count(world.ctx,inputs(row(rate="999")))
    mv=plan.moves[0]
    assert (mv.qty,mv.value,mv.qty_after,mv.value_after)==(D(4),D("44.33"),D(184),D("2039.33"))
    assert plan.observations[0].rate_used is None
    assert [(ln.account_id,ln.debit,ln.credit) for ln in plan.lines]==[("inv",D("44.33"),D(0)),("adjust",D(0),D("44.33"))]
    card=o.preview_count(world.ctx,inputs(row(rate="999"))).card
    assert "average cost on hand" in card.lines[1][1] and "supplied rate is not used" in card.lines[1][1]
    if method=="fifo":
        assert [(g.qty,g.value) for g in mv.pair.groups][-1]==(D(4),D("44.33"))
        assert mv.layer and mv.merge_into is None


def test_gain_may_exceed_quantity_on_hand_without_rounded_unit_rate(world):
    plan=o.plan_count(world.ctx,inputs(row(counted=400)))
    assert plan.moves[0].value==D("2438.33") and plan.moves[0].qty_after==400


@pytest.mark.parametrize("method,cost,remaining",[("fifo","80.00","1915.00"),("moving_average","88.67","1906.33")])
def test_k4_loss_uses_actual_method(world,method,cost,remaining):
    world.stocks["wire"]["method"]=method
    mv=o.plan_count(world.ctx,inputs(row(counted=172))).moves[0]
    assert (mv.qty,mv.value,mv.qty_after,mv.value_after)==(D(8),D(cost),D(172),D(remaining))
    if method=="fifo":assert mv.takes==[("g1",D(8),D("80.00"))]


def test_zero_count_takes_whole_value(world):
    mv=o.plan_count(world.ctx,inputs(row(counted="0.0000"))).moves[0]
    assert (mv.value,mv.qty_after,mv.value_after)==(D("1995.00"),D(0),D(0))


def test_k8_offsetting_count_keeps_four_gross_lines(world):
    inp=inputs(row("Hex Bolt",102),row("Fuse",45))
    plan=o.plan_count(world.ctx,inp)
    assert [(mv.direction,mv.value) for mv in plan.moves]==[("in",D("10.00")),("out",D("10.00"))]
    assert [(ln.account_id,ln.debit,ln.credit) for ln in plan.lines]==[("inv",D(10),D(0)),("adjust",D(0),D(10)),
                                                                   ("adjust",D(10),D(0)),("inv",D(0),D(10))]
    card=approvals._preview(world.ctx,o.CONTRACT,inp)
    assert len(card["books"])==4
    check_card_amounts(card)


def test_empty_pair_missing_rate_is_scoped_and_names_stored_unit(world):
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs(row(counted=6,warehouse="Shop Warehouse")))
    assert exc.value.code=="rate_missing" and exc.value.data["field"]=="lines.0.rate"
    assert "per Meter" in exc.value.message and "for it" in exc.value.message
    assert world.conn.events==[]
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs(row("Fuse",6,"Shop Warehouse")))
    assert "for them per Fuse" in exc.value.message


def test_k3c_empty_gain_uses_exact_owner_cost_and_fifo_layer(world):
    world.stocks["wire"]["method"]="fifo"
    plan=o.plan_count(world.ctx,inputs(row(counted=6,warehouse="Shop Warehouse",rate="7.4000")))
    mv=plan.moves[0]
    assert mv.value==D("44.40") and mv.qty_after==6 and mv.layer
    assert plan.observations[0].rate_used==D("7.4")
    card=approvals._preview(world.ctx,o.CONTRACT,inputs(row(counted=6,warehouse="Shop Warehouse",rate="7.4000")))
    assert "owner cost 7.40 USD per Meter" in dict(card["lines"])["Copper Wire"]
    check_card_amounts(card)


@pytest.mark.parametrize("empty",[False,True])
def test_gain_rounding_to_zero_is_refused(world,empty):
    if empty:inp=inputs(row(counted="0.0001",warehouse="Shop Warehouse",rate="0.0001"))
    else:
        world.states[("wire","wh")]["value"]=D("0.01")
        inp=inputs(row(counted=181))
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inp)
    assert exc.value.code=="receipt_below_minor_unit" and world.conn.events==[]


def test_all_unchanged_has_original_no_card_and_no_role_requirement(world,monkeypatch):
    def forbidden(*a):raise AssertionError("No adjustment account is needed for a matching count")
    monkeypatch.setattr(ledger,"account_by_type",forbidden)
    with pytest.raises(Refusal) as exc:approvals._preview(world.ctx,o.CONTRACT,inputs(row(counted=180)))
    assert exc.value.code=="count_matches"
    assert exc.value.message=="The count matches the books: 180 Copper Wire in Main Warehouse. Nothing to adjust."
    assert world.conn.events==[] and world.conn.inserts==[]


def test_duplicate_pair_after_name_resolution_has_original_sentence(world):
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs(row(),row("copper wire",185,"main warehouse")))
    assert exc.value.code=="count_duplicate"
    assert exc.value.message=="Copper Wire in Main Warehouse appears twice in that count; give one figure for it."
    assert world.conn.events==[]


def test_holder_questions_are_scoped_to_the_count_line(world):
    world.states[("wire","shop")]=dict(qty=D(1),value=D(5),head="shop-head",last_date=TODAY,groups=[])
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs(row(warehouse=None)))
    assert exc.value.code=="warehouse_ambiguous" and exc.value.data["field"]=="lines.0.warehouse"
    assert len(exc.value.options)==2
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs(row("Copper",184)))
    assert exc.value.code=="item_ambiguous"


def test_service_item_refused(world):
    world.items["wire"]["kind"]="service"
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs())
    assert exc.value.code=="item_not_stock"


def test_date_closed_and_backdated_refused(world,monkeypatch):
    assert o.plan_count(world.ctx,inputs()).date==TODAY
    said=dt.date(2026,5,1)
    assert o.plan_count(world.ctx,inputs(date=said)).date==said
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs(date=dt.date(2025,12,31)))
    assert exc.value.code=="backdated"
    def closed(*a):raise Refusal("Closed.","period_closed")
    monkeypatch.setattr(ledger,"open_period",closed)
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs())
    assert exc.value.code=="period_closed"


@pytest.mark.parametrize("code",["no_stock_adjustment_account","several_stock_adjustment_accounts","frozen_account"])
def test_counter_role_failures_preserved(world,monkeypatch,code):
    def refuse(*a):raise Refusal("Stock adjustment account unusable.",code)
    monkeypatch.setattr(ledger,"account_by_type",refuse)
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs())
    assert exc.value.code==code and world.conn.events==[]


def mixed():return inputs(row(counted=180),row("Hex Bolt",102),row("Fuse",50))


def test_mixed_card_preserves_all_observations_and_head_read_sets(world):
    inp=mixed();plan=o.plan_count(world.ctx,inp)
    assert len(plan.observations)==3 and [mv.line_no for mv in plan.moves]==[2]
    assert {r for r in plan.read_set if r[0]=="stock_head"}=={("stock_head","head-wire"),("stock_head","head-bolt"),("stock_head","head-fuse")}
    card=approvals._preview(world.ctx,o.CONTRACT,inp)
    lines=dict(card["lines"])
    assert "books 180, counted 180, difference 0 Meter, value 0.00 USD; no adjustment" in lines["Copper Wire"]
    assert "books 100, counted 102, difference +2 Each, value 10.00 USD" in lines["Hex Bolt"]
    assert "no adjustment" in lines["Fuse"]
    assert len(inp.model_dump()["lines"])==3
    check_card_amounts(card)
    assert world.conn.inserts==[]


def prepare_commit(world,monkeypatch,inp):
    world.conn.read_set=o.count_read_set(world.ctx,inp);world.conn.events.clear()
    def number(ctx,series,when):
        assert series=="SC" and when==(inp.date or TODAY)
        world.conn.events.append("number");return "SC-2026-00001"
    monkeypatch.setattr(o,"next_number",number)
    movements=[]
    def entry(ctx,mv,when,kind,vid):
        assert kind=="stock_count"
        movements.append((mv.line_no,mv.direction,mv.qty,mv.value,vid));world.conn.events.append("entry")
        return "entry-"+str(mv.line_no)
    monkeypatch.setattr(s,"_entry_id",entry)
    def post(ctx,when,kind,vid,lines,**kw):
        assert kind=="stock_count" and lines and kw=={"operation_key":"key"}
        world.conn.events.append("post")
    monkeypatch.setattr(ledger,"post",post)
    return movements


def test_mixed_commit_has_only_real_movements_with_original_line_number(world,monkeypatch):
    inp=mixed();moves=prepare_commit(world,monkeypatch,inp)
    result=o.commit_count(world.ctx,inp,"key")
    assert world.conn.events==["preflight","preflight","number","insert:stock_voucher","insert:stock_voucher_line","entry","post"]
    header,line=[data for _,data in world.conn.inserts]
    assert header["kind"]=="count" and header["number"]=="SC-2026-00001"
    assert line["line_no"]==2 and line["item_id"]=="bolt" and line["direction"]=="in"
    assert (line["books_qty"],line["counted_qty"],line["qty_moved"],line["rate"])==("100","102","2",None)
    assert moves==[(2,"in",D(2),D("10.00"),header["id"])]
    assert result.data["adjustments"]==1 and result.data["voucher_type"]=="stock_count"
    check_answer(o.CONTRACT,result)


def test_unchanged_head_changes_invalidate_mixed_count_before_any_write(world,monkeypatch):
    inp=mixed();prepare_commit(world,monkeypatch,inp)
    monkeypatch.setattr(approvals,"_row_digest",lambda conn,t,k:"changed" if (t,k)==("stock_head","head-wire") else "digest")
    with pytest.raises(Refusal) as exc:o.commit_count(world.ctx,inp,"key")
    assert exc.value.code=="stale" and world.conn.events==["preflight"] and world.conn.inserts==[]


def test_unchanged_line_becoming_changed_alters_actual_card(world):
    before=approvals._preview(world.ctx,o.CONTRACT,mixed())
    world.states[("wire","wh")]["qty"]=D(179)
    after=approvals._preview(world.ctx,o.CONTRACT,mixed())
    assert approvals.digest(before)!=approvals.digest(after)
    assert "difference +1 Meter" in dict(after["lines"])["Copper Wire"]


def test_empty_gain_voucher_binds_counted_books_rate_and_actual_value(world,monkeypatch):
    inp=inputs(row(counted="6.0000",warehouse="Shop Warehouse",rate="7.4000"))
    prepare_commit(world,monkeypatch,inp);o.commit_count(world.ctx,inp,"key")
    line=world.conn.inserts[1][1];dialect=postgresql.dialect()
    bound={name:t.voucher_line.c[name].type.bind_processor(dialect)(line[name]) for name in ["qty_moved","books_qty","counted_qty","rate","value"]}
    assert bound==dict(qty_moved="6",books_qty="0",counted_qty="6",rate="7.4",value="44.40")


def test_k8_commit_posts_all_four_lines(world,monkeypatch):
    inp=inputs(row("Hex Bolt",102),row("Fuse",45));prepare_commit(world,monkeypatch,inp)
    postings=[]
    monkeypatch.setattr(ledger,"post",lambda ctx,date,kind,vid,lines,**kw:postings.extend(lines))
    o.commit_count(world.ctx,inp,"key")
    assert len(postings)==4
    assert [(p.account_id,p.debit,p.credit) for p in postings]==[("inv",D(10),D(0)),("adjust",D(0),D(10)),("adjust",D(10),D(0)),("inv",D(0),D(10))]
    assert len([x for x in world.conn.inserts if x[0]=="stock_voucher_line"])==2


def test_zero_cost_loss_still_records_stock_without_empty_posting(world,monkeypatch):
    world.states[("wire","wh")].update(qty=D(10),value=D("0.01"))
    inp=inputs(row(counted=9));plan=o.plan_count(world.ctx,inp)
    assert plan.moves[0].value==0 and plan.lines==[]
    card=approvals._preview(world.ctx,o.CONTRACT,inp)
    assert dict(card["lines"])["Accounting destination"]=="No ledger posting"
    prepare_commit(world,monkeypatch,inp);o.commit_count(world.ctx,inp,"key")
    assert world.conn.events==["number","insert:stock_voucher","insert:stock_voucher_line","entry"]


@pytest.mark.parametrize("bad",["missing","pending","outcome","wrong_contract"])
def test_actual_authority_refuses_before_effects(world,monkeypatch,bad):
    inp=inputs();prepare_commit(world,monkeypatch,inp)
    if bad=="missing":world.authority.enabled = False
    elif bad=="pending":world.conn.status="pending"
    elif bad=="outcome":world.conn.outcome=("done",)
    else:world.conn.contract="another"
    with pytest.raises(Refusal) as exc:o.commit_count(world.ctx,inp,"key")
    assert exc.value.code=="not_approved" and world.conn.events==[]


def test_preflight_failure_stops_number_and_writes(world,monkeypatch):
    inp=inputs();prepare_commit(world,monkeypatch,inp)
    def refuse(*a):raise Refusal("Posting invalid.","bad_posting")
    monkeypatch.setattr(ledger,"check_post",refuse)
    with pytest.raises(Refusal):o.commit_count(world.ctx,inp,"key")
    assert world.conn.events==[]


def test_reused_valuation_reread_cannot_mix_head_snapshots(world,monkeypatch):
    original=s._pair;calls=[]
    def changed(*args,**kw):
        p=original(*args,**kw);calls.append(1)
        if len(calls)==2:p.head["last_seq"]+=1
        return p
    monkeypatch.setattr(s,"_pair",changed)
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs(row(counted=172)))
    assert exc.value.code=="stale" and world.conn.events==[]


def external(world,monkeypatch,name="Test ERP",codes=None):
    codes=dict(inv="INV-1300",adjust="ADJ-5200") if codes is None else codes
    monkeypatch.setattr(o.integration,"system_of_record",lambda ctx,area:("external",name))
    def mapped(ctx,system,accounts):
        assert system==name and set(accounts)=={"inv","adjust"}
        return dict(codes)
    monkeypatch.setattr(o.integration,"account_codes",mapped)


def test_actual_accounting_destination_and_codes_are_card_bound(world,monkeypatch):
    native=approvals._preview(world.ctx,o.CONTRACT,inputs());external(world,monkeypatch)
    first=approvals._preview(world.ctx,o.CONTRACT,inputs());external(world,monkeypatch,"Other ERP")
    other=approvals._preview(world.ctx,o.CONTRACT,inputs());external(world,monkeypatch,"Other ERP",dict(inv="INV-new",adjust="ADJ-5200"))
    changed=approvals._preview(world.ctx,o.CONTRACT,inputs())
    assert len({approvals.digest(card) for card in [native,first,other,changed]})==4
    assert "external account INV-new" in changed["books"][0]
    for card in [native,first,other,changed]:check_card_amounts(card)
    rows=o.count_read_set(world.ctx,inputs())
    assert {r for r in rows if r[0]=="account_map"}=={("account_map","map-inv"),("account_map","map-adjust")}
    assert not any(t=="system_of_record" for t,_ in rows)


def test_missing_external_mapping_is_typed_before_writes(world,monkeypatch):
    external(world,monkeypatch)
    def refuse(*a):raise Refusal("Unmapped.","unmapped_account")
    monkeypatch.setattr(o.integration,"account_codes",refuse)
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs())
    assert exc.value.code=="unmapped_account" and world.conn.inserts==[]


def test_oversized_gain_is_typed_before_writes(world):
    with pytest.raises(Refusal) as exc:o.plan_count(world.ctx,inputs(row(counted="99999999999999",warehouse="Shop Warehouse",rate="1000")))
    assert exc.value.code=="count_value" and world.conn.events==[]


def test_replay_is_scoped_to_actual_count_contract_company_and_key():
    seen=[];payload={"text":"Recorded stock count SC-2026-00001.","data":{"voucher_type":"stock_count"},"figures":["1"],"amounts":[]}
    def execute(stmt):seen.append(stmt);return ReadRows(scalar=json.dumps(payload))
    ctx=SimpleNamespace(conn=SimpleNamespace(execute=execute),company_id="co")
    result=o.find_count(ctx,"key")
    assert set(seen[0].compile().params.values())=={"co","key","committed",o.NAME}
    assert "JOIN approval" in str(seen[0]) and result.data==payload["data"]
    ctx.conn.execute=lambda q:ReadRows()
    assert o.find_count(ctx,"none") is None


def test_registered_contract_stays_planned(as_released):
    assert o.CONTRACT in PLUGIN.contracts and o.CONTRACT.moves_money
    assert MANIFEST.contracts[o.NAME]=="write" and "stock_count" in MANIFEST.voucher_types
    assert o.CONTRACT.planned=="Core"
    with pytest.raises(Refusal) as exc:approvals._write_contract(o.CONTRACT)
    assert exc.value.code=="planned" and exc.value.capability=="planned.op.record_stock_count"
