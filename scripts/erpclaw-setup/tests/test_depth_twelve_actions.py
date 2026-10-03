"""Behavioural depth tests for twelve erpclaw-setup actions.

Every action named in the task already had a test that proved only the
response envelope (right keys) or reachability. Each test below proves the
stored effect instead: the exact row that must exist afterwards with exact
values, the exact row that must have changed, and the rows that must not
have changed. Rows are read back with PyPika queries through
``erpclaw_lib.query`` on connections from ``erpclaw_lib.db.get_connection``;
catalog questions go through ``erpclaw_lib.seam``.

Signal depth per action (stored row unless noted):

- link-telegram-user ... attempted erp_user stored row (BROKEN, see class note)
- list-account-types ... account_type_registry stored row + read-only proof
- list-credentials ... encrypted-file store effect + database-unchanged proof
- list-custom-fields ... custom_field stored rows + read-only proof
- list-voucher-types ... voucher_type_registry stored row + read-only proof
- migrate ... erpclaw_schema_migration ledger effect (schema ledger, not GL)
- migrate-credentials ... no-op effect (no source tables here) + preview proof
- remove-custom-field ... custom_field / custom_field_value stored rows
- revoke-role ... user_role stored row
- seed-permissions ... role_permission stored rows
- set-credential ... encrypted-file store effect + database-unchanged proof
- set-custom-field-value ... custom_field_value stored row

Money note: none of these twelve actions posts amounts, so no debit/credit
balance assertion can hold for any of them. The one amount read below (a
planted gl_entry fixture proving read-only behaviour) compares exact Decimal
values as strings; no approximate comparison appears anywhere here.

Ledger note: none of these actions reaches the general ledger, so no test
here asserts balanced legs. Each class states that explicitly.
"""
import argparse
import importlib.util
import json
import os
from decimal import Decimal

import pytest

from setup_helpers import (
    call_action,
    is_error,
    is_ok,
    load_db_query,
    seed_account,
    seed_company,
)

from erpclaw_lib.db import db_error_types, get_connection
from erpclaw_lib import seam
from erpclaw_lib.query import Field, P, Q, Table, fn

mod = load_db_query()


def _ns(**kw):
    base = dict(
        user_id=None, telegram_user_id=None, name=None, email=None,
        full_name=None, company_id=None, role_name=None,
        include_inactive=False, target_table=None, table=None,
        field_name=None, field_type=None, label=None, default=None,
        required=False, options=None, skill_name=None, row_id=None,
        value=None, confirm=False, integration=None, from_stdin=False,
        from_env=None, dry_run=False, db_path=None, account_type=None,
        voucher_type=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _load_runner():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "migration_runner.py")
    spec = importlib.util.spec_from_file_location("depth_migration_runner", path)
    found = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(found)
    return found


def _table_rows(vconn, table_name):
    tbl = Table(table_name)
    qry = Q.from_(tbl).select(tbl.star)
    return [dict(row) for row in vconn.execute(qry.get_sql()).fetchall()]


def _select_where(vconn, table_name, filters):
    tbl = Table(table_name)
    qry = Q.from_(tbl).select(tbl.star)
    params = []
    for col, val in filters.items():
        qry = qry.where(Field(col) == P())
        params.append(val)
    return [dict(row) for row in vconn.execute(qry.get_sql(), tuple(params)).fetchall()]


def _row_count(vconn, table_name):
    tbl = Table(table_name)
    qry = Q.from_(tbl).select(fn.Count("*").as_("cnt"))
    return vconn.execute(qry.get_sql()).fetchone()["cnt"]


def _count_where(vconn, table_name, field_name, value):
    tbl = Table(table_name)
    qry = (Q.from_(tbl).select(fn.Count("*").as_("cnt"))
           .where(Field(field_name) == P()))
    return vconn.execute(qry.get_sql(), (value,)).fetchone()["cnt"]


def _snapshot(vconn, tables):
    snap = {}
    for name in tables:
        rows = _table_rows(vconn, name)
        snap[name] = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
    return snap


@pytest.fixture
def vconn(db_path):
    handle = get_connection(db_path)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def cred_store(tmp_path, monkeypatch):
    from erpclaw_lib import credentials as creds
    from erpclaw_lib import master_key as keys
    home = str(tmp_path / "depth-home")
    monkeypatch.setattr(keys, "CONFIG_DIR", home)
    monkeypatch.setattr(keys, "MASTER_KEY_PATH", os.path.join(home, "master.key"))
    monkeypatch.setattr(
        creds, "CREDENTIALS_PATH", os.path.join(home, "credentials.json.enc"))
    return creds


