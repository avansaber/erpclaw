"""Pure reversal and future-registration boundaries, never a live DB approval.

Real immutable mirror/read-set/Card/Registry/scope helpers run on recording
objects. SQL/network and transaction lifecycle are forbidden by the runner.
No atomic rollback, constraints, privileges or public activation are qualified.
"""
import copy
import json
from datetime import date,datetime,timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from erpclaw_core import approvals,approval_scope as scope,core_contracts,plugins
from erpclaw_core import supplier_payment_reversals as r
from erpclaw_core.common import Ctx,Refusal
from erpclaw_core.contracts import Input,WriteContract,check_answer

from registry_fixtures import core_registry

D=Decimal
PAID=date(2026,4,20)
WHEN=date(2026,5,1)


class FutureInput(Input):
    payment_id:str
    date:date
    reason:str|None=None
    @property
    def reversal_date(self): return self.date


class Transaction:
    def __init__(self,conn): self.connection,self.is_active,self._previous_nested=conn,True,None


class Rows:
    def __init__(self,rows=(),rowcount=1): self.rows,self.rowcount=list(rows),rowcount
    def mappings(self): return self
    def first(self): return self.rows[0] if self.rows else None
    def all(self): return self.rows
    def scalars(self): return iter([next(iter(x.values())) for x in self.rows])
    def scalar_one_or_none(self): return next(iter(self.rows[0].values())) if self.rows else None
    def __iter__(self): return iter(self.rows)


class Recording:
    def __init__(self):
        self.closed=self.invalidated=False
        self.root,self.nested=Transaction(self),Transaction(self)
        self.data={};self.events=[];self.queries=[];self.cas={};self.hook=None
    def get_transaction(self): return self.root
    def get_nested_transaction(self): return self.nested
    def begin(self): raise AssertionError("Caller owns transaction")
    def begin_nested(self): raise AssertionError("Caller owns savepoint")
    def commit(self): raise AssertionError("Caller owns commit")
    def rollback(self): raise AssertionError("Caller owns rollback")
    def execute(self,q):
        params=q.compile().params
        if q.is_select:
            table=next(iter(q.selected_columns)).table.name
            self.queries.append((table,q,params))
            if self.hook: self.hook(table,q)
            rows=list(self.data.get(table,[]))
            for name,value in params.items():
                if name.startswith("param_"): continue
                field=name.rsplit("_",1)[0]
                if table=="operation_outcome" and field=="contract":
                    rows=[v for v in rows if v.get("contract")==value];continue
                if field=="start_date": rows=[v for v in rows if v[field]<=value]
                elif field=="end_date": rows=[v for v in rows if v[field]>=value]
                else: rows=[v for v in rows if v.get(field) in value] if isinstance(value,list) else [v for v in rows if v.get(field)==value]
            if q._limit_clause is not None: rows=rows[:q._limit_clause.value]
            # Limit bind parameters aren't business predicates.
            return Rows(rows)
        assert q.is_update and q.table.name in ("supplier_payment","supplier_payable")
        table=q.table.name
        self.events.append(("update",table,params,q))
        count=self.cas.get(table,1)
        if count==1:
            row=next(x for x in self.data[table] if x["id"]==params["id_1"])
            for name in ("status","reverse_operation_key","outstanding"):
                if name in params: row[name]=params[name]
        return Rows(rowcount=count)


