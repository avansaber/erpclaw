"""Sales invoice targets behind single-use authorization."""
import os
import sys
import uuid

import pytest

_SI_TESTS = os.path.dirname(os.path.abspath(__file__))
_SI_MODULE = os.path.dirname(_SI_TESTS)
_SI_SETUP_TESTS = os.path.join(os.path.dirname(_SI_MODULE), "erpclaw-setup", "tests")
_SI_PAYMENTS_TESTS = os.path.join(
    os.path.dirname(_SI_MODULE), "erpclaw-payments", "tests")
if _SI_SETUP_TESTS not in sys.path:
    sys.path.insert(0, _SI_SETUP_TESTS)
if _SI_PAYMENTS_TESTS not in sys.path:
    sys.path.insert(0, _SI_PAYMENTS_TESTS)
if _SI_TESTS not in sys.path:
    sys.path.insert(0, _SI_TESTS)

_SEEDED = set()
_SEED_CAPS = set()


def _std(action, path, si_id, extra=None):
    argv = ["--action", action, "--db-path", path,
            "--sales-invoice-id", si_id]
    if extra:
        argv = argv + list(extra)
    return argv


def _si(path, env):
    import json as _json
    import selling_helpers as helpers
    from erpclaw_lib.db import get_connection
    items = _json.dumps([
        {"item_id": env["item1"], "qty": "10", "rate": "6.00",
         "warehouse_id": env["warehouse"]},
        {"item_id": env["item2"], "qty": "2", "rate": "20.00",
         "warehouse_id": env["warehouse"]},
    ])
    handle = get_connection(path)
    try:
        result = helpers.call_action(
            helpers.load_db_query().create_sales_invoice, handle,
            helpers.ns(sales_order_id=None, delivery_note_id=None,
                       customer_id=env["customer"],
                       company_id=env["company_id"],
                       posting_date="2026-06-20",
                       due_date="2026-07-20", items=items,
                       tax_template_id=None, payment_terms_id=None))
    finally:
        handle.close()
    assert result.get("status") == "ok", result
    assert result.get("grand_total") == "100.00", result
    return result["sales_invoice_id"]


def _update_si(path, si_id, values):
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import Field, P, Q, Table
    handle = get_connection(path)
    try:
        table = Table("sales_invoice")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("id") == P())
        params.append(si_id)
        handle.execute(query.get_sql(), params)
        handle.commit()
    finally:
        handle.close()


def _grant(path, company_id, si_id, action):
    import authority_fixtures as fx
    from erpclaw_lib import authority_clock
    from erpclaw_lib.db import get_connection
    if path not in _SEEDED:
        fx.seed_authority(path, company_id)
        _SEEDED.add(path)
    handle = get_connection(path)
    try:
        install_id = fx._install_id(handle)
        fx._insert_row(handle, "authority_right", {
            "install_id": install_id, "principal_id": fx.SERVICE,
            "company_id": company_id, "resource_kind": "sales-invoice",
            "resource_id": si_id, "action": action, "effect": "allow"})
        fx._insert_row(handle, "authority_delegation_right", {
            "install_id": install_id, "delegation_id": fx.DELEGATION,
            "company_id": company_id, "resource_kind": "sales-invoice",
            "resource_id": si_id, "action": action})
        if (path, action) not in _SEED_CAPS:
            now = authority_clock.now_ms()
            fx._insert_row(handle, "authority_delegation_cap", {
                "install_id": install_id, "delegation_id": fx.DELEGATION,
                "action": action, "currency": "USD", "scale": 2,
                "per_operation": "500.00", "aggregate_limit": "1000.00",
                "window_start": now - fx.DAY_MS,
                "window_end": now + 10 * fx.DAY_MS})
            _SEED_CAPS.add((path, action))
        handle.commit()
    finally:
        handle.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "sqlite")
    monkeypatch.delenv("ERPCLAW_DB_URL", raising=False)
    yield
    from erpclaw_lib import seam
    seam.dispose_engines()


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    import test_payment_envelope_gate as pgate
    import selling_helpers as helpers
    from erpclaw_lib.db import get_connection
    path = pgate._fresh_db(tmp_path, monkeypatch, "selling")
    handle = get_connection(path)
    try:
        env = helpers.build_selling_env(handle)
    finally:
        handle.close()
    return (path, env)


