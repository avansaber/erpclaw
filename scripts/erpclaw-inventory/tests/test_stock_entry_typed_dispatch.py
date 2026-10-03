"""Wave 2 S6 — typed stock-entry dispatch for the 3 previously-unhandled types.

Covers `repack`, `send_to_subcontractor`, and `material_consumption` on
`add-stock-entry` + `submit-stock-entry`, plus the two convenience wrappers
`add-repack-stock-entry` and `add-material-consumption`.

L0 (constitutional): `test_repack_total_cost_balanced` — exact Decimal balance on
a valid repack, and a hard rollback (no row written) when input value != output
value beyond the $0.01 tolerance.

L1 (unit): each of the 3 dispatch paths × happy + error.

All amounts are asserted as exact Decimals against a fresh DB (init_schema).
"""
import json
import uuid
from decimal import Decimal

import pytest
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_account, seed_item, seed_warehouse, seed_stock_entry_sle, _uuid,
)

mod = load_db_query()


def _ensure_cogs(env, conn):
    """The base inventory env omits a COGS account; any entry that issues stock
    (repack consume leg, subcontract out-leg, material_consumption) posts a COGS
    GL leg on submit and needs one. Seed it once per test that submits."""
    seed_account(conn, env["company_id"], "COGS", "expense",
                 "cost_of_goods_sold", "5100")


# ── local seed helpers ───────────────────────────────────────────────────────

def _seed_typed_warehouse(conn, company_id, name, warehouse_type, account_id=None):
    """Seed a warehouse with an explicit warehouse_type (the base helper always
    defaults to 'stores')."""
    wid = _uuid()
    conn.execute(
        "INSERT INTO warehouse (id, name, warehouse_type, company_id, account_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (wid, name, warehouse_type, company_id, account_id),
    )
    conn.commit()
    return wid


def _seed_work_order(conn, company_id, item_id, status="in_process"):
    """Seed a minimal bom + work_order so material_consumption has a real parent."""
    bom_id = _uuid()
    conn.execute(
        "INSERT INTO bom (id, item_id, quantity, company_id) VALUES (?, ?, '1', ?)",
        (bom_id, item_id, company_id),
    )
    wo_id = _uuid()
    conn.execute(
        "INSERT INTO work_order (id, item_id, bom_id, qty, status, company_id) "
        "VALUES (?, ?, ?, '10', ?, ?)",
        (wo_id, item_id, bom_id, status, company_id),
    )
    conn.commit()
    return wo_id


def _ns(env, **extra):
    """Build a Namespace pre-populated with every flag add_stock_entry +
    the two wrappers read, so a single call covers any path."""
    base = dict(
        entry_type=None, company_id=env["company_id"], posting_date="2026-06-15",
        items=None, supplier_warehouse_id=None, work_order_id=None,
        warehouse=None, from_item_id=None, from_qty=None, to_item_id=None,
        to_qty=None, standard_rate=None, item_id=None, qty=None, rate=None,
    )
    base.update(extra)
    return ns(**base)


def _submit(conn, se_id):
    return call_action(mod.submit_stock_entry, conn, ns(stock_entry_id=se_id))


def _sle_rows(conn, se_id):
    return conn.execute(
        "SELECT item_id, warehouse_id, actual_qty, valuation_rate, stock_value "
        "FROM stock_ledger_entry WHERE voucher_id=? AND is_cancelled=0 "
        "ORDER BY actual_qty",
        (se_id,),
    ).fetchall()


# ─────────────────────────────────────────────────────────────────────────────
# L0 — repack cost-balance invariant (constitutional)
# ─────────────────────────────────────────────────────────────────────────────