@pytest.fixture
def world(monkeypatch):
    c=Recording();ctx=Ctx(c,"co","Synthetic books","USD",date(2026,5,10),extra={})
    c.data={
      "company":[dict(id="co",name="Synthetic books",currency="USD")],
      "party":[dict(id="supplier",company_id="co",name="Copperline Metals",kind="supplier")],
      "account":[dict(id="ap",company_id="co",number="2199",name="Original payable",account_type="payable",frozen=False,is_group=False),
                 dict(id="bank",company_id="co",number="1199",name="Original bank",account_type="bank",frozen=False,is_group=False)],
      "fiscal_period":[dict(id="old-period",company_id="co",start_date=date(2026,4,1),end_date=date(2026,4,30),status="closed",fiscal_year_id="year"),
                       dict(id="period",company_id="co",start_date=date(2026,5,1),end_date=date(2026,5,31),status="open",fiscal_year_id="year")],
      "fiscal_year":[dict(id="year",company_id="co",start_date=date(2026,1,1),end_date=date(2026,12,31),status="open")],
      "supplier_payment":[dict(id="payment",company_id="co",number="SPAY-2026-00001",supplier_id="supplier",posting_date=PAID,
          currency="USD",amount=D(1600),unallocated=D(0),account_id="bank",status="posted",operation_key="forward-key",reverse_operation_key=None)],
      "supplier_payable":[],"supplier_payment_allocation":[],"posting":[],"ledger_entry":[],"approval":[],"operation_outcome":[],"account_map":[]}
    def posting(key,kind,voucher,day,amount,bank=False):
        c.data["posting"].append(dict(id=key,company_id="co",voucher_type=kind,voucher_id=voucher,posting_date=day,
                                  reverses_posting_id=None,seq=len(c.data["posting"])+1))
        pairs=[("ap","supplier",amount,D(0)),("bank",None,D(0),amount)] if bank else [
               ("inventory",None,amount,D(0)),("ap","supplier",D(0),amount)]
        for n,(account,party,debit,credit) in enumerate(pairs,1):
            c.data["ledger_entry"].append(dict(id=f"{key}-{n}",company_id="co",posting_id=key,line_no=n,account_id=account,
                party_id=party,currency="USD",debit=debit,credit=credit))
    for key,amount,outstanding,allocated in [("one",1450,0,1450),("two",300,150,150)]:
        c.data["supplier_payable"].append(dict(id=key,company_id="co",supplier_id="supplier",currency="USD",
            source_type=r.payment.payables.DELIVERY,source_id="receipt-"+key,source_number="GR-"+key,
            posting_date=date(2026,4,10),amount=D(amount),outstanding=D(outstanding),status="paid" if outstanding==0 else "open"))
        c.data["supplier_payment_allocation"].append(dict(id="allocation-"+key,company_id="co",payment_id="payment",
            payable_id=key,currency="USD",amount=D(allocated)))
        posting("source-"+key,r.payment.payables.DELIVERY,"receipt-"+key,date(2026,4,10),D(amount))
    posting("original",r.PAYMENT,"payment",PAID,D(1600),True)
    w=SimpleNamespace(conn=c,ctx=ctx,request=FutureInput(payment_id="payment",date=WHEN,reason="Recorded twice"),checks=[])
    def period(context,day):
        row=next((p for p in c.data["fiscal_period"] if p["start_date"]<=day<=p["end_date"]),None)
        if row is None or row["status"]!="open": raise Refusal("Period is not open","period_closed")
        return row
    monkeypatch.setattr(r.ledger,"open_period",period)
    def check_post(context,day,kind,key,lines):
        period(context,day)
        for line in lines:
            account=next(a for a in c.data["account"] if a["id"]==line.account_id)
            if account["frozen"] or account["is_group"]: raise Refusal("Account cannot be posted","bad_account")
        w.checks.append((day,kind,key,lines))
        return lines
    monkeypatch.setattr(r.ledger,"check_post",check_post)
    def post(context,day,kind,key,lines,**kwargs):
        check_post(context,day,kind,key,lines)
        c.events.append(("mirror",day,kind,key,lines,kwargs))
        c.data["posting"].append(dict(id="reversal",company_id="co",voucher_type=kind,voucher_id=key,posting_date=day,
            reverses_posting_id=kwargs["_reverses"],seq=4))
        return r.ledger.PostingResult("reversal",4,"recorded-hash")
    monkeypatch.setattr(r.ledger,"post",post)
    monkeypatch.setattr(r.integration,"system_of_record",lambda ctx,area:("native",None))
    monkeypatch.setattr(r.integration,"account_codes",lambda ctx,external,ids:{i:"EXT-"+i for i in ids})
    monkeypatch.setattr(r,"audit",lambda *args,**kwargs:c.events.append(("audit",args[1:],kwargs)))
    return w


