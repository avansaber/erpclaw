"""M477 depth: behavioural evidence for 10 inventory actions.

Each action below already had a test that proved the wrong thing (response
shape or routability). Each test here proves the database effect instead:
what row exists afterwards with which exact values, what changed from what
to what, and what did not change. Money is TEXT: exact string comparisons,
Decimal for arithmetic, never float, never round().

Per-action depth signal:
- add-pick-list-item .......... STORED ROW (pick_list_item)
- apply-putaway-on-receipt .... READ-ONLY routing plan (no stored row, no ledger
                                effect; asserts exact routes + ledgers unchanged)
- create-stock-ledger-entries . RETIRED refusal (no stored row, no ledger
                                effect; asserts the steer + ledgers unchanged)
- get-stock-entry ............. STORED ROW readback (stock_entry + its items)
- get-stock-revaluation ....... STORED ROW + LEDGER EFFECT read (surfaces the
                                revaluation's SLE row and both GL legs and
                                asserts the legs balance)
- import-items ................ STORED ROWS (item)
- list-putaway-rules .......... STORED ROWS readback (ordered rule rows)
- list-reservations ........... STORED ROWS readback (reservation rows)
- reverse-stock-ledger-entries  RETIRED refusal (as create-)
- update-putaway-rule ......... STORED ROW change (putaway_rule before -> after)

Ledger note: none of these ten actions posts to the stock or general ledger
on its success path (the two ledger gateways are retired; the getters/lists
are reads; the writers touch pick/putaway/item/reservation tables only), so
no success test below asserts a new ledger leg. Where a read surfaces ledger
rows (get-stock-revaluation) both legs are asserted and shown to balance.
Every success test for a non-ledger writer pins the stock_ledger_entry and
gl_entry counts unchanged so a later reader does not add a leg assertion
that cannot hold.
"""
import json
import uuid
from decimal import Decimal

from inventory_helpers import call_action, ns, is_error, is_ok, load_db_query

mod = load_db_query()

_DEFAULTS = dict(
    id=None, name=None, priority=None, target_warehouse_id=None,
    match_item_id=None, match_item_group=None, active_only=False,
    stock_entry_id=None, sales_order_id=None, pick_list_id=None,
    item_id=None, warehouse_id=None, warehouse=None, qty=None,
    source_bin=None, picked_qty=None, voucher_type=None, voucher_id=None,
    reservation_status=None, item_status=None, company_id=None,
    company_name=None, reason=None, posting_date=None, entry_type=None,
    items=None, db_path=None, revaluation_id=None, csv_path=None,
    new_rate=None,
)


def _ns(**kw):
    d = dict(_DEFAULTS)
    d.update(kw)
    return ns(**d)


_SNAPSHOT_TABLES = (
    "pick_list", "pick_list_item", "putaway_rule", "item",
    "stock_reservation_entry", "stock_ledger_entry", "gl_entry",
    "stock_entry", "stock_entry_item", "stock_revaluation", "audit_log",
)


def _snapshot(conn):
    """Byte-level dump of every table these actions could touch."""
    snap = {}
    for table in _SNAPSHOT_TABLES:
        snap[table] = [tuple(r) for r in
                       conn.execute("SELECT * FROM %s ORDER BY id" % table).fetchall()]
    return snap


def _counts(conn):
    return {t: len(v) for t, v in _snapshot(conn).items()}


def _seed_so(conn, env, item_id, qty="10"):
    cust = str(uuid.uuid4())
    conn.execute("INSERT INTO customer (id, name, company_id) VALUES (?, 'Cust', ?)",
                 (cust, env["company_id"]))
    so = str(uuid.uuid4())
    conn.execute("INSERT INTO sales_order (id, customer_id, order_date, status, company_id) "
                 "VALUES (?, ?, '2026-06-19', 'confirmed', ?)",
                 (so, cust, env["company_id"]))
    conn.execute("INSERT INTO sales_order_item "
                 "(id, sales_order_id, item_id, quantity, delivered_qty, warehouse_id) "
                 "VALUES (?, ?, ?, ?, '0', ?)",
                 (str(uuid.uuid4()), so, item_id, qty, env["warehouse"]))
    conn.commit()
    return so