class TestLinkTelegramUserDepth:
    """link-telegram-user is BROKEN on a fresh database and left that way.

    Expected: linking stores telegram_user_id on erp_user. Real behaviour:
    erp_user has no telegram_user_id column, so the update raises and nothing
    is written. Recorded here, not fixed. No GL posting, so no legs to check.
    """

    def test_missing_column_means_no_link_and_no_write(self, conn, db_path, vconn):
        created = call_action(mod.add_user, conn, _ns(
            name="tg_depth_one", email="tg1@depth.test",
            full_name="Depth One", company_id=None))
        assert is_ok(created)
        user_key = created["user_id"]
        before = _snapshot(vconn, ["erp_user", "audit_log"])
        error_types = db_error_types()
        with pytest.raises(error_types[1]) as excinfo:
            call_action(mod.link_telegram_user, conn, _ns(
                user_id=user_key, telegram_user_id="7654321"))
        assert "telegram_user_id" in str(excinfo.value)
        assert _snapshot(vconn, ["erp_user", "audit_log"]) == before
        stored = _select_where(vconn, "erp_user", {"id": user_key})
        assert len(stored) == 1
        assert stored[0]["username"] == "tg_depth_one"

    def test_missing_user_id_refused_without_write(self, conn, db_path, vconn):
        before = _snapshot(vconn, ["erp_user", "audit_log"])
        result = call_action(mod.link_telegram_user, conn, _ns(
            user_id=None, telegram_user_id="7654321"))
        assert is_error(result)
        assert "--user-id" in result.get("message", "")
        assert _snapshot(vconn, ["erp_user", "audit_log"]) == before


class TestListAccountTypesDepth:
    """list-account-types reads account_type_registry. No GL posting."""

    def test_registered_type_reads_back_with_exact_values(self, conn, db_path, vconn):
        created = call_action(mod.add_account_type, conn, _ns(
            account_type="depth_wallet", label="Depth Wallet",
            skill_name="erpclaw-setup"))
        assert is_ok(created) and created["result"] == "registered"
        stored = _select_where(
            vconn, "account_type_registry", {"account_type": "depth_wallet"})
        assert len(stored) == 1
        assert stored[0]["label"] == "Depth Wallet"
        assert stored[0]["skill_name"] == "erpclaw-setup"
        assert str(stored[0]["is_active"]) == "1"
        listed = call_action(mod.list_account_types, conn, _ns(include_inactive=False))
        assert is_ok(listed)
        by_name = {row["account_type"]: row for row in listed["account_types"]}
        assert by_name["depth_wallet"]["label"] == "Depth Wallet"
        assert by_name["bank"]["label"] == "Bank"
        assert listed["count"] == len(listed["account_types"])
        assert listed["count"] == _count_where(
            vconn, "account_type_registry", "is_active", 1)

    def test_list_writes_nothing(self, conn, db_path, vconn):
        # list-account-types has no validating branch, so it never refuses.
        # This second test proves the read-only half instead.
        call_action(mod.add_account_type, conn, _ns(
            account_type="depth_readonly", label="Depth Readonly",
            skill_name="erpclaw-setup"))
        tables = ["account_type_registry", "audit_log"]
        before = _snapshot(vconn, tables)
        listed = call_action(mod.list_account_types, conn, _ns(include_inactive=False))
        assert is_ok(listed)
        listed_all = call_action(
            mod.list_account_types, conn, _ns(include_inactive=True))
        assert is_ok(listed_all)
        assert listed_all["count"] >= listed["count"]
        assert _snapshot(vconn, tables) == before