def row(w,table,key): return next(x for x in w.conn.data[table] if x["id"]==key)


def plan(w): return r.plan_reversal(w.ctx,w.request)


def refuse(w,code,fn=None):
    with pytest.raises(Refusal) as found: (fn or (lambda:plan(w)))()
    assert found.value.code==code
    assert not w.conn.events


def register(w,monkeypatch):
    contract=WriteContract(r.NAME,"core","write","Future resolved reversal",FutureInput,preview=r.preview_reversal,
        commit=r.commit_reversal,read_set=r.reversal_read_set,find_by_operation_key=r.find_reversal_by_key,
        amounts=("amount","reopened"),moves_money=True)
    manifest=core_contracts.MANIFEST.model_copy(update={"contracts":{r.NAME:"write"},"metrics":[],"uses":[]})
    candidate_version=manifest.version;installed=plugins.version
    def version(name): return candidate_version if name=="erpclaw-core" else installed(name)
    monkeypatch.setattr(plugins,"version",version);monkeypatch.setattr(approvals,"version",version)
    monkeypatch.setattr(plugins,"_core_revisions",lambda:{"0001"})
    registry=core_registry(monkeypatch,manifest,contract)
    monkeypatch.setattr(plugins,"REGISTRY",registry);monkeypatch.setattr(approvals,"REGISTRY",registry)
    w.registry,w.contract=registry,registry.contracts[r.NAME]
    seal(w)


def seal(w,date_source=None):
    card=dict(id="card",company_id="co",principal_id="owner",conversation_id="conversation",contract=r.NAME,status="consumed",
        operation_key="reverse-key",contract_version=w.registry.plugins["core"].version,policy_version="policy",
        impl_version=approvals.impl_version(w.contract),nonce="nonce",expires_at=datetime(2026,5,1,12,tzinfo=timezone.utc))
    args=w.request.model_dump(mode="json");preview=approvals._preview(w.ctx,w.contract,w.request,date_source)
    rows=approvals.read_set(w.conn,w.contract,w.ctx,w.request)
    card.update(args_json=json.dumps(args),card_json=json.dumps(preview),read_set_json=json.dumps(rows),
                digest=approvals.digest(approvals._fields(card,args,rows,preview)))
    w.conn.data["approval"]=[card]


def commit(w,*,owner="core",name=r.NAME,scope_key="reverse-key",key="reverse-key",ctx=None):
    with scope._issue(w.ctx,"card",name,scope_key,owner,w.conn.nested):
        return w.contract.commit(ctx or w.ctx,w.request,key)


def test_complete_reversal_includes_paid_target_and_original_accounts(world):
    result=plan(world)
    assert [(x.item["id"],x.before,x.amount,x.after) for x in result.releases]==[
        ("one",D(0),D(1450),D(1450)),("two",D(150),D(150),D(300))]
    assert [(x.account_id,x.debit,x.credit,x.party_id) for x in result.mirror]==[
        ("ap",D(0),D(1600),"supplier"),("bank",D(1600),D(0),None)]
    assert ("supplier_payable","one") in result.read_set
    assert ("supplier_payment_allocation","allocation-one") in result.read_set
    assert result.original_period["status"]=="closed" and result.reversal_period["status"]=="open"
    assert not world.conn.events