def _receive_submitted(conn, env, lines, posting_date="2026-03-01"):
    draft = call_action(mod.add_stock_entry, conn, ns(
        entry_type="receive", company_id=env["company_id"],
        posting_date=posting_date, items=json.dumps(lines)))
    assert is_ok(draft), draft
    submitted = call_action(mod.submit_stock_entry, conn,
                            ns(stock_entry_id=draft["stock_entry_id"]))
    assert is_ok(submitted), submitted
    return draft["stock_entry_id"]


# ── add-pick-list-item: STORED ROW ──────────────────────────────────────────

class TestAddPickListItemDepth:
    def test_add_line_writes_exact_row_and_nothing_else(self, conn, env):
        # This action does NOT reach the ledger: it appends one pick_list_item
        # row (plus its audit row). Both ledger counts are pinned unchanged.
        so = _seed_so(conn, env, env["item1"], "10")
        pl = call_action(mod.create_pick_list, conn,
                         _ns(sales_order_id=so, warehouse_id=None))
        assert is_ok(pl), pl
        before = _counts(conn)

        r = call_action(mod.add_pick_list_item, conn, _ns(
            pick_list_id=pl["pick_list_id"], item_id=env["item2"],
            qty="7", source_bin="BIN-A-01"))
        assert is_ok(r), r
        assert r["expected_qty"] == "7.00"

        row = conn.execute(
            "SELECT pick_list_id, item_id, expected_qty, picked_qty, "
            "source_warehouse_bin FROM pick_list_item WHERE id = ?",
            (r["pick_list_item_id"],)).fetchone()
        assert row is not None, "the added line must exist"
        assert row["pick_list_id"] == pl["pick_list_id"]
        assert row["item_id"] == env["item2"]
        assert row["expected_qty"] == "7.00"
        assert row["picked_qty"] == "0"
        assert row["source_warehouse_bin"] == "BIN-A-01"

        # The SO-derived line is untouched; the list is still a draft of 2.
        # (TEXT sort would order "10.00" before "7.00"; sort numerically.)
        lines = conn.execute(
            "SELECT item_id, expected_qty FROM pick_list_item "
            "WHERE pick_list_id = ?", (pl["pick_list_id"],)).fetchall()
        assert sorted((l["item_id"], l["expected_qty"]) for l in lines) == sorted([
            (env["item2"], "7.00"), (env["item1"], "10.00")])
        assert conn.execute("SELECT status FROM pick_list WHERE id = ?",
                            (pl["pick_list_id"],)).fetchone()["status"] == "draft"

        after = _counts(conn)
        assert after["pick_list_item"] == before["pick_list_item"] + 1
        assert after["audit_log"] == before["audit_log"] + 1
        for table in _SNAPSHOT_TABLES:
            if table in ("pick_list_item", "audit_log"):
                continue
            assert after[table] == before[table], table
        assert after["stock_ledger_entry"] == before["stock_ledger_entry"]
        assert after["gl_entry"] == before["gl_entry"]

    def test_zero_qty_refused_truthfully_and_writes_nothing(self, conn, env):
        so = _seed_so(conn, env, env["item1"], "10")
        pl = call_action(mod.create_pick_list, conn,
                         _ns(sales_order_id=so, warehouse_id=None))
        assert is_ok(pl), pl
        before = _snapshot(conn)

        r = call_action(mod.add_pick_list_item, conn, _ns(
            pick_list_id=pl["pick_list_id"], item_id=env["item2"],
            qty="0", source_bin=None))
        assert is_error(r)
        assert r["message"] == "--qty must be > 0"

        assert _snapshot(conn) == before, "a refused add must half-write nothing"


# ── apply-putaway-on-receipt: READ-ONLY routing plan ────────────────────────

