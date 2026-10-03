"""Company scope for get-payment (m795).

A read by id names its company already: with no company given the action
behaves exactly as today. When a company is given it must exist (via
``resolve_scope_company``) and the entry must belong to it, else the read
is refused. The read writes nothing.
"""
import json
import uuid

from payments_helpers import call_action, is_error, is_ok, load_db_query, ns
from erpclaw_lib.query import Q, P, Table, insert_row

PAY = load_db_query()

STATE_TABLES = ("account", "journal_entry", "journal_entry_line",
                "payment_entry", "gl_entry", "payment_ledger_entry",
                "audit_log")


def _uuid():
    return str(uuid.uuid4())


def _insert(conn, table, **data):
    markers = {k: P() for k in data}
    sql, cols = insert_row(table, markers)
    conn.execute(sql, [data[c] for c in cols])
    conn.commit()


def _mk_company(conn, name, abbr):
    cid = _uuid()
    _insert(conn, "company", id=cid, name=name, abbr=abbr,
            default_currency="USD", country="United States",
            fiscal_year_start_month=1)
    return cid


def _mk_account(conn, company_id, name, root_type, account_type, number):
    aid = _uuid()
    direction = ("debit_normal" if root_type in ("asset", "expense")
                 else "credit_normal")
    _insert(conn, "account", id=aid, name=name, account_number=number,
            root_type=root_type, account_type=account_type,
            balance_direction=direction, company_id=company_id, depth=0)
    return aid


def _seed_two(conn):
    """Two companies, each with Bank/Receivable/Customer and one submitted
    40.00 receipt (via add/submit-payment)."""
    acme = _mk_company(conn, "Acme Widgets", "ACME")
    wayne = _mk_company(conn, "Wayne Enterprises", "WAYNE")
    env = {"acme": acme, "wayne": wayne}
    for key, cid, bank_no, ar_no, cust_name in (
            ("acme", acme, "1001", "1101", "Acme Customer"),
            ("wayne", wayne, "1002", "1102", "Wayne Customer")):
        _insert(conn, "fiscal_year", id=_uuid(), name="FY-%s" % key,
                start_date="2026-01-01", end_date="2026-12-31",
                is_closed=0, company_id=cid)
        cc = _uuid()
        _insert(conn, "cost_center", id=cc, name="Main CC",
                company_id=cid, is_group=0)
        conn.execute("UPDATE company SET default_cost_center_id = ? "
                     "WHERE id = ?", (cc, cid))
        conn.commit()
        bank_name = "Acme Bank" if key == "acme" else "Wayne Bank"
        bank = _mk_account(conn, cid, bank_name, "asset", "bank", bank_no)
        ar = _mk_account(conn, cid, "Debtors", "asset", "receivable", ar_no)
        cust = _uuid()
        _insert(conn, "customer", id=cust, name=cust_name,
                customer_type="company", status="active", company_id=cid)
        add = call_action(PAY.add_payment, conn, ns(
            company_id=cid, payment_type="receive",
            posting_date="2026-06-01", party_type="customer",
            party_id=cust, paid_from_account=ar,
            paid_to_account=bank, paid_amount="40.00",
            exchange_rate=None, payment_currency=None,
            reference_number=None, reference_date=None,
            allocations=None, deductions=None))
        assert is_ok(add), add
        sub = call_action(PAY.submit_payment, conn, ns(
            payment_entry_id=add["payment_entry_id"]))
        assert is_ok(sub), sub
        env[key + "_pe"] = add["payment_entry_id"]
    return env


def _state(conn):
    out = {}
    for table in STATE_TABLES:
        t = Table(table)
        q = Q.from_(t).select(t.star)
        rows = conn.execute(q.get_sql(), ()).fetchall()
        out[table] = sorted(
            json.dumps({k: (None if v is None else str(v))
                        for k, v in dict(r).items()}, sort_keys=True)
            for r in rows)
    return out


def _get(conn, pe_id, company_id=None, company_name=None):
    return call_action(PAY.get_payment, conn, ns(
        payment_entry_id=pe_id,
        company_id=company_id, company_name=company_name))


def test_get_payment_foreign_record_refuses(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get(conn, env["acme_pe"], company_id=env["wayne"])
    assert is_error(r)
    assert r["message"] == (
        "Payment entry %s belongs to another company" % env["acme_pe"])
    assert _state(conn) == before


def test_get_payment_unknown_company_refuses(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get(conn, env["acme_pe"], company_id="bogus")
    assert r == {"status": "error", "error": "Company not found: bogus",
                 "message": "Company not found: bogus"}
    assert _state(conn) == before


def test_get_payment_own_company_unchanged(conn):
    env = _seed_two(conn)
    before = _state(conn)
    plain = _get(conn, env["acme_pe"])
    assert is_ok(plain), plain
    scoped = _get(conn, env["acme_pe"], company_id=env["acme"])
    assert is_ok(scoped), scoped
    assert scoped == plain
    assert _state(conn) == before


def test_get_payment_company_name(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get(conn, env["acme_pe"], company_name="Wayne Enterprises")
    assert is_error(r)
    assert r["message"] == (
        "Payment entry %s belongs to another company" % env["acme_pe"])
    assert _state(conn) == before