class TestRepackCostBalanceL0:
    def test_repack_total_cost_balanced(self, conn, env):
        """A valid repack: consume 100 of item1 @ 50.00 (= $5,000) and produce
        20 of item2 @ 250.00 (= $5,000). Input value == output value exactly,
        and the posted SLE nets to a zero stock-value change."""
        _ensure_cogs(env, conn)
        item2 = env["item2"]
        # Value item2 at 250.00 so 20 units == the $5,000 of consumed item1.
        conn.execute("UPDATE item SET standard_rate='250.00' WHERE id=?", (item2,))
        conn.commit()

        items = [
            {"item_id": env["item1"], "qty": "100", "rate": "50.00",
             "from_warehouse_id": env["warehouse"]},
            {"item_id": item2, "qty": "20", "rate": "250.00",
             "to_warehouse_id": env["warehouse"]},
        ]
        se = call_action(mod.add_stock_entry, conn, _ns(
            env, entry_type="repack", items=json.dumps(items)))
        assert is_ok(se), f"balanced repack should draft: {se}"
        assert Decimal(se["total_incoming_value"]) == Decimal("5000.00")
        assert Decimal(se["total_outgoing_value"]) == Decimal("5000.00")
        assert Decimal(se["value_difference"]) == Decimal("0.00")

        result = _submit(conn, se["stock_entry_id"])
        assert is_ok(result), f"balanced repack should submit: {result}"

        rows = _sle_rows(conn, se["stock_entry_id"])
        assert len(rows) == 2
        consumed = next(r for r in rows if Decimal(r["actual_qty"]) < 0)
        produced = next(r for r in rows if Decimal(r["actual_qty"]) > 0)
        assert Decimal(consumed["actual_qty"]) == Decimal("-100")
        assert Decimal(produced["actual_qty"]) == Decimal("20")
        # Both legs carry $5,000 of value, so the repack nets to a zero
        # stock-value change (consumed $5,000 out, produced $5,000 in).
        assert Decimal(produced["stock_value"]) == Decimal("5000.00")
        # GL must balance: total debits == total credits for the voucher.
        gl = conn.execute(
            "SELECT debit, credit FROM gl_entry WHERE voucher_id=? "
            "AND voucher_type='stock_entry'", (se["stock_entry_id"],)).fetchall()
        total_dr = sum(Decimal(r["debit"]) for r in gl)
        total_cr = sum(Decimal(r["credit"]) for r in gl)
        assert total_dr == total_cr
        legs = conn.execute(
            "SELECT account_id, debit, credit FROM gl_entry WHERE voucher_id=? "
            "AND voucher_type='stock_entry' AND is_cancelled=0",
            (se["stock_entry_id"],)).fetchall()
        leg_tuples = sorted((r["account_id"], r["debit"], r["credit"]) for r in legs)
        assert leg_tuples == sorted([(env["stock_acct"], "0.00", "5000.00"),
                                     (env["stock_acct"], "5000.00", "0.00")])
        contra = conn.execute(
            "SELECT COUNT(*) c FROM gl_entry g JOIN account a ON a.id=g.account_id "
            "WHERE g.voucher_id=? AND g.voucher_type='stock_entry' "
            "AND a.account_type IN ('cost_of_goods_sold', 'stock_received_not_billed')",
            (se["stock_entry_id"],)).fetchone()["c"]
        assert contra == 0

    def test_repack_imbalance_rolls_back(self, conn, env):
        """An unbalanced repack (input $5,000 vs output $4,000, beyond the $0.01
        tolerance) is refused at draft and writes NO stock_entry row."""
        item2 = env["item2"]
        before = conn.execute("SELECT COUNT(*) c FROM stock_entry").fetchone()["c"]

        items = [
            {"item_id": env["item1"], "qty": "100", "rate": "50.00",
             "from_warehouse_id": env["warehouse"]},   # $5,000 in
            {"item_id": item2, "qty": "20", "rate": "200.00",
             "to_warehouse_id": env["warehouse"]},      # $4,000 out
        ]
        result = call_action(mod.add_stock_entry, conn, _ns(
            env, entry_type="repack", items=json.dumps(items)))
        assert is_error(result)
        assert "cost-balanced" in result.get("message", "")

        after = conn.execute("SELECT COUNT(*) c FROM stock_entry").fetchone()["c"]
        assert after == before, "imbalanced repack must not persist a stock_entry"

    def test_repack_within_one_cent_tolerance_succeeds(self, conn, env):
        """A 1-cent gap is inside tolerance and posts (boundary check)."""
        item2 = env["item2"]
        items = [
            {"item_id": env["item1"], "qty": "100", "rate": "50.00",
             "from_warehouse_id": env["warehouse"]},    # 5000.00 in
            {"item_id": item2, "qty": "1", "rate": "4999.99",
             "to_warehouse_id": env["warehouse"]},       # 4999.99 out (1c gap)
        ]
        se = call_action(mod.add_stock_entry, conn, _ns(
            env, entry_type="repack", items=json.dumps(items)))
        assert is_ok(se), f"1-cent gap is within tolerance: {se}"