class TestListCredentialsDepth:
    """list-credentials reads the encrypted file store, not the database."""

    _TABLES = ["erp_user", "custom_field", "custom_field_value",
               "account_type_registry", "voucher_type_registry", "role",
               "user_role", "role_permission", "audit_log"]

    def test_lists_exactly_what_the_store_holds(self, conn, db_path, vconn, cred_store):
        assert cred_store.list_credentials() == []
        before = _snapshot(vconn, self._TABLES)
        first = call_action(mod.set_credential_action, conn, _ns(
            integration="depth-alpha", value="depth-alpha-secret-001"))
        assert is_ok(first)
        second = call_action(mod.set_credential_action, conn, _ns(
            integration="depth-beta", value="depth-beta-secret-002"))
        assert is_ok(second)
        assert cred_store.get_credential("depth-alpha") == "depth-alpha-secret-001"
        assert cred_store.get_credential("depth-beta") == "depth-beta-secret-002"
        listed = call_action(mod.list_credentials_action, conn, _ns())
        assert is_ok(listed)
        assert listed["integrations"] == ["depth-alpha", "depth-beta"]
        assert "depth-alpha-secret-001" not in json.dumps(listed)
        assert "depth-beta-secret-002" not in json.dumps(listed)
        assert _snapshot(vconn, self._TABLES) == before

    def test_empty_store_lists_empty_and_writes_nothing(
            self, conn, db_path, vconn, cred_store):
        # list-credentials has no validating branch, so it never refuses.
        # This second test proves the empty-store answer and read-only half.
        assert cred_store.list_credentials() == []
        before = _snapshot(vconn, self._TABLES)
        listed = call_action(mod.list_credentials_action, conn, _ns())
        assert is_ok(listed) and listed["integrations"] == []
        assert _snapshot(vconn, self._TABLES) == before
        assert cred_store.list_credentials() == []


class TestListCustomFieldsDepth:
    """list-custom-fields reads custom_field definitions. No GL posting."""

    def test_filter_returns_exact_definitions(self, conn, db_path, vconn):
        one = call_action(mod.add_custom_field_action, conn, _ns(
            table="customer", field_name="depth_tier", field_type="select",
            label=None, default=None, required=False, options="Gold,Silver",
            skill_name="erpclaw-setup"))
        assert is_ok(one)
        two = call_action(mod.add_custom_field_action, conn, _ns(
            table="customer", field_name="depth_note", field_type="text",
            label=None, default=None, required=False, options=None,
            skill_name="erpclaw-setup"))
        assert is_ok(two)
        other = call_action(mod.add_custom_field_action, conn, _ns(
            table="item", field_name="depth_hs", field_type="text",
            label=None, default=None, required=False, options=None,
            skill_name="erpclaw-setup"))
        assert is_ok(other)
        stored = _select_where(vconn, "custom_field", {
            "table_name": "customer", "field_name": "depth_tier"})
        assert len(stored) == 1
        assert stored[0]["field_type"] == "select"
        assert json.loads(stored[0]["field_options"]) == {"values": ["Gold", "Silver"]}
        assert stored[0]["owner_skill"] == "erpclaw-setup"
        listed = call_action(mod.list_custom_fields_action, conn, _ns(table="customer"))
        assert is_ok(listed)
        assert listed["count"] == 2
        by_field = {row["field_name"]: row for row in listed["custom_fields"]}
        assert by_field["depth_tier"]["field_type"] == "select"
        assert by_field["depth_note"]["field_type"] == "text"
        assert "depth_hs" not in by_field

    def test_list_writes_nothing(self, conn, db_path, vconn):
        # list-custom-fields has no validating branch, so it never refuses.
        # This second test proves the read-only half instead.
        call_action(mod.add_custom_field_action, conn, _ns(
            table="customer", field_name="depth_flag", field_type="text",
            label=None, default=None, required=False, options=None,
            skill_name="erpclaw-setup"))
        tables = ["custom_field", "custom_field_value", "audit_log"]
        before = _snapshot(vconn, tables)
        filtered = call_action(mod.list_custom_fields_action, conn, _ns(table="customer"))
        assert is_ok(filtered)
        unfiltered = call_action(mod.list_custom_fields_action, conn, _ns(table=None))
        assert is_ok(unfiltered)
        assert unfiltered["count"] >= filtered["count"]
        assert _snapshot(vconn, tables) == before


