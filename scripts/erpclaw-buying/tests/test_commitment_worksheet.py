"""Buying commitment calculations separate requests, orders and relief."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from decimal import Decimal

import pytest

from buying_helpers import build_buying_env, call_action, init_all_tables, load_db_query, ns, seed_company
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table


MOD = load_db_query()
SCRIPTS = Path(__file__).resolve().parents[2]


def args(**overrides):
    values = dict(material_request_id=None, request_type="purchase", items=None,
                  company_id=None, supplier_id=None, posting_date="2026-10-01",
                  tax_template_id=None, purchase_order_id=None, dimensions=None,
                  dimension_key=None, dimension_value=None, worksheet_json=None,
                  worksheet_id=None)
    values.update(overrides)
    return ns(**values)


def rows(conn, name):
    table = Table(name)
    return [dict(r) for r in conn.execute(Q.from_(table).select(table.star).orderby(table.id).get_sql()).fetchall()]


def update(conn, name, row_id, **values):
    table = Table(name)
    query = Q.update(table)
    for key in values:
        query = query.set(table[key], P())
    conn.execute(query.where(table.id == P()).get_sql(), (*values.values(), row_id))
    conn.commit()


def insert(conn, name, **values):
    table = Table(name)
    query = Q.into(table).columns(*values).insert(*(P() for _ in values))
    conn.execute(query.get_sql(), tuple(values.values()))
    conn.commit()


@pytest.fixture
def worksheet(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "data.sqlite"
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    monkeypatch.setenv("ERPCLAW_DB_PATH", str(path))
    init_all_tables(str(path))
    conn = get_connection(str(path))
    try:
        env = build_buying_env(conn)
        mr = call_action(MOD.add_material_request, conn, args(company_id=env["company_id"], items=json.dumps([
            {"item_id": env["item1"], "qty": "10", "warehouse_id": env["warehouse"]}])))
        rid = mr["material_request_id"]
        assert call_action(MOD.submit_material_request, conn, args(material_request_id=rid))["status"] == "ok"
        assert rows(conn, "material_request")[0]["status"] == "submitted"
        request_line = rows(conn, "material_request_item")[0]
        po = call_action(MOD.create_po_from_material_request, conn, args(material_request_id=rid,
            supplier_id=env["supplier"], items=json.dumps([
                {"material_request_item_id": request_line["id"], "qty": "4", "rate": "10.01"}])))
        pid = po["purchase_order_id"]
        assert call_action(MOD.submit_purchase_order, conn, args(purchase_order_id=pid))["status"] == "ok"
        assert rows(conn, "purchase_order")[0]["status"] == "confirmed"
        order_line = rows(conn, "purchase_order_item")[0]
        data = {"fund_reference": "GENERAL", "award_reference": "AWARD-2026",
                "fiscal_year_id": env["fiscal_year_id"], "budget_amount": "1000.00", "actual_amount": "100.00",
                "requisitions": [{"material_request_id": rid, "rates": [
                    {"material_request_item_id": request_line["id"], "unit_rate": "10.01"}]}],
                "purchase_orders": [{"purchase_order_id": pid, "requisition_links": [
                    {"purchase_order_item_id": order_line["id"], "material_request_item_id": request_line["id"]}]}]}
        yield conn, env, data, home
    finally:
        conn.close()


def add(conn, env, data):
    return call_action(MOD.ACTIONS["add-commitment-worksheet"], conn,
                       args(company_id=env["company_id"], worksheet_json=json.dumps(data)))


def snapshot(conn):
    return {name: rows(conn, name) for name in ("material_request", "material_request_item", "purchase_order", "purchase_order_item",
            "purchase_receipt", "purchase_receipt_item", "purchase_invoice", "purchase_invoice_item", "gl_entry", "stock_ledger_entry", "payment_ledger_entry", "audit_log")}


def test_exact_distinct_commitments_and_persistent_snapshot(worksheet):
    conn, env, data, home = worksheet
    before = snapshot(conn)
    result = add(conn, env, data)
    assert result["status"] == "ok", result
    assert result["pre_encumbrance"] == "60.06"
    assert result["encumbrance"] == "40.04"
    assert result["available_balance"] == "799.90"
    assert Decimal(result["available_balance"]) == Decimal("1000.00") - Decimal("100.00") - Decimal("100.10")
    assert result["enforced"] is False
    assert result["budget_exceeded"] is False
    after = snapshot(conn)
    for name in before.keys() - {"audit_log"}:
        assert before[name] == after[name]
    assert len(after["audit_log"]) == len(before["audit_log"]) + 1
    audit_rows = [r for r in after["audit_log"] if r["entity_id"] == result["worksheet_id"]]
    assert len(audit_rows) == 1
    assert audit_rows[0]["entity_type"] == "commitment_worksheet"
    assert json.loads(audit_rows[0]["new_values"])["available_balance"] == "799.90"
    with get_connection(str(home / "data.sqlite")) as reopened:
        stored = call_action(MOD.get_commitment_worksheet, reopened, args(company_id=env["company_id"], worksheet_id=result["worksheet_id"]))
        assert stored == result


def relief(conn, env, data, kind, quantity, status="submitted"):
    pid = data["purchase_orders"][0]["purchase_order_id"]
    lid = data["purchase_orders"][0]["requisition_links"][0]["purchase_order_item_id"]
    parent_id = str(uuid.uuid4())
    insert(conn, "purchase_" + kind, id=parent_id, supplier_id=env["supplier"],
           company_id=env["company_id"], purchase_order_id=pid, posting_date="2026-10-02", status=status)
    insert(conn, "purchase_" + kind + "_item", id=str(uuid.uuid4()),
           **{"purchase_" + kind + "_id": parent_id}, purchase_order_item_id=lid,
           item_id=env["item1"], quantity=quantity)
    return parent_id


def test_receipt_and_invoice_relief_counted_once(worksheet):
    conn, env, data, _ = worksheet
    relief(conn, env, data, "receipt", "2")
    relief(conn, env, data, "invoice", "2")
    result = add(conn, env, data)
    assert result["encumbrance"] == "20.02"
    assert result["pre_encumbrance"] == "60.06"
    assert result["available_balance"] == "819.92"


@pytest.mark.parametrize("kind", ["receipt", "invoice"])
def test_relief_with_a_different_uom_refuses_without_writes(worksheet, kind):
    conn, env, data, _ = worksheet
    relief(conn, env, data, kind, "2")
    line = rows(conn, "purchase_" + kind + "_item")[0]
    update(conn, "purchase_" + kind + "_item", line["id"], uom="Box")
    before = snapshot(conn)
    result = add(conn, env, data)
    assert result["status"] == "error"
    assert result["message"] == "Commitment relief and purchase order must use the same UOM"
    assert snapshot(conn) == before


@pytest.mark.parametrize("kind", ["receipt", "invoice"])
def test_matching_explicit_relief_uom_preserves_exact_amount(worksheet, kind):
    conn, env, data, _ = worksheet
    relief(conn, env, data, kind, "2")
    line = rows(conn, "purchase_" + kind + "_item")[0]
    update(conn, "purchase_" + kind + "_item", line["id"], uom="Each")
    result = add(conn, env, data)
    assert result["status"] == "ok"
    assert result["encumbrance"] == "20.02"
    assert result["available_balance"] == "819.92"


@pytest.mark.parametrize("kind,status", [("receipt", "draft"), ("receipt", "cancelled"), ("invoice", "draft"), ("invoice", "cancelled")])
def test_unposted_or_cancelled_relief_does_not_reduce_commitment(worksheet, kind, status):
    conn, env, data, _ = worksheet
    relief(conn, env, data, kind, "4", status)
    assert add(conn, env, data)["encumbrance"] == "40.04"


@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_closed_orders_have_no_remaining_commitment(worksheet, status):
    conn, env, data, _ = worksheet
    relief(conn, env, data, "receipt", "4")
    receipt_line = rows(conn, "purchase_receipt_item")[0]
    update(conn, "purchase_receipt_item", receipt_line["id"], uom="Box")
    update(conn, "purchase_order", data["purchase_orders"][0]["purchase_order_id"], status=status)
    before = snapshot(conn)
    result = add(conn, env, data)
    assert result["status"] == "ok", result
    assert result["encumbrance"] == "0.00"
    after = snapshot(conn)
    for name in before.keys() - {"audit_log"}:
        assert before[name] == after[name]
    assert len(after["audit_log"]) == len(before["audit_log"]) + 1


def test_tax_and_discount_remainders_are_exact(worksheet):
    conn, env, data, _ = worksheet
    po = data["purchase_orders"][0]
    lid = po["requisition_links"][0]["purchase_order_item_id"]
    update(conn, "purchase_order_item", lid, net_amount="36.04", discount_percentage="10")
    update(conn, "purchase_order", po["purchase_order_id"], total_amount="36.04", tax_amount="3.61", grand_total="39.65")
    relief(conn, env, data, "receipt", "2")
    assert add(conn, env, data)["encumbrance"] == "19.83"


def test_exceeded_budget_is_reported_without_enforcement(worksheet):
    conn, env, data, _ = worksheet
    data["budget_amount"] = "100.00"
    result = add(conn, env, data)
    assert result["available_balance"] == "-100.10"
    assert result["budget_exceeded"] is True
    assert result["enforced"] is False


@pytest.mark.parametrize("field,value", [("budget_amount", 1.1), ("actual_amount", "-1"), ("budget_amount", "NaN"),
    ("budget_amount", "1.001"), ("fund_reference", ""), ("award_reference", []), ("fiscal_year_id", "missing"),
    ("requisitions", {}), ("purchase_orders", []), ("unexpected", True)])
def test_invalid_input_writes_nothing(worksheet, field, value):
    conn, env, data, _ = worksheet
    data[field] = value
    before = snapshot(conn)
    result = add(conn, env, data)
    assert result["status"] == "error", result
    assert snapshot(conn) == before


@pytest.mark.parametrize("mutation", ["repeated_request", "repeated_order", "missing_rates", "missing_link", "duplicate_link", "wrong_item", "draft_order", "wrong_year", "foreign_company", "wrong_fund", "wrong_award", "foreign_currency"])
def test_document_and_link_refusals_write_nothing(worksheet, mutation):
    conn, env, data, _ = worksheet
    po = data["purchase_orders"][0]
    if mutation == "repeated_request":
        data["requisitions"].append(copy.deepcopy(data["requisitions"][0]))
    elif mutation == "repeated_order":
        data["purchase_orders"].append(copy.deepcopy(po))
    elif mutation == "missing_rates":
        data["requisitions"][0]["rates"] = []
    elif mutation == "missing_link":
        po["requisition_links"] = []
    elif mutation == "duplicate_link":
        po["requisition_links"].append(copy.deepcopy(po["requisition_links"][0]))
    elif mutation == "wrong_item":
        update(conn, "purchase_order_item", po["requisition_links"][0]["purchase_order_item_id"], item_id=env["item2"])
    elif mutation == "draft_order":
        update(conn, "purchase_order", po["purchase_order_id"], status="draft")
    elif mutation == "wrong_year":
        update(conn, "purchase_order", po["purchase_order_id"], order_date="2025-10-01")
    elif mutation == "foreign_company":
        update(conn, "purchase_order", po["purchase_order_id"], company_id=seed_company(conn, "Other"))
    elif mutation in ("wrong_fund", "wrong_award"):
        update(conn, "purchase_order", po["purchase_order_id"], dimensions_json=json.dumps({mutation[6:]: "OTHER"}))
    elif mutation == "foreign_currency":
        update(conn, "purchase_order", po["purchase_order_id"], currency="EUR")
    before = snapshot(conn)
    assert add(conn, env, data)["status"] == "error"
    assert snapshot(conn) == before


def test_saved_snapshot_is_company_scoped_and_read_does_not_write(worksheet):
    conn, env, data, _ = worksheet
    saved = add(conn, env, data)
    other = seed_company(conn, "Other")
    before = snapshot(conn)
    result = call_action(MOD.get_commitment_worksheet, conn, args(company_id=other, worksheet_id=saved["worksheet_id"]))
    assert result["status"] == "error"
    assert snapshot(conn) == before


def test_both_actions_routed_through_foundation(worksheet):
    conn, env, data, home = worksheet
    process_env = {**os.environ, "ERPCLAW_HOME": str(home), "PYTHONPATH": str(SCRIPTS / "erpclaw-setup" / "lib")}
    first = subprocess.run([sys.executable, str(SCRIPTS / "db_query.py"), "--action", "add-commitment-worksheet",
        "--company-id", env["company_id"], "--worksheet-json", json.dumps(data)], env=process_env, capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    saved = json.loads(first.stdout)
    second = subprocess.run([sys.executable, str(SCRIPTS / "db_query.py"), "--action", "get-commitment-worksheet",
        "--company-id", env["company_id"], "--worksheet-id", saved["worksheet_id"]], env=process_env, capture_output=True, text=True)
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)["available_balance"] == "799.90"
