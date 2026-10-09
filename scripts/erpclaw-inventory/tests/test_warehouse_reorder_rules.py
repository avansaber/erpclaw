"""Warehouse replenishment proposals use exact quantities and write nothing."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from decimal import Decimal

import pytest

from inventory_helpers import (
    call_action, load_db_query, ns, seed_company, seed_stock_entry_sle,
    seed_warehouse,
)


mod = load_db_query()


def _rule(env, **overrides):
    rule = {
        "item_id": env["item2"], "warehouse_id": env["warehouse"],
        "min_qty": "10.25", "max_qty": "50.55", "trigger": "stock",
        "interval_days": 7, "horizon_days": 14, "history_days": 10,
    }
    rule.update(overrides)
    return rule


def _call(conn, env, rules, **overrides):
    args = dict(company_id=env["company_id"], company_name=None,
                as_of_date="2026-01-10", reorder_rules=json.dumps(rules))
    args.update(overrides)
    return call_action(mod.check_reorder, conn, ns(**args))


def _snapshot(conn):
    return tuple(conn.iterdump())


def test_exact_warehouse_min_max_and_cross_warehouse_isolation(conn, env):
    seed_stock_entry_sle(conn, env["item2"], env["warehouse"], "5.125")
    seed_stock_entry_sle(conn, env["item2"], env["warehouse2"], "999")
    before = _snapshot(conn)
    result = _call(conn, env, [_rule(env)])
    assert result["status"] == "ok", result
    row = result["rules"][0]
    assert row["triggered"] is True
    assert Decimal(row["current_stock"]) == Decimal("5.125")
    assert Decimal(row["reorder_qty"]) == Decimal("45.425")
    assert result["items_below_reorder"] == 1
    assert row["next_review_date"] == "2026-01-17"
    assert _snapshot(conn) == before
    assert not conn.in_transaction


def test_forecast_trigger_uses_only_posted_company_warehouse_history(conn, env):
    for qty, date, cancelled, warehouse in (
        ("-5", "2026-01-08", 0, env["warehouse"]),
        ("-999", "2026-01-09", 1, env["warehouse"]),
        ("-777", "2026-01-11", 0, env["warehouse"]),
        ("-666", "2026-01-09", 0, env["warehouse2"]),
    ):
        sid = seed_stock_entry_sle(conn, env["item1"], warehouse, qty)
        conn.execute("UPDATE stock_ledger_entry SET posting_date=?, is_cancelled=? WHERE id=?",
                     (date, cancelled, sid))
    conn.commit()
    before = _snapshot(conn)
    rule = _rule(env, item_id=env["item1"], min_qty="90", max_qty="130",
                 trigger="forecast")
    result = _call(conn, env, [rule])
    assert result["status"] == "ok", result
    row = result["rules"][0]
    assert Decimal(row["current_stock"]) == Decimal("95")
    assert Decimal(row["projected_demand"]) == Decimal("7")
    assert Decimal(row["projected_stock"]) == Decimal("88")
    assert Decimal(row["reorder_qty"]) == Decimal("42")
    assert row["triggered"] is True
    stock_rule = dict(rule, trigger="stock")
    stock = _call(conn, env, [stock_rule])["rules"][0]
    assert stock["triggered"] is False
    assert Decimal(stock["reorder_qty"]) == 0
    assert _snapshot(conn) == before


def test_interval_defers_proposal_until_review_is_due(conn, env):
    rule = _rule(env, last_review_date="2026-01-07")
    before = _snapshot(conn)
    row = _call(conn, env, [rule])["rules"][0]
    assert row["review_due"] is False
    assert row["triggered"] is False
    assert row["next_review_date"] == "2026-01-14"
    assert Decimal(row["reorder_qty"]) == 0
    result = _call(conn, env, [rule], as_of_date="2026-01-14")
    assert result["rules"][0]["review_due"] is True
    assert result["rules"][0]["triggered"] is True
    assert result["rules"][0]["next_review_date"] == "2026-01-21"
    assert _snapshot(conn) == before


def test_large_exact_quantities_do_not_pass_through_float(conn, env):
    rule = _rule(env, min_qty="9007199254740992.25", max_qty="9007199254740992.55")
    seed_stock_entry_sle(conn, env["item2"], env["warehouse"], "9007199254740992.20")
    row = _call(conn, env, [rule])["rules"][0]
    assert Decimal(row["reorder_qty"]) == Decimal("0.35")


@pytest.mark.parametrize("override", [
    {"min_qty": "-0.001"}, {"max_qty": "1"}, {"min_qty": "NaN"},
    {"max_qty": 50.55}, {"interval_days": 0}, {"horizon_days": True},
    {"history_days": 367}, {"trigger": "guess"},
    {"last_review_date": "2026-01-11"}, {"warehouse_id": "missing"},
    {"item_id": "missing"}, {"unexpected": "ignored"},
])
def test_invalid_rule_refuses_without_writes(conn, env, override):
    before = _snapshot(conn)
    result = _call(conn, env, [_rule(env, **override)])
    assert result["status"] == "error", result
    assert _snapshot(conn) == before
    assert not conn.in_transaction


def test_foreign_warehouse_and_missing_company_refuse(conn, env):
    other = seed_company(conn, name="Other company")
    foreign = seed_warehouse(conn, other, name="Other store")
    before = _snapshot(conn)
    for company, rule in ((env["company_id"], _rule(env, warehouse_id=foreign)),
                          ("missing", _rule(env)), (None, _rule(env))):
        result = _call(conn, env, [rule], company_id=company)
        assert result["status"] == "error", result
        assert _snapshot(conn) == before


@pytest.mark.parametrize("raw", ["{}", "[]", "[1]", "bad-json"])
def test_invalid_rule_collection_refuses_without_writes(conn, env, raw):
    before = _snapshot(conn)
    result = _call(conn, env, [], reorder_rules=raw)
    assert result["status"] == "error", result
    assert _snapshot(conn) == before


def test_duplicate_item_warehouse_rule_refuses(conn, env):
    before = _snapshot(conn)
    result = _call(conn, env, [_rule(env), _rule(env)])
    assert result["status"] == "error", result
    assert _snapshot(conn) == before


@pytest.mark.parametrize("as_of", ["0001-01-01", "9999-12-31"])
def test_out_of_calendar_windows_refuse_without_writes(conn, env, as_of):
    before = _snapshot(conn)
    result = _call(conn, env, [_rule(env)], as_of_date=as_of)
    assert result["status"] == "error", result
    assert _snapshot(conn) == before


def test_real_router_runs_rule_on_readonly_file_without_changes(conn, env, db_path):
    seed_stock_entry_sle(conn, env["item2"], env["warehouse"], "5.125")
    home = Path(db_path).parent / "readonly-home"
    home.mkdir()
    (home / "lib").symlink_to(Path(os.environ["ERPCLAW_HOME"]) / "lib", target_is_directory=True)
    db = home / "data.sqlite"
    conn.execute("VACUUM INTO ?", (str(db),))
    conn.close()
    db.chmod(0o444)
    before = (hashlib.sha256(db.read_bytes()).hexdigest(),
              db.stat().st_mode, sorted(p.name for p in db.parent.iterdir()))
    router = Path(__file__).resolve().parents[2] / "db_query.py"
    child_env = dict(os.environ, ERPCLAW_HOME=str(home), ERPCLAW_DB_PATH=str(db),
                     ERPCLAW_DB_READONLY="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, str(router), "--action", "check-reorder", "--db-path", str(db),
         "--company-id", env["company_id"], "--as-of-date", "2026-01-10",
         "--reorder-rules", json.dumps([_rule(env)])],
        env=child_env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    answer = json.loads(result.stdout)
    assert Decimal(answer["rules"][0]["reorder_qty"]) == Decimal("45.425")
    assert (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mode,
            sorted(p.name for p in db.parent.iterdir())) == before
