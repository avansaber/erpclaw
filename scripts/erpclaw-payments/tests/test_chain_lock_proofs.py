"""Chain-head lock-order and busy-loser proofs (task m332c).

Puts the /tmp proofs from the previous returns into the tree as rerunnable
tests. No product code changes here; every leg drives the real actions.

The concurrently-run actions execute as subprocesses of the module script
(the in-process ``call_action`` helper patches process-wide stdout, so two
actions cannot run in-process at once). Every ``wait``/``join`` is bounded;
``poll() is None`` after ``wait(timeout=...)`` raising ``TimeoutExpired`` is
the only liveness check. Every connection is opened before any thread start.
Money is exact text; reads go through ``erpclaw_lib.query`` builders on
connections from ``erpclaw_lib.db.get_connection``.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from payments_helpers import (build_ar_env, call_action, get_conn, is_ok,  # noqa: E402
                              load_db_query, ns, seed_account,
                              seed_sales_invoice)

from erpclaw_lib.db import get_connection, get_dialect  # noqa: E402
from erpclaw_lib.gl_posting import insert_gl_entries  # noqa: E402
from erpclaw_lib.query import P, Q, Table  # noqa: E402

mod = load_db_query()

_TESTS_DIR_ABS = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR_ABS))
_PAYMENTS_SCRIPT = os.path.join(_SCRIPTS_DIR, "erpclaw-payments", "db_query.py")
_SETUP_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")
_GL_SCRIPT = os.path.join(_SCRIPTS_DIR, "erpclaw-gl", "db_query.py")

LOCK_MESSAGE = ("Another transaction is using a record this action needs; "
                "nothing was written. Retry the action.")

_PG_SKIP = pytest.mark.skipif(
    not os.environ.get("ERPCLAW_PG_TEST_URL"),
    reason="needs ERPCLAW_PG_TEST_URL (private PostgreSQL cluster)",
)



def _pg_only():
    if get_dialect() != "postgresql":
        pytest.skip("PostgreSQL-only case: needs the ERPCLAW_PG_TEST_URL lane")


def _proc_env(**overrides):
    env = dict(os.environ)
    env["PYTHONPATH"] = (_SETUP_LIB + os.pathsep + env.get("PYTHONPATH", ""))
    env.update(overrides)
    return env


def _load_gl():
    spec = importlib.util.spec_from_file_location("db_query_gl_proofs",
                                                  _GL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_invoice(conn, env, grand_total="1000.00"):
    fy_name = conn.execute(
        "SELECT name FROM fiscal_year WHERE company_id = ?",
        (env["company_id"],)).fetchone()[0]
    si = seed_sales_invoice(conn, env, grand_total)
    insert_gl_entries(
        conn,
        [{"account_id": env["ar"], "debit": grand_total, "credit": "0",
          "party_type": "customer", "party_id": env["customer"],
          "fiscal_year": fy_name},
         {"account_id": env["income"], "debit": "0", "credit": grand_total,
          "cost_center_id": env["cc"], "fiscal_year": fy_name}],
        voucher_type="sales_invoice", voucher_id=si,
        posting_date="2026-06-01", company_id=env["company_id"],
        remarks="sales_invoice %s" % si)
    conn.commit()
    return si


def _draft_payment(conn, env, voucher_id, amount):
    created = call_action(
        mod.add_payment, conn, ns(
            company_id=env["company_id"], payment_type="receive",
            posting_date="2026-06-01", party_type="customer",
            party_id=env["customer"], paid_from_account=env["ar"],
            paid_to_account=env["bank"], paid_amount=amount,
            exchange_rate=None, payment_currency=None,
            reference_number=None, reference_date=None,
            allocations=json.dumps(
                [{"voucher_type": "sales_invoice", "voucher_id": voucher_id,
                  "allocated_amount": amount}]),
            deductions=None))
    assert is_ok(created), created
    conn.commit()
    return created["payment_entry_id"]


def _draft_ar_env(conn):
    env = build_ar_env(conn)
    env["income"] = seed_account(conn, env["company_id"], "Sales", "income")
    env["bad_debt"] = seed_account(conn, env["company_id"],
                                   "Bad Debt Expense", "expense")
    return env


def _outstanding_raw(conn, doc_id):
    return conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (doc_id,)).fetchone()[0]


def _voucher_row_counts(conn, voucher_type, voucher_id):
    g = Table("gl_entry")
    p = Table("payment_ledger_entry")
    gl_n = conn.execute(
        Q.from_(g).select(g.id)
        .where(g.voucher_type == P()).where(g.voucher_id == P())
        .get_sql(), (voucher_type, voucher_id)).fetchall()
    ple_n = conn.execute(
        Q.from_(p).select(p.id)
        .where(p.voucher_type == P()).where(p.voucher_id == P())
        .get_sql(), (voucher_type, voucher_id)).fetchall()
    return len(gl_n), len(ple_n)


def _assert_contiguous(conn, company_id):
    g = Table("gl_entry").as_("g")
    a = Table("account").as_("a")
    q = (Q.from_(g).join(a).on(g.account_id == a.id)
         .select(g.sequence)
         .where(a.company_id == P())
         .where(g.gl_checksum.isnotnull())
         .where(g.sequence.isnotnull())
         .orderby(g.sequence))
    seqs = [r["sequence"]
            for r in conn.execute(q.get_sql(), (company_id,)).fetchall()]
    assert seqs == list(range(1, len(seqs) + 1)), seqs
    return len(seqs)


def _assert_chain_intact(conn, company_id):
    gl = _load_gl()
    result = call_action(gl.check_gl_integrity, conn,
                         ns(company_id=company_id, company_name=None))
    assert is_ok(result), result
    assert result["chain_intact"] is True, result
    assert result["broken_links"] == 0, result


def _head(conn, company_id):
    t = Table("gl_chain_head")
    q = Q.from_(t).select(t.star).where(t.company_id == P())
    row = conn.execute(q.get_sql(), (company_id,)).fetchone()
    return dict(row) if row else None


# ── 3. lock order: ledger first on both actions, no deadlock ──

@_PG_SKIP
def test_write_off_and_submit_share_ledger_first_lock_order(db_path):
    """Submit holds the head while blocked; write-off waits on the head.

    Invoice 1000.00, payment 600.00 allocated to it, write-off 400.00 on it.
    A scripted connection makes a no-op update of the payment entry's own row
    and holds it, so ``submit-payment`` blocks after its ledger posting (it
    holds the head). ``write-off-invoice`` (ledger first) blocks on the head.
    After the rollback both exit 0 within 12 s, the invoice is fully cleared,
    the chain is intact and sequences are contiguous.
    """
    _pg_only()
    conn = get_connection()
    holder = get_connection()
    try:
        env = _draft_ar_env(conn)
        si = _seed_invoice(conn, env)
        pe_id = _draft_payment(conn, env, si, "600.00")

        holder.execute("UPDATE payment_entry SET status = status WHERE id = ?",
                       (pe_id,))

        penv = _proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        submit = subprocess.Popen(
            [sys.executable, _PAYMENTS_SCRIPT,
             "--action", "submit-payment", "--payment-entry-id", pe_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            submit.wait(timeout=1.0)
            pytest.fail("submit-payment should block on the held payment row"
                        " (rc=%s out=%s err=%s)"
                        % (submit.returncode, submit.stdout,
                           submit.stderr))
        except subprocess.TimeoutExpired:
            assert submit.poll() is None, "submit-payment must still be alive"

        write_off = subprocess.Popen(
            [sys.executable, _PAYMENTS_SCRIPT,
             "--action", "write-off-invoice",
             "--voucher-type", "sales_invoice", "--voucher-id", si,
             "--write-off-amount", "400.00",
             "--write-off-account-id", env["bad_debt"],
             "--reason", "lock order probe",
             "--posting-date", "2026-06-15"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            write_off.wait(timeout=1.0)
            pytest.fail("write-off-invoice should block on the held head"
                        " (rc=%s out=%s err=%s)"
                        % (write_off.returncode, write_off.stdout,
                           write_off.stderr))
        except subprocess.TimeoutExpired:
            assert write_off.poll() is None, \
                "write-off-invoice must still be alive"

        time.sleep(1.5)
        holder.rollback()
        out_s, err_s = submit.communicate(timeout=12)
        out_w, err_w = write_off.communicate(timeout=12)
        assert submit.returncode == 0, (out_s, err_s)
        assert write_off.returncode == 0, (out_w, err_w)
        # Canonical zero is the bare "0" the clearing lib stores, not "0.00".
        assert _outstanding_raw(conn, si) == "0"
        # Invoice pair + payment pair + write-off pair, one chain 1..6.
        assert _assert_contiguous(conn, env["company_id"]) == 6
        _assert_chain_intact(conn, env["company_id"])
    finally:
        try:
            holder.rollback()
        except Exception:  # noqa: BLE001, S110 - best effort
            pass
        holder.close()
        conn.close()


# ── 4. busy loser: exact message, nothing written, retry succeeds ──

@_PG_SKIP
def test_busy_loser_reports_retry_and_writes_nothing(db_path):
    """A head held elsewhere turns ``submit-payment`` into a clean retry.

    A scripted connection takes the company's head with ``take_chain_heads``
    and holds it; ``submit-payment`` with a 500 ms lock timeout exits 1 with
    exactly one JSON object carrying the retry message, and nothing is
    written (payment still draft, no ledger or payment ledger rows for it,
    head unchanged after the scripted rollback). Submitting again succeeds.
    """
    _pg_only()
    from erpclaw_lib.gl_posting import take_chain_heads  # noqa: E402
    conn = get_connection()
    holder = get_connection()
    try:
        env = _draft_ar_env(conn)
        si = _seed_invoice(conn, env)
        pe_id = _draft_payment(conn, env, si, "600.00")
        head_before = _head(conn, env["company_id"])
        assert head_before is not None

        take_chain_heads(holder, [env["company_id"]])

        penv = _proc_env(ERPCLAW_PG_LOCK_TIMEOUT="500ms")
        lost = subprocess.run(
            [sys.executable, _PAYMENTS_SCRIPT,
             "--action", "submit-payment", "--payment-entry-id", pe_id],
            capture_output=True, text=True, timeout=30, env=penv)
        assert lost.returncode == 1, (lost.stdout, lost.stderr)
        assert json.loads(lost.stdout) == {"status": "error",
                                           "message": LOCK_MESSAGE}
        assert conn.execute(
            "SELECT status FROM payment_entry WHERE id = ?",
            (pe_id,)).fetchone()[0] == "draft"
        assert _voucher_row_counts(conn, "payment_entry", pe_id) == (0, 0)
        holder.rollback()
        assert _head(conn, env["company_id"]) == head_before

        won = subprocess.run(
            [sys.executable, _PAYMENTS_SCRIPT,
             "--action", "submit-payment", "--payment-entry-id", pe_id],
            capture_output=True, text=True, timeout=30, env=penv)
        assert won.returncode == 0, (won.stdout, won.stderr)
        assert _outstanding_raw(conn, si) == "400.00"
    finally:
        try:
            holder.rollback()
        except Exception:  # noqa: BLE001, S110 - best effort
            pass
        holder.close()
        conn.close()


# ── SQLite leg of the same branch ──

def test_busy_loser_sqlite_reports_retry_and_writes_nothing(db_path):
    """SQLite: a held write lock turns ``submit-payment`` into a clean retry.

    A second connection holds ``BEGIN IMMEDIATE`` while the action runs as a
    subprocess of the module script. It exits 1 with stdout exactly one JSON
    object carrying the retry message, and nothing is written (the payment is
    still draft, with no ledger or payment ledger rows for it).
    """
    if get_dialect() != "sqlite":
        pytest.skip("SQLite-only case: the held write lock is SQLite's")
    conn = get_conn(db_path)
    holder = get_conn(db_path)
    try:
        env = _draft_ar_env(conn)
        si = _seed_invoice(conn, env)
        pe_id = _draft_payment(conn, env, si, "600.00")

        holder.execute("BEGIN IMMEDIATE")

        proc = subprocess.run(
            [sys.executable, _PAYMENTS_SCRIPT,
             "--action", "submit-payment", "--payment-entry-id", pe_id],
            capture_output=True, text=True, timeout=30,
            env=_proc_env())
        assert proc.returncode == 1, (proc.stdout, proc.stderr)
        assert json.loads(proc.stdout) == {"status": "error",
                                           "message": LOCK_MESSAGE}
        assert conn.execute(
            "SELECT status FROM payment_entry WHERE id = ?",
            (pe_id,)).fetchone()[0] == "draft"
        assert _voucher_row_counts(conn, "payment_entry", pe_id) == (0, 0)
    finally:
        try:
            holder.rollback()
        except Exception:  # noqa: BLE001, S110 - best effort
            pass
        holder.close()
        conn.close()