# ─────────────────────────────────────────────────────────────────────────────
# L1 — repack dispatch (happy + error)
# ─────────────────────────────────────────────────────────────────────────────

class TestRepackDispatch:
    def test_repack_wrapper_happy(self, conn, env):
        """add-repack-stock-entry shortcut builds a balanced one-in/one-out repack."""
        conn.execute("UPDATE item SET standard_rate='250.00' WHERE id=?",
                     (env["item2"],))
        conn.commit()
        result = call_action(mod.add_repack_stock_entry, conn, _ns(
            env, warehouse=env["warehouse"],
            from_item_id=env["item1"], from_qty="100",
            to_item_id=env["item2"], to_qty="20", standard_rate="250.00"))
        assert is_ok(result), result
        assert Decimal(result["value_difference"]) == Decimal("0.00")

    def test_repack_line_needs_single_direction(self, conn, env):
        """A repack line with BOTH from and to warehouses is rejected."""
        items = [
            {"item_id": env["item1"], "qty": "100", "rate": "50.00",
             "from_warehouse_id": env["warehouse"],
             "to_warehouse_id": env["warehouse"]},
        ]
        result = call_action(mod.add_stock_entry, conn, _ns(
            env, entry_type="repack", items=json.dumps(items)))
        assert is_error(result)
        assert "exactly one" in result.get("message", "")

    def test_repack_needs_input_and_output(self, conn, env):
        """A repack with only an input (no output) is rejected."""
        items = [
            {"item_id": env["item1"], "qty": "100", "rate": "50.00",
             "from_warehouse_id": env["warehouse"]},
        ]
        result = call_action(mod.add_stock_entry, conn, _ns(
            env, entry_type="repack", items=json.dumps(items)))
        assert is_error(result)
        assert "input line" in result.get("message", "")


# ─────────────────────────────────────────────────────────────────────────────
# L1 — send_to_subcontractor dispatch (happy + error)
# ─────────────────────────────────────────────────────────────────────────────

