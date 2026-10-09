"""Location-triggered transfer drafts retain the ordinary posting flow."""
from decimal import Decimal

import pytest
from inventory_helpers import (
    call_action, ns, load_db_query, seed_company, seed_warehouse, seed_account,
    seed_stock_entry_sle,
)

mod = load_db_query()


def args(env, **changes):
    values = dict(company_id=env["company_id"], item_id=env["item1"],
                  warehouse_id=env["warehouse"], target_warehouse_id=env["warehouse2"],
                  posting_date="2026-06-15", min_qty="5.00", max_qty="15.25")
    values.update(changes)
    return ns(**values)


def snapshot(conn):
    return tuple(conn.iterdump())


def run(conn, env, **changes):
    return call_action(mod.add_location_resupply, conn, args(env, **changes))


def test_transfer_draft_replay_submit_and_balanced_books(conn, env, db_path):
    target_account = seed_account(conn, env["company_id"], "Secondary Stock",
                                  "asset", "stock", "1210")
    seed_account(conn, env["company_id"], "COGS", "expense", "cost_of_goods_sold", "5100")
    conn.execute("UPDATE warehouse SET account_id=? WHERE id=?",
                 (target_account, env["warehouse2"]))
    conn.commit()
    result = run(conn, env)
    assert result["status"] == "ok", result
    entry_id = result["stock_entry_id"]
    line = conn.execute("SELECT * FROM stock_entry_item WHERE stock_entry_id=?",
                        (entry_id,)).fetchone()
    assert line["quantity"] == "15.25"
    assert line["amount"] == "762.50"
    assert conn.execute("SELECT status FROM stock_entry WHERE id=?", (entry_id,)).fetchone()[0] == "draft"
    before = snapshot(conn)
    repeated = run(conn, env)
    assert repeated["existing_draft"] is True
    assert repeated["stock_entry_id"] == entry_id
    assert snapshot(conn) == before
    submitted = call_action(mod.submit_stock_entry, conn, ns(stock_entry_id=entry_id))
    assert submitted["status"] == "ok", submitted
    assert Decimal(mod.get_stock_balance(conn, env["item1"], env["warehouse"])["qty"]) == Decimal("84.75")
    assert Decimal(mod.get_stock_balance(conn, env["item1"], env["warehouse2"])["qty"]) == Decimal("15.25")
    legs = conn.execute("SELECT account_id,debit,credit FROM gl_entry WHERE voucher_id=? AND is_cancelled=0",
                        (entry_id,)).fetchall()
    assert len(legs) == 2
    assert {(row["account_id"], row["debit"], row["credit"]) for row in legs} == {
        (env["stock_acct"], "0.00", "762.50"), (target_account, "762.50", "0.00")}
    assert sum((Decimal(row["debit"]) - Decimal(row["credit"]) for row in legs), Decimal("0")) == 0
    from erpclaw_lib.gl_invariants import check_gl_invariants
    proof = check_gl_invariants(db_path)
    assert proof["result"] == "pass", proof
    assert proof["verified"] == 5
    before = snapshot(conn)
    assert run(conn, env)["triggered"] is False
    assert snapshot(conn) == before


@pytest.mark.parametrize("changes", [
    {"company_id": None}, {"item_id": "absent"}, {"posting_date": "2026-2-1"},
    {"min_qty": "NaN"}, {"max_qty": "Infinity"}, {"min_qty": "-0.01"},
    {"max_qty": "5.00"}, {"max_qty": "15.251"}, {"max_qty": "1e90"},
    {"max_qty": True}, {"max_qty": 15.25}, {"warehouse_id": "absent"},
])
def test_invalid_inputs_write_nothing(conn, env, changes):
    before = snapshot(conn)
    result = run(conn, env, **changes)
    assert result["status"] == "error", result
    assert snapshot(conn) == before
    assert not conn.in_transaction


