"""Tests for set-advance-account (S2 phase 1) + the payment B1-vocabulary aliases."""
import argparse
import pytest
from setup_helpers import call_action, seed_company, is_ok, is_error, load_db_query

mod = load_db_query()


def _ns(**kw):
    base = dict(company_id=None, account_id=None, type=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _acct(conn, company_id, root_type, is_group=0, name="Adv"):
    import uuid
    aid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO account (id, name, root_type, account_type, is_group, company_id) "
        "VALUES (?, ?, ?, NULL, ?, ?)", (aid, f"{name}-{aid[:6]}", root_type, is_group, company_id))
    conn.commit()
    return aid


class TestSetAdvanceAccount:
    def test_customer_requires_liability(self, conn):
        cid = seed_company(conn)
        liab = _acct(conn, cid, "liability")
        r = call_action(mod.set_advance_account, conn, _ns(company_id=cid, account_id=liab, type="customer"))
        assert is_ok(r) and r["column"] == "advance_from_customer_account_id"
        got = conn.execute("SELECT advance_from_customer_account_id FROM company WHERE id=?", (cid,)).fetchone()[0]
        assert got == liab

    def test_supplier_requires_asset(self, conn):
        cid = seed_company(conn)
        asset = _acct(conn, cid, "asset")
        r = call_action(mod.set_advance_account, conn, _ns(company_id=cid, account_id=asset, type="supplier"))
        assert is_ok(r) and r["column"] == "advance_to_supplier_account_id"

    def test_wrong_root_type_rejected(self, conn):
        cid = seed_company(conn)
        asset = _acct(conn, cid, "asset")
        # customer advance needs liability, not asset
        assert is_error(call_action(mod.set_advance_account, conn,
                        _ns(company_id=cid, account_id=asset, type="customer")))

    def test_group_account_rejected(self, conn):
        cid = seed_company(conn)
        grp = _acct(conn, cid, "liability", is_group=1)
        assert is_error(call_action(mod.set_advance_account, conn,
                        _ns(company_id=cid, account_id=grp, type="customer")))

    def test_bad_type_and_missing_args(self, conn):
        cid = seed_company(conn)
        liab = _acct(conn, cid, "liability")
        assert is_error(call_action(mod.set_advance_account, conn, _ns(company_id=cid, account_id=liab, type="bogus")))
        assert is_error(call_action(mod.set_advance_account, conn, _ns(company_id=cid, type="customer")))


def test_payment_b1_aliases_route_to_existing_actions():
    """The S2 aliases must map to the existing advance-lifecycle functions."""
    import importlib.util, os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "..", "erpclaw-payments", "db_query.py")
    spec = importlib.util.spec_from_file_location("pay_dq", p)
    pay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pay)
    assert pay.ACTIONS["list-open-advances"] is pay.ACTIONS["get-unallocated-payments"]
    assert pay.ACTIONS["apply-advance-to-invoice"] is pay.ACTIONS["allocate-payment"]