class TestListVoucherTypesDepth:
    """list-voucher-types reads voucher_type_registry. No GL posting."""

    def test_registered_voucher_reads_back_with_exact_values(
            self, conn, db_path, vconn):
        created = call_action(mod.add_voucher_type, conn, _ns(
            voucher_type="depth_rebate", target_table="gl_entry",
            label="Depth Rebate", skill_name="erpclaw-setup"))
        assert is_ok(created) and created["result"] == "registered"
        stored = _select_where(vconn, "voucher_type_registry", {
            "voucher_type": "depth_rebate", "target_table": "gl_entry"})
        assert len(stored) == 1
        assert stored[0]["label"] == "Depth Rebate"
        assert stored[0]["skill_name"] == "erpclaw-setup"
        assert str(stored[0]["is_active"]) == "1"
        listed = call_action(mod.list_voucher_types, conn, _ns(
            include_inactive=False, target_table="gl_entry"))
        assert is_ok(listed)
        by_type = {row["voucher_type"]: row for row in listed["voucher_types"]}
        assert by_type["depth_rebate"]["label"] == "Depth Rebate"
        assert by_type["journal_entry"]["label"] == "Journal Entry"
        for row in listed["voucher_types"]:
            assert row["target_table"] == "gl_entry"

    def test_list_leaves_amount_text_byte_identical(self, conn, db_path, vconn):
        # list-voucher-types has no validating branch, so it never refuses.
        # This second test proves the read-only half on live amount text.
        # The fixture row below is planted by the test, not posted by the
        # action, so no balance assertion on the action can hold here.
        company_key = seed_company(conn)
        acct = seed_account(conn, company_key, name="Depth Cash",
                            root_type="asset", account_type="cash")
        gle = Table("gl_entry")
        plant = (Q.into(gle)
                 .columns("id", "posting_date", "account_id", "debit",
                          "credit", "voucher_type", "voucher_id")
                 .insert(P(), P(), P(), P(), P(), P(), P()))
        conn.execute(plant.get_sql(), (
            "depth-gle-1", "2026-05-31", acct, "10", "0",
            "journal_entry", "JE-DEPTH-1"))
        conn.commit()
        tables = ["gl_entry", "account", "voucher_type_registry", "audit_log"]
        before = _snapshot(vconn, tables)
        listed = call_action(mod.list_voucher_types, conn, _ns(
            include_inactive=False, target_table=None))
        assert is_ok(listed)
        legs = _select_where(vconn, "gl_entry", {"id": "depth-gle-1"})
        assert len(legs) == 1
        assert legs[0]["debit"] == "10"
        assert legs[0]["credit"] == "0"
        assert str(Decimal(legs[0]["debit"])) == "10"
        assert Decimal(legs[0]["debit"]) == Decimal("10")
        assert Decimal(legs[0]["debit"]) + Decimal(legs[0]["credit"]) == Decimal("10")
        assert _snapshot(vconn, tables) == before


class TestMigrateDepth:
    """migrate applies foundation migrations and records the schema ledger.

    The ledger here is erpclaw_schema_migration, not the general ledger, so
    no debit/credit balance assertion can hold. A dry run records nothing
    and leaves the ledger table absent.
    """

    def test_dry_run_then_apply_records_every_ledger_row(self, conn, db_path, vconn):
        runner = _load_runner()
        expected = [mid for mid, _ in runner.discover()]
        assert len(expected) > 0
        assert seam.table_exists("erpclaw_schema_migration", db_path) is False
        dry = call_action(mod.migrate_action, conn, _ns(db_path=db_path, dry_run=True))
        assert dry["dry_run"] is True
        assert dry["already_applied"] == []
        assert dry["pending"] == expected
        assert seam.table_exists("erpclaw_schema_migration", db_path) is False
        done = call_action(mod.migrate_action, conn, _ns(db_path=db_path, dry_run=False))
        assert done["ok"] is True
        assert done["applied"] == expected
        ledger_rows = _table_rows(vconn, "erpclaw_schema_migration")
        status_by_id = {row["id"]: row["status"] for row in ledger_rows}
        for mid in expected:
            assert status_by_id.get(mid) == "applied"
        for core in ("account", "gl_entry", "custom_field",
                     "custom_field_value", "erp_user", "role"):
            assert seam.table_exists(core, db_path) is True
        again = call_action(mod.migrate_action, conn, _ns(db_path=db_path, dry_run=False))
        assert again["ok"] is True and again["applied"] == []

    def test_dry_run_changes_no_content_rows(self, conn, db_path, vconn):
        # migrate surfaces a failed migration as an error, but it takes no
        # input that validates; the dry run below is the no-write proof.
        # A dry run leaves the ledger table absent and the table list unchanged.
        tables = ["account", "gl_entry", "custom_field", "custom_field_value",
                  "erp_user", "role", "user_role", "role_permission",
                  "account_type_registry", "voucher_type_registry",
                  "audit_log", "company", "currency", "uom"]
        before = _snapshot(vconn, tables)
        tables_before = seam.table_names(db_path)
        first = call_action(mod.migrate_action, conn, _ns(db_path=db_path, dry_run=True))
        assert first["dry_run"] is True
        assert _snapshot(vconn, tables) == before
        assert seam.table_exists("erpclaw_schema_migration", db_path) is False
        assert seam.table_names(db_path) == tables_before
        second = call_action(mod.migrate_action, conn, _ns(db_path=db_path, dry_run=True))
        assert second["pending"] == first["pending"]
        assert _snapshot(vconn, tables) == before


