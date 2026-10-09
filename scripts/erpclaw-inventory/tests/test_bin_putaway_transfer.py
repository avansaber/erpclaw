"""Company-owned bin nodes and receipt-linked putaway transfers."""
from decimal import Decimal
import json

import pytest
from inventory_helpers import (
    call_action, ns, load_db_query, seed_company, seed_warehouse, seed_account,
)

mod = load_db_query()


def snapshot(conn):
    return tuple(conn.iterdump())


def parent(conn, env):
    node = seed_warehouse(conn, env["company_id"], "Bin Group", env["stock_acct"])
    conn.execute("UPDATE warehouse SET is_group=1 WHERE id=?", (node,))
    conn.commit()
    return node


def add_bin(conn, env, **extra):
    values = dict(company_id=env["company_id"], parent_id=parent(conn, env), name="A-01")
    values.update(extra)
    return call_action(mod.add_bin_location, conn, ns(**values))


def receipt(conn, env, quantities=("20.00",)):
    result = call_action(mod.add_stock_entry, conn, ns(
        entry_type="receive", company_id=env["company_id"], posting_date="2026-06-10",
        items=json.dumps([dict(item_id=env["item1"], qty=qty, rate="50.00",
                               to_warehouse_id=env["warehouse"]) for qty in quantities])))
    assert result["status"] == "ok", result
    receipt_id = result["stock_entry_id"]
    posted = call_action(mod.submit_stock_entry, conn, ns(stock_entry_id=receipt_id))
    assert posted["status"] == "ok", posted
    return receipt_id


def rule(conn, env, target, **changes):
    values = dict(company_id=env["company_id"], name="Putaway", target_warehouse_id=target,
                  match_item_id=env["item1"], match_item_group=None, priority="1")
    values.update(changes)
    result = call_action(mod.add_putaway_rule, conn, ns(**values))
    assert result["status"] == "ok", result


def putaway(conn, env, receipt_id, **changes):
    values = dict(company_id=env["company_id"], stock_entry_id=receipt_id,
                  posting_date="2026-06-15")
    values.update(changes)
    return call_action(mod.create_putaway_transfer, conn, ns(**values))


def test_bin_inherits_stock_account_and_leaf_identity(conn, env):
    group = parent(conn, env)
    result = add_bin(conn, env, parent_id=group)
    assert result["status"] == "ok", result
    row = conn.execute("SELECT * FROM warehouse WHERE id=?", (result["warehouse_id"],)).fetchone()
    assert (row["parent_id"], row["is_group"], row["warehouse_type"], row["company_id"], row["account_id"]) == (
        group, 0, "stores", env["company_id"], env["stock_acct"])
    before = snapshot(conn)
    duplicate = call_action(mod.add_bin_location, conn, ns(company_id=env["company_id"], parent_id=group, name="A-01"))
    assert duplicate["status"] == "error"
    assert snapshot(conn) == before


