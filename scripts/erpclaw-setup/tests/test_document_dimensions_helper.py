"""Part 1 — one validator every later task calls (m686b).

`erpclaw_lib.dimensions` holds the shared accounting-dimension pieces:
`parse_dimension_input` (CLI spellings to dict), `validate_document_dimensions`
(draft checks against the active registry rows) and `dimensions_json_text`
(the GL writer's serialisation). The stock GL builder gains an optional
`dimensions` argument so a later selling task can tag cost-of-goods-sold legs.

Every pin runs against a fresh database from the setup suite's fixtures;
registry rows are planted with PyPika inserts, accounts and companies from
the existing setup helpers.
"""
import json
import os
import sys
import uuid

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from setup_helpers import (  # noqa: E402  (binds erpclaw_lib to this tree)
    seed_account, seed_company, seed_cost_center, seed_customer)
from erpclaw_lib import dimensions  # noqa: E402
from erpclaw_lib.query import Field, P, Q, Table  # noqa: E402
from erpclaw_lib.stock_posting import (  # noqa: E402
    create_perpetual_inventory_gl)


def _register(conn, key, data_type="text", referenced_table=None,
              allowed=None, required=None, is_active=1):
    """Plant one dimension_registry row exactly as the registry actions do."""
    t = Table("dimension_registry")
    conn.execute(
        Q.into(t).columns(
            "id", "key", "label", "data_type", "referenced_table",
            "allowed_values_json", "is_required_on_account_types_json",
            "is_active")
        .insert(P(), P(), P(), P(), P(), P(), P(), P()).get_sql(),
        (str(uuid.uuid4()), key, key, data_type, referenced_table, allowed,
         required, is_active))
    conn.commit()


def _refusal_text(excinfo):
    return str(excinfo.value)


# ── parse_dimension_input ────────────────────────────────────────────────────

def test_parse_json_only():
    assert dimensions.parse_dimension_input(
        '{"b": "2", "a": "1"}', None, None) == {"b": "2", "a": "1"}


def test_parse_pairs_only():
    assert dimensions.parse_dimension_input(
        None, ["a", "b"], ["1", "2"]) == {"a": "1", "b": "2"}


def test_parse_both_forms_merge():
    assert dimensions.parse_dimension_input(
        '{"a": "1"}', ["b"], ["2"]) == {"a": "1", "b": "2"}


def test_parse_same_key_same_value_accepted_once():
    assert dimensions.parse_dimension_input(
        '{"a": "1"}', ["a"], ["1"]) == {"a": "1"}
    assert dimensions.parse_dimension_input(
        None, ["a", "a"], ["1", "1"]) == {"a": "1"}


def test_parse_invalid_json_refused():
    with pytest.raises(ValueError) as excinfo:
        dimensions.parse_dimension_input("{nope", None, None)
    assert _refusal_text(excinfo) == "--dimensions is not valid JSON"


def test_parse_non_object_refused():
    for raw in ("[1, 2]", '"just a string"', "42", "null"):
        with pytest.raises(ValueError) as excinfo:
            dimensions.parse_dimension_input(raw, None, None)
        assert _refusal_text(excinfo) == "--dimensions must be a JSON object"


def test_parse_unpaired_lists_refused():
    with pytest.raises(ValueError) as excinfo:
        dimensions.parse_dimension_input(None, ["a"], ["1", "2"])
    assert _refusal_text(excinfo) == (
        "--dimension-key and --dimension-value must be given in pairs")


def test_parse_empty_key_or_value_refused():
    with pytest.raises(ValueError) as excinfo:
        dimensions.parse_dimension_input(None, ["  "], ["1"])
    assert _refusal_text(excinfo) == (
        "Dimension '  ' must be a non-empty string")
    with pytest.raises(ValueError) as excinfo:
        dimensions.parse_dimension_input(None, ["a"], ["   "])
    assert _refusal_text(excinfo) == (
        "Dimension 'a' must be a non-empty string")
    with pytest.raises(ValueError) as excinfo:
        dimensions.parse_dimension_input('{"b": 7}', None, None)
    assert _refusal_text(excinfo) == (
        "Dimension 'b' must be a non-empty string")
    with pytest.raises(ValueError) as excinfo:
        dimensions.parse_dimension_input('{"": "1"}', None, None)
    assert _refusal_text(excinfo) == (
        "Dimension '' must be a non-empty string")