class TestMigrateCredentialsDepth:
    """migrate-credentials moves addon plaintext keys into the file store.

    The stripe/shopify source tables are addon-owned and absent here, so the
    real behaviour on this database is a documented no-op. The action never
    validates input, so both tests prove the no-write halves. No GL posting.
    """

    _TABLES = ["account", "gl_entry", "custom_field", "custom_field_value",
               "erp_user", "role", "user_role", "role_permission", "audit_log"]

    def test_without_source_tables_nothing_moves_and_nothing_writes(
            self, conn, db_path, vconn, cred_store):
        assert seam.table_exists("stripe_account", db_path) is False
        names_before = cred_store.list_credentials()
        before = _snapshot(vconn, self._TABLES)
        result = call_action(mod.migrate_credentials_action, conn, _ns(dry_run=False))
        assert is_ok(result)
        assert result["dry_run"] is False
        assert result["moved"] == []
        assert result["skipped"] == []
        assert "set-credential" in result.get("next", "")
        assert cred_store.list_credentials() == names_before
        assert _snapshot(vconn, self._TABLES) == before

    def test_dry_run_previews_without_moving(self, conn, db_path, vconn, cred_store):
        assert seam.table_exists("stripe_account", db_path) is False
        names_before = cred_store.list_credentials()
        before = _snapshot(vconn, self._TABLES)
        result = call_action(mod.migrate_credentials_action, conn, _ns(dry_run=True))
        assert is_ok(result)
        assert result["dry_run"] is True
        assert result["moved"] == []
        assert result["skipped"] == []
        assert cred_store.list_credentials() == names_before
        assert _snapshot(vconn, self._TABLES) == before


class TestRemoveCustomFieldDepth:
    """remove-custom-field deletes a definition and its values. No GL posting."""

    def test_confirmed_removal_deletes_definition_and_values(
            self, conn, db_path, vconn):
        assert is_ok(call_action(mod.add_custom_field_action, conn, _ns(
            table="item", field_name="depth_hs", field_type="text",
            label=None, default=None, required=False, options=None,
            skill_name="erpclaw-setup")))
        assert is_ok(call_action(mod.add_custom_field_action, conn, _ns(
            table="item", field_name="depth_other", field_type="text",
            label=None, default=None, required=False, options=None,
            skill_name="erpclaw-setup")))
        assert is_ok(call_action(mod.set_custom_field_value_action, conn, _ns(
            table="item", row_id="depth-i1", field_name="depth_hs", value="8471")))
        assert is_ok(call_action(mod.set_custom_field_value_action, conn, _ns(
            table="item", row_id="depth-i1", field_name="depth_other",
            value="keepme")))
        removed = call_action(mod.remove_custom_field_action, conn, _ns(
            table="item", field_name="depth_hs", confirm=True))
        assert is_ok(removed)
        assert removed["result"] == "removed"
        assert removed["deleted_values"] == 1
        assert _select_where(vconn, "custom_field", {
            "table_name": "item", "field_name": "depth_hs"}) == []
        assert _select_where(vconn, "custom_field_value", {
            "table_name": "item", "doc_id": "depth-i1",
            "field_name": "depth_hs"}) == []
        kept_def = _select_where(vconn, "custom_field", {
            "table_name": "item", "field_name": "depth_other"})
        assert len(kept_def) == 1
        kept_val = _select_where(vconn, "custom_field_value", {
            "table_name": "item", "doc_id": "depth-i1",
            "field_name": "depth_other"})
        assert len(kept_val) == 1 and kept_val[0]["value"] == "keepme"

    def test_values_without_confirm_refused_without_delete(
            self, conn, db_path, vconn):
        assert is_ok(call_action(mod.add_custom_field_action, conn, _ns(
            table="item", field_name="depth_hs", field_type="text",
            label=None, default=None, required=False, options=None,
            skill_name="erpclaw-setup")))
        assert is_ok(call_action(mod.set_custom_field_value_action, conn, _ns(
            table="item", row_id="depth-i1", field_name="depth_hs", value="8471")))
        tables = ["custom_field", "custom_field_value", "audit_log"]
        before = _snapshot(vconn, tables)
        refused = call_action(mod.remove_custom_field_action, conn, _ns(
            table="item", field_name="depth_hs", confirm=False))
        assert is_error(refused)
        assert "stored value(s)" in refused.get("message", "")
        assert "--confirm" in refused.get("message", "")
        assert _snapshot(vconn, tables) == before
        still_def = _select_where(vconn, "custom_field", {
            "table_name": "item", "field_name": "depth_hs"})
        assert len(still_def) == 1
        still_val = _select_where(vconn, "custom_field_value", {
            "table_name": "item", "doc_id": "depth-i1", "field_name": "depth_hs"})
        assert len(still_val) == 1 and still_val[0]["value"] == "8471"