def test_projection_reads_the_database(db_env):
    import test_payment_envelope_gate as pgate
    path, env = db_env
    si = _si(path, env)
    _grant(path, env["company_id"], si, "submit-sales-invoice")
    _update_si(path, si, {"grand_total": "100"})
    from erpclaw_lib import authority_gate
    handle = pgate._open(path)
    try:
        derived = authority_gate.ENVELOPE_ACTIONS[
            "submit-sales-invoice"]["derive"](
            handle, "submit-sales-invoice", [["sales-invoice-id", si]])
    finally:
        handle.close()
    assert derived["company_ids"] == [env["company_id"]]
    assert len(derived["targets"]) == 1
    assert derived["targets"][0]["kind"] == "sales-invoice"
    assert derived["targets"][0]["id"] == si
    assert derived["amounts"] == [{"currency": "USD", "value": "100.00",
                                   "scale": 2}]
    auth_id = pgate._issue(path, "submit-sales-invoice",
                           _std("submit-sales-invoice", path, si))
    assert auth_id
    inv_cols = ("id", "company_id", "naming_series", "customer_id",
                "posting_date", "due_date", "currency", "exchange_rate",
                "total_amount", "tax_amount", "grand_total",
                "outstanding_amount", "rounding_adjustment",
                "tax_template_id", "payment_terms_id", "status",
                "sales_order_id", "delivery_note_id", "is_return",
                "return_against", "update_stock", "amended_from",
                "is_intercompany", "intercompany_reference_id",
                "dimensions_json")
    item_cols = ("id", "item_id", "quantity", "uom", "rate", "amount",
                 "discount_percentage", "net_amount",
                 "sales_order_item_id", "delivery_note_item_id",
                 "cost_center_id", "project_id")
    from erpclaw_lib.query import Field, P, Q, Table
    handle = pgate._open(path)
    try:
        inv_table = Table("sales_invoice")
        inv_query = Q.from_(inv_table).select(
            *[Field(column) for column in inv_cols]).where(
            Field("id") == P()).get_sql()
        inv_row = handle.execute(inv_query, (si,)).fetchone()
        invoice = {column: dict(inv_row)[column] for column in inv_cols}
        item_table = Table("sales_invoice_item")
        item_query = Q.from_(item_table).select(
            *[Field(column) for column in item_cols]).where(
            Field("sales_invoice_id") == P()).orderby(
            Field("id")).get_sql()
        item_rows = handle.execute(item_query, (si,)).fetchall()
    finally:
        handle.close()
    own_items = [{column: dict(record)[column] for column in item_cols}
                 for record in item_rows]
    assert invoice["company_id"] == env["company_id"]
    assert invoice["customer_id"] == env["customer"]
    assert invoice["posting_date"] == "2026-06-20"
    assert invoice["due_date"] == "2026-07-20"
    assert invoice["grand_total"] == "100"
    assert invoice["status"] == "draft"
    assert len(own_items) == 2
    import hashlib as _hl
    import json as _js
    want_text = _js.dumps(
        {"invoice": invoice, "items": own_items},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False)
    want_digest = _hl.sha256(want_text.encode("utf-8")).hexdigest()
    assert derived["targets"][0]["state_digest"] == want_digest
    _update_si(path, si, {"grand_total": "-100.00"})
    handle = pgate._open(path)
    try:
        negated = authority_gate.ENVELOPE_ACTIONS[
            "submit-sales-invoice"]["derive"](
            handle, "submit-sales-invoice", [["sales-invoice-id", si]])
    finally:
        handle.close()
    assert negated["amounts"] == [{"currency": "USD", "value": "100.00",
                                   "scale": 2}]
    _update_si(path, si, {"grand_total": "100.125"})
    handle = pgate._open(path)
    try:
        with pytest.raises(ValueError):
            authority_gate.ENVELOPE_ACTIONS[
                "submit-sales-invoice"]["derive"](
                handle, "submit-sales-invoice",
                [["sales-invoice-id", si]])
    finally:
        handle.close()
    _update_si(path, si, {"grand_total": "100.00"})
    handle = pgate._open(path)
    try:
        base = authority_gate.ENVELOPE_ACTIONS[
            "submit-sales-invoice"]["derive"](
            handle, "submit-sales-invoice", [["sales-invoice-id", si]])
    finally:
        handle.close()
    assert base["amounts"] == [{"currency": "USD", "value": "100.00",
                                "scale": 2}]
    import selling_helpers as helpers
    from erpclaw_lib.db import get_connection
    handle = get_connection(path)
    try:
        second_company = helpers.seed_company(handle)
    finally:
        handle.close()
    home_name = pgate._read_one(
        path, "company", ["id", "name"], env["company_id"])["name"]
    other_name = pgate._read_one(
        path, "company", ["id", "name"], second_company)["name"]
    assert other_name != home_name
    handle = pgate._open(path)
    try:
        with pytest.raises(ValueError):
            authority_gate.ENVELOPE_ACTIONS[
                "submit-sales-invoice"]["derive"](
                handle, "submit-sales-invoice",
                [["sales-invoice-id", si],
                 ["company-id", second_company]])
    finally:
        handle.close()
    handle = pgate._open(path)
    try:
        with pytest.raises(ValueError):
            authority_gate.ENVELOPE_ACTIONS[
                "submit-sales-invoice"]["derive"](
                handle, "submit-sales-invoice",
                [["sales-invoice-id", si], ["company", other_name]])
    finally:
        handle.close()
    handle = pgate._open(path)
    try:
        named = authority_gate.ENVELOPE_ACTIONS[
            "submit-sales-invoice"]["derive"](
            handle, "submit-sales-invoice",
            [["sales-invoice-id", si], ["company", home_name]])
    finally:
        handle.close()
    assert named == base
    from erpclaw_lib import authorization_issuance
    handle = pgate._open(path)
    try:
        with pytest.raises(Exception) as excinfo:
            authorization_issuance.issue_envelope(
                handle, principal_id="svc-1", delegation_id="del-1",
                action="submit-sales-invoice",
                argv=["--sales-invoice-id", si, "--company-id",
                      second_company],
                reason_code="ops-need", reason_text="need units",
                idempotency_key=str(uuid.uuid4()))
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_ISSUANCE_REFUSED",)
    from erpclaw_lib import action_impact
    product = {key for key in authority_gate.ENVELOPE_ACTIONS
               if key in action_impact.IMPACT}
    assert product == {"submit-journal-entry", "cancel-journal-entry",
                       "amend-journal-entry", "submit-payment",
                       "cancel-payment", "submit-sales-invoice",
                       "cancel-sales-invoice", "submit-purchase-invoice",
                       "cancel-purchase-invoice"}
