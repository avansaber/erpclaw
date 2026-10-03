"""Part A — behaviour of create-stock-ledger-entries, reverse-stock-ledger-entries
and apply-putaway-on-receipt, read back from the database.

The two stock-ledger gateways are retired: each answers with one JSON error that
steers to the stock-entry flow. Their behaviour is therefore "refuse, and leave
both ledgers exactly as they were". These tests post a real receipt through
add-stock-entry -> submit-stock-entry (stock ledger AND general ledger), then pin
every stock_ledger_entry row, every gl_entry leg by account and the resulting
stock balance as exact strings, call the gateway, and require all of it to be
unchanged. The reversal case is also driven after the receipt was cancelled, so
the mirrored rows (netting to zero, originals flagged is_cancelled = 1) are pinned
and must survive a second reversal request untouched.

apply-putaway-on-receipt is a read-only routing plan: per received line it names
the warehouse the active putaway rules route it to. It is pinned by value (route
per item, qty as a two-decimal string, needs_transfer, routed_count) and by its
refusals, and it must write nothing to either ledger.

All dates are fixed; rows whose order depends on UUIDs are compared as sorted
lists or keyed by item.
"""
import json
from decimal import Decimal

from inventory_helpers import (call_action, is_error, is_ok, load_db_query, ns,
                               seed_item, _uuid)

inv = load_db_query()

RECEIPT_DATE = "2026-03-01"

RETIRED_MESSAGE = (
    "has been retired. It wrote stock-ledger rows without a balancing "
    "general-ledger leg, leaving the stock ledger untied to the books (INV-24)."
)


# ── helpers ────────────────────────────────────────────────────────────────

def _receive(conn, env, lines, posting_date=RECEIPT_DATE):
    """add-stock-entry (receive) -> submit-stock-entry; return the entry id."""
    draft = call_action(inv.add_stock_entry, conn, ns(
        entry_type="receive", company_id=env["company_id"],
        posting_date=posting_date, items=json.dumps(lines)))
    assert is_ok(draft), draft
    se_id = draft["stock_entry_id"]
    submitted = call_action(inv.submit_stock_entry, conn, ns(stock_entry_id=se_id))
    assert is_ok(submitted), submitted
    return se_id


def _standard_receipt(conn, env):
    """40 x 25.00 of item2 and 12 x 50.00 of item1, both into the main warehouse."""
    return _receive(conn, env, [
        {"item_id": env["item2"], "qty": "40", "rate": "25.00",
         "to_warehouse_id": env["warehouse"]},
        {"item_id": env["item1"], "qty": "12", "rate": "50.00",
         "to_warehouse_id": env["warehouse"]},
    ])


def _sle_rows(conn, env, voucher_id):
    """Every stock-ledger row of a voucher, item named by env key, sorted."""
    names = {env["item1"]: "item1", env["item2"]: "item2"}
    rows = conn.execute(
        "SELECT item_id, warehouse_id, actual_qty, valuation_rate, stock_value, "
        "stock_value_difference, qty_after_transaction, voucher_type, is_cancelled "
        "FROM stock_ledger_entry WHERE voucher_id = ?", (voucher_id,)).fetchall()
    return sorted(
        (names[r["item_id"]], r["warehouse_id"] == env["warehouse"], r["actual_qty"],
         r["valuation_rate"], r["stock_value"], r["stock_value_difference"],
         r["qty_after_transaction"], r["voucher_type"], r["is_cancelled"])
        for r in rows)


def _gl_legs(conn, env, voucher_id):
    """Every general-ledger leg of a voucher, account named by env key, sorted."""
    names = {env["stock_acct"]: "stock_acct", env["srnb"]: "srnb"}
    rows = conn.execute(
        "SELECT account_id, debit, credit, voucher_type, is_cancelled "
        "FROM gl_entry WHERE voucher_id = ?", (voucher_id,)).fetchall()
    return sorted((names.get(r["account_id"], r["account_id"]), r["debit"],
                   r["credit"], r["voucher_type"], r["is_cancelled"]) for r in rows)


def _ledger_counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0],
    )


def _balance(conn, item_id, warehouse_id):
    r = call_action(inv.get_stock_balance_action, conn,
                    ns(item_id=item_id, warehouse_id=warehouse_id))
    assert is_ok(r), r
    return (r["qty"], r["valuation_rate"], r["stock_value"])


ORIGINAL_SLE = [
    ("item1", True, "12.00", "50.00", "5600.00", "600.00", "112.00", "stock_entry", 0),
    ("item2", True, "40.00", "25.00", "1000.00", "1000.00", "40.00", "stock_entry", 0),
]