class TestApplyPutawayOnReceiptDepth:
    def test_routing_pinned_and_both_ledgers_untouched(self, conn, env):
        # This action does NOT reach the ledger and writes no stored row: it
        # returns a routing plan. Behaviour = exact routes grounded against
        # the rule rows, plus both ledgers byte-identical.
        rule = call_action(mod.add_putaway_rule, conn, _ns(
            name="m477-item2", match_item_id=env["item2"],
            target_warehouse_id=env["warehouse2"], priority=50,
            company_id=env["company_id"]))
        assert is_ok(rule), rule
        se_id = _receive_submitted(conn, env, [
            {"item_id": env["item2"], "qty": "40", "rate": "25.00",
             "to_warehouse_id": env["warehouse"]}])
        before = _snapshot(conn)

        r = call_action(mod.apply_putaway_on_receipt, conn,
                        _ns(stock_entry_id=se_id))
        assert is_ok(r), r
        assert r["stock_entry_id"] == se_id
        assert r["routed_count"] == 1
        assert r["routes"] == [{
            "item_id": env["item2"],
            "received_warehouse_id": env["warehouse"],
            "target_warehouse_id": env["warehouse2"],
            "needs_transfer": True,
            "qty": "40.00"}]

        # The plan names the rule's target: the rule row justifies the route.
        assert conn.execute("SELECT target_warehouse_id FROM putaway_rule "
                            "WHERE id = ?", (rule["putaway_rule_id"],)).fetchone()[
            "target_warehouse_id"] == env["warehouse2"]
        # A plan, not a move: stock still sits where the receipt put it.
        assert _snapshot(conn) == before
        bal = call_action(mod.get_stock_balance_action, conn, ns(
            item_id=env["item2"], warehouse_id=env["warehouse"]))
        assert (bal["qty"], bal["valuation_rate"], bal["stock_value"]) == (
            "40.00", "25.00", "1000.00")

    def test_unknown_entry_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        ghost = "m477-ghost-entry"
        r = call_action(mod.apply_putaway_on_receipt, conn,
                        _ns(stock_entry_id=ghost))
        assert is_error(r)
        assert r["message"] == "Stock entry %s not found" % ghost
        assert _snapshot(conn) == before


# ── create-stock-ledger-entries: RETIRED refusal ────────────────────────────

class TestCreateStockLedgerEntriesDepth:
    def test_refusal_names_action_steers_and_leaves_ledgers_exact(self, conn, env):
        # Retired: the ONLY behaviour is "refuse, and leave both ledgers
        # exactly as they were". Posts a real receipt first so there is a
        # populated ledger worth protecting, then pins every row of it.
        se_id = _receive_submitted(conn, env, [
            {"item_id": env["item2"], "qty": "40", "rate": "25.00",
             "to_warehouse_id": env["warehouse"]}])
        sle_before = [tuple(r) for r in conn.execute(
            "SELECT item_id, actual_qty, valuation_rate, stock_value, "
            "stock_value_difference, is_cancelled FROM stock_ledger_entry "
            "ORDER BY item_id").fetchall()]
        gl_before = [tuple(r) for r in conn.execute(
            "SELECT account_id, debit, credit, is_cancelled FROM gl_entry "
            "ORDER BY account_id, debit").fetchall()]
        assert len(sle_before) >= 2 and len(gl_before) >= 2
        before = _snapshot(conn)

        r = call_action(mod.ACTIONS["create-stock-ledger-entries"], conn, _ns(
            voucher_type="Delivery Note", voucher_id="DN-M477",
            posting_date="2026-06-01"))
        assert is_error(r)
        assert "create-stock-ledger-entries" in r["message"], \
            "the message must name the action the caller typed"
        assert "retired" in r["message"].lower()
        assert "stock-entry" in r.get("suggestion", ""), \
            "the steer must name the replacement flow"

        assert _snapshot(conn) == before
        assert [tuple(r) for r in conn.execute(
            "SELECT item_id, actual_qty, valuation_rate, stock_value, "
            "stock_value_difference, is_cancelled FROM stock_ledger_entry "
            "ORDER BY item_id").fetchall()] == sle_before
        assert [tuple(r) for r in conn.execute(
            "SELECT account_id, debit, credit, is_cancelled FROM gl_entry "
            "ORDER BY account_id, debit").fetchall()] == gl_before
        assert se_id is not None  # the receipt setup above is what we pinned

    def test_empty_invocation_refuses_identically(self, conn, env):
        before = _snapshot(conn)
        r = call_action(mod.ACTIONS["create-stock-ledger-entries"], conn, _ns())
        assert is_error(r)
        assert "create-stock-ledger-entries" in r["message"]
        assert _snapshot(conn) == before


