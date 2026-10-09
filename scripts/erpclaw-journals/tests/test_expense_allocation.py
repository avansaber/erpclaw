"""Internal recharges use exact cents and the ordinary journal lifecycle."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from decimal import Decimal

import pytest
from journals_helpers import call_action, is_error, is_ok, load_db_query, ns, seed_account, _uuid

mod = load_db_query()
D = Decimal


@pytest.fixture
def centers(conn, env):
    ids = [_uuid(), _uuid()]
    for index, center in enumerate(ids):
        conn.execute("INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, ?, ?, 0)",
                     (center, f"Department {index}", env["company_id"]))
    conn.commit()
    return ids


def _args(env, centers, amount="500.00", **changes):
    values = dict(company_id=env["company_id"], source_account_id=env["expense"],
                  source_cost_center_id=env["cc"], posting_date="2026-06-20", amount=amount,
                  allocations=json.dumps([{"cost_center_id": centers[0], "percentage": "60"},
                                          {"cost_center_id": centers[1], "percentage": "40"}]))
    values.update(changes)
    return ns(**values)


def _snapshot(conn):
    return "\n".join(conn.iterdump())


def _rows(conn, journal_id, centers):
    rows = conn.execute("SELECT account_id, debit, credit, cost_center_id, dimensions_json "
                        "FROM journal_entry_line WHERE journal_entry_id = ?",
                        (journal_id,)).fetchall()
    return sorted(rows, key=lambda row: centers.index(row["cost_center_id"]))


def test_balanced_draft_submits_and_dimensions_reach_books(conn, env, centers, db_path):
    original = call_action(mod.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date="2026-06-20", entry_type="journal", remark="Service costs",
        cwip_asset_id=None, lines=json.dumps([
            {"account_id": env["expense"], "debit": "500.00", "credit": "0.00", "cost_center_id": env["cc"]},
            {"account_id": env["cash"], "debit": "0.00", "credit": "500.00"}])))
    assert is_ok(original), original
    posted = call_action(mod.submit_journal_entry, conn, ns(journal_entry_id=original["journal_entry_id"]))
    assert is_ok(posted), posted
    before_gl = conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0]
    result = call_action(mod.create_expense_allocation, conn, _args(env, centers))
    assert is_ok(result), result
    journal_id = result["journal_entry_id"]
    rows = _rows(conn, journal_id, [env["cc"], *centers])
    assert [(D(r["debit"]), D(r["credit"])) for r in rows] == [
        (D("0"), D("500")), (D("300"), D("0")), (D("200"), D("0"))]
    assert [r["cost_center_id"] for r in rows] == [env["cc"], *centers]
    assert [json.loads(r["dimensions_json"]) for r in rows] == [
        {"cost_center": center} for center in [env["cc"], *centers]]
    assert conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0] == before_gl
    submitted = call_action(mod.submit_journal_entry, conn, ns(journal_entry_id=journal_id))
    assert is_ok(submitted), submitted
    gl = conn.execute("SELECT debit, credit, dimensions_json FROM gl_entry WHERE voucher_id = ?",
                      (journal_id,)).fetchall()
    assert len(gl) == 3
    assert sum(D(r["debit"]) for r in gl) == sum(D(r["credit"]) for r in gl) == D("500.00")
    assert {json.loads(r["dimensions_json"])["cost_center"] for r in gl} == {env["cc"], *centers}
    from erpclaw_lib.gl_invariants import check_gl_invariants
    checks = check_gl_invariants(db_path)
    assert checks["result"] == "pass", checks
    assert checks["verified"] == 4
    assert checks["vacuous"] == ["valid_fiscal_year"]
    assert checks["unexamined"] == []
    assert checks["violations"] == []


@pytest.mark.parametrize("amount, percentages, expected", [
    ("50.005", ["50", "50"], ["25.01", "25.00"]),
    ("0.01", ["50", "50"], ["0.01"]),
    ("1.00", ["33.333333", "66.666667"], ["0.33", "0.67"]),
])
def test_remainder_cents_preserve_exact_total(conn, env, centers, amount, percentages, expected):
    allocations = json.dumps([{"cost_center_id": center, "percentage": percent}
                              for center, percent in zip(centers, percentages)])
    result = call_action(mod.create_expense_allocation, conn, _args(env, centers, amount, allocations=allocations))
    assert is_ok(result), result
    rows = _rows(conn, result["journal_entry_id"], [env["cc"], *centers])
    assert [r["debit"] for r in rows[1:]] == expected
    assert sum(D(r["debit"]) for r in rows) == sum(D(r["credit"]) for r in rows)


def test_target_can_use_another_owned_expense_account(conn, env, centers):
    target = seed_account(conn, env["company_id"], "Rent recharge", "expense", "expense", "5100")
    allocation = json.dumps([{"account_id": target, "cost_center_id": centers[0], "percentage": "100"}])
    result = call_action(mod.create_expense_allocation, conn, _args(env, centers, allocations=allocation))
    assert is_ok(result), result
    assert _rows(conn, result["journal_entry_id"], [env["cc"], *centers])[1]["account_id"] == target


@pytest.mark.parametrize("amount", ["0", "-0.001", "0.004", "NaN", "Infinity", "1e100000", True, 1.5])
def test_bad_amount_refuses_before_any_write(conn, env, centers, amount):
    before = _snapshot(conn)
    result = call_action(mod.create_expense_allocation, conn, _args(env, centers, amount))
    assert is_error(result), result
    assert _snapshot(conn) == before


@pytest.mark.parametrize("case", ["sum", "duplicate", "source", "float", "precision", "unknown", "empty", "json", "header_dimensions"])
def test_bad_allocation_refuses_before_any_write(conn, env, centers, case):
    targets = [{"cost_center_id": centers[0], "percentage": "60"},
               {"cost_center_id": centers[1], "percentage": "40"}]
    if case == "sum": targets[1]["percentage"] = "39.99"
    if case == "duplicate": targets[1]["cost_center_id"] = centers[0]
    if case == "source": targets[0]["cost_center_id"] = env["cc"]
    if case == "float": targets[0]["percentage"] = 60.0
    if case == "precision": targets[0]["percentage"] = "60.0000001"
    if case == "unknown": targets[0]["dimensions"] = {"project": "foreign"}
    if case == "empty": targets = []
    raw = "{" if case == "json" else json.dumps(targets)
    before = _snapshot(conn)
    changes = {"dimensions": json.dumps({"project": "foreign"})} if case == "header_dimensions" else {}
    result = call_action(mod.create_expense_allocation, conn, _args(env, centers, allocations=raw, **changes))
    assert is_error(result), result
    assert _snapshot(conn) == before


@pytest.mark.parametrize("case", ["company", "source_account", "target_account", "source_center", "target_center", "frozen", "group", "asset", "date"])
def test_scope_and_leaf_refusals_write_nothing(conn, env, centers, case):
    other_company = _uuid()
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, 'Other Company', 'OC')", (other_company,))
    account = seed_account(conn, other_company, "Foreign expense", "expense", "expense", "5200")
    center = _uuid()
    conn.execute("INSERT INTO cost_center (id, name, company_id, is_group) VALUES (?, 'Foreign', ?, 0)",
                 (center, other_company))
    changes = {}
    if case == "company": changes["company_id"] = other_company
    if case == "source_account": changes["source_account_id"] = account
    if case == "source_center": changes["source_cost_center_id"] = center
    if case in ("target_account", "target_center"):
        changes["allocations"] = json.dumps([{"cost_center_id": center if case == "target_center" else centers[0],
                                              "account_id": account if case == "target_account" else env["expense"],
                                              "percentage": "100"}])
    if case == "frozen": conn.execute("UPDATE account SET is_frozen = 1 WHERE id = ?", (env["expense"],))
    if case == "group": conn.execute("UPDATE cost_center SET is_group = 1 WHERE id = ?", (env["cc"],))
    if case == "asset": changes["source_account_id"] = env["cash"]
    if case == "date": changes["posting_date"] = "20260620"
    conn.commit()
    before = _snapshot(conn)
    result = call_action(mod.create_expense_allocation, conn, _args(env, centers, **changes))
    assert is_error(result), result
    assert _snapshot(conn) == before


def test_root_router_creates_allocation_draft(conn, env, centers, db_path, tmp_path):
    home = tmp_path / "root-home"
    home.mkdir()
    conn.commit()
    conn.close()
    shutil.copy2(db_path, home / "data.sqlite")
    scripts = Path(__file__).resolve().parents[2]
    (home / "lib").symlink_to(scripts / "erpclaw-setup" / "lib", target_is_directory=True)
    process_env = dict(os.environ, ERPCLAW_HOME=str(home))
    process_env.pop("ERPCLAW_DB_PATH", None)
    command = [sys.executable, str(scripts / "db_query.py"), "--action", "create-expense-allocation",
               "--company-id", env["company_id"], "--source-account-id", env["expense"],
               "--source-cost-center-id", env["cc"], "--posting-date", "2026-06-20", "--amount", "500.00",
               "--allocations", json.dumps([{"cost_center_id": centers[0], "percentage": "100"}])]
    result = subprocess.run(command, env=process_env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    response = json.loads(result.stdout)
    assert is_ok(response), response
    assert response["journal_entry_id"]
    fetched = subprocess.run([sys.executable, str(scripts / "db_query.py"), "--action", "get-journal-entry",
                              "--journal-entry-id", response["journal_entry_id"]],
                             env=process_env, text=True, capture_output=True, timeout=30)
    assert fetched.returncode == 0, fetched.stdout + fetched.stderr
    assert D(json.loads(fetched.stdout)["total_debit"]) == D("500.00")