ORIGINAL_GL = [
    ("srnb", "0.00", "1000.00", "stock_entry", 0),
    ("srnb", "0.00", "600.00", "stock_entry", 0),
    ("stock_acct", "1000.00", "0.00", "stock_entry", 0),
    ("stock_acct", "600.00", "0.00", "stock_entry", 0),
]


def _legacy_create_args(env, se_id):
    """The old create-stock-ledger-entries invocation: +5 of item2 at 25.00."""
    return ns(voucher_type="stock_entry", voucher_id=se_id,
              posting_date="2026-03-05", company_id=env["company_id"],
              entries=json.dumps([{"item_id": env["item2"],
                                   "warehouse_id": env["warehouse"],
                                   "actual_qty": "5", "incoming_rate": "25.00"}]))


# ── create-stock-ledger-entries ───────────────────────────────────────────

def test_create_stock_ledger_entries_refuses_and_leaves_both_ledgers_exact(conn, env):
    assert inv.ACTIONS["create-stock-ledger-entries"] is inv.create_stock_ledger_entries
    se_id = _standard_receipt(conn, env)

    # The receipt posted exactly these rows and legs.
    assert _sle_rows(conn, env, se_id) == ORIGINAL_SLE
    assert _gl_legs(conn, env, se_id) == ORIGINAL_GL
    assert _balance(conn, env["item2"], env["warehouse"]) == ("40.00", "25.00", "1000.00")
    assert _balance(conn, env["item1"], env["warehouse"]) == ("112.00", "50.00", "5600.00")
    before = _ledger_counts(conn)

    r = call_action(inv.create_stock_ledger_entries, conn, _legacy_create_args(env, se_id))
    conn.commit()

    assert is_error(r), r
    assert r["message"] == f"'create-stock-ledger-entries' {RETIRED_MESSAGE}"
    assert "add-stock-entry -> submit-stock-entry" in r["suggestion"]

    # Nothing landed: no +5 row, no GL leg, balance unchanged to the cent.
    assert _ledger_counts(conn) == before
    assert _sle_rows(conn, env, se_id) == ORIGINAL_SLE
    assert _gl_legs(conn, env, se_id) == ORIGINAL_GL
    assert _balance(conn, env["item2"], env["warehouse"]) == ("40.00", "25.00", "1000.00")
    assert conn.execute(
        "SELECT COUNT(*) FROM stock_ledger_entry WHERE posting_date = '2026-03-05'"
    ).fetchone()[0] == 0


# ── reverse-stock-ledger-entries ──────────────────────────────────────────

def test_reverse_stock_ledger_entries_refuses_on_a_live_voucher(conn, env):
    assert inv.ACTIONS["reverse-stock-ledger-entries"] is inv.reverse_stock_ledger_entries
    se_id = _standard_receipt(conn, env)
    before = _ledger_counts(conn)

    r = call_action(inv.reverse_stock_ledger_entries, conn, ns(
        voucher_type="stock_entry", voucher_id=se_id, posting_date=RECEIPT_DATE))
    conn.commit()

    assert is_error(r), r
    assert r["message"] == f"'reverse-stock-ledger-entries' {RETIRED_MESSAGE}"
    assert "cancel-stock-entry" in r["suggestion"]

    # No mirror rows were appended and the originals were not flagged.
    assert _ledger_counts(conn) == before
    assert _sle_rows(conn, env, se_id) == ORIGINAL_SLE
    assert _gl_legs(conn, env, se_id) == ORIGINAL_GL
    assert conn.execute("SELECT status FROM stock_entry WHERE id = ?",
                        (se_id,)).fetchone()["status"] == "submitted"
    assert _balance(conn, env["item2"], env["warehouse"]) == ("40.00", "25.00", "1000.00")
    assert _balance(conn, env["item1"], env["warehouse"]) == ("112.00", "50.00", "5600.00")


