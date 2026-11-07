"""Pure durable payment-result lookup on recorded joined SELECT boundaries.

No SQL, current balance lookup, registration, approval or transaction mutation.
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.sql import operators

from erpclaw_core import core_contracts,supplier_payments as payments
from erpclaw_core.contracts import Amount,Result


class Rows:
    def __init__(self,value): self.value=value
    def scalar_one_or_none(self): return self.value


class Recording:
    def __init__(self,record=None): self.record,self.queries=record,[]
    def execute(self,statement):
        assert statement.is_select,"Readback must not mutate"
        self.queries.append(statement)
        assert [str(c) for c in statement.selected_columns]==["operation_outcome.result_json"]
        join=statement.get_final_froms()[0]
        assert {join.left.name,join.right.name}=={"operation_outcome","approval"}
        assert str(join.onclause)=="approval.id = operation_outcome.approval_id"
        predicates={}
        for part in statement._where_criteria:
            assert part.operator is operators.eq
            predicates[(part.left.table.name,part.left.name)]=part.right.value
        # Independent scope oracle; missing either company or exact contract fails here.
        assert predicates=={("operation_outcome","company_id"):"co",("approval","company_id"):"co",
            ("operation_outcome","key"):"payment-key",("operation_outcome","status"):"committed",
            ("approval","contract"):"pay_supplier"}
        if self.record is None: return Rows(None)
        matches=all(self.record[table][field]==value for (table,field),value in predicates.items())
        return Rows(self.record["operation_outcome"]["result_json"] if matches else None)
    def begin(self): raise AssertionError("No transaction lifecycle")
    def begin_nested(self): raise AssertionError("No savepoint")
    def commit(self): raise AssertionError("No commit")
    def rollback(self): raise AssertionError("No rollback")


def stored_result():
    return dict(text="Paid Copperline Metals 1,600.00 USD. Still owed: 150.00 USD.",
        data={"id":"payment","number":"SPAY-2026-00001","posting_id":"posting","operation_key":"payment-key"},
        figures=["1,600.00","150.00"],amounts=[dict(what="amount",of="SPAY-2026-00001",value="1600.00",currency="USD"),
         dict(what="outstanding",of="Copperline Metals",value="150.00",currency="USD")])


def world(payload=None):
    payload=stored_result() if payload is None else payload
    record={"operation_outcome":dict(company_id="co",key="payment-key",status="committed",result_json=json.dumps(payload)),
            "approval":dict(company_id="co",contract="pay_supplier")}
    conn=Recording(record)
    return SimpleNamespace(conn=conn,ctx=SimpleNamespace(conn=conn,company_id="co"),payload=payload,record=record)


def test_exact_stored_result_and_amounts_return_without_recomputation(monkeypatch):
    w=world()
    def forbidden(*args,**kwargs): raise AssertionError("Readback must not run business logic")
    for name in ("plan_payment","preview_payment","payment_read_set","commit_payment","_payment_approval"):
        monkeypatch.setattr(payments,name,forbidden)
    result=payments.find_payment_by_key(w.ctx,"payment-key")
    assert result==Result(w.payload["text"],w.payload["data"],w.payload["figures"],[Amount(**a) for a in w.payload["amounts"]])
    assert len(w.conn.queries)==1
    assert payments.find_payment_by_key(w.ctx,"payment-key")==result


@pytest.mark.parametrize("table,field,value",[("operation_outcome","company_id","foreign"),
    ("approval","company_id","foreign"),("operation_outcome","key","other-key"),
    ("operation_outcome","status","refused"),("operation_outcome","status","pending"),
    ("approval","contract","reverse_supplier_payment"),("approval","contract","other")])
def test_only_this_company_exact_payment_committed_outcome_is_returned(table,field,value):
    w=world();w.record[table][field]=value
    assert payments.find_payment_by_key(w.ctx,"payment-key") is None
    assert len(w.conn.queries)==1


def test_missing_outcome_is_none_without_payment_row_fallback():
    w=world();w.conn.record=None
    assert payments.find_payment_by_key(w.ctx,"payment-key") is None
    assert len(w.conn.queries)==1


def test_optional_stored_result_fields_follow_existing_readback_convention():
    w=world({"text":"Stored words exactly."})
    assert payments.find_payment_by_key(w.ctx,"payment-key")==Result("Stored words exactly.")


def test_corrupt_stored_payload_is_not_relabelled_no_outcome():
    w=world();w.record["operation_outcome"]["result_json"]="broken"
    with pytest.raises(json.JSONDecodeError): payments.find_payment_by_key(w.ctx,"payment-key")


def test_no_public_payment_registration_is_added():
    assert "pay_supplier" not in core_contracts.MANIFEST.contracts