def test_parse_conflicting_key_refused():
    with pytest.raises(ValueError) as excinfo:
        dimensions.parse_dimension_input('{"a": "1"}', ["a"], ["2"])
    assert _refusal_text(excinfo) == (
        "Dimension 'a' given twice with different values")
    with pytest.raises(ValueError) as excinfo:
        dimensions.parse_dimension_input(None, ["a", "a"], ["1", "2"])
    assert _refusal_text(excinfo) == (
        "Dimension 'a' given twice with different values")


def test_parse_explicit_empty_object_clears():
    assert dimensions.parse_dimension_input("{}", None, None) == {}


def test_parse_all_empty_returns_none():
    assert dimensions.parse_dimension_input(None, None, None) is None
    assert dimensions.parse_dimension_input(None, [], []) is None
    assert dimensions.parse_dimension_input("", None, None) is None


def test_parse_strips_keys_and_values():
    assert dimensions.parse_dimension_input(
        '{" a ": " 1 "}', [" b "], [" 2 "]) == {"a": "1", "b": "2"}


# ── validate_document_dimensions ─────────────────────────────────────────────

def test_validate_active_text_passes(conn):
    _register(conn, "m686b_dept")
    assert dimensions.validate_document_dimensions(
        conn, {"m686b_dept": "Ops"}) is None


def test_validate_inactive_and_unknown_refused(conn):
    _register(conn, "m686b_old", is_active=0)
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(conn, {"m686b_old": "x"})
    assert _refusal_text(excinfo) == (
        "Unknown or inactive dimension 'm686b_old'; run list-dimensions")
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(conn, {"m686b_nope": "x"})
    assert _refusal_text(excinfo) == (
        "Unknown or inactive dimension 'm686b_nope'; run list-dimensions")


def test_validate_enum_allowed_values(conn):
    _register(conn, "m686b_region", data_type="enum",
              allowed=json.dumps(["EMEA", "APAC"]))
    assert dimensions.validate_document_dimensions(
        conn, {"m686b_region": "EMEA"}) is None
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(
            conn, {"m686b_region": "Mars"})
    assert _refusal_text(excinfo) == (
        "Dimension 'm686b_region' value 'Mars' is not one of its allowed values")


def test_validate_enum_without_usable_list_accepts(conn):
    _register(conn, "m686b_free_enum", data_type="enum")
    assert dimensions.validate_document_dimensions(
        conn, {"m686b_free_enum": "anything"}) is None
    _register(conn, "m686b_broken_enum", data_type="enum",
              allowed="not json at all")
    assert dimensions.validate_document_dimensions(
        conn, {"m686b_broken_enum": "anything"}) is None


def test_validate_uuid_fk_row_exists_and_missing(conn):
    company_id = seed_company(conn)
    ccid = seed_cost_center(conn, company_id)
    _register(conn, "m686b_cc", data_type="uuid_fk",
              referenced_table="cost_center")
    assert dimensions.validate_document_dimensions(
        conn, {"m686b_cc": ccid}) is None
    ghost = str(uuid.uuid4())
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(conn, {"m686b_cc": ghost})
    assert _refusal_text(excinfo) == (
        "Dimension 'm686b_cc' references cost_center id '%s' which does not "
        "exist" % ghost)


def test_validate_invalid_referenced_table_name_refused(conn):
    _register(conn, "m686b_badref", data_type="uuid_fk",
              referenced_table="bad-table!")
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(conn, {"m686b_badref": "x"})
    assert _refusal_text(excinfo) == (
        "Dimension 'm686b_badref' has an invalid referenced table")


def test_validate_null_referenced_table_refused(conn):
    _register(conn, "m686b_nullref", data_type="uuid_fk",
              referenced_table=None)
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(conn, {"m686b_nullref": "x"})
    assert _refusal_text(excinfo) == (
        "Dimension 'm686b_nullref' has an invalid referenced table")


def test_validate_missing_table_refuses_and_keeps_caller_transaction(conn,
                                                                     db_path):
    _register(conn, "m686b_ghost", data_type="uuid_fk",
              referenced_table="m686b_ghost_table")
    company_id = seed_company(conn)
    t = Table("company")
    before = conn.execute(
        Q.from_(t).select(Field("id"), Field("name"))
        .where(Field("id") == P()).get_sql(), (company_id,)).fetchone()
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(conn, {"m686b_ghost": "x"})
    assert _refusal_text(excinfo) == (
        "Dimension 'm686b_ghost' has an invalid referenced table")
    after = conn.execute(
        Q.from_(t).select(Field("id"), Field("name"))
        .where(Field("id") == P()).get_sql(), (company_id,)).fetchone()
    assert dict(after) == dict(before)