def test_putaway_plan_draft_posting_and_replay(conn, env, db_path):
    target_account = seed_account(conn, env["company_id"], "Bin Stock", "asset", "stock", "1210")
    seed_account(conn, env["company_id"], "COGS", "expense", "cost_of_goods_sold", "5100")
    target = add_bin(conn, env, account_id=target_account)["warehouse_id"]
    receipt_id = receipt(conn, env)
    rule(conn, env, target)
    plan = call_action(mod.apply_putaway_on_receipt, conn, ns(stock_entry_id=receipt_id))
    assert plan["routes"][0]["target_warehouse_id"] == target
    result = putaway(conn, env, receipt_id)
    assert result["status"] == "ok", result
    transfer_id = result["stock_entry_id"]
    record = conn.execute("SELECT * FROM stock_entry WHERE id=?", (transfer_id,)).fetchone()
    assert (record["status"], record["purpose_reference_type"], record["purpose_reference_id"]) == (
        "draft", "putaway_receipt", receipt_id)
    item = conn.execute("SELECT * FROM stock_entry_item WHERE stock_entry_id=?", (transfer_id,)).fetchone()
    assert (item["from_warehouse_id"], item["to_warehouse_id"], item["quantity"], item["amount"]) == (
        env["warehouse"], target, "20.00", "1000.00")
    before = snapshot(conn)
    assert putaway(conn, env, receipt_id)["stock_entry_id"] == transfer_id
    assert snapshot(conn) == before
    posted = call_action(mod.submit_stock_entry, conn, ns(stock_entry_id=transfer_id))
    assert posted["status"] == "ok", posted
    assert Decimal(mod.get_stock_balance(conn, env["item1"], env["warehouse"])["qty"]) == Decimal("100.00")
    assert Decimal(mod.get_stock_balance(conn, env["item1"], target)["qty"]) == Decimal("20.00")
    legs = conn.execute("SELECT account_id,debit,credit FROM gl_entry WHERE voucher_id=? AND is_cancelled=0", (transfer_id,)).fetchall()
    assert {(r["account_id"], r["debit"], r["credit"]) for r in legs} == {
        (env["stock_acct"], "0.00", "1000.00"), (target_account, "1000.00", "0.00")}
    from erpclaw_lib.gl_invariants import check_gl_invariants
    proof = check_gl_invariants(db_path)
    assert proof["result"] == "pass", proof
    assert proof["verified"] == 5
    before = snapshot(conn)
    replay = putaway(conn, env, receipt_id)
    assert replay["existing_transfer"] is True
    assert replay["transfer_status"] == "submitted"
    assert snapshot(conn) == before


@pytest.mark.parametrize("case", ["foreign-parent", "leaf-parent", "foreign-account", "expense-account", "blank-name"])
def test_bin_refusals_write_nothing(conn, env, case):
    group = parent(conn, env)
    values = dict(company_id=env["company_id"], parent_id=group, name="A-01")
    if case == "foreign-parent":
        values["company_id"] = seed_company(conn)
    elif case == "leaf-parent":
        values["parent_id"] = env["warehouse"]
    elif case == "foreign-account":
        values["account_id"] = seed_account(conn, seed_company(conn), "Foreign", "asset", "stock")
    elif case == "expense-account":
        values["account_id"] = env["expense"]
    else:
        values["name"] = " "
    before = snapshot(conn)
    assert call_action(mod.add_bin_location, conn, ns(**values))["status"] == "error"
    assert snapshot(conn) == before
    assert not conn.in_transaction


@pytest.mark.parametrize("case", ["foreign-company", "foreign-target", "group-target", "before-receipt", "bad-date", "batch", "serial", "inactive", "bad-rate", "bad-quantity", "draft-receipt"])
def test_putaway_refusals_before_draft_writes(conn, env, case):
    receipt_id = receipt(conn, env)
    target = env["warehouse2"]
    changes = {}
    if case == "foreign-company":
        changes["company_id"] = seed_company(conn)
    elif case == "foreign-target":
        target = seed_warehouse(conn, seed_company(conn), "Foreign")
    elif case == "group-target":
        target = parent(conn, env)
    elif case == "before-receipt":
        changes["posting_date"] = "2026-06-09"
    elif case == "bad-date":
        changes["posting_date"] = "June 15"
    elif case in ("batch", "serial"):
        conn.execute(f"UPDATE item SET has_{case}=1 WHERE id=?", (env["item1"],))
    elif case == "inactive":
        conn.execute("UPDATE item SET status='disabled' WHERE id=?", (env["item1"],))
    elif case == "bad-rate":
        conn.execute("UPDATE item SET standard_rate='NaN' WHERE id=?", (env["item1"],))
    elif case == "bad-quantity":
        conn.execute("UPDATE stock_entry_item SET quantity='NaN' WHERE stock_entry_id=?", (receipt_id,))
    else:
        conn.execute("UPDATE stock_entry SET status='draft' WHERE id=?", (receipt_id,))
    conn.commit()
    rule(conn, env, target)
    before = snapshot(conn)
    result = putaway(conn, env, receipt_id, **changes)
    assert result["status"] == "error", result
    assert snapshot(conn) == before
    assert not conn.in_transaction