# ── get-stock-entry: STORED ROW readback ────────────────────────────────────

class TestGetStockEntryDepth:
    def test_get_returns_the_stored_header_and_lines_exactly(self, conn, env):
        # Read-only: writes nothing (not even on its success path, so no
        # ledger leg can be asserted here). Behaviour = the response matches
        # the stored stock_entry + stock_entry_item rows field by field.
        draft = call_action(mod.add_stock_entry, conn, ns(
            entry_type="receive", company_id=env["company_id"],
            posting_date="2026-06-15", items=json.dumps([
                {"item_id": env["item1"], "qty": "10", "rate": "50.00",
                 "to_warehouse_id": env["warehouse"]}])))
        assert is_ok(draft), draft
        se_id = draft["stock_entry_id"]
        before = _snapshot(conn)

        r = call_action(mod.get_stock_entry, conn, ns(stock_entry_id=se_id))
        assert is_ok(r), r

        se = conn.execute(
            "SELECT stock_entry_type, posting_date, total_incoming_value, "
            "total_outgoing_value, value_difference, status, company_id "
            "FROM stock_entry WHERE id = ?", (se_id,)).fetchone()
        assert r["stock_entry_type"] == se["stock_entry_type"] == "material_receipt"
        assert r["posting_date"] == se["posting_date"] == "2026-06-15"
        # The envelope owns "status" ("ok"); the document state rides along
        # as "document_status" (response.ok contract) and must equal the row.
        assert r["status"] == "ok"
        assert r["document_status"] == se["status"] == "draft"
        assert r["company_id"] == se["company_id"] == env["company_id"]
        assert r["total_incoming_value"] == se["total_incoming_value"] == "500.00"
        assert r["total_outgoing_value"] == se["total_outgoing_value"] == "0.00"
        assert r["value_difference"] == se["value_difference"] == "500.00"

        db_lines = conn.execute(
            "SELECT item_id, quantity, valuation_rate, amount, "
            "to_warehouse_id FROM stock_entry_item "
            "WHERE stock_entry_id = ?", (se_id,)).fetchall()
        assert len(r["items"]) == len(db_lines) == 1
        assert r["items"][0]["item_id"] == db_lines[0]["item_id"] == env["item1"]
        assert r["items"][0]["quantity"] == db_lines[0]["quantity"] == "10.00"
        assert r["items"][0]["valuation_rate"] == db_lines[0][
            "valuation_rate"] == "50.00"
        assert r["items"][0]["amount"] == db_lines[0]["amount"] == "500.00"
        assert r["items"][0]["to_warehouse_id"] == db_lines[0][
            "to_warehouse_id"] == env["warehouse"]

        assert _snapshot(conn) == before, "a get must write nothing"

    def test_unknown_entry_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        ghost = "m477-ghost-entry"
        r = call_action(mod.get_stock_entry, conn, ns(stock_entry_id=ghost))
        assert is_error(r)
        assert r["message"] == "Stock entry %s not found" % ghost
        assert _snapshot(conn) == before


# ── get-stock-revaluation: STORED ROW + LEDGER EFFECT read ──────────────────

