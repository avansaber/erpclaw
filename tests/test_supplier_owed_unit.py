"""Pure typed supplier balance reads; recording SELECT and inspector boundaries.

No SQL/network, schema changes, name resolution, public registration or writes.
This does not qualify a live database connection, grants or reporting activation.
"""
from dataclasses import FrozenInstanceError,replace
from datetime import date,datetime
from decimal import Decimal,localcontext
from types import SimpleNamespace

import pytest

from erpclaw_core import core_contracts,supplier_owed as owed
from erpclaw_core.common import Ctx,Refusal
from erpclaw_core.contracts import Contract,Input,check_answer

D=Decimal


class Rows:
    def __init__(self,rows): self.rows=rows
    def mappings(self): return self
    def first(self): return self.rows[0] if self.rows else None
    def __iter__(self): return iter(self.rows)


class Recording:
    def __init__(self):
        self.supplier=dict(id="supplier",company_id="co",name="Copperline Metals",kind="supplier")
        self.rows=[];self.queries=[];self.error=None
    def execute(self,statement):
        assert statement.is_select,"The read attempted a mutation"
        if self.error: raise self.error
        params=statement.compile().params
        table=next(iter(statement.selected_columns)).table.name
        self.queries.append((table,statement,params))
        assert params["company_id_1"]=="co"
        if table=="party":
            assert params["id_1"]=="supplier"
            return Rows([] if self.supplier is None else [self.supplier])
        assert table=="supplier_payable" and params["supplier_id_1"]=="supplier"
        assert statement._limit_clause is not None
        return Rows(self.rows[:statement._limit_clause.value])
    def begin(self): raise AssertionError("Read owns no transaction lifecycle")
    def begin_nested(self): raise AssertionError("Read owns no savepoint")
    def commit(self): raise AssertionError("Read owns no commit")
    def rollback(self): raise AssertionError("Read owns no rollback")


@pytest.fixture
def world(monkeypatch):
    c=Recording();ctx=Ctx(c,"co","Synthetic books","USD",date(2026,5,1),extra={})
    w=SimpleNamespace(conn=c,ctx=ctx,present=True,inspections=[])
    def inspect(conn):
        assert conn is c
        def has_table(name):
            assert name=="supplier_payable"
            w.inspections.append(name)
            return w.present
        return SimpleNamespace(has_table=has_table)
    monkeypatch.setattr(owed,"inspect",inspect)
    return w


def item(key="one",amount="100.00",outstanding="100.00",status="open",**changes):
    row=dict(id=key,company_id="co",supplier_id="supplier",currency="USD",source_number="GR-"+key,
             posting_date=date(2026,4,10),amount=D(amount),outstanding=D(outstanding),status=status)
    row.update(changes)
    return row


def read(w): return owed.owed(w.ctx,"supplier")


def answer(w):
    claim=read(w);result=owed.render_owed(w.ctx,claim)
    contract=Contract("supplier_owed","core","read","Future read boundary",Input,amounts=("outstanding",))
    check_answer(contract,result)
    return claim,result


def refused(w,code="owed_source"):
    with pytest.raises(Refusal) as found: read(w)
    assert found.value.code==code


def test_figure_uses_all_open_balances_with_paid_and_reversed_history(world):
    world.conn.rows=[item("one","1450","850"),item("two","300","150"),
                     item("paid",outstanding="0",status="paid"),item("reversed",outstanding="0",status="reversed")]
    claim,result=answer(world)
    assert claim.state=="figure" and claim.value==D(1000) and claim.source_count==4
    assert [x.id for x in claim.items]==["one","two"]
    assert result.data==dict(state="figure",value="1000.00",currency="USD",supplier_id="supplier",source_count=4)
    assert result.figures==["1,000.00","850.00","1,450.00","150.00","300.00"]
    assert result.text=="You owe Copperline Metals 1,000.00 USD: GR-one 850.00 of 1,450.00; GR-two 150.00 of 300.00."
    assert [x[0] for x in world.conn.queries]==["party","supplier_payable"]
    assert world.inspections==["supplier_payable"]
    assert [x.value for x in result.amounts]==["1000.00","850.00","1450.00","150.00","300.00"]


@pytest.mark.parametrize("statuses",[("paid",),("reversed",),("paid","reversed")])
def test_verified_zero_requires_existing_complete_set(world,statuses):
    world.conn.rows=[item(str(n),outstanding="0",status=s) for n,s in enumerate(statuses)]
    claim,result=answer(world)
    assert claim.state=="verified_zero" and claim.value==D("0.00")
    assert result.data["value"]=="0.00" and result.figures==["0.00"]
    assert result.amounts[0].value=="0.00" and claim.source_count==len(statuses)
    assert not claim.items


def test_no_activity_is_not_a_money_zero(world):
    claim,result=answer(world)
    assert claim.state=="no_activity" and claim.value is None and claim.source_count==0
    assert result.data["value"] is None and result.figures==[] and result.amounts==[]
    assert "Nothing has been owed" in result.text


def test_missing_source_precedes_any_row_query_and_never_becomes_zero(world):
    world.present=False
    claim,result=answer(world)
    assert claim.state=="not_available" and claim.unavailable_reason=="source_absent"
    assert claim.value is None and result.data["value"] is None and not result.amounts
    assert not world.conn.queries


