"""Part A — VALUE tests for stock-ledger reversal (Wave G F21, re-homed by M103).

These pins were originally written against `reverse-stock-ledger-entries`, an
action M103 de-routed on 2026-08-13: it wrote (and reversed) stock-ledger rows
with no balancing GL leg, had zero production callers, and could never acquire
one, because the foundation router dispatches with `os.execvp` and a handler
inside a transaction cannot call a routed action without abandoning it.

The behaviour those pins protect is real and still shipping — it just lives one
level down. `erpclaw_lib.stock_posting.reverse_sle_entries` is what selling,
buying, manufacturing and this module's own `cancel-stock-entry` all import and
call inside their transaction. So the file now drives two live things:

* the sanctioned action path, `add-stock-entry` -> `submit-stock-entry` ->
  `cancel-stock-entry`, which is what a user actually reaches; and
* the primitive directly, for the refusals the action path guards earlier (a
  second cancel is stopped by the document status before the ledger is asked).

The immutable-ledger rule is what these pins protect: cancelling stock must
APPEND a mirror row and flag the original, never edit or delete a posted row.
"""
import json
from decimal import Decimal

import pytest
from inventory_helpers import (call_action, is_error, is_ok, load_db_query, ns,
                               _uuid)

inv = load_db_query()

from erpclaw_lib.stock_posting import reverse_sle_entries  # noqa: E402

D = Decimal
RECEIPT_DATE = "2026-03-01"


def _msg(result: dict) -> str:
    return result.get("message", "") + result.get("error", "")


@pytest.fixture
def env(conn):
    from inventory_helpers import build_inventory_env
    return build_inventory_env(conn)


def _receive(conn, env, qty="40", rate="25.00", item_key="item2"):
    """Drive the sanctioned path to a SUBMITTED receipt; return its id."""
    items = json.dumps([{"item_id": env[item_key], "qty": qty, "rate": rate,
                         "to_warehouse_id": env["warehouse"]}])
    draft = call_action(inv.add_stock_entry, conn, ns(
        entry_type="receive", company_id=env["company_id"],
        posting_date=RECEIPT_DATE, items=items))
    assert is_ok(draft), draft
    se_id = draft["stock_entry_id"]

    submitted = call_action(inv.submit_stock_entry, conn, ns(stock_entry_id=se_id))
    assert is_ok(submitted), submitted
    assert submitted["sle_entries_created"] == 1
    return se_id


def _sle(conn, voucher_id):
    return conn.execute(
        "SELECT id, actual_qty, qty_after_transaction, stock_value, "
        "stock_value_difference, is_cancelled, posting_date, valuation_rate "
        "FROM stock_ledger_entry WHERE voucher_id = ? "
        "ORDER BY is_cancelled, created_at", (voucher_id,)).fetchall()


def _live_qty(conn, env, item_key="item2"):
    row = conn.execute(
        "SELECT COALESCE(decimal_sum(actual_qty), '0') FROM stock_ledger_entry "
        "WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0",
        (env[item_key], env["warehouse"])).fetchone()
    return D(row[0])


def test_cancel_appends_a_mirror_row_and_flags_the_original(conn, env):
    se_id = _receive(conn, env, qty="40", rate="25.00")
    assert _live_qty(conn, env) == D("40")

    res = call_action(inv.cancel_stock_entry, conn, ns(stock_entry_id=se_id))
    assert is_ok(res), res
    assert res["sle_reversals"] == 1

    rows = _sle(conn, se_id)
    assert len(rows) == 2, "the original must survive; a mirror is appended"
    original, mirror = rows[0], rows[1]

    # Immutable ledger: the original's amounts are untouched, only its flag moved.
    assert D(original["actual_qty"]) == D("40")
    assert D(original["stock_value_difference"]) == D("1000.00")
    assert original["is_cancelled"] == 1
    assert original["posting_date"] == RECEIPT_DATE

    # The mirror negates quantity AND value, and is itself flagged (audit row).
    assert D(mirror["actual_qty"]) == D("-40")
    assert D(mirror["stock_value_difference"]) == D("-1000.00")
    assert D(mirror["qty_after_transaction"]) == D("0")
    assert D(mirror["stock_value"]) == D("0")
    assert mirror["is_cancelled"] == 1
    assert mirror["posting_date"] == RECEIPT_DATE

    # Net live stock for the item is back where it started.
    assert _live_qty(conn, env) == D("0")