class TestGetStockRevaluationDepth:
    def test_get_surfaces_stored_row_and_balanced_legs(self, conn, env):
        # Read-only itself, but the row it returns REACHES the ledger: the
        # revaluation posted one zero-qty SLE row and a DR/CR pair. Both legs
        # are asserted from the response, matched to the tables, and balanced.
        rv = call_action(mod.revalue_stock, conn, _ns(
            item_id=env["item1"], warehouse_id=env["warehouse"],
            new_rate="60.00", posting_date="2026-06-15",
            company_id=env["company_id"], reason="M477 depth"))
        assert is_ok(rv), rv
        reval_id = rv["revaluation_id"]
        before = _snapshot(conn)

        r = call_action(mod.get_stock_revaluation, conn,
                        _ns(revaluation_id=reval_id))
        assert is_ok(r), r

        sr = conn.execute(
            "SELECT item_id, warehouse_id, current_qty, old_rate, new_rate, "
            "adjustment_amount, status FROM stock_revaluation WHERE id = ?",
            (reval_id,)).fetchone()
        assert r["item_id"] == sr["item_id"] == env["item1"]
        assert r["warehouse_id"] == sr["warehouse_id"] == env["warehouse"]
        assert r["current_qty"] == sr["current_qty"] == "100.00"
        assert r["old_rate"] == sr["old_rate"] == "50.00"
        assert r["new_rate"] == sr["new_rate"] == "60.00"
        assert r["adjustment_amount"] == sr["adjustment_amount"] == "1000.00"
        # Envelope owns "status"; the document state is "document_status".
        assert r["status"] == "ok"
        assert r["document_status"] == sr["status"] == "submitted"

        assert len(r["sle_entries"]) == 1
        sle = r["sle_entries"][0]
        assert sle["actual_qty"] == "0"
        assert sle["valuation_rate"] == "60.00"
        assert sle["stock_value"] == "6000.00"
        assert sle["stock_value_difference"] == "1000.00"
        assert sle["qty_after_transaction"] == "100.00"
        assert sle["voucher_type"] == "stock_revaluation"
        db_sle = conn.execute(
            "SELECT actual_qty, valuation_rate, stock_value, "
            "stock_value_difference FROM stock_ledger_entry WHERE voucher_id = ? "
            "AND voucher_type = 'stock_revaluation'", (reval_id,)).fetchone()
        assert (sle["actual_qty"], sle["valuation_rate"], sle["stock_value"],
                sle["stock_value_difference"]) == tuple(db_sle)

        assert len(r["gl_entries"]) == 2
        by_acct = {g["account_id"]: g for g in r["gl_entries"]}
        assert set(by_acct) == {env["stock_acct"], env["stock_adj"]}
        assert by_acct[env["stock_acct"]]["debit"] == "1000.00"
        assert by_acct[env["stock_acct"]]["credit"] == "0.00"
        assert by_acct[env["stock_adj"]]["debit"] == "0.00"
        assert by_acct[env["stock_adj"]]["credit"] == "1000.00"
        total_dr = sum(Decimal(g["debit"]) for g in r["gl_entries"])
        total_cr = sum(Decimal(g["credit"]) for g in r["gl_entries"])
        assert str(total_dr) == str(total_cr) == "1000.00"
        assert str(total_dr) == r["adjustment_amount"]

        assert _snapshot(conn) == before, "a get must write nothing"

    def test_unknown_revaluation_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        ghost = "m477-ghost-reval"
        r = call_action(mod.get_stock_revaluation, conn,
                        _ns(revaluation_id=ghost))
        assert is_error(r)
        assert r["message"] == "Stock revaluation %s not found" % ghost
        assert _snapshot(conn) == before


# ── import-items: STORED ROWS ───────────────────────────────────────────────

class TestImportItemsDepth:
    def test_import_writes_exact_item_rows(self, conn, env, tmp_path):
        # This action does NOT reach the ledger: it inserts item-master rows
        # only. Both ledger counts are pinned unchanged.
        csv_path = str(tmp_path / "m477_items.csv")
        with open(csv_path, "w") as f:
            f.write("item_code,name,uom,valuation_method,description\n"
                    "M477-001,M477 Widget,Each,moving_average,from csv\n"
                    "M477-002,M477 Gizmo,Kg,fifo,\n")
        before = _counts(conn)

        r = call_action(mod.import_items, conn, _ns(csv_path=csv_path))
        assert is_ok(r), r
        assert (r["imported"], r["skipped"], r["total_rows"]) == (2, 0, 2)

        row1 = conn.execute(
            "SELECT item_code, item_name, stock_uom, valuation_method, "
            "description, status FROM item WHERE item_code = ?",
            ("M477-001",)).fetchone()
        assert tuple(row1) == ("M477-001", "M477 Widget", "Each",
                               "moving_average", "from csv", "active")
        row2 = conn.execute(
            "SELECT item_code, item_name, stock_uom, valuation_method, "
            "description, status FROM item WHERE item_code = ?",
            ("M477-002",)).fetchone()
        assert tuple(row2) == ("M477-002", "M477 Gizmo", "Kg",
                               "fifo", None, "active")

        after = _counts(conn)
        assert after["item"] == before["item"] + 2
        for table in _SNAPSHOT_TABLES:
            if table == "item":
                continue
            assert after[table] == before[table], table

        # Re-import is a pure skip: nothing written, counts byte-identical.
        snap = _snapshot(conn)
        r2 = call_action(mod.import_items, conn, _ns(csv_path=csv_path))
        assert is_ok(r2), r2
        assert (r2["imported"], r2["skipped"], r2["total_rows"]) == (0, 2, 2)
        assert _snapshot(conn) == snap

    def test_missing_file_refused_truthfully_and_writes_nothing(self, conn, env, tmp_path):
        before = _snapshot(conn)
        missing = str(tmp_path / "m477_missing.csv")
        r = call_action(mod.import_items, conn, _ns(csv_path=missing))
        assert is_error(r)
        assert r["message"] == "File not found: %s" % missing
        assert _snapshot(conn) == before, "a refused import must write nothing"


