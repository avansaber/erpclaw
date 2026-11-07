"""Restricted-role real-auth lost-success recovery on a disposable PostgreSQL cluster."""
import os
from datetime import date, datetime, timedelta, timezone
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from erpclaw_api.app import create_app
from erpclaw_assistant.model import ScriptedModel
from erpclaw_cli import journal, main
from erpclaw_core import administration, approvals, ledger, models as m, principals
from erpclaw_core.common import company_ctx
from erpclaw_core.db import make_engine
from erpclaw_core.masters import create_party
from erpclaw_sales import tables as sales_tables
from erpclaw_core.plugins import REGISTRY

pytestmark = pytest.mark.skipif(
    os.environ.get("ERPCLAW_M3_RECEIPT_TEST") != "1",
    reason="requires explicit opt-in to the dedicated receipt PostgreSQL runner",
)

def test_committed_lost_response_then_revoked_view_keeps_receipt_and_recovers(
    engine, runtime_login, monkeypatch, tmp_path
):
    admin = engine
    # This integration harness runs only on dedicated loopback test books.
    assert (admin.url.host, admin.url.port, admin.url.database, admin.url.username) == (
        "127.0.0.1", 55465, "m3_t5_receipt_test", "m3_t5_receipt_owner"
    )
    with admin.connect() as conn:
        assert tuple(conn.exec_driver_sql(
            "SELECT current_database(),current_user,inet_server_addr() IS NOT NULL"
        ).one()) == ("m3_t5_receipt_test", "m3_t5_receipt_owner", True)
    home = tmp_path / "app_home"
    assert home.resolve().is_relative_to(tmp_path.resolve()) and not home.exists()
    monkeypatch.setenv("ERPCLAW_HOME", str(home))
    with admin.begin() as conn:
        company=company_ctx(conn).company_id
        token=administration.add_principal(conn,"Journal Alice",["view","draft","approve","post"],[company])
    runtime=make_engine(runtime_login)
    try:
        client=TestClient(create_app(runtime,ScriptedModel([])))
        with runtime.connect() as conn:
            principal=principals.authenticate(conn,token)
        card=approvals.propose(runtime,REGISTRY.contracts["create_invoice"],
            {"customer":"Acme Corporation","items":[{"item":"Copper Wire","quantity":2}]},
            principal=principal,company_id=company)
        assert card.status=="card"
        class LostThenRevoked:
            def __init__(self): self.replies=[]
            def post(self,path,**kwargs):
                response=client.post(path,headers={"Authorization":"Bearer "+token},**kwargs)
                self.replies.append(response.status_code)
                if len(self.replies)==1:
                    assert response.status_code==200
                    assert any(e.get("type")=="result" and e.get("status")=="committed"
                               for e in response.json()["events"])
                    with admin.begin() as conn: administration.revoke(conn,"Journal Alice",["view"],company)
                    raise httpx.ReadTimeout("synthetic lost successful reply")
                return response
        adapter=LostThenRevoked()
        monkeypatch.setattr(main,"con",main.Console(record=True,width=240))
        monkeypatch.setattr(main,"_ask",lambda prompt:"yes")
        folder=home/"cli_receipts"
        assert folder.resolve().is_relative_to(tmp_path.resolve()) and not folder.exists()
        with journal.Journal(folder,"synthetic-books") as j:
            main.approve(adapter,j,"synthetic-conversation",
                         {"approval_id":card.approval_id,"digest":card.digest})
            output=main.con.export_text()
            assert adapter.replies==[200,403]
            assert "may or may not have been saved" in output
            assert len(list(folder.glob("*.json")))==1
            with admin.connect() as conn:
                assert conn.execute(select(m.approval.c.status).where(m.approval.c.id==card.approval_id)).scalar_one()=="consumed"
                count=conn.execute(select(func.count()).select_from(m.operation_outcome).where(
                    m.operation_outcome.c.approval_id==card.approval_id)).scalar_one()
                assert count==1
            with admin.begin() as conn: administration.grant(conn,"Journal Alice",["view"],company)
            main.recover(adapter,j)
            assert adapter.replies==[200,403,200]
            assert list(folder.glob("*.json"))==[]
            with admin.connect() as conn:
                assert conn.execute(select(func.count()).select_from(m.operation_outcome).where(
                    m.operation_outcome.c.approval_id==card.approval_id)).scalar_one()==1
        # Exercise actual 200 outcome shapes after replay, without claiming
        # that every API event or stale-decision path has been tested.
        monkeypatch.setattr(main,"_card_answer",lambda:None)
        def new_invoice_card(customer="Acme Corporation"):
            with runtime.connect() as conn:
                who=principals.authenticate(conn,token)
            return approvals.propose(runtime,REGISTRY.contracts["create_invoice"],
                {"customer":customer,"items":[{"item":"Copper Wire","quantity":2}]},
                principal=who,company_id=company)
        def show_stored_response(old,expected_kind,label,expected_status=None):
            path=f"/v1/cards/{old.approval_id}/approve"
            response=client.post(path,headers={"Authorization":"Bearer "+token},json={"digest":old.digest})
            assert response.status_code==200
            assert any(e.get("type")==expected_kind for e in response.json()["events"]),response.json()
            if expected_status is not None:
                assert any(e.get("status")==expected_status for e in response.json()["events"]),response.json()
            events=main._approval_outcome_events(response)
            assert events is not None,response.json()
            receipt=home/label
            assert receipt.resolve().is_relative_to(tmp_path.resolve()) and not receipt.exists()
            with journal.Journal(receipt,"synthetic-books") as record:
                entry=record.publish(old.approval_id,old.digest,"synthetic-conversation")
                main._answered(client,record,entry,response)
                assert not entry.path.exists()
        expired=new_invoice_card()
        with admin.begin() as conn:
            conn.execute(m.approval.update().where(m.approval.c.id==expired.approval_id)
                         .values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        show_stored_response(expired,"result","expired_receipt","expired")
        declined=new_invoice_card()
        with runtime.connect() as conn:
            who=principals.authenticate(conn,token)
        assert approvals.decline(runtime,declined.approval_id,principal=who).status=="declined"
        show_stored_response(declined,"result","declined_receipt","declined")
        invalidated=new_invoice_card()
        with admin.begin() as conn: administration.revoke(conn,"Journal Alice",["draft"],company)
        show_stored_response(invalidated,"result","invalidated_receipt","invalidated")
        with admin.begin() as conn: administration.grant(conn,"Journal Alice",["draft"],company)
        stale=new_invoice_card()
        with admin.begin() as conn:
            conn.execute(m.item.update().where(m.item.c.company_id==company,m.item.c.name=="Copper Wire")
                         .values(standard_rate="29.00"))
        stale_first=client.post(f"/v1/cards/{stale.approval_id}/approve",
                    headers={"Authorization":"Bearer "+token},json={"digest":stale.digest})
        assert stale_first.status_code==200 and any(e.get("type")=="card" for e in stale_first.json()["events"])
        show_stored_response(stale,"card","stale_receipt")
        question=new_invoice_card()
        with admin.begin() as conn:
            conn.execute(m.party.update().where(m.party.c.company_id==company,m.party.c.name=="Acme Corporation")
                         .values(name="Acme Corporation North"))
            create_party(company_ctx(conn),"customer","Acme Corporation South")
        question_first=client.post(f"/v1/cards/{question.approval_id}/approve",
                    headers={"Authorization":"Bearer "+token},json={"digest":question.digest})
        assert question_first.status_code==200 and any(e.get("type")=="ask" for e in question_first.json()["events"])
        show_stored_response(question,"ask","question_receipt")
        with runtime.connect() as conn:
            who=principals.authenticate(conn,token)
        with admin.connect() as conn:
            number=conn.execute(select(sales_tables.invoice.c.number).where(
                sales_tables.invoice.c.company_id==company,sales_tables.invoice.c.status=="draft")).scalars().first()
        assert number
        refusal=approvals.propose(runtime,REGISTRY.contracts["submit_invoice"],{"invoice":number},
                                   principal=who,company_id=company)
        with admin.begin() as conn: ledger.lock_through(company_ctx(conn),date(2026,10,31))
        refused_first=client.post(f"/v1/cards/{refusal.approval_id}/approve",
                    headers={"Authorization":"Bearer "+token},json={"digest":refusal.digest})
        assert refused_first.status_code==200 and any(e.get("type")=="refused" for e in refused_first.json()["events"])
        show_stored_response(refusal,"refused","refusal_receipt")
    finally:
        runtime.dispose()