def test_no_rule_and_same_target_do_not_write(conn, env):
    receipt_id = receipt(conn, env)
    before = snapshot(conn)
    assert putaway(conn, env, receipt_id)["created"] is False
    assert snapshot(conn) == before
    rule(conn, env, env["warehouse"])
    before = snapshot(conn)
    assert putaway(conn, env, receipt_id)["created"] is False
    assert snapshot(conn) == before


def test_duplicate_lines_sum_source_headroom_before_any_write(conn, env):
    receipt_id = receipt(conn, env, ("10.00", "15.25"))
    rule(conn, env, env["warehouse2"])
    conn.execute("INSERT INTO stock_reservation_entry (id,voucher_type,item_id,warehouse_id,company_id,reserved_qty,status) VALUES ('r','manual',?,?,?,'110.00','active')", (env["item1"], env["warehouse"], env["company_id"]))
    conn.commit()
    before = snapshot(conn)
    assert putaway(conn, env, receipt_id)["status"] == "error"
    assert snapshot(conn) == before


def test_item_rule_precedes_group_rule(conn, env):
    receipt_id = receipt(conn, env)
    conn.execute("INSERT INTO item_group (id,name) VALUES ('putaway-group','Putaway Group')")
    conn.execute("UPDATE item SET item_group_id='putaway-group' WHERE id=?", (env["item1"],))
    conn.commit()
    group_name = "Putaway Group"
    rule(conn, env, env["warehouse"], match_item_id=None, match_item_group=group_name, priority="0")
    rule(conn, env, env["warehouse2"], priority="9")
    result = putaway(conn, env, receipt_id)
    assert result["status"] == "ok", result
    row = conn.execute("SELECT to_warehouse_id FROM stock_entry_item WHERE stock_entry_id=?", (result["stock_entry_id"],)).fetchone()
    assert row[0] == env["warehouse2"]


def test_foreign_group_is_not_used_for_company_rule_matching(conn, env):
    receipt_id = receipt(conn, env)
    foreign = seed_company(conn)
    conn.execute("INSERT INTO item_group (id,name,company_id) VALUES ('foreign-group','Other Group',?)", (foreign,))
    conn.execute("UPDATE item SET item_group_id='foreign-group' WHERE id=?", (env["item1"],))
    conn.commit()
    rule(conn, env, env["warehouse2"], match_item_id=None, match_item_group="Other Group")
    before = snapshot(conn)
    assert putaway(conn, env, receipt_id)["created"] is False
    assert snapshot(conn) == before


def test_real_router_bin_and_putaway_draft(conn, env, db_path):
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    group = parent(conn, env)
    receipt_id = receipt(conn, env)
    conn.close()
    home = Path(db_path).parent / "putaway-home"
    home.mkdir()
    (home / "lib").symlink_to(Path(os.environ["ERPCLAW_HOME"]) / "lib", target_is_directory=True)
    db = home / "data.sqlite"
    shutil.copy2(db_path, db)
    router = Path(__file__).resolve().parents[2] / "db_query.py"
    child_env = dict(os.environ, ERPCLAW_HOME=str(home), ERPCLAW_DB_PATH=str(db),
                     PYTHONDONTWRITEBYTECODE="1")

    def invoke(action, flags):
        result = subprocess.run([sys.executable, str(router), "--action", action,
                                 "--db-path", str(db), "--company-id", env["company_id"], *flags],
                                env=child_env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    bin_id = invoke("add-bin-location", ["--parent-id", group, "--name", "CLI Bin"])["warehouse_id"]
    invoke("add-putaway-rule", ["--name", "CLI Putaway", "--match-item", env["item1"],
                               "--target-warehouse", bin_id])
    result = invoke("create-putaway-transfer", ["--stock-entry", receipt_id,
                                                "--posting-date", "2026-06-15"])
    assert result["stock_entry_id"]