def test_later_live_and_reversed_history_include_unreleased_paid_sibling(world,monkeypatch):
    item=row(world,"supplier_payable","two");item["outstanding"]=D(120)
    later={**row(world,"supplier_payment","payment"),"id":"later","number":"SPAY-2","amount":D(130),"operation_key":"later-key"}
    old={**later,"id":"reversed-old","number":"SPAY-3","amount":D(20),"status":"reversed","reverse_operation_key":"old-reverse"}
    world.conn.data["supplier_payment"].extend([later,old])
    sibling={**item,"id":"sibling","source_id":"receipt-sibling","source_number":"GR-sibling","amount":D(100),"outstanding":D(0),"status":"paid"}
    world.conn.data["supplier_payable"].append(sibling)
    for aid,pid,target,amount in [("later-two","later","two",30),("later-sibling","later","sibling",100),("old-two","reversed-old","two",20)]:
        world.conn.data["supplier_payment_allocation"].append(dict(id=aid,company_id="co",payment_id=pid,payable_id=target,currency="USD",amount=D(amount)))
    result=plan(world)
    assert result.releases[1].after==D(270)
    assert [x.item["id"] for x in result.releases]==["one","two"]
    expected={("company","co"),("party","supplier"),("account","bank"),("account","ap"),
              ("fiscal_period","old-period"),("fiscal_period","period"),("fiscal_year","year")}
    expected.update(("supplier_payment",x) for x in ("payment","later","reversed-old"))
    expected.update(("supplier_payable",x) for x in ("one","two","sibling"))
    expected.update(("supplier_payment_allocation",x) for x in ("allocation-one","allocation-two","later-two","later-sibling","old-two"))
    expected.update(("posting",x) for x in ("original","source-one","source-two"))
    expected.update(("ledger_entry",f"{x}-{i}") for x in ("original","source-one","source-two") for i in (1,2))
    assert set(result.read_set)==expected
    register(world,monkeypatch)
    # This inspected row isn't displayed/released. Its actual row digest still binds the card.
    sibling["source_number"]="CHANGED"
    refuse(world,"stale",lambda:commit(world))


def test_reversal_history_loop_carries_shrinking_limit_before_materialization(world,monkeypatch):
    row(world,"supplier_payable","two")["outstanding"]=D(120)
    later={**row(world,"supplier_payment","payment"),"id":"later","number":"SPAY-2","amount":D(30),"operation_key":"later-key"}
    world.conn.data["supplier_payment"].append(later)
    world.conn.data["supplier_payment_allocation"].append(dict(id="later-alloc",company_id="co",payment_id="later",payable_id="two",currency="USD",amount=D(30)))
    monkeypatch.setattr(r.payment,"MAX_HISTORY",2)  # The original two-member payment fits exactly.
    refuse(world,"payment_items_limit")
    queries=[(params["payable_id_1"],q._limit_clause.value) for table,q,params in world.conn.queries
             if table=="supplier_payment_allocation" and "payable_id_1" in params]
    assert queries==[("one",3),("two",2)]  # Remaining 2 then1, each LIMIT+1; no prefix release.


@pytest.mark.parametrize("field,value",[("amount",D(1601)),("unallocated",D(1)),("currency","EUR"),("company_id","foreign"),("reverse_operation_key","old")])
def test_inconsistent_payment_header_refuses(world,field,value):
    row(world,"supplier_payment","payment")[field]=value
    refuse(world,"payment_reversal_source")


@pytest.mark.parametrize("field,value",[("company_id","foreign"),("currency","EUR"),("supplier_id","other"),("source_type","opening"),("status","reversed")])
def test_every_allocation_target_identity_is_checked(world,field,value):
    row(world,"supplier_payable","one")[field]=value
    refuse(world,"payment_source")


@pytest.mark.parametrize("mutation",["missing","duplicate","wrong_sum","foreign_currency","wrong_source_account","outstanding","live_payment_missing"])
def test_incomplete_history_or_source_refuses(world,mutation):
    if mutation=="missing": world.conn.data["supplier_payable"].pop(0)
    elif mutation=="duplicate": world.conn.data["supplier_payment_allocation"].append(dict(world.conn.data["supplier_payment_allocation"][0]))
    elif mutation=="wrong_sum": world.conn.data["supplier_payment_allocation"][0]["amount"]-=D(1)
    elif mutation=="foreign_currency": world.conn.data["supplier_payment_allocation"][0]["currency"]="EUR"
    elif mutation=="wrong_source_account": row(world,"ledger_entry","source-one-2")["account_id"]="another-ap"
    elif mutation=="outstanding": row(world,"supplier_payable","two")["outstanding"]=D(151)
    else: world.conn.data["supplier_payment_allocation"].append(dict(id="orphan",company_id="co",payment_id="missing",payable_id="two",currency="USD",amount=D(1)))
    with pytest.raises(Refusal): plan(world)
    assert not world.conn.events