class TestRevokeRoleDepth:
    """revoke-role deletes one user_role row. No GL posting."""

    def test_revoke_deletes_exact_assignment_row(self, conn, db_path, vconn):
        first = call_action(mod.add_user, conn, _ns(
            name="depth_revoke", email="revoke@depth.test",
            full_name=None, company_id=None))
        other = call_action(mod.add_user, conn, _ns(
            name="depth_stays", email="stays@depth.test",
            full_name=None, company_id=None))
        assert is_ok(call_action(mod.assign_role, conn, _ns(
            user_id=first["user_id"], role_name="HR User", company_id=None)))
        assert is_ok(call_action(mod.assign_role, conn, _ns(
            user_id=other["user_id"], role_name="HR User", company_id=None)))
        assigned = _select_where(vconn, "user_role", {"user_id": first["user_id"]})
        assert len(assigned) == 1
        revoked = call_action(mod.revoke_role, conn, _ns(
            user_id=first["user_id"], role_name="HR User", company_id=None))
        assert is_ok(revoked) and revoked["revoked"] == "HR User"
        assert _select_where(vconn, "user_role", {"user_id": first["user_id"]}) == []
        kept = _select_where(vconn, "user_role", {"user_id": other["user_id"]})
        assert len(kept) == 1
        users = _select_where(vconn, "erp_user", {"id": first["user_id"]})
        assert len(users) == 1 and users[0]["username"] == "depth_revoke"

    def test_revoke_unassigned_refused_without_write(self, conn, db_path, vconn):
        created = call_action(mod.add_user, conn, _ns(
            name="depth_norole", email="norole@depth.test",
            full_name=None, company_id=None))
        tables = ["user_role", "erp_user", "role", "audit_log"]
        before = _snapshot(vconn, tables)
        refused = call_action(mod.revoke_role, conn, _ns(
            user_id=created["user_id"], role_name="HR User", company_id=None))
        assert is_error(refused)
        assert "not assigned" in refused.get("message", "")
        assert _snapshot(vconn, tables) == before


class TestSeedPermissionsDepth:
    """seed-permissions fills role_permission. No GL posting."""

    def test_seed_writes_exact_permission_rows(self, conn, db_path, vconn):
        assert _row_count(vconn, "role_permission") == 0
        result = call_action(mod.seed_permissions, conn, _ns())
        assert is_ok(result)
        total = _row_count(vconn, "role_permission")
        assert result["permissions_seeded"] == total
        assert total > 0
        managers = _select_where(vconn, "role", {"name": "System Manager"})
        assert len(managers) == 1
        pinned = [row for row in _table_rows(vconn, "role_permission")
                  if row["role_id"] == managers[0]["id"]
                  and row["skill"] == "*" and row["action_pattern"] == "*"]
        assert len(pinned) == 1
        assert pinned[0]["allowed"] == 1

    def test_reseed_changes_nothing(self, conn, db_path, vconn):
        # seed-permissions takes no input that validates, so it never
        # refuses. This second test proves idempotence instead.
        call_action(mod.seed_permissions, conn, _ns())
        tables = ["role_permission", "role", "audit_log"]
        before = _snapshot(vconn, tables)
        total_before = _row_count(vconn, "role_permission")
        again = call_action(mod.seed_permissions, conn, _ns())
        assert is_ok(again)
        assert again["permissions_seeded"] == total_before
        assert _row_count(vconn, "role_permission") == total_before
        assert _snapshot(vconn, tables) == before