def test_reverse_stock_ledger_entries_refuses_an_already_reversed_voucher(conn, env):
    se_id = _standard_receipt(conn, env)
    cancelled = call_action(inv.cancel_stock_entry, conn, ns(stock_entry_id=se_id))
    assert is_ok(cancelled), cancelled
    assert (cancelled["sle_reversals"], cancelled["gl_reversals"]) == (2, 4)

    # The sanctioned reversal: originals flagged, mirrors appended and flagged.
    reversed_sle = [
        ("item1", True, "-12.00", "50.00", "5000.00", "-600.00", "100.00", "stock_entry", 1),
        ("item1", True, "12.00", "50.00", "5600.00", "600.00", "112.00", "stock_entry", 1),
        ("item2", True, "-40.00", "0.00", "0.00", "-1000.00", "0.00", "stock_entry", 1),
        ("item2", True, "40.00", "25.00", "1000.00", "1000.00", "40.00", "stock_entry", 1),
    ]
    reversed_gl = [
        ("srnb", "0.00", "1000.00", "stock_entry", 1),
        ("srnb", "0.00", "600.00", "stock_entry", 1),
        ("srnb", "1000.00", "0.00", "stock_entry", 1),
        ("srnb", "600.00", "0.00", "stock_entry", 1),
        ("stock_acct", "0.00", "1000.00", "stock_entry", 1),
        ("stock_acct", "0.00", "600.00", "stock_entry", 1),
        ("stock_acct", "1000.00", "0.00", "stock_entry", 1),
        ("stock_acct", "600.00", "0.00", "stock_entry", 1),
    ]
    assert _sle_rows(conn, env, se_id) == reversed_sle
    assert _gl_legs(conn, env, se_id) == reversed_gl

    # Mirrors net to zero per item and per account.
    for item in ("item1", "item2"):
        rows = [r for r in reversed_sle if r[0] == item]
        assert sum((Decimal(r[2]) for r in rows), Decimal("0")) == Decimal("0")
        assert sum((Decimal(r[5]) for r in rows), Decimal("0")) == Decimal("0")
    for acct in ("stock_acct", "srnb"):
        legs = [g for g in reversed_gl if g[0] == acct]
        net = sum((Decimal(g[1]) - Decimal(g[2]) for g in legs), Decimal("0"))
        assert net == Decimal("0")
    assert _balance(conn, env["item2"], env["warehouse"]) == ("0.00", "0.00", "0.00")
    assert _balance(conn, env["item1"], env["warehouse"]) == ("100.00", "50.00", "5000.00")
    before = _ledger_counts(conn)

    r = call_action(inv.reverse_stock_ledger_entries, conn, ns(
        voucher_type="stock_entry", voucher_id=se_id, posting_date=RECEIPT_DATE))
    conn.commit()

    assert is_error(r), r
    assert r["message"] == f"'reverse-stock-ledger-entries' {RETIRED_MESSAGE}"
    assert _ledger_counts(conn) == before
    assert _sle_rows(conn, env, se_id) == reversed_sle
    assert _gl_legs(conn, env, se_id) == reversed_gl
    assert _balance(conn, env["item2"], env["warehouse"]) == ("0.00", "0.00", "0.00")


def test_reverse_stock_ledger_entries_refuses_an_unknown_voucher(conn, env):
    before = _ledger_counts(conn)
    r = call_action(inv.ACTIONS["reverse-stock-ledger-entries"], conn, ns(
        voucher_type="stock_entry", voucher_id=_uuid(), posting_date=RECEIPT_DATE))
    conn.commit()
    assert is_error(r), r
    assert r["message"] == f"'reverse-stock-ledger-entries' {RETIRED_MESSAGE}"
    assert _ledger_counts(conn) == before
    # The opening 100 x 50.00 of item1 is still the only live stock.
    assert _balance(conn, env["item1"], env["warehouse"]) == ("100.00", "50.00", "5000.00")


# ── apply-putaway-on-receipt ──────────────────────────────────────────────

def _rule(conn, env, name, target_key, priority, match_item_id=None, match_item_group=None):
    r = call_action(inv.add_putaway_rule, conn, ns(
        name=name, target_warehouse_id=env[target_key], priority=priority,
        match_item_id=match_item_id, match_item_group=match_item_group,
        company_id=env["company_id"], company_name=None))
    assert is_ok(r), r
    return r["putaway_rule_id"]