@pytest.mark.parametrize("when,code",[(date(2026,6,1),"future_date"),(date(2026,4,1),"reversal_before_original"),(PAID,"period_closed"),("2026-05-01","payment_date")])
def test_dates_are_exact_and_original_period_may_remain_closed(world,when,code):
    world.request=r.ReverseSupplierPayment("payment",when)
    refuse(world,code)


@pytest.mark.parametrize("reason",["x"*141,7])
def test_reason_bound(world,reason):
    world.request=r.ReverseSupplierPayment("payment",WHEN,reason)
    refuse(world,"payment_reversal_reason")


def test_real_check_reverse_refuses_already_linked_posting(world):
    world.conn.data["posting"].append(dict(id="old-reverse",company_id="co",reverses_posting_id="original"))
    refuse(world,"already_reversed")


@pytest.mark.parametrize("field",["frozen","is_group"])
def test_mirror_checks_precede_card(world,field):
    row(world,"account","bank")[field]=True
    refuse(world,"bad_account")


def test_posting_cap_precedes_shared_unbounded_mirror_read(world,monkeypatch):
    monkeypatch.setattr(r.payment,"MAX_SOURCE_LINES",1)
    seen=[]
    monkeypatch.setattr(r.ledger,"check_reverse",lambda *a:seen.append(a))
    refuse(world,"payment_items_limit")
    assert not seen
    assert all(q._limit_clause is not None for table,q,_ in world.conn.queries if table=="ledger_entry")


@pytest.mark.parametrize("limit",["MAX_ITEMS","MAX_HISTORY_PER_ITEM","MAX_HISTORY"])
def test_global_and_member_limits_refuse_whole_request(world,monkeypatch,limit):
    monkeypatch.setattr(r.payment,limit,1 if limit!="MAX_HISTORY_PER_ITEM" else 0)
    refuse(world,"payment_items_limit")


def test_actual_card_shows_period_meaning_and_all_money(world,monkeypatch):
    register(world,monkeypatch)
    card=json.loads(world.conn.data["approval"][0]["card_json"])
    assert any("period beginning 2026-04-01" in s for s in card["warnings"])
    assert any("Original bank" in s and "1199" in s for s in card["books"])
    assert any("Original payable" in s and "2199" in s for s in card["books"])
    assert len(card["amounts"])==9


def test_real_mirror_then_header_then_item_cas_audit_immutable_history(world,monkeypatch):
    register(world,monkeypatch)
    originals=copy.deepcopy(world.conn.data["ledger_entry"])
    allocations=copy.deepcopy(world.conn.data["supplier_payment_allocation"])
    result=commit(world)
    assert [e[0] for e in world.conn.events]==["mirror","update","update","update","audit"]
    assert [e[1] for e in world.conn.events if e[0]=="update"]==["supplier_payment","supplier_payable","supplier_payable"]
    assert row(world,"supplier_payment","payment")["reverse_operation_key"]=="reverse-key"
    assert [x["outstanding"] for x in world.conn.data["supplier_payable"]]==[D(1450),D(300)]
    assert all(x["status"]=="open" for x in world.conn.data["supplier_payable"])
    assert originals==world.conn.data["ledger_entry"] and allocations==world.conn.data["supplier_payment_allocation"]
    check_answer(world.contract,result)
    assert result.data["posting_id"]=="reversal" and result.data["operation_key"]=="reverse-key"
    assert result.figures==["1,600.00","1,450.00","150.00"]
    assert world.conn.events[0][5]["_reverses"]=="original"
    assert not world.conn.data["operation_outcome"]


