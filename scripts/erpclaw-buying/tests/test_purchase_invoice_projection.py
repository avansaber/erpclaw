"""Purchase invoice targets behind single-use authorization."""
import os
import sys
import uuid

import pytest

_PI_TESTS = os.path.dirname(os.path.abspath(__file__))
_PI_MODULE = os.path.dirname(_PI_TESTS)
_PI_SETUP_TESTS = os.path.join(os.path.dirname(_PI_MODULE), "erpclaw-setup", "tests")
_PI_PAYMENTS_TESTS = os.path.join(
    os.path.dirname(_PI_MODULE), "erpclaw-payments", "tests")
if _PI_SETUP_TESTS not in sys.path:
    sys.path.insert(0, _PI_SETUP_TESTS)
if _PI_PAYMENTS_TESTS not in sys.path:
    sys.path.insert(0, _PI_PAYMENTS_TESTS)
if _PI_TESTS not in sys.path:
    sys.path.insert(0, _PI_TESTS)

_SEEDED = set()
_SEED_CAPS = set()


def _std(action, path, pi_id, extra=None):
    argv = ["--action", action, "--db-path", path,
            "--purchase-invoice-id", pi_id]
    if extra:
        argv = argv + list(extra)
    return argv


def _pi(path, env):
    import json as _json
    import buying_helpers as helpers
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
            helpers.load_db_query().create_purchase_invoice, handle,
            helpers.ns(purchase_order_id=None, purchase_receipt_id=None,
                       supplier_id=env["supplier"],
                       company_id=env["company_id"],
                       posting_date="2026-06-20",
                       due_date="2026-07-20", items=items,
                       tax_template_id=None))
    finally:
        handle.close()
    assert result.get("status") == "ok", result
    assert result.get("grand_total") == "100.00", result
    return result["purchase_invoice_id"]


def _update_pi(path, pi_id, values):
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import Field, P, Q, Table
    handle = get_connection(path)
    try:
        table = Table("purchase_invoice")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("id") == P())
        params.append(pi_id)
        handle.execute(query.get_sql(), params)
        handle.commit()
    finally:
        handle.close()


def _update_item(path, item_row_id, values):
    from erpclaw_lib.db import get_connection
    from erpclaw_lib.query import Field, P, Q, Table
    handle = get_connection(path)
    try:
        table = Table("purchase_invoice_item")
        query = Q.update(table)
        params = []
        for column, value in values.items():
            query = query.set(Field(column), P())
            params.append(value)
        query = query.where(Field("id") == P())
        params.append(item_row_id)
        handle.execute(query.get_sql(), params)
        handle.commit()
    finally:
        handle.close()


def _grant(path, company_id, pi_id, action):
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
            "company_id": company_id, "resource_kind": "purchase-invoice",
            "resource_id": pi_id, "action": action, "effect": "allow"})
        fx._insert_row(handle, "authority_delegation_right", {
            "install_id": install_id, "delegation_id": fx.DELEGATION,
            "company_id": company_id, "resource_kind": "purchase-invoice",
            "resource_id": pi_id, "action": action})
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
    import buying_helpers as helpers
    from erpclaw_lib.db import get_connection
    path = pgate._fresh_db(tmp_path, monkeypatch, "buying")
    handle = get_connection(path)
    try:
        env = helpers.build_buying_env(handle)
    finally:
        handle.close()
    return (path, env)