# ── list-putaway-rules: STORED ROWS readback ────────────────────────────────

class TestListPutawayRulesDepth:
    def test_list_returns_stored_rows_in_precedence_order(self, conn, env):
        # Read-only: writes nothing, so no ledger leg can be asserted here.
        # Behaviour = the rows come back in match-precedence order with the
        # exact stored values.
        g = call_action(mod.add_putaway_rule, conn, _ns(
            name="m477-group", match_item_group="Widgets",
            target_warehouse_id=env["warehouse2"], priority=10,
            company_id=env["company_id"]))
        assert is_ok(g), g
        it = call_action(mod.add_putaway_rule, conn, _ns(
            name="m477-item", match_item_id=env["item1"],
            target_warehouse_id=env["warehouse"], priority=50,
            company_id=env["company_id"]))
        assert is_ok(it), it
        before = _snapshot(conn)

        r = call_action(mod.list_putaway_rules, conn,
                        _ns(company_id=env["company_id"], active_only=False))
        assert is_ok(r), r
        assert r["count"] == 2
        # Item match precedes item-group match even at worse priority.
        assert [x["name"] for x in r["putaway_rules"]] == [
            "m477-item", "m477-group"]

        db = {row["id"]: row for row in conn.execute(
            "SELECT id, name, priority, match_item_id, match_item_group, "
            "target_warehouse_id, is_active FROM putaway_rule").fetchall()}
        for entry in r["putaway_rules"]:
            stored = db[entry["id"]]
            assert entry["name"] == stored["name"]
            assert entry["priority"] == stored["priority"]
            assert entry["target_warehouse_id"] == stored["target_warehouse_id"]
            assert entry["match_item_id"] == stored["match_item_id"]
            assert entry["match_item_group"] == stored["match_item_group"]
        assert db[it["putaway_rule_id"]]["match_item_id"] == env["item1"]
        assert db[g["putaway_rule_id"]]["match_item_group"] == "Widgets"

        # Both lists above were reads: the snapshot is untouched.
        assert _snapshot(conn) == before

        # active_only filters the soft-deleted rule.
        assert is_ok(call_action(mod.delete_putaway_rule, conn,
                                 _ns(id=g["putaway_rule_id"])))
        active = call_action(mod.list_putaway_rules, conn,
                             _ns(company_id=env["company_id"],
                                  active_only=True))
        assert is_ok(active), active
        assert active["count"] == 1
        assert active["putaway_rules"][0]["name"] == "m477-item"

        # The delete wrote exactly one row-flip plus its audit row; the
        # active_only LIST itself wrote nothing (no audit row for it).
        after = _snapshot(conn)
        for table in _SNAPSHOT_TABLES:
            if table in ("putaway_rule", "audit_log"):
                continue
            assert after[table] == before[table], table
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1
        assert conn.execute(
            "SELECT is_active FROM putaway_rule WHERE id = ?",
            (g["putaway_rule_id"],)).fetchone()["is_active"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'list-putaway-rules'"
        ).fetchone()[0] == 0

    def test_unknown_company_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        r = call_action(mod.list_putaway_rules, conn,
                        _ns(company_id=None, company_name="M477 No Such Co",
                             active_only=False))
        # resolve_company_id answers {"error": ...} (no "status" key): the
        # refusal is real, its shape is the foundation contract, and the
        # message names the company the caller typed.
        assert "M477 No Such Co" in r.get("error", "")
        assert _snapshot(conn) == before