@pytest.mark.parametrize("kind",["no_registration","flag","copy","wrong_owner","wrong_contract","wrong_key","readonly","inactive"])
def test_actual_scope_is_required(world,monkeypatch,kind):
    register(world,monkeypatch)
    if kind=="no_registration":
        world.registry.contracts.clear();fn=lambda:commit(world)
    elif kind=="flag":
        world.ctx.extra["commit"]="card";fn=lambda:r.commit_reversal(world.ctx,world.request,"reverse-key")
    elif kind=="copy": fn=lambda:commit(world,ctx=copy.copy(world.ctx))
    elif kind=="wrong_owner": fn=lambda:commit(world,owner="stock")
    elif kind=="wrong_contract": fn=lambda:commit(world,name="pay_supplier")
    elif kind=="wrong_key": fn=lambda:commit(world,key="other")
    elif kind=="inactive": world.conn.nested.is_active=False;fn=lambda:commit(world)
    else:
        def fn():
            wrapped=scope._wrap_contract(lambda ctx,inp:r.commit_reversal(ctx,inp,"reverse-key"),"core",r.NAME,"preview")
            with scope._issue(world.ctx,"card",r.NAME,"reverse-key","core",world.conn.nested): wrapped(world.ctx,world.request)
    refuse(world,"not_approved",fn)


@pytest.mark.parametrize("kind",["card","digest","row","history","reason","version","destination"])
def test_actual_card_readset_and_destination_changes_refuse_before_effect(world,monkeypatch,kind):
    register(world,monkeypatch)
    if kind=="card": world.conn.data["approval"][0]["card_json"]="{}"
    elif kind=="digest": world.conn.data["approval"][0]["digest"]="0"*64
    elif kind=="row": row(world,"account","bank")["number"]="changed"
    elif kind=="history": row(world,"supplier_payment","payment")["number"]="changed"
    elif kind=="reason": world.request=world.request.model_copy(update={"reason":"Different reason"})
    elif kind=="version":
        owner=world.registry.plugins["core"];owner.manifest=owner.manifest.model_copy(update={"version":owner.version+".post1"})
    else: monkeypatch.setattr(r.integration,"system_of_record",lambda *a:("external","OtherLedger"))
    refuse(world,"stale",lambda:commit(world))


def test_real_date_source_envelope_is_bound(world,monkeypatch):
    register(world,monkeypatch);seal(world,{"phrase":"1 May 2026","date":"2026-05-01"})
    assert commit(world).data["posting_id"]=="reversal"


def test_live_scope_rechecked_after_reads(world,monkeypatch):
    register(world,monkeypatch)
    def hook(table,q):
        if table=="account": world.conn.nested.is_active=False
    world.conn.hook=hook
    refuse(world,"not_approved",lambda:commit(world))


@pytest.mark.parametrize("table,count",[("supplier_payment",0),("supplier_payment",2),("supplier_payable",0),("supplier_payable",2)])
def test_cas_conflicts_propagate_for_caller_rollback(world,monkeypatch,table,count):
    register(world,monkeypatch);world.conn.cas[table]=count
    with pytest.raises(Refusal) as found: commit(world)
    assert found.value.code=="conflict"
    assert world.conn.events[0][0]=="mirror" and not any(e[0]=="audit" for e in world.conn.events)
    # This recorder cannot establish rollback; Core must roll back its effect savepoint.


def test_durable_outcome_readback_does_not_recompute_later_outstanding(world,monkeypatch):
    register(world,monkeypatch);result=commit(world)
    stored=dict(text=result.text,data=result.data,figures=result.figures,amounts=[vars(a) for a in result.amounts])
    world.conn.data["operation_outcome"]=[dict(result_json=json.dumps(stored),company_id="co",key="reverse-key",status="committed",contract=r.NAME)]
    row(world,"supplier_payable","one")["outstanding"]=D(1)
    read=r.find_reversal_by_key(world.ctx,"reverse-key")
    assert read==result
    _,query,params=world.conn.queries[-1]
    assert params["company_id_1"]=="co" and params["company_id_2"]=="co" and params["contract_1"]==r.NAME
    assert r.find_reversal_by_key(world.ctx,"missing") is None


def test_public_reversal_remains_absent():
    assert r.NAME not in core_contracts.MANIFEST.contracts
