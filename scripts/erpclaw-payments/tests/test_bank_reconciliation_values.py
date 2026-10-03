"""Behaviour of bank-reconciliation, read back against a fully known bank book.

bank-reconciliation takes a bank account and an inclusive date window and
reports three figures for that account: the number of live GL legs posted to
it, their net movement (debit minus credit, rounded to cents) and the number
of SUBMITTED payment entries that name it as paid-from or paid-to account.
It takes no statement balance and writes nothing.

Every document below is posted through its real action: payments through
erpclaw-payments (this directory) and one bank-charge journal entry through
the sibling erpclaw-journals domain, whose db_query.py is loaded by path.

Book (one company, two bank accounts, all dates fixed):
  PE-6  2026-02-28  submitted  receive  50.00 into Bank 1     (before window)
  PE-1  2026-03-05  submitted  receive 1250.00 into Bank 1
  PE-2  2026-03-12  CANCELLED  receive  480.75 into Bank 1
  PE-7  2026-03-15  submitted  receive  210.00 into Bank 2
  PE-3  2026-03-18  draft      receive   99.99 into Bank 1
  JE-1  2026-03-20  submitted  DR Commission 35.25 / CR Bank 1 35.25
  PE-8  2026-03-25  submitted  pay     400.40 from Bank 1 to Gotham Steel
  PE-4  2026-03-31  submitted  receive  300.10 into Bank 1  (last day of window)
  PE-5  2026-04-01  submitted  receive  700.00 into Bank 1  (after window)

Bank 1, window 2026-03-01..2026-03-31: live legs PE-1 DR 1250.00, JE-1 CR
35.25, PE-8 CR 400.40, PE-4 DR 300.10 -> 4 legs, debit 1550.10, credit 435.65,
net 1114.45; submitted payments PE-1, PE-8, PE-4 -> 3. PE-2's original leg and
its reversal are both marked is_cancelled = 1 and are not counted; PE-3 posted
nothing; PE-7 is on the other bank.
"""
import importlib.util
import json
import os
import uuid
from decimal import Decimal

import pytest
from payments_helpers import (build_ar_env, call_action, is_error, is_ok,
                              load_db_query, ns, seed_account)

PAY = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR))


def _load(name, domain):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_SCRIPTS_DIR, domain, "db_query.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


JR = _load("db_query_journals_m349", "erpclaw-journals")

WRITE_TABLES = ("gl_entry", "payment_entry", "payment_ledger_entry",
                "payment_allocation", "journal_entry", "audit_log")


def _counts(conn):
    return {t: conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
            for t in WRITE_TABLES}


def _payment(conn, env, date, payment_type, amount, bank, submit=True):
    if payment_type == "receive":
        party = ("customer", env["customer"], env["ar"], bank)
    else:
        party = ("supplier", env["supplier"], bank, env["ap"])
    r = call_action(PAY.add_payment, conn, ns(
        company_id=env["company_id"], payment_type=payment_type, posting_date=date,
        party_type=party[0], party_id=party[1], paid_from_account=party[2],
        paid_to_account=party[3], paid_amount=amount, exchange_rate=None,
        payment_currency=None, reference_number=None, reference_date=None,
        allocations=None, deductions=None))
    assert is_ok(r), r
    pe_id = r["payment_entry_id"]
    if submit:
        r = call_action(PAY.submit_payment, conn, ns(payment_entry_id=pe_id))
        assert is_ok(r), r
    return pe_id


@pytest.fixture
def book(conn):
    env = build_ar_env(conn)
    env["bank2"] = seed_account(conn, env["company_id"], "Payroll Bank", "asset", "bank")
    env["ap"] = seed_account(conn, env["company_id"], "Creditors", "liability")
    env["supplier"] = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO supplier (id, name, supplier_type, status, company_id) "
        "VALUES (?, 'Gotham Steel', 'company', 'active', ?)",
        (env["supplier"], env["company_id"]))
    conn.commit()
    bank = env["bank"]

    ids = {"env": env}
    ids["pe6"] = _payment(conn, env, "2026-02-28", "receive", "50.00", bank)
    ids["pe1"] = _payment(conn, env, "2026-03-05", "receive", "1250.00", bank)
    ids["pe2"] = _payment(conn, env, "2026-03-12", "receive", "480.75", bank)
    r = call_action(PAY.cancel_payment, conn, ns(payment_entry_id=ids["pe2"]))
    assert is_ok(r), r
    ids["pe7"] = _payment(conn, env, "2026-03-15", "receive", "210.00", env["bank2"])
    ids["pe3"] = _payment(conn, env, "2026-03-18", "receive", "99.99", bank, submit=False)

    lines = [{"account_id": env["commission"], "debit": "35.25", "credit": "0",
              "cost_center_id": env["cc"]},
             {"account_id": bank, "debit": "0", "credit": "35.25",
              "cost_center_id": env["cc"]}]
    r = call_action(JR.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date="2026-03-20", entry_type=None,
        remark="Bank charges", lines=json.dumps(lines), cwip_asset_id=None))
    assert is_ok(r), r
    ids["je1"] = r["journal_entry_id"]
    r = call_action(JR.submit_journal_entry, conn, ns(journal_entry_id=ids["je1"]))
    assert is_ok(r), r

    ids["pe8"] = _payment(conn, env, "2026-03-25", "pay", "400.40", bank)
    ids["pe4"] = _payment(conn, env, "2026-03-31", "receive", "300.10", bank)
    ids["pe5"] = _payment(conn, env, "2026-04-01", "receive", "700.00", bank)
    return ids


