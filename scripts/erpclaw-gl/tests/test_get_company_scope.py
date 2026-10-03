"""Company scope for get-account and get-account-balance (m795).

A read by id names its company already: with no company given the actions
behave exactly as today. When a company is given it must exist (via
``resolve_scope_company``) and the account must belong to it, else the read
is refused. The reads write nothing.
"""
import json
import uuid

from gl_helpers import call_action, is_error, is_ok, load_db_query, ns
from erpclaw_lib.query import Q, P, Table, insert_row

GL = load_db_query()

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


def _seed_two(conn):
    """Two companies, each with a Bank account (via add-account) plus a
    40.00 receipt-like posting into it (via post-gl-entries)."""
    acme = _mk_company(conn, "Acme Widgets", "ACME")
    wayne = _mk_company(conn, "Wayne Enterprises", "WAYNE")
    env = {"acme": acme, "wayne": wayne}
    for key, cid, bank_no, rev_no, voucher in (
            ("acme", acme, "1001", "4001", "JE-SEED-ACME"),
            ("wayne", wayne, "1002", "4002", "JE-SEED-WAYNE")):
        _insert(conn, "fiscal_year", id=_uuid(), name="FY-%s" % key,
                start_date="2026-01-01", end_date="2026-12-31",
                company_id=cid)
        cc = _uuid()
        _insert(conn, "cost_center", id=cc, name="Main CC",
                company_id=cid, is_group=0)
        bank_name = "Acme Bank" if key == "acme" else "Wayne Bank"
        bank = call_action(GL.add_account, conn, ns(
            name=bank_name, company_id=cid, root_type="asset",
            account_type="bank", account_number=bank_no, parent_id=None,
            currency=None, is_group=False))
        assert is_ok(bank), bank
        rev = call_action(GL.add_account, conn, ns(
            name="Sales", company_id=cid, root_type="income",
            account_type="revenue", account_number=rev_no, parent_id=None,
            currency=None, is_group=False))
        assert is_ok(rev), rev
        entries = json.dumps([
            {"account_id": bank["account_id"],
             "debit": "40.00", "credit": "0"},
            {"account_id": rev["account_id"],
             "debit": "0", "credit": "40.00", "cost_center_id": cc},
        ])
        post = call_action(GL.post_gl_entries, conn, ns(
            voucher_type="journal_entry", voucher_id=voucher,
            posting_date="2026-06-15", company_id=cid, entries=entries))
        assert is_ok(post), post
        env[key + "_bank"] = bank["account_id"]
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


def _get_account(conn, account_id, company_id=None, company_name=None):
    return call_action(GL.get_account, conn, ns(
        account_id=account_id, as_of_date=None,
        company_id=company_id, company_name=company_name))


def _get_balance(conn, account_id, as_of_date="2026-12-31",
                 company_id=None, company_name=None):
    return call_action(GL.get_account_balance_action, conn, ns(
        account_id=account_id, as_of_date=as_of_date,
        party_type=None, party_id=None,
        company_id=company_id, company_name=company_name))


def test_get_account_foreign_record_refuses(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get_account(conn, env["acme_bank"], company_id=env["wayne"])
    assert is_error(r)
    assert r["message"] == (
        "Account %s belongs to another company" % env["acme_bank"])
    assert _state(conn) == before


def test_get_account_unknown_company_refuses(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get_account(conn, env["acme_bank"], company_id="bogus")
    assert r == {"status": "error", "error": "Company not found: bogus",
                 "message": "Company not found: bogus"}
    assert _state(conn) == before


def test_get_account_own_company_unchanged(conn):
    env = _seed_two(conn)
    before = _state(conn)
    plain = _get_account(conn, env["acme_bank"])
    assert is_ok(plain), plain
    scoped = _get_account(conn, env["acme_bank"], company_id=env["acme"])
    assert is_ok(scoped), scoped
    assert scoped == plain
    assert _state(conn) == before


def test_get_account_company_name(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get_account(conn, env["acme_bank"],
                     company_name="Wayne Enterprises")
    assert is_error(r)
    assert r["message"] == (
        "Account %s belongs to another company" % env["acme_bank"])
    assert _state(conn) == before


def test_get_account_balance_foreign_record_refuses(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get_balance(conn, env["acme_bank"], company_id=env["wayne"])
    assert is_error(r)
    assert r["message"] == (
        "Account %s belongs to another company" % env["acme_bank"])
    assert _state(conn) == before


def test_get_account_balance_unknown_company_refuses(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get_balance(conn, env["acme_bank"], company_id="bogus")
    assert r == {"status": "error", "error": "Company not found: bogus",
                 "message": "Company not found: bogus"}
    assert _state(conn) == before


def test_get_account_balance_own_company_unchanged(conn):
    env = _seed_two(conn)
    before = _state(conn)
    plain = _get_balance(conn, env["acme_bank"])
    assert is_ok(plain), plain
    scoped = _get_balance(conn, env["acme_bank"], company_id=env["acme"])
    assert is_ok(scoped), scoped
    assert scoped == plain
    assert _state(conn) == before


def test_get_account_balance_company_name(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get_balance(conn, env["acme_bank"],
                     company_name="Wayne Enterprises")
    assert is_error(r)
    assert r["message"] == (
        "Account %s belongs to another company" % env["acme_bank"])
    assert _state(conn) == before


def test_get_account_balance_seeded_receipt(conn):
    env = _seed_two(conn)
    before = _state(conn)
    r = _get_balance(conn, env["wayne_bank"],
                     as_of_date="2026-12-31", company_id=env["wayne"])
    assert is_ok(r), r
    assert r["balance"] == "40.00"
    assert r["debit_total"] == "40.00"
    assert r["credit_total"] == "0.00"
    assert _state(conn) == before