def test_same_and_foreign_warehouses_refused_before_writes(conn, env):
    foreign = seed_warehouse(conn, seed_company(conn), "Foreign")
    for changes in ({"target_warehouse_id": env["warehouse"]},
                    {"target_warehouse_id": foreign}, {"warehouse_id": foreign}):
        before = snapshot(conn)
        assert run(conn, env, **changes)["status"] == "error"
        assert snapshot(conn) == before


@pytest.mark.parametrize("field", ["has_batch", "has_serial", "is_stock_item", "status"])
def test_unsupported_item_refused(conn, env, field):
    values = {"has_batch": 1, "has_serial": 1, "is_stock_item": 0, "status": "disabled"}
    conn.execute(f"UPDATE item SET {field}=? WHERE id=?", (values[field], env["item1"]))
    conn.commit()
    before = snapshot(conn)
    assert run(conn, env)["status"] == "error"
    assert snapshot(conn) == before


def test_group_warehouse_refused(conn, env):
    conn.execute("UPDATE warehouse SET is_group=1 WHERE id=?", (env["warehouse2"],))
    conn.commit()
    before = snapshot(conn)
    assert run(conn, env)["status"] == "error"
    assert snapshot(conn) == before


def test_source_shortage_writes_nothing(conn, env):
    before = snapshot(conn)
    assert run(conn, env, max_qty="101.00")["status"] == "error"
    assert snapshot(conn) == before


def test_active_reservation_reduces_source_headroom(conn, env):
    conn.execute("INSERT INTO stock_reservation_entry (id,voucher_type,voucher_id,item_id,warehouse_id,company_id,reserved_qty,status) VALUES ('reserve','sales_order','order',?,?,?, '99.00','active')",
                 (env["item1"], env["warehouse"], env["company_id"]))
    conn.commit()
    before = snapshot(conn)
    assert run(conn, env)["status"] == "error"
    assert snapshot(conn) == before


def test_future_source_stock_does_not_supply_backdated_draft(conn, env):
    conn.execute("UPDATE stock_ledger_entry SET posting_date='2026-07-01' WHERE warehouse_id=?",
                 (env["warehouse"],))
    conn.commit()
    before = snapshot(conn)
    assert run(conn, env)["status"] == "error"
    assert snapshot(conn) == before


def test_equal_minimum_does_not_trigger(conn, env):
    seed_stock_entry_sle(conn, env["item1"], env["warehouse2"], qty="5.00")
    before = snapshot(conn)
    assert run(conn, env)["triggered"] is False
    assert snapshot(conn) == before


def test_router_declares_transfer_draft_action():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    module = ast.parse((root / "scripts" / "db_query.py").read_text())
    mappings = [node.value for node in module.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "ACTION_MAP" for target in node.targets)]
    assert ast.literal_eval(mappings[0])["add-location-resupply"] == "erpclaw-inventory"
    assert mod.ACTIONS["add-location-resupply"] is mod.add_location_resupply


def test_real_router_creates_transfer_draft(conn, env, db_path):
    import json
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    # Closing the only connection checkpoints committed fixture data before copying.
    conn.close()
    home = Path(db_path).parent / "resupply-home"
    home.mkdir()
    (home / "lib").symlink_to(Path(os.environ["ERPCLAW_HOME"]) / "lib", target_is_directory=True)
    db = home / "data.sqlite"
    shutil.copy2(db_path, db)
    router = Path(__file__).resolve().parents[2] / "db_query.py"
    child_env = dict(os.environ, ERPCLAW_HOME=str(home), ERPCLAW_DB_PATH=str(db),
                     PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, str(router), "--action", "add-location-resupply",
         "--db-path", str(db), "--company-id", env["company_id"],
         "--item-id", env["item1"], "--warehouse-id", env["warehouse"],
         "--target-warehouse", env["warehouse2"], "--posting-date", "2026-06-15",
         "--min-qty", "5.00", "--max-qty", "15.25"],
        env=child_env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["stock_entry_id"]