def test_validate_required_dimension(conn):
    company_id = seed_company(conn)
    expense_id = seed_account(conn, company_id, name="Travel",
                              root_type="expense", account_type="expense")
    asset_id = seed_account(conn, company_id, name="Vault",
                            root_type="asset", account_type="asset")
    _register(conn, "m686b_costcode", required=json.dumps(["expense"]))
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(conn, {}, [expense_id])
    assert _refusal_text(excinfo) == (
        "Dimension 'm686b_costcode' is required for account 'Travel' "
        "(account_type 'expense')")
    assert dimensions.validate_document_dimensions(
        conn, {"m686b_costcode": "CC1"}, [expense_id]) is None
    assert dimensions.validate_document_dimensions(
        conn, {}, [asset_id]) is None


def test_validate_empty_dims_without_accounts_makes_no_query():
    class _RefusingConnection:
        def execute(self, *args, **kwargs):
            raise AssertionError("must not query")

    assert dimensions.validate_document_dimensions(
        _RefusingConnection(), {}) is None
    assert dimensions.validate_document_dimensions(
        _RefusingConnection(), None) is None


def test_validate_non_list_required_types_requires_nothing(conn):
    company_id = seed_company(conn)
    expense_id = seed_account(conn, company_id, name="Travel",
                              root_type="expense", account_type="expense")
    _register(conn, "m686b_lax", required='"expense"')
    assert dimensions.validate_document_dimensions(
        conn, {}, [expense_id]) is None


def test_validate_unknown_account_refused(conn):
    _register(conn, "m686b_dept")
    ghost = str(uuid.uuid4())
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(
            conn, {"m686b_dept": "Ops"}, [ghost])
    assert _refusal_text(excinfo) == "Account '%s' not found" % ghost


def test_validate_null_account_type_treated_as_empty(conn):
    company_id = seed_company(conn)
    aid = seed_account(conn, company_id, name="Mystery",
                       root_type="asset", account_type=None)
    _register(conn, "m686b_req", required=json.dumps([""]))
    with pytest.raises(ValueError) as excinfo:
        dimensions.validate_document_dimensions(conn, {}, [aid])
    assert _refusal_text(excinfo) == (
        "Dimension 'm686b_req' is required for account 'Mystery' "
        "(account_type '')")


# ── dimensions_json_text ─────────────────────────────────────────────────────

def test_dimensions_json_text_sorts_keys():
    assert dimensions.dimensions_json_text({"b": "2", "a": "1"}) == (
        '{"a": "1", "b": "2"}')
    assert dimensions.dimensions_json_text(None) == "{}"


# ── stock GL builder ─────────────────────────────────────────────────────────

def _seed_warehouse(conn, company_id, account_id):
    wid = str(uuid.uuid4())
    t = Table("warehouse")
    conn.execute(
        Q.into(t).columns("id", "name", "company_id", "account_id")
        .insert(P(), P(), P(), P()).get_sql(),
        (wid, "Stores %s" % wid[:6], company_id, account_id))
    conn.commit()
    return wid


def _seed_item(conn):
    iid = str(uuid.uuid4())
    t = Table("item")
    conn.execute(
        Q.into(t).columns("id", "item_code", "item_name")
        .insert(P(), P(), P()).get_sql(),
        (iid, "IT-%s" % iid[:6], "Widget %s" % iid[:6]))
    conn.commit()
    return iid


