"""PostgreSQL race proof: two blocked receipt submits split the discount.

Runs only on the private PostgreSQL lane
(`ERPCLAW_DB_DIALECT=postgresql` with `ERPCLAW_PG_TEST_URL` set); skipped
everywhere else. Provisioning/reset is shared with the rest of the buying
suite via ``buying_helpers._init_pg_schema`` (guarded: the reset refuses
any database outside the expendable-test pattern).

Shape: order line 3 x 10.00 with discount "1.00". Receipt A of 1 is
submitted; drafts X and Y of 1 each are created; a holder connection takes
the GL chain head; X and Y are submitted as subprocesses of the buying
module script (the in-process `call_action` helper patches process-wide
stdout, so two actions cannot run in-process at once). Each submit is alive
after 1 s; the holder rolls back; both exit 0 within 15 s. The re-derivation
at submit then splits the remainder across the serialised commits: X and Y
carry "0.33"/"0.34" in some order, all three sum to "1.00", the SLE
differences sum to "29.00" and the Stock GL nets to 29.00.
"""
import json
import os
import subprocess
import sys
import time
from decimal import Decimal

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from buying_helpers import (
    build_buying_env, call_action, is_ok, load_db_query, ns,
    _init_pg_schema,
)

from erpclaw_lib.db import get_connection, get_dialect

mod = load_db_query()

_TESTS_DIR_ABS = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(_TESTS_DIR_ABS))
_BUYING_SCRIPT = os.path.join(_SCRIPTS_DIR, "erpclaw-buying", "db_query.py")
_SETUP_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")

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


# Per-test PostgreSQL isolation (reset guard + schema provisioning) lives in
# ``buying_helpers`` so every buying test file shares the one guard; see the
# ``_init_pg_schema`` import above.


def _create_confirmed_po(conn, env):
    items = json.dumps([{"item_id": env["item1"], "qty": "3",
                         "rate": "10.00", "discount_amount": "1.00",
                         "warehouse_id": env["warehouse"]}])
    po = call_action(mod.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items,
        tax_template_id=None, name=None,
    ))
    assert is_ok(po), f"PO creation failed: {po}"
    submit = call_action(mod.submit_purchase_order, conn, ns(
        purchase_order_id=po["purchase_order_id"],
    ))
    assert is_ok(submit), f"PO submit failed: {submit}"
    return po["purchase_order_id"]


def _po_item_id(conn, po_id):
    row = conn.execute(
        "SELECT id FROM purchase_order_item WHERE purchase_order_id = ?",
        (po_id,)).fetchone()
    assert row is not None
    return row["id"]


def _make_draft(conn, env, po_id, po_item_id, qty):
    items = json.dumps([{"purchase_order_item_id": po_item_id,
                         "qty": qty}])
    res = call_action(mod.create_purchase_receipt, conn, ns(
        purchase_order_id=po_id, company_id=env["company_id"],
        posting_date="2026-06-20", items=items,
        purchase_receipt_id=None,
    ))
    assert is_ok(res), f"draft receipt failed: {res}"
    return res["purchase_receipt_id"]