# ── list-reservations: STORED ROWS readback ─────────────────────────────────

class TestListReservationsDepth:
    def test_list_returns_stored_rows_with_filters(self, conn, env):
        # Read-only: writes nothing, so no ledger leg can be asserted here.
        # Behaviour = the rows match the stored reservation rows exactly,
        # and the item/status filters select the right subset.
        r1 = call_action(mod.add_reservation, conn, _ns(
            voucher_type="manual", voucher_id=None, item_id=env["item1"],
            warehouse_id=env["warehouse"], qty="30"))
        assert is_ok(r1), r1
        r2 = call_action(mod.add_reservation, conn, _ns(
            voucher_type="manual", voucher_id=None, item_id=env["item1"],
            warehouse_id=env["warehouse"], qty="20"))
        assert is_ok(r2), r2
        before = _snapshot(conn)

        r = call_action(mod.list_reservations, conn, _ns(
            item_id=None, warehouse_id=None, warehouse=None,
            reservation_status=None, item_status=None))
        assert is_ok(r), r
        assert r["count"] == 2

        db = {row["id"]: row for row in conn.execute(
            "SELECT id, voucher_type, item_id, warehouse_id, reserved_qty, "
            "status, company_id FROM stock_reservation_entry").fetchall()}
        got = {e["id"]: e for e in r["reservations"]}
        assert set(got) == set(db) == {r1["reservation_id"], r2["reservation_id"]}
        for rid, entry in got.items():
            stored = db[rid]
            assert entry["voucher_type"] == stored["voucher_type"] == "manual"
            assert entry["item_id"] == stored["item_id"] == env["item1"]
            assert entry["warehouse_id"] == stored["warehouse_id"] == env["warehouse"]
            assert entry["status"] == stored["status"] == "active"
            assert entry["reserved_qty"] == stored["reserved_qty"]
        assert sorted(e["reserved_qty"] for e in got.values()) == [
            "20.00", "30.00"]

        # Filter by item with no reservations: empty, still ok.
        none = call_action(mod.list_reservations, conn, _ns(
            item_id=env["item2"], warehouse_id=None, warehouse=None,
            reservation_status=None, item_status=None))
        assert is_ok(none), none
        assert none["count"] == 0
        assert none["reservations"] == []

        # Filter by status: both rows are active.
        active = call_action(mod.list_reservations, conn, _ns(
            item_id=None, warehouse_id=None, warehouse=None,
            reservation_status="active", item_status=None))
        assert is_ok(active), active
        assert active["count"] == 2

        assert _snapshot(conn) == before, "lists must write nothing"

    def test_bad_status_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        r = call_action(mod.list_reservations, conn, _ns(
            item_id=None, warehouse_id=None, warehouse=None,
            reservation_status="m477-bogus", item_status=None))
        assert is_error(r)
        assert r["message"] == "--status must be one of: active, released, consumed"
        assert _snapshot(conn) == before


# ── reverse-stock-ledger-entries: RETIRED refusal ───────────────────────────