def test_projection_reads_the_database(db_env):
    import test_payment_envelope_gate as pgate
    path, env = db_env
    pi = _pi(path, env)
    _grant(path, env["company_id"], pi, "submit-purchase-invoice")
    _update_pi(path, pi, {"grand_total": "100"})
    from erpclaw_lib import authority_gate
    handle = pgate._open(path)
    try:
        derived = authority_gate.ENVELOPE_ACTIONS[
            "submit-purchase-invoice"]["derive"](
            handle, "submit-purchase-invoice",
            [["purchase-invoice-id", pi]])
    finally:
        handle.close()
    assert derived["company_ids"] == [env["company_id"]]
    assert len(derived["targets"]) == 1
    assert derived["targets"][0]["kind"] == "purchase-invoice"
    assert derived["targets"][0]["id"] == pi
    assert derived["amounts"] == [{"currency": "USD", "value": "100.00",
                                   "scale": 2}]
    auth_id = pgate._issue(path, "submit-purchase-invoice",
                           _std("submit-purchase-invoice", path, pi))
    assert auth_id
    inv_cols = ("id", "company_id", "naming_series", "supplier_id",
                "posting_date", "due_date", "currency", "exchange_rate",
                "total_amount", "tax_amount", "grand_total",
                "outstanding_amount", "rounding_adjustment",
                "tax_template_id", "payment_terms_id", "status",
                "purchase_order_id", "purchase_receipt_id", "is_return",
                "return_against", "update_stock", "amended_from",
                "cwip_asset_id", "is_intercompany",
                "intercompany_reference_id", "dimensions_json")
    item_cols = ("id", "item_id", "quantity", "uom", "rate", "amount",
                 "expense_account_id", "cost_center_id", "project_id",
                 "purchase_order_item_id", "purchase_receipt_item_id",
                 "discount_amount")
    from erpclaw_lib.query import Field, P, Q, Table
    handle = pgate._open(path)
    try:
        inv_table = Table("purchase_invoice")
        inv_query = Q.from_(inv_table).select(
            *[Field(column) for column in inv_cols]).where(
            Field("id") == P()).get_sql()
        inv_row = handle.execute(inv_query, (pi,)).fetchone()
        invoice = {column: dict(inv_row)[column] for column in inv_cols}
        item_table = Table("purchase_invoice_item")
        item_query = Q.from_(item_table).select(
            *[Field(column) for column in item_cols]).where(
            Field("purchase_invoice_id") == P()).orderby(
            Field("id")).get_sql()
        item_rows = handle.execute(item_query, (pi,)).fetchall()
    finally:
        handle.close()
    own_items = [{column: dict(record)[column] for column in item_cols}
                 for record in item_rows]
    assert invoice["company_id"] == env["company_id"]
    assert invoice["supplier_id"] == env["supplier"]
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
    handle = pgate._open(path)
    try:
        with_extra = authority_gate.ENVELOPE_ACTIONS[
            "submit-purchase-invoice"]["derive"](
            handle, "submit-purchase-invoice",
            [["purchase-invoice-id", pi], ["amount", "1.00"]])
    finally:
        handle.close()
    assert with_extra == derived
    _update_pi(path, pi, {"grand_total": "-100.00"})
    handle = pgate._open(path)
    try:
        negated = authority_gate.ENVELOPE_ACTIONS[
            "submit-purchase-invoice"]["derive"](
            handle, "submit-purchase-invoice",
            [["purchase-invoice-id", pi]])
    finally:
        handle.close()
    assert negated["amounts"] == [{"currency": "USD", "value": "100.00",
                                   "scale": 2}]
    _update_pi(path, pi, {"grand_total": "100.125"})
    handle = pgate._open(path)
    try:
        with pytest.raises(ValueError) as excinfo:
            authority_gate.ENVELOPE_ACTIONS[
                "submit-purchase-invoice"]["derive"](
                handle, "submit-purchase-invoice",
                [["purchase-invoice-id", pi]])
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    _update_pi(path, pi, {"grand_total": "abc"})
    handle = pgate._open(path)
    try:
        with pytest.raises(ValueError) as excinfo:
            authority_gate.ENVELOPE_ACTIONS[
                "submit-purchase-invoice"]["derive"](
                handle, "submit-purchase-invoice",
                [["purchase-invoice-id", pi]])
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    _update_pi(path, pi, {"grand_total": "100.00"})
    handle = pgate._open(path)
    try:
        base = authority_gate.ENVELOPE_ACTIONS[
            "submit-purchase-invoice"]["derive"](
            handle, "submit-purchase-invoice",
            [["purchase-invoice-id", pi]])
    finally:
        handle.close()
    assert base["amounts"] == [{"currency": "USD", "value": "100.00",
                                "scale": 2}]
    first_item_id = own_items[0]["id"]
    first_discount = own_items[0]["discount_amount"]
    _update_item(path, first_item_id, {"discount_amount": "1.00"})
    handle = pgate._open(path)
    try:
        moved = authority_gate.ENVELOPE_ACTIONS[
            "submit-purchase-invoice"]["derive"](
            handle, "submit-purchase-invoice",
            [["purchase-invoice-id", pi]])
    finally:
        handle.close()
    assert moved["targets"][0]["state_digest"] != base["targets"][0]["state_digest"]
    _update_item(path, first_item_id, {"discount_amount": first_discount})
    handle = pgate._open(path)
    try:
        restored = authority_gate.ENVELOPE_ACTIONS[
            "submit-purchase-invoice"]["derive"](
            handle, "submit-purchase-invoice",
            [["purchase-invoice-id", pi]])
    finally:
        handle.close()
    assert restored["targets"][0]["state_digest"] == base["targets"][0]["state_digest"]
    handle = pgate._open(path)
    try:
        cancelled = authority_gate.ENVELOPE_ACTIONS[
            "cancel-purchase-invoice"]["derive"](
            handle, "cancel-purchase-invoice",
            [["purchase-invoice-id", pi]])
    finally:
        handle.close()
    assert cancelled == base
    handle = pgate._open(path)
    try:
        with pytest.raises(ValueError) as excinfo:
            authority_gate.ENVELOPE_ACTIONS[
                "submit-purchase-invoice"]["derive"](
                handle, "submit-purchase-invoice",
                [["purchase-invoice-id", "00000000-0000-0000-0000-000000000000"]])
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    handle = pgate._open(path)
    try:
        with pytest.raises(ValueError) as excinfo:
            authority_gate.ENVELOPE_ACTIONS[
                "submit-purchase-invoice"]["derive"](
                handle, "submit-purchase-invoice",
                [["purchase-invoice-id", pi],
                 ["purchase-invoice-id", pi]])
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    handle = pgate._open(path)
    try:
        with pytest.raises(ValueError) as excinfo:
            authority_gate.ENVELOPE_ACTIONS[
                "submit-purchase-invoice"]["derive"](
                handle, "submit-purchase-invoice",
                [["purchase-invoice-id", ""]])
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    import buying_helpers as helpers
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
        with pytest.raises(ValueError) as excinfo:
            authority_gate.ENVELOPE_ACTIONS[
                "submit-purchase-invoice"]["derive"](
                handle, "submit-purchase-invoice",
                [["purchase-invoice-id", pi],
                 ["company-id", second_company]])
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    handle = pgate._open(path)
    try:
        with pytest.raises(ValueError) as excinfo:
            authority_gate.ENVELOPE_ACTIONS[
                "submit-purchase-invoice"]["derive"](
                handle, "submit-purchase-invoice",
                [["purchase-invoice-id", pi], ["company", other_name]])
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    handle = pgate._open(path)
    try:
        named = authority_gate.ENVELOPE_ACTIONS[
            "submit-purchase-invoice"]["derive"](
            handle, "submit-purchase-invoice",
            [["purchase-invoice-id", pi], ["company", home_name]])
    finally:
        handle.close()
    assert named == base
    from erpclaw_lib import authorization_issuance
    handle = pgate._open(path)
    try:
        with pytest.raises(Exception) as excinfo:
            authorization_issuance.issue_envelope(
                handle, principal_id="svc-1", delegation_id="del-1",
                action="submit-purchase-invoice",
                argv=["--purchase-invoice-id", pi, "--company-id",
                      second_company],
                reason_code="ops-need", reason_text="need units",
                idempotency_key=str(uuid.uuid4()))
    finally:
        handle.close()
    assert excinfo.value.args == ("AUTHORIZATION_ISSUANCE_REFUSED",)
    from erpclaw_lib.authority_projections import purchase_invoice_result
    assert purchase_invoice_result(
        {"purchase_invoice_id": "x",
         "document_status": "submitted"}) == ("purchase-invoice", "x",
                                              "submitted")
    with pytest.raises(KeyError):
        purchase_invoice_result({"purchase_invoice_id": "x"})
    with pytest.raises(ValueError) as excinfo:
        purchase_invoice_result(
            {"purchase_invoice_id": "x", "document_status": "submitted",
             "discount_rederived": [{"line": "l1", "old": "0",
                                     "new": "1.00"}]})
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    from erpclaw_lib import action_impact
    product = {key for key in authority_gate.ENVELOPE_ACTIONS
               if key in action_impact.IMPACT}
    assert product == {"submit-journal-entry", "cancel-journal-entry",
                       "amend-journal-entry", "submit-payment",
                       "cancel-payment", "submit-sales-invoice",
                       "cancel-sales-invoice", "submit-purchase-invoice",
                       "cancel-purchase-invoice"}