@_PG_SKIP
def test_concurrent_receipt_discount_split():
    """Two submits blocked on the head serialise and split the discount."""
    _pg_only()
    from erpclaw_lib.gl_posting import take_chain_heads

    base_url = os.environ["ERPCLAW_PG_TEST_URL"]
    old_db_url = os.environ.get("ERPCLAW_DB_URL")
    os.environ["ERPCLAW_DB_URL"] = base_url
    conn = None
    holder = None
    try:
        _init_pg_schema()
        conn = get_connection()
        env = build_buying_env(conn)
        po_id = _create_confirmed_po(conn, env)
        poi = _po_item_id(conn, po_id)

        pr_a = _make_draft(conn, env, po_id, poi, "1")
        ok_a = call_action(mod.submit_purchase_receipt, conn, ns(
            purchase_receipt_id=pr_a,
        ))
        assert is_ok(ok_a), f"A submit failed: {ok_a}"

        pr_x = _make_draft(conn, env, po_id, poi, "1")
        pr_y = _make_draft(conn, env, po_id, poi, "1")

        holder = get_connection()
        take_chain_heads(holder, [env["company_id"]])

        penv = _proc_env(ERPCLAW_PG_LOCK_TIMEOUT="10s")
        sub_x = subprocess.Popen(
            [sys.executable, _BUYING_SCRIPT,
             "--action", "submit-purchase-receipt",
             "--purchase-receipt-id", pr_x],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            sub_x.wait(timeout=1.0)
            pytest.fail("submit X should block on the held chain head "
                        "(rc=%s)" % (sub_x.returncode,))
        except subprocess.TimeoutExpired:
            assert sub_x.poll() is None, "submit X must still be alive"

        sub_y = subprocess.Popen(
            [sys.executable, _BUYING_SCRIPT,
             "--action", "submit-purchase-receipt",
             "--purchase-receipt-id", pr_y],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=penv)
        try:
            sub_y.wait(timeout=1.0)
            pytest.fail("submit Y should block on the held chain head "
                        "(rc=%s)" % (sub_y.returncode,))
        except subprocess.TimeoutExpired:
            assert sub_y.poll() is None, "submit Y must still be alive"

        time.sleep(0.5)
        holder.rollback()
        out_x, err_x = sub_x.communicate(timeout=15)
        out_y, err_y = sub_y.communicate(timeout=15)
        assert sub_x.returncode == 0, (out_x, err_x)
        assert sub_y.returncode == 0, (out_y, err_y)

        disc_rows = conn.execute(
            "SELECT purchase_receipt_id, discount_amount "
            "FROM purchase_receipt_item "
            "WHERE purchase_receipt_id = ? OR purchase_receipt_id = ?",
            (pr_x, pr_y)).fetchall()
        discs = sorted(r["discount_amount"] for r in disc_rows)
        assert discs == ["0.33", "0.34"], discs

        all_rows = conn.execute(
            "SELECT discount_amount FROM purchase_receipt_item "
            "WHERE purchase_receipt_id = ? OR purchase_receipt_id = ? "
            "OR purchase_receipt_id = ?",
            (pr_a, pr_x, pr_y)).fetchall()
        total_disc = sum((Decimal(r["discount_amount"]) for r in all_rows),
                         Decimal("0"))
        assert str(total_disc) == "1.00"

        sle_rows = conn.execute(
            "SELECT stock_value_difference FROM stock_ledger_entry "
            "WHERE voucher_type = 'purchase_receipt' AND is_cancelled = 0 AND "
            "(voucher_id = ? OR voucher_id = ? OR voucher_id = ?)",
            (pr_a, pr_x, pr_y)).fetchall()
        total_diff = sum((Decimal(r["stock_value_difference"])
                          for r in sle_rows), Decimal("0"))
        assert str(total_diff) == "29.00"

        gl_rows = conn.execute(
            "SELECT debit, credit FROM gl_entry "
            "WHERE voucher_type = 'purchase_receipt' AND is_cancelled = 0 AND "
            "account_id = ? AND "
            "(voucher_id = ? OR voucher_id = ? OR voucher_id = ?)",
            (env["stock_acct"], pr_a, pr_x, pr_y)).fetchall()
        total_gl = sum((Decimal(r["debit"]) - Decimal(r["credit"])
                        for r in gl_rows), Decimal("0"))
        assert str(total_gl) == "29.00"
    finally:
        for handle in (holder, conn):
            if handle is not None:
                try:
                    handle.rollback()
                except Exception:
                    pass
                try:
                    handle.close()
                except Exception:
                    pass
        if old_db_url is None:
            os.environ.pop("ERPCLAW_DB_URL", None)
        else:
            os.environ["ERPCLAW_DB_URL"] = old_db_url