class TestSubcontractDispatch:
    def test_subcontract_happy(self, conn, env):
        """send_to_subcontractor transfers stock out to a transit sub-store and
        posts a balanced two-leg SLE (out of from_wh, into the sub-store)."""
        _ensure_cogs(env, conn)
        sub_wh = _seed_typed_warehouse(
            conn, env["company_id"], "Subcontractor Store", "transit",
            env["stock_acct"])
        items = [
            {"item_id": env["item1"], "qty": "30", "rate": "50.00",
             "from_warehouse_id": env["warehouse"]},
        ]
        se = call_action(mod.add_stock_entry, conn, _ns(
            env, entry_type="subcontract", items=json.dumps(items),
            supplier_warehouse_id=sub_wh))
        assert is_ok(se), se

        # The parent records the subcontract destination.
        ref = conn.execute(
            "SELECT purpose_reference_type, purpose_reference_id "
            "FROM stock_entry WHERE id=?", (se["stock_entry_id"],)).fetchone()
        assert ref["purpose_reference_type"] == "subcontracting_warehouse"
        assert ref["purpose_reference_id"] == sub_wh

        result = _submit(conn, se["stock_entry_id"])
        assert is_ok(result), result

        rows = _sle_rows(conn, se["stock_entry_id"])
        assert len(rows) == 2
        out_leg = next(r for r in rows if Decimal(r["actual_qty"]) < 0)
        in_leg = next(r for r in rows if Decimal(r["actual_qty"]) > 0)
        assert out_leg["warehouse_id"] == env["warehouse"]
        assert in_leg["warehouse_id"] == sub_wh
        assert Decimal(out_leg["actual_qty"]) == Decimal("-30")
        assert Decimal(in_leg["actual_qty"]) == Decimal("30")
        # Sub-store leg inherits the source valuation (50.00).
        assert Decimal(in_leg["valuation_rate"]) == Decimal("50.00")

    def test_subcontract_rejects_stores_warehouse(self, conn, env):
        """A normal 'stores' warehouse is not a valid subcontractor sub-store."""
        bad_wh = _seed_typed_warehouse(
            conn, env["company_id"], "Plain Store", "stores", env["stock_acct"])
        items = [
            {"item_id": env["item1"], "qty": "30", "rate": "50.00",
             "from_warehouse_id": env["warehouse"]},
        ]
        result = call_action(mod.add_stock_entry, conn, _ns(
            env, entry_type="subcontract", items=json.dumps(items),
            supplier_warehouse_id=bad_wh))
        assert is_error(result)
        assert "warehouse_type" in result.get("message", "")

    def test_subcontract_requires_supplier_warehouse(self, conn, env):
        """Omitting --supplier-warehouse-id is a clean error."""
        items = [
            {"item_id": env["item1"], "qty": "30", "rate": "50.00",
             "from_warehouse_id": env["warehouse"]},
        ]
        result = call_action(mod.add_stock_entry, conn, _ns(
            env, entry_type="subcontract", items=json.dumps(items)))
        assert is_error(result)
        assert "supplier-warehouse-id" in result.get("message", "")


# ─────────────────────────────────────────────────────────────────────────────
# L1 — material_consumption dispatch (happy + error)
# ─────────────────────────────────────────────────────────────────────────────

class TestMaterialConsumptionDispatch:
    def test_consume_happy(self, conn, env):
        """material_consumption issues raw material against an active work order
        and posts a single negative SLE at the source warehouse."""
        _ensure_cogs(env, conn)
        wo_id = _seed_work_order(conn, env["company_id"], env["item2"],
                                 status="in_process")
        result = call_action(mod.add_material_consumption, conn, _ns(
            env, warehouse=env["warehouse"], work_order_id=wo_id,
            item_id=env["item1"], qty="25", rate="50.00"))
        assert is_ok(result), result

        ref = conn.execute(
            "SELECT purpose_reference_type, purpose_reference_id "
            "FROM stock_entry WHERE id=?", (result["stock_entry_id"],)).fetchone()
        assert ref["purpose_reference_type"] == "work_order"
        assert ref["purpose_reference_id"] == wo_id

        submitted = _submit(conn, result["stock_entry_id"])
        assert is_ok(submitted), submitted

        rows = _sle_rows(conn, result["stock_entry_id"])
        assert len(rows) == 1
        assert rows[0]["warehouse_id"] == env["warehouse"]
        assert Decimal(rows[0]["actual_qty"]) == Decimal("-25")

    def test_consume_rejects_inactive_work_order(self, conn, env):
        """A completed work order cannot consume material."""
        wo_id = _seed_work_order(conn, env["company_id"], env["item2"],
                                 status="completed")
        result = call_action(mod.add_material_consumption, conn, _ns(
            env, warehouse=env["warehouse"], work_order_id=wo_id,
            item_id=env["item1"], qty="25"))
        assert is_error(result)
        assert "active work order" in result.get("message", "")

    def test_consume_rejects_unknown_work_order(self, conn, env):
        """A non-existent work order is a clean error."""
        result = call_action(mod.add_material_consumption, conn, _ns(
            env, warehouse=env["warehouse"], work_order_id="no-such-wo",
            item_id=env["item1"], qty="25"))
        assert is_error(result)
        assert "not found" in result.get("message", "")
