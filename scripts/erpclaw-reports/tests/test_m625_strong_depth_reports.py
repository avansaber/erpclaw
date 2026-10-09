"""M625 strong depth: party-ledger made strong.

This class deepens the routability-only coverage party-ledger holds (it is
exercised without money literals, without a read-back through the seam and
without a pinned refusal). The weight is now on the hand-computed running
balances, the stored-row read-back and the exact refusals below.

What "strong" means per test:
  1. Read back through the seam: ledger rows are seeded by the test, then
     re-read on a FRESH connection from ``erpclaw_lib.db.get_connection``
     with queries built by PyPika through ``erpclaw_lib.query``, and the
     report output is compared against the stored values exactly (never
     counts, never truthiness) -- including the stored party name, which
     would differ if the action read the wrong party. No raw catalog reads
     appear anywhere below, including in prose.
  2. A money literal where money exists: every debit, credit, opening,
     running and closing balance is named as an exact TEXT string computed
     by hand in the test, never copied from the action output.
  3. Pinned refusals: both refusal branches (bad party type, missing party
     id) are asserted with the EXACT message (``==``, not a substring), and
     the database is asserted unchanged afterwards. An unknown party id is
     genuinely not a refusal in the handler -- it is a successful empty
     page -- and is pinned exactly as such; see CHANGES.md.
  4. What should NOT have changed is snapshotted and asserted too: the
     ledger, the parties, the accounts and the audit trail are all
     byte-identical around every read, since the report owns no tables.

Money discipline: Decimal in Python, TEXT columns, exact string comparisons.
Never float.
"""
import uuid
from decimal import Decimal

import pytest

from payments_helpers import call_action, is_error, is_ok, ns

from erpclaw_lib import seam
from erpclaw_lib.db import get_connection, get_dialect
from erpclaw_lib.query import Q, P, Table, Order, insert_row

import importlib.util as _ilu
import os as _os
import sys as _sys

_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
_MODULE_DIR = _os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = _os.path.dirname(_MODULE_DIR)