def test_apply_putaway_on_receipt_routes_each_line_and_writes_nothing(conn, env):
    assert inv.ACTIONS["apply-putaway-on-receipt"] is inv.apply_putaway_on_receipt
    # item1 belongs to item group "Widgets"; item3 has only a deactivated rule.
    grp = call_action(inv.add_item_group, conn, ns(
        name="Widgets", company_id=env["company_id"], parent_id=None))
    assert is_ok(grp), grp
    conn.execute("UPDATE item SET item_group_id = ? WHERE id = ?",
                 (grp["item_group_id"], env["item1"]))
    item3 = seed_item(conn, "Widget C", "Each", "stock", "8.00")
    conn.commit()

    # item2: an item rule wins over a group rule of better priority.
    _rule(conn, env, "item2-to-secondary", "warehouse2", 50, match_item_id=env["item2"])
    # item1: two group rules; priority 10 beats 90, and it routes to where it landed.
    _rule(conn, env, "widgets-main", "warehouse", 10, match_item_group="Widgets")
    _rule(conn, env, "widgets-secondary", "warehouse2", 90, match_item_group="Widgets")
    # item3: its only rule is deactivated, so it does not route.
    dead = _rule(conn, env, "item3-dead", "warehouse2", 1, match_item_id=item3)
    assert is_ok(call_action(inv.delete_putaway_rule, conn, ns(id=dead)))

    se_id = _receive(conn, env, [
        {"item_id": env["item2"], "qty": "40", "rate": "25.00",
         "to_warehouse_id": env["warehouse"]},
        {"item_id": env["item1"], "qty": "12.5", "rate": "50.00",
         "to_warehouse_id": env["warehouse"]},
        {"item_id": item3, "qty": "3", "rate": "8.00",
         "to_warehouse_id": env["warehouse"]},
    ])
    sle_before = conn.execute(
        "SELECT id, actual_qty, stock_value_difference, is_cancelled "
        "FROM stock_ledger_entry ORDER BY id").fetchall()
    gl_before = conn.execute(
        "SELECT id, debit, credit, is_cancelled FROM gl_entry ORDER BY id").fetchall()
    assert len(sle_before) == 4 and len(gl_before) == 6

    r = call_action(inv.apply_putaway_on_receipt, conn, ns(stock_entry_id=se_id))
    conn.commit()

    assert is_ok(r), r
    assert r["stock_entry_id"] == se_id
    assert r["routed_count"] == 2
    routes = {x["item_id"]: x for x in r["routes"]}
    assert set(routes) == {env["item1"], env["item2"], item3}
    assert routes[env["item2"]] == {
        "item_id": env["item2"], "received_warehouse_id": env["warehouse"],
        "target_warehouse_id": env["warehouse2"], "needs_transfer": True,
        "qty": "40.00"}
    assert routes[env["item1"]] == {
        "item_id": env["item1"], "received_warehouse_id": env["warehouse"],
        "target_warehouse_id": env["warehouse"], "needs_transfer": False,
        "qty": "12.50"}
    assert routes[item3] == {
        "item_id": item3, "received_warehouse_id": env["warehouse"],
        "target_warehouse_id": None, "needs_transfer": False, "qty": "3.00"}

    # A routing plan, not a move: both ledgers are byte-identical and the stock
    # still sits where the receipt put it.
    after_sle = conn.execute(
        "SELECT id, actual_qty, stock_value_difference, is_cancelled "
        "FROM stock_ledger_entry ORDER BY id").fetchall()
    after_gl = conn.execute(
        "SELECT id, debit, credit, is_cancelled FROM gl_entry ORDER BY id").fetchall()
    assert [tuple(x) for x in after_sle] == [tuple(x) for x in sle_before]
    assert [tuple(x) for x in after_gl] == [tuple(x) for x in gl_before]
    assert _balance(conn, env["item2"], env["warehouse"]) == ("40.00", "25.00", "1000.00")
    assert _balance(conn, env["item2"], env["warehouse2"]) == ("0.00", "0.00", "0.00")
    assert _balance(conn, env["item1"], env["warehouse"]) == ("112.50", "50.00", "5625.00")
    assert conn.execute("SELECT status FROM stock_entry WHERE id = ?",
                        (se_id,)).fetchone()["status"] == "submitted"


def test_apply_putaway_on_receipt_refusals_write_nothing(conn, env):
    before = _ledger_counts(conn)

    missing = call_action(inv.apply_putaway_on_receipt, conn, ns(stock_entry_id=None))
    assert is_error(missing)
    assert missing["message"] == "--stock-entry SE is required"

    ghost = _uuid()
    unknown = call_action(inv.apply_putaway_on_receipt, conn, ns(stock_entry_id=ghost))
    assert is_error(unknown)
    assert unknown["message"] == f"Stock entry {ghost} not found"

    issue = call_action(inv.add_stock_entry, conn, ns(
        entry_type="issue", company_id=env["company_id"], posting_date="2026-03-02",
        items=json.dumps([{"item_id": env["item1"], "qty": "5",
                           "from_warehouse_id": env["warehouse"]}])))
    assert is_ok(issue), issue
    assert issue["total_outgoing_value"] == "250.00"
    wrong = call_action(inv.apply_putaway_on_receipt, conn,
                        ns(stock_entry_id=issue["stock_entry_id"]))
    assert is_error(wrong)
    assert wrong["message"] == (
        "Putaway applies to material_receipt only (entry is 'material_issue')")

    conn.commit()
    assert _ledger_counts(conn) == before
    assert _balance(conn, env["item1"], env["warehouse"]) == ("100.00", "50.00", "5000.00")