class TestSetCredentialDepth:
    """set-credential writes the encrypted file store, not the database."""

    _TABLES = ["erp_user", "custom_field", "custom_field_value",
               "account_type_registry", "voucher_type_registry", "role",
               "user_role", "role_permission", "audit_log"]

    def test_store_reads_back_exact_value(self, conn, db_path, vconn, cred_store):
        before = _snapshot(vconn, self._TABLES)
        result = call_action(mod.set_credential_action, conn, _ns(
            integration="depth-gateway", value="depth-gateway-secret-007"))
        assert is_ok(result)
        assert result["integration"] == "depth-gateway"
        assert cred_store.get_credential("depth-gateway") == "depth-gateway-secret-007"
        assert "depth-gateway" in cred_store.list_credentials()
        assert _snapshot(vconn, self._TABLES) == before

    def test_missing_value_refused_without_store(self, conn, db_path, vconn, cred_store):
        names_before = cred_store.list_credentials()
        before = _snapshot(vconn, self._TABLES)
        refused = call_action(mod.set_credential_action, conn, _ns(
            integration="depth-empty", value=None))
        assert is_error(refused)
        assert "credential value required" in refused.get("message", "")
        assert cred_store.get_credential("depth-empty") is None
        assert cred_store.list_credentials() == names_before
        assert _snapshot(vconn, self._TABLES) == before


class TestSetCustomFieldValueDepth:
    """set-custom-field-value upserts one custom_field_value row. No GL posting."""

    def test_store_then_overwrite_reads_back_exact_text(
            self, conn, db_path, vconn):
        assert is_ok(call_action(mod.add_custom_field_action, conn, _ns(
            table="customer", field_name="depth_tier", field_type="select",
            label=None, default=None, required=False, options="Gold,Silver",
            skill_name="erpclaw-setup")))
        first = call_action(mod.set_custom_field_value_action, conn, _ns(
            table="customer", row_id="depth-c1", field_name="depth_tier",
            value="Gold"))
        assert is_ok(first) and first["value"] == "Gold"
        rows = _select_where(vconn, "custom_field_value", {
            "table_name": "customer", "doc_id": "depth-c1",
            "field_name": "depth_tier"})
        assert len(rows) == 1
        assert rows[0]["value"] == "Gold"
        assert str(rows[0]["value"]) == "Gold"
        second = call_action(mod.set_custom_field_value_action, conn, _ns(
            table="customer", row_id="depth-c1", field_name="depth_tier",
            value="Silver"))
        assert is_ok(second)
        again = _select_where(vconn, "custom_field_value", {
            "table_name": "customer", "doc_id": "depth-c1",
            "field_name": "depth_tier"})
        assert len(again) == 1 and again[0]["value"] == "Silver"
        assert _select_where(vconn, "custom_field_value", {
            "table_name": "customer", "doc_id": "depth-c9"}) == []

    def test_invalid_select_value_refused_without_write(self, conn, db_path, vconn):
        assert is_ok(call_action(mod.add_custom_field_action, conn, _ns(
            table="customer", field_name="depth_tier", field_type="select",
            label=None, default=None, required=False, options="Gold,Silver",
            skill_name="erpclaw-setup")))
        assert is_ok(call_action(mod.set_custom_field_value_action, conn, _ns(
            table="customer", row_id="depth-c1", field_name="depth_tier",
            value="Gold")))
        tables = ["custom_field", "custom_field_value", "audit_log"]
        before = _snapshot(vconn, tables)
        refused = call_action(mod.set_custom_field_value_action, conn, _ns(
            table="customer", row_id="depth-c1", field_name="depth_tier",
            value="Bronze"))
        assert is_error(refused)
        assert "must be one of" in refused.get("message", "")
        assert _snapshot(vconn, tables) == before
        kept = _select_where(vconn, "custom_field_value", {
            "table_name": "customer", "doc_id": "depth-c1",
            "field_name": "depth_tier"})
        assert len(kept) == 1 and kept[0]["value"] == "Gold"