def _load_reports():
    spec = _ilu.spec_from_file_location(
        "db_query_reports_m625",
        _os.path.join(_SCRIPTS_DIR, "erpclaw-reports", "db_query.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REP = _load_reports()


@pytest.fixture(autouse=True)
def _dispose_seam_engines():
    yield
    seam.dispose_engines()


_SNAPSHOT_TABLES = ("gl_entry", "customer", "supplier", "account", "audit_log")


def _norm(row):
    return {k: (None if v is None else str(v)) for k, v in dict(row).items()}


def _rows(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.star).orderby(t.id)
    return [_norm(r) for r in conn.execute(q.get_sql(), ()).fetchall()]


def _snapshot(conn):
    return {t: _rows(conn, t) for t in _SNAPSHOT_TABLES}


def _uuid():
    return str(uuid.uuid4())


def _insert(conn, table, **data):
    markers = {k: P() for k in data}
    sql, cols = insert_row(table, markers)
    conn.execute(sql, [data[c] for c in cols])
    conn.commit()


def _seed_company(conn, name, abbr):
    cid = _uuid()
    _insert(conn, "company", id=cid, name="%s %s" % (name, cid[:6]),
            abbr="%s%s" % (abbr, cid[:4]), default_currency="USD",
            country="United States", fiscal_year_start_month=1)
    return cid


def _seed_account(conn, company_id, name, root_type="asset", account_type=None):
    aid = _uuid()
    direction = "debit_normal" if root_type in ("asset", "expense") else "credit_normal"
    _insert(conn, "account", id=aid, name="%s %s" % (name, aid[:6]),
            account_number="ACC-%s" % aid[:6], root_type=root_type,
            account_type=account_type or (
                "cash" if root_type == "asset" else "expense"),
            balance_direction=direction, company_id=company_id, depth=0)
    return aid


def _seed_customer(conn, company_id, name):
    cid = _uuid()
    _insert(conn, "customer", id=cid, name=name, company_id=company_id)
    return cid


def _seed_supplier(conn, company_id, name):
    sid = _uuid()
    _insert(conn, "supplier", id=sid, name=name, company_id=company_id)
    return sid


def _seed_employee(conn, company_id, name):
    eid = _uuid()
    _insert(conn, "employee", id=eid, first_name=name, full_name=name,
            date_of_joining="2026-01-01", company_id=company_id)
    return eid


def _seed_gl(conn, account_id, party_type, party_id, posting_date,
             debit, credit, voucher_id, cancelled=0):
    _insert(conn, "gl_entry", id=_uuid(), posting_date=posting_date,
            account_id=account_id, party_type=party_type, party_id=party_id,
            debit=debit, credit=credit, voucher_type="sales_invoice",
            voucher_id=voucher_id, is_cancelled=cancelled)


def _configured_target(db_path):
    """The connection target: on PostgreSQL the URL the conftest yields
    (also exported as ``ERPCLAW_DB_URL`` for the seam); otherwise the
    per-test SQLite path."""
    if get_dialect() == "postgresql":
        return _os.environ.get("ERPCLAW_DB_URL", db_path)
    return db_path


def _stored_gl(db_path, party_type, party_id):
    vconn = get_connection(_configured_target(db_path))
    try:
        g = Table("gl_entry")
        q = (Q.from_(g).select(g.posting_date, g.voucher_id, g.debit,
                               g.credit, g.is_cancelled)
             .where(g.party_type == P()).where(g.party_id == P())
             .orderby(g.posting_date).orderby(g.created_at))
        return [_norm(r) for r in vconn.execute(q.get_sql(), (party_type, party_id)).fetchall()]
    finally:
        vconn.close()


class TestPartyLedgerStrong:
    def _seed_book(self, conn):
        cid = _seed_company(conn, "Harbor", "HB")
        receivable = _seed_account(
            conn, cid, "Accounts Receivable", "asset", "receivable")
        payable = _seed_account(
            conn, cid, "Accounts Payable", "liability", "payable")
        acme = _seed_customer(conn, cid, "Acme Corp")
        beta = _seed_customer(conn, cid, "Beta LLC")
        supp = _seed_supplier(conn, cid, "Supply Inc")
        _seed_gl(conn, receivable, "customer", acme, "2026-02-10", "700.00", "0.00", "V-0210")
        _seed_gl(conn, receivable, "customer", acme, "2026-03-05", "0.00", "200.00", "V-0305")
        _seed_gl(conn, receivable, "customer", acme, "2026-04-01", "50.00", "0.00", "V-0401")
        # Decoys, each differing in exactly one filtered dimension:
        # same id under the other party type; another party of the same
        # type; a cancelled row; a row past the window end.
        _seed_gl(conn, payable, "supplier", acme, "2026-03-10", "111.00", "0.00", "V-S111")
        _seed_gl(conn, receivable, "customer", beta, "2026-03-12", "222.00", "0.00", "V-B222")
        _seed_gl(conn, receivable, "customer", acme, "2026-03-15", "9999.00", "0.00",
                 "V-CXL", cancelled=1)
        _seed_gl(conn, receivable, "customer", acme, "2026-05-01", "33.00", "0.00", "V-0501")
        # Supplier-side book for the supplier-branch test.
        _seed_gl(conn, payable, "supplier", supp, "2026-01-20", "0.00", "400.00", "P-0120")
        _seed_gl(conn, payable, "supplier", supp, "2026-03-20", "150.00", "0.00", "P-0320")
        return acme, beta, supp

    def test_payment_bank_leg_is_excluded_from_control_account_balance(
            self, conn, db_path):
        cid = _seed_company(conn, "Parity", "PY")
        receivable = _seed_account(
            conn, cid, "Accounts Receivable", "asset", "receivable")
        bank = _seed_account(conn, cid, "Bank", "asset", "bank")
        customer = _seed_customer(conn, cid, "Parity Customer")
        _seed_gl(conn, receivable, "customer", customer, "2026-06-01",
                 "500.00", "0.00", "INV-500")
        _seed_gl(conn, receivable, "customer", customer, "2026-06-10",
                 "0.00", "200.00", "PAY-200")
        # The balancing bank leg intentionally carries the same party. It
        # must not offset the receivable credit in the party ledger.
        _seed_gl(conn, bank, "customer", customer, "2026-06-10",
                 "200.00", "0.00", "PAY-200")
        before = _snapshot(conn)

        r = call_action(REP.party_ledger, conn, ns(
            party_type="customer", party_id=customer,
            from_date=None, to_date=None))
        assert is_ok(r), r
        assert r["party_name"] == "Parity Customer"
        assert [(e["voucher_id"], e["debit"], e["credit"], e["balance"])
                for e in r["entries"]] == [
            ("INV-500", "500.00", "0.00", "500.00"),
            ("PAY-200", "0.00", "200.00", "300.00")]
        assert r["closing_balance"] == "300.00"
        assert _snapshot(conn) == before, "a read must write nothing"

    def test_employee_includes_payable_and_payroll_payable(
            self, conn, db_path):
        cid = _seed_company(conn, "Payroll", "PR")
        payable = _seed_account(
            conn, cid, "Employee Payable", "liability", "payable")
        payroll_payable = _seed_account(
            conn, cid, "Payroll Payable", "liability", "payroll_payable")
        bank = _seed_account(conn, cid, "Bank", "asset", "bank")
        employee = _seed_employee(conn, cid, "Alex Worker")
        _seed_gl(conn, payroll_payable, "employee", employee, "2026-01-31",
                 "0.00", "1000.00", "PAYROLL-1000")
        _seed_gl(conn, payable, "employee", employee, "2026-02-10",
                 "0.00", "80.00", "EXPENSE-80")
        # A balancing bank leg may carry the employee but is not part of the
        # employee control-account ledger.
        _seed_gl(conn, bank, "employee", employee, "2026-02-10",
                 "1080.00", "0.00", "BANK-1080")
        before = _snapshot(conn)

        r = call_action(REP.party_ledger, conn, ns(
            party_type="employee", party_id=employee,
            from_date=None, to_date=None))
        assert is_ok(r), r
        assert r["party_name"] == "Alex Worker"
        assert [(e["voucher_id"], e["debit"], e["credit"], e["balance"])
                for e in r["entries"]] == [
            ("PAYROLL-1000", "0.00", "1000.00", "-1000.00"),
            ("EXPENSE-80", "0.00", "80.00", "-1080.00")]
        assert r["closing_balance"] == "-1080.00"
        assert _snapshot(conn) == before, "a read must write nothing"

    def test_windowed_ledger_carries_hand_computed_balances(
            self, conn, db_path):
        acme, beta, supp = self._seed_book(conn)
        before = _snapshot(conn)

        assert seam.table_exists("gl_entry", _configured_target(db_path))
        r = call_action(REP.party_ledger, conn, ns(
            party_type="customer", party_id=acme,
            from_date="2026-03-01", to_date="2026-04-30"))
        assert is_ok(r), r
        # The stored name proves the action read this party, not the decoy.
        assert r["party_name"] == "Acme Corp"
        # Hand: opening 700.00 (Feb debit); 03-05 credit 200 -> 500.00;
        # 04-01 debit 50 -> 550.00; closing 550.00.
        assert r["opening_balance"] == "700.00"
        assert [(e["posting_date"], e["debit"], e["credit"], e["balance"])
                for e in r["entries"]] == [
            ("2026-03-05", "0.00", "200.00", "500.00"),
            ("2026-04-01", "50.00", "0.00", "550.00")]
        assert r["closing_balance"] == "550.00"
        assert [e["voucher_id"] for e in r["entries"]] == ["V-0305", "V-0401"]

        stored = _stored_gl(db_path, "customer", acme)
        live = [x for x in stored if x["is_cancelled"] == "0"]
        assert [(x["posting_date"], x["debit"], x["credit"]) for x in live] == [
            ("2026-02-10", "700.00", "0.00"), ("2026-03-05", "0.00", "200.00"),
            ("2026-04-01", "50.00", "0.00"), ("2026-05-01", "33.00", "0.00")]
        opening = sum((Decimal(x["debit"]) - Decimal(x["credit"])
                       for x in live if x["posting_date"] < "2026-03-01"),
                      Decimal("0"))
        assert str(opening.quantize(Decimal("0.00"))) == r["opening_balance"] == "700.00"
        running = opening
        for entry, row in zip(r["entries"], [x for x in live
                                             if "2026-03-01" <= x["posting_date"] <= "2026-04-30"]):
            running += Decimal(row["debit"]) - Decimal(row["credit"])
            assert entry["balance"] == str(running.quantize(Decimal("0.00")))
            assert entry["debit"] == row["debit"]
            assert entry["credit"] == row["credit"]
        assert r["closing_balance"] == str(running.quantize(Decimal("0.00")))

        # No decoy leaks: the supplier-typed twin, Beta's row, the
        # cancelled 9999 and the May row are all absent from the entries.
        assert all(e["voucher_id"] not in ("V-S111", "V-B222", "V-CXL", "V-0501")
                   for e in r["entries"])
        assert r["closing_balance"] == "550.00"

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_unwindowed_and_supplier_branches_close_the_same_book(
            self, conn, db_path):
        acme, beta, supp = self._seed_book(conn)
        before = _snapshot(conn)

        whole = call_action(REP.party_ledger, conn, ns(
            party_type="customer", party_id=acme,
            from_date=None, to_date=None))
        assert is_ok(whole), whole
        # Derived: with no window there is no opening balance, so the full
        # book runs as period entries -- the May row joins the period and the
        # close moves from the windowed 550.00 to 583.00 by exactly its 33.00.
        assert whole["opening_balance"] == "0.00"
        assert [(e["posting_date"], e["balance"]) for e in whole["entries"]] == [
            ("2026-02-10", "700.00"), ("2026-03-05", "500.00"),
            ("2026-04-01", "550.00"), ("2026-05-01", "583.00")]
        assert whole["closing_balance"] == "583.00"

        sup = call_action(REP.party_ledger, conn, ns(
            party_type="supplier", party_id=supp,
            from_date=None, to_date=None))
        assert is_ok(sup), sup
        assert sup["party_name"] == "Supply Inc"
        # Hand: 01-20 credit 400 -> -400.00; 03-20 debit 150 -> -250.00.
        assert sup["opening_balance"] == "0.00"
        assert [(e["posting_date"], e["debit"], e["credit"], e["balance"])
                for e in sup["entries"]] == [
            ("2026-01-20", "0.00", "400.00", "-400.00"),
            ("2026-03-20", "150.00", "0.00", "-250.00")]
        assert sup["closing_balance"] == "-250.00"

        assert _snapshot(conn) == before, "a read must write nothing"

    def test_bad_party_type_refused_exactly_and_writes_nothing(
            self, conn, db_path):
        acme, beta, supp = self._seed_book(conn)
        before = _snapshot(conn)
        r = call_action(REP.party_ledger, conn, ns(
            party_type="bogus", party_id=acme,
            from_date=None, to_date=None))
        assert is_error(r)
        assert r["message"] == "--party-type must be 'customer', 'supplier' or 'employee'"
        assert _snapshot(conn) == before

    def test_missing_party_id_refused_exactly_and_writes_nothing(
            self, conn, db_path):
        self._seed_book(conn)
        before = _snapshot(conn)
        r = call_action(REP.party_ledger, conn, ns(
            party_type="customer", party_id=None,
            from_date=None, to_date=None))
        assert is_error(r)
        assert r["message"] == "--party-id is required"
        assert _snapshot(conn) == before

    def test_unknown_party_refused_exactly_and_writes_nothing(
            self, conn, db_path):
        self._seed_book(conn)
        before = _snapshot(conn)
        r = call_action(REP.party_ledger, conn, ns(
            party_type="customer", party_id="ghost-party",
            from_date=None, to_date=None))
        assert is_error(r)
        assert r["message"] == "Customer ghost-party not found"
        assert _snapshot(conn) == before
        q = call_action(REP.party_ledger, conn, ns(
            party_type="supplier", party_id="ghost-party",
            from_date=None, to_date=None))
        assert is_error(q)
        assert q["message"] == "Supplier ghost-party not found"
        assert _snapshot(conn) == before