class TestReverseStockLedgerEntriesDepth:
    def test_refusal_names_action_steers_and_leaves_ledgers_exact(self, conn, env):
        # Retired: the ONLY behaviour is "refuse, and leave both ledgers
        # exactly as they were" — pinned here AFTER a cancel so the mirrored
        # reversal rows (netting to zero, originals flagged is_cancelled = 1)
        # are part of what must survive untouched.
        se_id = _receive_submitted(conn, env, [
            {"item_id": env["item2"], "qty": "40", "rate": "25.00",
             "to_warehouse_id": env["warehouse"]}])
        cancel = call_action(mod.cancel_stock_entry, conn,
                             ns(stock_entry_id=se_id))
        assert is_ok(cancel), cancel
        sle_before = [tuple(r) for r in conn.execute(
            "SELECT item_id, actual_qty, valuation_rate, stock_value, "
            "stock_value_difference, is_cancelled FROM stock_ledger_entry "
            "ORDER BY item_id, actual_qty").fetchall()]
        gl_before = [tuple(r) for r in conn.execute(
            "SELECT account_id, debit, credit, is_cancelled FROM gl_entry "
            "ORDER BY account_id, debit, credit").fetchall()]
        assert any(r[5] == 1 for r in sle_before), \
            "setup must include cancelled originals"
        before = _snapshot(conn)

        r = call_action(mod.ACTIONS["reverse-stock-ledger-entries"], conn, _ns(
            voucher_type="stock_entry", voucher_id=se_id,
            posting_date="2026-06-01"))
        assert is_error(r)
        assert "reverse-stock-ledger-entries" in r["message"], \
            "the message must name the action the caller typed"
        assert "retired" in r["message"].lower()
        assert "cancel-stock-entry" in r.get("suggestion", ""), \
            "the steer must name the replacement flow"

        assert _snapshot(conn) == before
        assert [tuple(r) for r in conn.execute(
            "SELECT item_id, actual_qty, valuation_rate, stock_value, "
            "stock_value_difference, is_cancelled FROM stock_ledger_entry "
            "ORDER BY item_id, actual_qty").fetchall()] == sle_before
        assert [tuple(r) for r in conn.execute(
            "SELECT account_id, debit, credit, is_cancelled FROM gl_entry "
            "ORDER BY account_id, debit, credit").fetchall()] == gl_before

    def test_empty_invocation_refuses_identically(self, conn, env):
        before = _snapshot(conn)
        r = call_action(mod.ACTIONS["reverse-stock-ledger-entries"], conn, _ns())
        assert is_error(r)
        assert "reverse-stock-ledger-entries" in r["message"]
        assert _snapshot(conn) == before


# ── update-putaway-rule: STORED ROW change ──────────────────────────────────

class TestUpdatePutawayRuleDepth:
    def test_update_changes_exact_fields_from_old_to_new(self, conn, env):
        # This action does NOT reach the ledger: it UPDATEs one putaway_rule
        # row (plus its audit row). Both ledger counts are pinned unchanged.
        created = call_action(mod.add_putaway_rule, conn, _ns(
            name="m477-orig", match_item_id=env["item1"],
            target_warehouse_id=env["warehouse"], priority=10,
            company_id=env["company_id"]))
        assert is_ok(created), created
        rid = created["putaway_rule_id"]
        old = conn.execute(
            "SELECT name, priority, target_warehouse_id, match_item_id, "
            "match_item_group FROM putaway_rule WHERE id = ?", (rid,)).fetchone()
        assert (old["name"], old["priority"]) == ("m477-orig", 10)
        before = _counts(conn)

        r = call_action(mod.update_putaway_rule, conn, _ns(
            id=rid, name="m477-renamed", priority=5))
        assert is_ok(r), r
        assert r["putaway_rule_id"] == rid
        assert r["updated_fields"] == ["name", "priority"]

        new = conn.execute(
            "SELECT name, priority, target_warehouse_id, match_item_id, "
            "match_item_group FROM putaway_rule WHERE id = ?", (rid,)).fetchone()
        assert (old["name"], old["priority"]) == ("m477-orig", 10)
        assert (new["name"], new["priority"]) == ("m477-renamed", 5)
        # Untouched columns are byte-identical.
        assert new["target_warehouse_id"] == old["target_warehouse_id"] == env[
            "warehouse"]
        assert new["match_item_id"] == old["match_item_id"] == env["item1"]
        assert new["match_item_group"] == old["match_item_group"] is None

        after = _counts(conn)
        assert after["putaway_rule"] == before["putaway_rule"]
        assert after["audit_log"] == before["audit_log"] + 1
        for table in _SNAPSHOT_TABLES:
            if table in ("audit_log",):
                continue
            assert after[table] == before[table], table

    def test_unknown_rule_refused_truthfully_and_writes_nothing(self, conn, env):
        before = _snapshot(conn)
        ghost = "m477-ghost-rule"
        r = call_action(mod.update_putaway_rule, conn, _ns(
            id=ghost, name="m477-renamed", priority=5))
        assert is_error(r)
        assert r["message"] == "Putaway rule %s not found" % ghost
        assert _snapshot(conn) == before, "a refused update must write nothing"
