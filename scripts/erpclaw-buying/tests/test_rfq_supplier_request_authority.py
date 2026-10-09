"""A company-B principal cannot read company-A supplier preparations."""
import importlib.util
import sys
from pathlib import Path

import pytest

from test_rfq_supplier_requests import (BUYING, prepare, rfq, snapshot)
from buying_helpers import call_action, ns, seed_company
from erpclaw_lib import actor, company_scope


def test_company_b_principal_cannot_read_company_a_preparation_or_audit(rfq, monkeypatch):
    conn, home, env = rfq
    assert prepare(conn, env)["status"] == "ok"
    other = seed_company(conn, name="Company B requester")
    setup_tests = Path(__file__).resolve().parents[2] / "erpclaw-setup" / "tests"
    sys.path.insert(0, str(setup_tests))
    try:
        import authority_fixtures as fixtures
        fixtures.seed_authority(str(home / "data.sqlite"), other)
        fixtures.make_active(str(home / "data.sqlite"))
    finally:
        sys.path.remove(str(setup_tests))
    attested = actor.ActorContext(None, None, fixtures.SERVICE, (), actor.ATTESTED)
    monkeypatch.setattr(actor, "current", lambda: attested)
    before = snapshot(conn)
    for action in ("list-rfq-supplier-requests", "get-audit-log"):
        with pytest.raises(company_scope.ScopeRefused):
            company_scope.gate_note(conn, action,
                                    ["--company-id", env["company_id"]], "ACTIVE")
    with pytest.raises(company_scope.ScopeRefused):
        company_scope.gate_note(conn, "get-system-audit-log", [], "ACTIVE")
    assert company_scope.gate_note(conn, "list-rfq-supplier-requests",
                                  ["--company-id", other], "ACTIVE").status == "in_scope"
    result = call_action(BUYING.list_rfq_supplier_requests, conn,
                         ns(company_id=other, rfq_id=env["rfq_id"]))
    assert result == {"status": "error", "message": "RFQ not found in the selected company"}
    spec = importlib.util.spec_from_file_location(
        "setup_rfq_authority", setup_tests.parent / "db_query.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    audit = call_action(setup.get_audit_log, conn, ns(
        company_id=other, entity_type=None, entity_id=None, audit_action=None,
        from_date=None, to_date=None, limit=None))
    assert audit["entries"] == []
    assert snapshot(conn) == before