def test_cancel_reverses_the_gl_leg_too(conn, env):
    """The reason M103 removed the raw gateway: on the path that remains, the
    stock ledger and the books move together. Reversing one without the other is
    what INV-24 fails on."""
    se_id = _receive(conn, env, qty="8", rate="12.50")

    def _gl_net():
        row = conn.execute(
            "SELECT COALESCE(decimal_sum(g.debit), '0'), COALESCE(decimal_sum(g.credit), '0') "
            "FROM gl_entry g JOIN account a ON g.account_id = a.id "
            "WHERE a.account_type = 'stock' AND g.voucher_id = ?", (se_id,)).fetchone()
        return D(row[0]) - D(row[1])

    def _sle_net():
        row = conn.execute(
            "SELECT COALESCE(decimal_sum(stock_value_difference), '0') "
            "FROM stock_ledger_entry WHERE voucher_id = ?", (se_id,)).fetchone()
        return D(row[0])

    assert _gl_net() == D("100.00") == _sle_net(), "submit posts both legs"

    res = call_action(inv.cancel_stock_entry, conn, ns(stock_entry_id=se_id))
    assert is_ok(res), res
    assert res["gl_reversals"] >= 1, res

    # Both sides return to zero together — the tie-out INV-24 asserts globally.
    assert _gl_net() == D("0") == _sle_net()


def test_cancelling_twice_is_refused_and_adds_no_third_row(conn, env):
    se_id = _receive(conn, env, qty="7", rate="3.00")
    assert is_ok(call_action(inv.cancel_stock_entry, conn, ns(stock_entry_id=se_id)))

    again = call_action(inv.cancel_stock_entry, conn, ns(stock_entry_id=se_id))
    assert is_error(again)
    assert "must be 'submitted'" in _msg(again)
    assert len(_sle(conn, se_id)) == 2, "the refusal must not append a row"


def test_primitive_refuses_a_second_reversal_of_the_same_voucher(conn, env):
    """The document-status guard above stops the second cancel before the ledger
    is reached, so the ledger's OWN double-reversal refusal needs driving
    directly. This is the guard selling/buying/manufacturing rely on."""
    se_id = _receive(conn, env, qty="5", rate="9.00")
    assert is_ok(call_action(inv.cancel_stock_entry, conn, ns(stock_entry_id=se_id)))

    before = conn.execute("SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0]
    with pytest.raises(ValueError, match="No active SLE entries"):
        reverse_sle_entries(conn, voucher_type="stock_entry", voucher_id=se_id,
                            posting_date=RECEIPT_DATE)
    assert conn.execute(
        "SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0] == before


def test_primitive_refuses_an_unknown_voucher_without_writing(conn, env):
    before = conn.execute("SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0]
    with pytest.raises(ValueError, match="No active SLE entries"):
        reverse_sle_entries(conn, voucher_type="stock_entry",
                            voucher_id="no-such-voucher",
                            posting_date=RECEIPT_DATE)
    assert conn.execute(
        "SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0] == before


def test_cancel_requires_its_document_id(conn, env):
    bad = call_action(inv.cancel_stock_entry, conn, ns(stock_entry_id=None))
    assert is_error(bad)
    assert "--stock-entry-id is required" in _msg(bad)


def test_cancel_refuses_an_unknown_document_without_writing(conn, env):
    before = conn.execute("SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0]
    bad = call_action(inv.cancel_stock_entry, conn, ns(stock_entry_id=_uuid()))
    assert is_error(bad)
    assert "not found" in _msg(bad)
    assert conn.execute(
        "SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0] == before