def _reconcile(conn, bank_account_id, from_date="2026-03-01", to_date="2026-03-31"):
    return call_action(PAY.bank_reconciliation, conn, ns(
        bank_account_id=bank_account_id, from_date=from_date, to_date=to_date))


def _bank_name(conn, account_id):
    return conn.execute("SELECT name FROM account WHERE id = ?",
                        (account_id,)).fetchone()["name"]


def test_bank_reconciliation_counts_live_legs_and_submitted_payments(conn, book):
    env = book["env"]
    before = _counts(conn)
    r = _reconcile(conn, env["bank"])
    assert is_ok(r), r
    assert (r["bank_account"], r["from_date"], r["to_date"]) == (
        _bank_name(conn, env["bank"]), "2026-03-01", "2026-03-31")
    assert r["gl_entries"] == 4
    assert r["gl_balance"] == "1114.45"
    assert r["payment_entries"] == 3
    assert _counts(conn) == before


def test_bank_reconciliation_book_figures_match_the_ledger_rows(conn, book):
    env = book["env"]
    rows = conn.execute(
        "SELECT voucher_type, voucher_id, debit, credit, is_cancelled FROM gl_entry "
        "WHERE account_id = ? AND posting_date >= '2026-03-01' "
        "AND posting_date <= '2026-03-31'", (env["bank"],)).fetchall()
    live = sorted((x["voucher_type"], x["voucher_id"], x["debit"], x["credit"])
                  for x in rows if x["is_cancelled"] == 0)
    assert live == sorted([
        ("payment_entry", book["pe1"], "1250.00", "0.00"),
        ("journal_entry", book["je1"], "0.00", "35.25"),
        ("payment_entry", book["pe8"], "0.00", "400.40"),
        ("payment_entry", book["pe4"], "300.10", "0.00"),
    ])
    cancelled = sorted((x["voucher_id"], x["debit"], x["credit"])
                       for x in rows if x["is_cancelled"] == 1)
    assert cancelled == [(book["pe2"], "0.00", "480.75"), (book["pe2"], "480.75", "0.00")]

    debit = sum((Decimal(x[2]) for x in live), Decimal("0"))
    credit = sum((Decimal(x[3]) for x in live), Decimal("0"))
    assert (str(debit), str(credit)) == ("1550.10", "435.65")
    r = _reconcile(conn, env["bank"])
    assert r["gl_balance"] == str(debit - credit) == "1114.45"


def test_bank_reconciliation_excludes_cancelled_and_draft_payments(conn, book):
    env = book["env"]
    statuses = dict(conn.execute(
        "SELECT id, status FROM payment_entry WHERE id IN (?, ?)",
        (book["pe2"], book["pe3"])).fetchall())
    assert statuses == {book["pe2"]: "cancelled", book["pe3"]: "draft"}
    r = _reconcile(conn, env["bank"], "2026-03-12", "2026-03-18")
    assert is_ok(r), r
    assert (r["gl_entries"], r["gl_balance"], r["payment_entries"]) == (0, "0.00", 0)


def test_bank_reconciliation_date_bounds_are_inclusive(conn, book):
    env = book["env"]
    r = _reconcile(conn, env["bank"], "2026-03-05", "2026-03-05")
    assert (r["gl_entries"], r["gl_balance"], r["payment_entries"]) == (1, "1250.00", 1)
    r = _reconcile(conn, env["bank"], "2026-03-25", "2026-03-31")
    assert (r["gl_entries"], r["gl_balance"], r["payment_entries"]) == (2, "-100.30", 2)
    r = _reconcile(conn, env["bank"], "2026-02-28", "2026-04-01")
    assert (r["gl_entries"], r["gl_balance"], r["payment_entries"]) == (6, "1864.45", 5)


def test_bank_reconciliation_is_scoped_to_the_named_account(conn, book):
    env = book["env"]
    r = _reconcile(conn, env["bank2"])
    assert is_ok(r), r
    assert r["bank_account"] == _bank_name(conn, env["bank2"])
    assert (r["gl_entries"], r["gl_balance"], r["payment_entries"]) == (1, "210.00", 1)


def test_bank_reconciliation_refusals_write_nothing(conn, book):
    env = book["env"]
    before = _counts(conn)
    cases = [
        (dict(bank_account_id=None, from_date="2026-03-01", to_date="2026-03-31"),
         "--bank-account-id is required"),
        (dict(bank_account_id=env["bank"], from_date=None, to_date="2026-03-31"),
         "--from-date is required"),
        (dict(bank_account_id=env["bank"], from_date="2026-03-01", to_date=None),
         "--to-date is required"),
        (dict(bank_account_id="no-such-account", from_date="2026-03-01",
              to_date="2026-03-31"),
         "Bank account no-such-account not found"),
    ]
    for args, message in cases:
        r = call_action(PAY.bank_reconciliation, conn, ns(**args))
        assert is_error(r), r
        assert r["message"] == message
        assert "gl_balance" not in r
    assert _counts(conn) == before