def test_stock_builder_without_dimensions_returns_todays_shape(conn):
    company_id = seed_company(conn)
    stock_acct = seed_account(conn, company_id, name="Stock in Hand",
                              root_type="asset", account_type="stock")
    contra_acct = seed_account(conn, company_id, name="SRNB",
                               root_type="liability",
                               account_type="stock_received_not_billed")
    ccid = seed_cost_center(conn, company_id)
    wid = _seed_warehouse(conn, company_id, stock_acct)
    iid = _seed_item(conn)
    sles = [{"item_id": iid, "warehouse_id": wid, "actual_qty": "5",
             "stock_value_difference": "50.00"}]
    first = create_perpetual_inventory_gl(
        conn, sles, "stock_entry", "VD-1", "2026-03-10", company_id,
        cost_center_id=ccid)
    assert first == [
        {"account_id": stock_acct, "debit": "50.00", "credit": "0"},
        {"account_id": contra_acct, "debit": "0", "credit": "50.00",
         "cost_center_id": ccid},
    ]
    second = create_perpetual_inventory_gl(
        conn, sles, "stock_entry", "VD-1", "2026-03-10", company_id,
        cost_center_id=ccid, dimensions={"department": "Ops"})
    assert second == [
        dict(entry, dimensions={"department": "Ops"}) for entry in first
    ]
    second[0]["dimensions"]["mutated"] = "yes"
    assert "mutated" not in second[1]["dimensions"]


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg, plan §8.3)")
def test_pg_registry_validation_leaves_nothing_behind(monkeypatch):
    """Runs against an expendable database only — never point it at shared data."""
    from urllib.parse import unquote, urlparse

    from erpclaw_lib.db import get_connection
    from setup_helpers import init_all_tables

    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    expected_db = unquote(urlparse(_PG_URL).path.lstrip("/"))
    guard = get_connection(_PG_URL)
    try:
        current_db = guard.execute("SELECT current_database()").fetchone()[0]
        server_addr = guard.execute("SELECT inet_server_addr()").fetchone()[0]
        listen = guard.execute(
            "SELECT current_setting('listen_addresses')").fetchone()[0]
        version_num = guard.execute(
            "SELECT current_setting('server_version_num')").fetchone()[0]
        version = guard.execute("SELECT version()").fetchone()[0]
    finally:
        guard.close()
    print("version(): %s" % version)
    print("current_database(): %s" % current_db)
    if current_db != expected_db:
        pytest.fail("refusing: connected database is not the one in the URL")
    if server_addr is not None:
        pytest.fail("refusing: server is reachable over the network")
    if listen != "":
        pytest.fail("refusing: server listens on an address")
    if not str(version_num).startswith("16"):
        pytest.fail("refusing: server is not PostgreSQL 16")
    init_all_tables(_PG_URL)
    prefix = "m686bpg_%s_" % uuid.uuid4().hex[:8]
    text_key = prefix + "dept"
    fk_key = prefix + "cc"
    conn = get_connection(_PG_URL)
    company_id = str(uuid.uuid4())
    ccid = str(uuid.uuid4())
    reg = Table("dimension_registry")
    comp = Table("company")
    cct = Table("cost_center")
    try:
        conn.execute(
            Q.into(reg).columns(
                "id", "key", "label", "data_type", "referenced_table",
                "allowed_values_json", "is_required_on_account_types_json",
                "is_active")
            .insert(P(), P(), P(), P(), P(), P(), P(), P()).get_sql(),
            (str(uuid.uuid4()), text_key, text_key, "text", None, None,
             None, 1))
        conn.execute(
            Q.into(reg).columns(
                "id", "key", "label", "data_type", "referenced_table",
                "allowed_values_json", "is_required_on_account_types_json",
                "is_active")
            .insert(P(), P(), P(), P(), P(), P(), P(), P()).get_sql(),
            (str(uuid.uuid4()), fk_key, fk_key, "uuid_fk", "cost_center",
             None, None, 1))
        conn.execute(
            Q.into(comp).columns("id", "name", "abbr")
            .insert(P(), P(), P()).get_sql(),
            (company_id, "PG Co %s" % company_id[:6],
             "PG%s" % company_id[:4]))
        conn.execute(
            Q.into(cct).columns("id", "name", "company_id", "is_group")
            .insert(P(), P(), P(), P()).get_sql(),
            (ccid, "PG CC", company_id, 0))
        conn.commit()
        assert dimensions.validate_document_dimensions(
            conn, {text_key: "Ops", fk_key: ccid}) is None
        ghost = str(uuid.uuid4())
        with pytest.raises(ValueError) as excinfo:
            dimensions.validate_document_dimensions(conn, {fk_key: ghost})
        assert str(excinfo.value) == (
            "Dimension '%s' references cost_center id '%s' which does not "
            "exist" % (fk_key, ghost))
        with pytest.raises(ValueError) as excinfo:
            dimensions.validate_document_dimensions(conn, {prefix + "no": "x"})
        assert str(excinfo.value) == (
            "Unknown or inactive dimension '%s'; run list-dimensions"
            % (prefix + "no"))
    finally:
        conn.execute(
            Q.from_(reg).where(Field("key").like(P())).delete().get_sql(),
            (prefix + "%",))
        conn.execute(
            Q.from_(cct).where(Field("id") == P()).delete().get_sql(),
            (ccid,))
        conn.execute(
            Q.from_(comp).where(Field("id") == P()).delete().get_sql(),
            (company_id,))
        conn.commit()
        leftovers = conn.execute(
            Q.from_(reg).select(Field("key"))
            .where(Field("key").like(P())).get_sql(),
            (prefix + "%",)).fetchall()
        conn.close()
    assert [dict(r) for r in leftovers] == []