@pytest.mark.parametrize("supplier",[None,{"kind":"customer"},{"company_id":"foreign"},{"id":"foreign"}])
def test_unknown_non_supplier_or_other_company_is_not_available(world,supplier):
    if supplier is None: world.conn.supplier=None
    else: world.conn.supplier.update(supplier)
    claim,result=answer(world)
    assert claim.state=="not_available" and claim.unavailable_reason=="supplier_unknown"
    assert claim.value is None and not result.figures and not result.amounts
    assert [x[0] for x in world.conn.queries]==["party"]


@pytest.mark.parametrize("key",[None,"",7," supplier","x"*37])
def test_only_resolved_identity_enters_and_invalid_identity_does_not_query(world,key):
    claim=owed.owed(world.ctx,key)
    assert claim.state=="not_available" and claim.value is None
    assert not world.conn.queries


@pytest.mark.parametrize("field,value",[("id",""),("company_id","other"),("supplier_id","other"),("currency","EUR"),
    ("status","unknown"),("posting_date",datetime(2026,4,10)),("source_number",""),("source_number","x"*31)])
def test_item_identity_and_shape_must_support_report(world,field,value):
    world.conn.rows=[item(**{field:value})]
    refused(world)


@pytest.mark.parametrize("amount,outstanding,status",[(D(0),D(0),"paid"),(D(100),D(101),"open"),
    (D(100),D(0),"open"),(D(100),D(1),"paid"),(D(100),D(1),"reversed"),(D(100),D(-1),"open"),
    (True,D(1),"open"),(1.25,D(1),"open"),(D("NaN"),D(1),"open"),(D(100),D("Infinity"),"open"),
    (D(100),D("0.001"),"open"),(D("1000000000000000"),D(1),"open")])
def test_invalid_stored_money_or_state_refuses_instead_of_false_total(world,amount,outstanding,status):
    world.conn.rows=[item()]
    world.conn.rows[0].update(amount=amount,outstanding=outstanding,status=status)
    refused(world)


def test_duplicate_item_is_not_summed_twice(world):
    world.conn.rows=[item(),item()]
    refused(world)


def test_exact_cap_is_complete_and_over_cap_has_no_partial_claim(world):
    world.conn.rows=[item(str(n),"1","1") for n in range(200)]
    assert read(world).value==D(200)
    _,statement,params=world.conn.queries[-1]
    assert statement._limit_clause.value==201
    assert "status" not in str(statement.whereclause)
    assert [str(c) for c in statement._order_by_clauses]==[
        "supplier_payable.posting_date","supplier_payable.source_number","supplier_payable.id"]
    world.conn.rows.append(item("201","1","1"))
    refused(world,"owed_items_limit")


@pytest.mark.parametrize("currency,places",[("USD",2),("JPY",0),("KWD",3)])
def test_large_aggregate_is_not_limited_to_single_post_amount(world,currency,places):
    world.ctx.currency=currency
    value="999999999999999" + ("."+"9"*places if places else "")
    world.conn.rows=[item(str(n),value,value,currency=currency) for n in range(200)]
    claim,result=answer(world)
    assert claim.value==D(value)*200 and claim.value>=D("1000000000000000")
    assert result.data["value"]==format(claim.value,f".{places}f")
    assert len(result.amounts)==401


def test_arithmetic_and_render_are_independent_of_callers_small_decimal_context(world):
    world.conn.rows=[item("one","999999999999999.99","999999999999999.99"),item("two","10.01","10.01")]
    with localcontext() as arithmetic:
        arithmetic.prec=6
        claim=read(world);result=owed.render_owed(world.ctx,claim)
    assert claim.value==D("1000000000000010.00")
    assert result.data["value"]=="1000000000000010.00"
    assert result.figures[0]=="1,000,000,000,000,010.00"


def test_negative_zero_is_canonical_verified_zero(world):
    world.conn.rows=[item(outstanding="-0.00",status="paid")]
    claim,result=answer(world)
    assert not claim.value.is_signed() and result.data["value"]=="0.00"


def test_labels_are_data_and_read_does_not_restrict_future_stored_source_kind(world):
    world.conn.supplier["name"]="Copperline <read this as text>"
    world.conn.rows=[item(source_type="future_stored_source")]
    claim,result=answer(world)
    assert claim.value==100 and "Copperline <read this as text>" in result.text
    assert len(world.conn.queries)==2


@pytest.mark.parametrize("where",["inspection","select"])
def test_database_failures_are_not_relabelled_no_activity_or_zero(world,monkeypatch,where):
    failure=RuntimeError("recorded database failure")
    if where=="inspection":
        def bad(conn): raise failure
        monkeypatch.setattr(owed,"inspect",bad)
    else: world.conn.error=failure
    with pytest.raises(RuntimeError) as found: read(world)
    assert found.value is failure


def test_claim_and_item_are_immutable_and_cannot_render_in_other_books(world):
    world.conn.rows=[item()];claim=read(world)
    with pytest.raises(FrozenInstanceError): claim.state="verified_zero"
    with pytest.raises(FrozenInstanceError): claim.items[0].outstanding=D(0)
    with pytest.raises(Refusal): owed.render_owed(world.ctx,replace(claim,company_id="other"))
    with pytest.raises(Refusal): owed.render_owed(world.ctx,replace(claim,currency="EUR"))


def test_no_public_owed_registration_or_new_metric():
    assert "supplier_owed" not in core_contracts.MANIFEST.contracts
    assert "supplier_owed" not in core_contracts.MANIFEST.metrics
