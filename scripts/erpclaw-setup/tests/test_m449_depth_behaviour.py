"""M449 depth: behavioural evidence for 12 erpclaw-setup actions.

Each action below already had a test that proved the wrong thing (response
shape or routability). Each test here proves the database effect instead:
what row exists afterwards with which exact values, what changed from what
to what, and what did not change. Money is TEXT: exact string comparisons,
Decimal for arithmetic, never float, never round().

Verification reads go through a fresh ``erpclaw_lib.db.get_connection()``
handle (the seam's connection path, not the fixture handle the action ran
on) and row queries are built with PyPika through ``erpclaw_lib.query``;
catalog questions (table/column presence) go through ``erpclaw_lib.seam``.

Per-action depth signal:
- add-account-type ............ STORED ROW (account_type_registry)
- add-voucher-type ............ STORED ROW (voucher_type_registry)
- assign-role ................. STORED ROW (user_role, joined to role)
- check-telegram-permission ... READ-ONLY decision (no stored row, no ledger;
                                asserts the allow/deny readout follows seeded
                                rows, and tables are byte-identical afterwards)
- deactivate-account-type ..... STORED ROW change (is_active 1 -> 0)
- deactivate-voucher-type ..... STORED ROW change (is_active 1 -> 0)
- delete-credential ........... FILE EFFECT (credentials-store entry removed;
                                no DB row and no ledger; DB tables pinned
                                unchanged because the store is file-backed)
- fetch-exchange-rates ........ STORED ROWS (exchange_rate insert + update,
                                TEXT rates asserted with Decimal equality)
- get-credential .............. READ-ONLY decision (no stored row, no ledger;
                                asserts the existence readout and that the
                                secret value is never leaked)
- get-custom-field-values ..... STORED ROWS readback (custom_field_value)
- get-schema-version .......... STORED ROW readback (schema_version)
- import-master-key-from-backup  FILE EFFECT (master.key bytes installed;
                                no DB row and no ledger; DB tables pinned
                                unchanged because the key lives on disk)

Ledger note: none of the twelve actions posts to the stock or general ledger
on any path (the writers touch registry, RBAC, custom-field, FX, or file
stores only; the getters are pure reads). Every success test therefore pins
the gl_entry and stock_ledger_entry counts unchanged so a later reader does
not add a leg assertion that cannot hold. The one money-carrying action
(fetch-exchange-rates) asserts both the exact stored TEXT and Decimal
equality of the rate.
"""
import argparse
import json
import os
import urllib.request
import urllib.error
from datetime import date
from decimal import Decimal

import pytest

from setup_helpers import (
    call_action, is_error, is_ok, load_db_query, seed_company, seed_currency,
)
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, P, Table, Field

mod = load_db_query()


_DEFAULTS = dict(
    account_type=None, voucher_type=None, target_table=None, label=None,
    skill_name=None, include_inactive=False,
    user_id=None, role_name=None, company_id=None,
    name=None, email=None, full_name=None,
    telegram_user_id=None, skill=None, check_action=None,
    integration=None, value=None, from_stdin=False, from_env=None,
    backup_path=None, passphrase=None, passphrase_from_stdin=False,
    passphrase_from_env=None, force=False,
    table=None, field_name=None, field_type=None, options=None,
    required=False, default=None, row_id=None,
    module=None,
)


def _ns(**kw):
    d = dict(_DEFAULTS)
    d.update(kw)
    return argparse.Namespace(**d)


_LEDGERS = ("gl_entry", "stock_ledger_entry")


def _dump(db_path, tables):
    """Byte-level snapshot of the named tables via a fresh seam connection."""
    conn = get_connection(db_path)
    try:
        out = {}
        for name in tables:
            tbl = Table(name)
            rows = conn.execute(Q.from_(tbl).select(tbl.star).get_sql()).fetchall()
            out[name] = sorted((tuple(r) for r in rows), key=repr)
        return out
    finally:
        conn.close()


def _count(db_path, table):
    return len(_dump(db_path, [table])[table])


def _row_where(db_path, table, column, value):
    """All rows of table with column == value, as plain dicts."""
    conn = get_connection(db_path)
    try:
        tbl = Table(table)
        q = Q.from_(tbl).select(tbl.star).where(Field(column) == P())
        return [dict(r) for r in conn.execute(q.get_sql(), (value,)).fetchall()]
    finally:
        conn.close()


def _row_by_id(db_path, table, row_id):
    rows = _row_where(db_path, table, "id", row_id)
    assert len(rows) == 1, f"expected exactly one {table} row id={row_id}"
    return rows[0]


@pytest.fixture
def cred_sandbox(monkeypatch, tmp_path):
    """Redirect the credentials store + master-key path into tmp_path.

    The credential and master-key actions are file-backed (they never touch
    the database), so without isolation they would read and write the real
    per-user config dir. Patched attributes are read at call time by both
    the lib and the db_query wrappers, so this fully contains them.
    """
    import erpclaw_lib.credentials as creds_mod
    import erpclaw_lib.master_key as mk_mod
    cfg = tmp_path / "erpclaw_cfg"
    cfg.mkdir()
    monkeypatch.setattr(mk_mod, "CONFIG_DIR", str(cfg))
    monkeypatch.setattr(mk_mod, "MASTER_KEY_PATH", str(cfg / "master.key"))
    monkeypatch.setattr(creds_mod, "CREDENTIALS_PATH",
                        str(cfg / "credentials.json.enc"))
    monkeypatch.delenv("ERPCLAW_STRICT_ENV", raising=False)
    return cfg


def _make_backup(tmp_path, passphrase, master_key):
    """Fabricate a current-format encrypted backup carrying a wrapped key."""
    from erpclaw_lib.crypto import wrap_master_key, encrypt_file
    raw = tmp_path / "plain.db"
    raw.write_bytes(b"m449-probe-plaintext")
    wrapped = wrap_master_key(master_key, passphrase)
    out = tmp_path / "backup.enc"
    encrypt_file(str(raw), str(out), passphrase, wrapped_master_key=wrapped)
    return str(out)


class _FakeHttp:
    """Minimal context-manager stand-in for urllib responses."""

    def __init__(self, payload_bytes):
        self._payload = payload_bytes

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _mock_rates(monkeypatch, eur_text, jpy_text):
    # Rates travel as JSON numbers on the wire (as the real API sends them),
    # but the literals here stay TEXT so no float object ever appears: money
    # is text, end to end.
    payload = ('{"base": "USD", "rates": {"EUR": %s, "JPY": %s}}'
               % (eur_text, jpy_text)).encode("utf-8")

    def _fake_open(request, timeout=15):
        return _FakeHttp(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_open)


# ── add-account-type: STORED ROW ────────────────────────────────────────────

class TestAddAccountTypeDepth:
    TABLES = ("account_type_registry", "audit_log") + _LEDGERS

    def test_registers_exact_row_and_nothing_else(self, conn, db_path):
        # This action does NOT reach the ledger: one registry row plus its
        # audit row. Both ledger counts are pinned unchanged.
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.add_account_type, conn, _ns(
            account_type="m449_custody", label="M449 Custody",
            skill_name="erpclaw-gl",
        ))
        assert is_ok(result), result
        assert result["result"] == "registered"
        assert result["account_type"] == "m449_custody"

        rows = _row_where(db_path, "account_type_registry",
                          "account_type", "m449_custody")
        assert len(rows) == 1
        row = rows[0]
        assert row["account_type"] == "m449_custody"
        assert row["label"] == "M449 Custody"
        assert row["skill_name"] == "erpclaw-gl"
        assert row["is_active"] == 1

        bank = _row_where(db_path, "account_type_registry",
                          "account_type", "bank")[0]
        assert bank["is_active"] == 1

        after = _dump(db_path, self.TABLES)
        assert len(after["account_type_registry"]) == \
            len(before["account_type_registry"]) + 1
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1
        assert after["gl_entry"] == before["gl_entry"]
        assert after["stock_ledger_entry"] == before["stock_ledger_entry"]

    def test_missing_account_type_refused_without_write(self, conn, db_path):
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.add_account_type, conn, _ns(account_type=None))
        assert is_error(result), result
        assert "--account-type is required" in result.get("message", "")
        assert _dump(db_path, self.TABLES) == before


# ── add-voucher-type: STORED ROW ────────────────────────────────────────────

class TestAddVoucherTypeDepth:
    TABLES = ("voucher_type_registry", "audit_log") + _LEDGERS

    def test_registers_exact_row_and_nothing_else(self, conn, db_path):
        # This action does NOT reach the ledger: one registry row plus its
        # audit row. Both ledger counts are pinned unchanged.
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.add_voucher_type, conn, _ns(
            voucher_type="m449_rebate", target_table="gl_entry",
            label="M449 Rebate", skill_name="erpclaw-gl",
        ))
        assert is_ok(result), result
        assert result["result"] == "registered"

        rows = [r for r in _row_where(db_path, "voucher_type_registry",
                                      "voucher_type", "m449_rebate")
                if r["target_table"] == "gl_entry"]
        assert len(rows) == 1
        row = rows[0]
        assert row["voucher_type"] == "m449_rebate"
        assert row["target_table"] == "gl_entry"
        assert row["label"] == "M449 Rebate"
        assert row["skill_name"] == "erpclaw-gl"
        assert row["is_active"] == 1

        after = _dump(db_path, self.TABLES)
        assert len(after["voucher_type_registry"]) == \
            len(before["voucher_type_registry"]) + 1
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1
        assert after["gl_entry"] == before["gl_entry"]
        assert after["stock_ledger_entry"] == before["stock_ledger_entry"]

    def test_bad_target_table_refused_without_write(self, conn, db_path):
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.add_voucher_type, conn, _ns(
            voucher_type="m449_bad", target_table="bogus",
        ))
        assert is_error(result), result
        assert "--target-table must be one of" in result.get("message", "")
        assert _dump(db_path, self.TABLES) == before


# ── assign-role: STORED ROW ─────────────────────────────────────────────────

class TestAssignRoleDepth:
    TABLES = ("erp_user", "role", "user_role", "audit_log") + _LEDGERS

    def _make_user(self, conn, name):
        created = call_action(mod.add_user, conn, _ns(
            name=name, email=f"{name}@m449.test", full_name=None,
            company_id=None,
        ))
        assert is_ok(created), created
        return created["user_id"]

    def test_assign_writes_exact_user_role_row(self, conn, db_path):
        # This action does NOT reach the ledger: one user_role row plus its
        # audit row. Both ledger counts are pinned unchanged.
        uid = self._make_user(conn, "m449_assignee")
        other = self._make_user(conn, "m449_bystander")
        before = _dump(db_path, self.TABLES)

        result = call_action(mod.assign_role, conn, _ns(
            user_id=uid, role_name="System Manager", company_id=None,
        ))
        assert is_ok(result), result
        assert result["role_name"] == "System Manager"

        role = _row_where(db_path, "role", "name", "System Manager")[0]
        rows = _row_where(db_path, "user_role", "user_id", uid)
        assert len(rows) == 1
        assert rows[0]["user_id"] == uid
        assert rows[0]["role_id"] == role["id"]
        assert rows[0]["company_id"] is None

        assert _row_where(db_path, "user_role", "user_id", other) == []

        after = _dump(db_path, self.TABLES)
        assert len(after["user_role"]) == len(before["user_role"]) + 1
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1
        assert after["gl_entry"] == before["gl_entry"]
        assert after["stock_ledger_entry"] == before["stock_ledger_entry"]

    def test_missing_user_id_refused_without_write(self, conn, db_path):
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.assign_role, conn, _ns(
            user_id=None, role_name="System Manager", company_id=None,
        ))
        assert is_error(result), result
        assert "--user-id is required" in result.get("message", "")
        assert _dump(db_path, self.TABLES) == before


# ── check-telegram-permission: READ-ONLY decision (DEFECT) ──────────────────

class TestCheckTelegramPermissionDepth:
    TABLES = ("erp_user", "user_role", "role_permission",
              "audit_log") + _LEDGERS

    def test_decision_path_documents_missing_column_defect(
        self, conn, db_path,
    ):
        # FINDING (M449, deliberately not fixed): check-telegram-permission
        # cannot answer on a freshly provisioned database. erp_user carries
        # no telegram_user_id column (absent from the provisioned schema and
        # added by no migration), so even the not-linked deny path raises
        # instead of returning a decision. Expected
        # {"allowed": False, "reason": "not_linked"}; observed
        # OperationalError "no such column: telegram_user_id". The failed
        # read still writes nothing: tables are byte-identical afterwards.
        # This action does NOT reach the ledger on any path.
        from erpclaw_lib import seam
        assert "telegram_user_id" not in seam.column_names(
            "erp_user", db_path)
        before = _dump(db_path, self.TABLES)
        with pytest.raises(Exception, match="no such column"):
            call_action(mod.check_telegram_permission, conn, _ns(
                telegram_user_id="799999", skill="erpclaw-gl",
                check_action="submit-journal-entry",
            ))
        assert _dump(db_path, self.TABLES) == before

    def test_link_path_documents_missing_column_defect(
        self, conn, db_path,
    ):
        # FINDING (M449, deliberately not fixed): link-telegram-user, the
        # only writer that could make the decision path reachable, fails the
        # same way on the same missing column, so no telegram link can ever
        # be stored. Expected a link row effect; observed OperationalError
        # with the database byte-identical afterwards.
        created = call_action(mod.add_user, conn, _ns(
            name="m449_tg_mgr", email="tgmgr@m449.test", full_name=None,
            company_id=None,
        ))
        assert is_ok(created), created
        before = _dump(db_path, self.TABLES)
        with pytest.raises(Exception, match="no such column"):
            call_action(mod.link_telegram_user, conn, _ns(
                user_id=created["user_id"], telegram_user_id="710001",
            ))
        assert _dump(db_path, self.TABLES) == before

    def test_missing_telegram_user_id_refused_without_write(
        self, conn, db_path,
    ):
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.check_telegram_permission, conn, _ns(
            telegram_user_id=None, skill="erpclaw-gl",
            check_action="submit-journal-entry",
        ))
        assert is_error(result), result
        assert "--telegram-user-id is required" in result.get("message", "")
        assert _dump(db_path, self.TABLES) == before


# ── deactivate-account-type: STORED ROW change ──────────────────────────────

class TestDeactivateAccountTypeDepth:
    TABLES = ("account_type_registry", "account", "audit_log") + _LEDGERS

    def test_deactivate_flips_is_active(self, conn, db_path):
        # This action does NOT reach the ledger: one flag flip plus its audit
        # row. Both ledger counts are pinned unchanged.
        added = call_action(mod.add_account_type, conn, _ns(
            account_type="m449_temp", label="M449 Temp",
            skill_name="erpclaw-gl",
        ))
        assert is_ok(added), added
        assert _row_where(db_path, "account_type_registry",
                          "account_type", "m449_temp")[0]["is_active"] == 1
        before = _dump(db_path, self.TABLES)

        result = call_action(mod.deactivate_account_type, conn, _ns(
            account_type="m449_temp",
        ))
        assert is_ok(result), result
        assert result["result"] == "deactivated"

        row = _row_where(db_path, "account_type_registry",
                         "account_type", "m449_temp")[0]
        assert row["is_active"] == 0
        assert _row_where(db_path, "account_type_registry",
                          "account_type", "bank")[0]["is_active"] == 1

        after = _dump(db_path, self.TABLES)
        assert len(after["account_type_registry"]) == \
            len(before["account_type_registry"])
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1
        assert after["gl_entry"] == before["gl_entry"]
        assert after["stock_ledger_entry"] == before["stock_ledger_entry"]

    def test_deactivate_in_use_refused_without_write(self, conn, db_path):
        cid = seed_company(conn)
        conn.execute(
            "INSERT INTO account (id, name, root_type, account_type, company_id) "
            "VALUES ('m449-bank-acct', 'M449 Bank', 'asset', 'bank', ?)",
            (cid,),
        )
        conn.commit()
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.deactivate_account_type, conn, _ns(
            account_type="bank",
        ))
        assert is_error(result), result
        assert "still use it" in result.get("message", "")
        assert _row_where(db_path, "account_type_registry",
                          "account_type", "bank")[0]["is_active"] == 1
        assert _dump(db_path, self.TABLES) == before

    def test_deactivate_unregistered_refused(self, conn, db_path):
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.deactivate_account_type, conn, _ns(
            account_type="m449_ghost_type",
        ))
        assert is_error(result), result
        assert "not registered" in result.get("message", "")
        assert _dump(db_path, self.TABLES) == before


# ── deactivate-voucher-type: STORED ROW change ──────────────────────────────

class TestDeactivateVoucherTypeDepth:
    TABLES = ("voucher_type_registry", "gl_entry", "audit_log") + _LEDGERS

    def test_deactivate_flips_is_active(self, conn, db_path):
        # This action does NOT reach the ledger: one flag flip plus its audit
        # row. Both ledger counts are pinned unchanged.
        added = call_action(mod.add_voucher_type, conn, _ns(
            voucher_type="m449_temp_vt", target_table="gl_entry",
            label="M449 Temp", skill_name="erpclaw-gl",
        ))
        assert is_ok(added), added
        current = [r for r in _row_where(db_path, "voucher_type_registry",
                                         "voucher_type", "m449_temp_vt")
                   if r["target_table"] == "gl_entry"][0]
        assert current["is_active"] == 1
        before = _dump(db_path, self.TABLES)

        result = call_action(mod.deactivate_voucher_type, conn, _ns(
            voucher_type="m449_temp_vt", target_table="gl_entry",
        ))
        assert is_ok(result), result
        assert result["result"] == "deactivated"

        row = [r for r in _row_where(db_path, "voucher_type_registry",
                                     "voucher_type", "m449_temp_vt")
               if r["target_table"] == "gl_entry"][0]
        assert row["is_active"] == 0

        after = _dump(db_path, self.TABLES)
        assert len(after["voucher_type_registry"]) == \
            len(before["voucher_type_registry"])
        assert len(after["audit_log"]) == len(before["audit_log"]) + 1
        assert after["gl_entry"] == before["gl_entry"]
        assert after["stock_ledger_entry"] == before["stock_ledger_entry"]

    def test_deactivate_in_use_refused_without_write(self, conn, db_path):
        cid = seed_company(conn)
        conn.execute(
            "INSERT INTO account (id, name, root_type, account_type, company_id) "
            "VALUES ('m449-vt-acct', 'M449 Cash', 'asset', 'cash', ?)",
            (cid,),
        )
        added = call_action(mod.add_voucher_type, conn, _ns(
            voucher_type="m449_used_vt", target_table="gl_entry",
            label="M449 Used", skill_name="erpclaw-gl",
        ))
        assert is_ok(added), added
        conn.execute(
            "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
            "voucher_type, voucher_id) VALUES ('m449-vt-gl', '2026-05-31', "
            "'m449-vt-acct', '10.00', '0', 'm449_used_vt', 'M449-VT-1')",
        )
        conn.commit()
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.deactivate_voucher_type, conn, _ns(
            voucher_type="m449_used_vt", target_table="gl_entry",
        ))
        assert is_error(result), result
        assert "still use it" in result.get("message", "")
        row = [r for r in _row_where(db_path, "voucher_type_registry",
                                     "voucher_type", "m449_used_vt")
               if r["target_table"] == "gl_entry"][0]
        assert row["is_active"] == 1
        assert _dump(db_path, self.TABLES) == before


# ── delete-credential: FILE EFFECT ──────────────────────────────────────────

class TestDeleteCredentialDepth:
    TABLES = ("audit_log",) + _LEDGERS

    def test_delete_removes_store_entry_and_leaves_db_alone(
        self, conn, db_path, cred_sandbox,
    ):
        # The credentials store is file-backed: no DB row exists for it and
        # none is written. The effect is the store entry disappearing; the
        # proof of "nothing else" is byte-identical DB tables.
        seeded = call_action(mod.set_credential_action, conn, _ns(
            integration="m449-doom", value="K7mQ9xVz2RtY4UwE6",
        ))
        assert is_ok(seeded), seeded
        present = call_action(mod.get_credential_action, conn, _ns(
            integration="m449-doom",
        ))
        assert present["exists"] is True
        before = _dump(db_path, self.TABLES)

        result = call_action(mod.delete_credential_action, conn, _ns(
            integration="m449-doom",
        ))
        assert is_ok(result), result
        assert result["deleted"] is True
        assert result["integration"] == "m449-doom"

        gone = call_action(mod.get_credential_action, conn, _ns(
            integration="m449-doom",
        ))
        assert gone["exists"] is False

        repeat = call_action(mod.delete_credential_action, conn, _ns(
            integration="m449-doom",
        ))
        assert is_ok(repeat), repeat
        assert repeat["deleted"] is False
        assert "no credential found" in repeat.get("message", "")

        assert _dump(db_path, self.TABLES) == before

    def test_missing_integration_refused_without_write(
        self, conn, db_path, cred_sandbox,
    ):
        store = os.path.join(str(cred_sandbox), "credentials.json.enc")
        assert not os.path.exists(store)
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.delete_credential_action, conn, _ns(
            integration=None,
        ))
        assert is_error(result), result
        assert "--integration is required" in result.get("message", "")
        assert not os.path.exists(store)
        assert _dump(db_path, self.TABLES) == before


# ── fetch-exchange-rates: STORED ROWS ───────────────────────────────────────

class TestFetchExchangeRatesDepth:
    TABLES = ("exchange_rate", "currency", "audit_log") + _LEDGERS

    def _seed_currencies(self, conn):
        seed_currency(conn, "USD", "US Dollar", "$")
        seed_currency(conn, "EUR", "Euro", "E")
        seed_currency(conn, "JPY", "Japanese Yen", "Y")

    def test_fetch_inserts_then_updates_exact_text_rates(
        self, conn, db_path, monkeypatch,
    ):
        # The rate column is TEXT holding exact Decimal strings: both the
        # stored value and Decimal equality are asserted, never float.
        # The second fetch exercises the update path for the same pair+date.
        # No ledger legs exist for FX reads: ledger counts stay pinned.
        self._seed_currencies(conn)
        _mock_rates(monkeypatch, "0.92", "151.25")
        today = date.today().isoformat()
        before = _dump(db_path, self.TABLES)

        first = call_action(mod.fetch_exchange_rates, conn, _ns())
        assert is_ok(first), first
        assert first["rates_updated"] == 2
        assert first["base"] == "USD"
        assert first["date"] == today

        eur = [r for r in _row_where(db_path, "exchange_rate",
                                     "to_currency", "EUR")
               if r["from_currency"] == "USD" and r["effective_date"] == today]
        assert len(eur) == 1
        assert eur[0]["rate"] == "0.92"
        assert Decimal(eur[0]["rate"]) == Decimal("0.92")
        assert eur[0]["source"] == "api"
        jpy = [r for r in _row_where(db_path, "exchange_rate",
                                     "to_currency", "JPY")
               if r["from_currency"] == "USD" and r["effective_date"] == today]
        assert len(jpy) == 1
        assert jpy[0]["rate"] == "151.25"
        assert Decimal(jpy[0]["rate"]) == Decimal("151.25")

        mid = _dump(db_path, self.TABLES)
        assert len(mid["exchange_rate"]) == len(before["exchange_rate"]) + 2

        _mock_rates(monkeypatch, "0.95", "151.25")
        second = call_action(mod.fetch_exchange_rates, conn, _ns())
        assert is_ok(second), second
        assert second["rates_updated"] == 2

        eur2 = [r for r in _row_where(db_path, "exchange_rate",
                                      "to_currency", "EUR")
                if r["from_currency"] == "USD" and r["effective_date"] == today]
        assert len(eur2) == 1
        assert eur2[0]["rate"] == "0.95"
        assert Decimal(eur2[0]["rate"]) == Decimal("0.95")
        jpy2 = [r for r in _row_where(db_path, "exchange_rate",
                                      "to_currency", "JPY")
                if r["from_currency"] == "USD" and r["effective_date"] == today]
        assert len(jpy2) == 1
        assert jpy2[0]["rate"] == "151.25"

        after = _dump(db_path, self.TABLES)
        assert len(after["exchange_rate"]) == len(mid["exchange_rate"])
        assert after["currency"] == before["currency"]
        assert after["gl_entry"] == before["gl_entry"]
        assert after["stock_ledger_entry"] == before["stock_ledger_entry"]

    def test_network_failure_refused_without_write(
        self, conn, db_path, monkeypatch,
    ):
        self._seed_currencies(conn)

        def _boom(request, timeout=15):
            raise urllib.error.URLError("m449 simulated outage")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.fetch_exchange_rates, conn, _ns())
        assert is_error(result), result
        assert "Failed to fetch exchange rates" in result.get("message", "")
        assert _dump(db_path, self.TABLES) == before


# ── get-credential: READ-ONLY decision ──────────────────────────────────────

class TestGetCredentialDepth:
    TABLES = ("audit_log",) + _LEDGERS

    def test_reports_existence_without_leaking_value(
        self, conn, db_path, cred_sandbox,
    ):
        # Pure read: the store file must be byte-identical afterwards and the
        # DB tables untouched; the secret value must never appear in output.
        seeded = call_action(mod.set_credential_action, conn, _ns(
            integration="m449-probe", value="K7mQ9xVz2RtY4UwE6",
        ))
        assert is_ok(seeded), seeded
        before = _dump(db_path, self.TABLES)
        store = os.path.join(str(cred_sandbox), "credentials.json.enc")
        with open(store, "rb") as fh:
            file_before = fh.read()

        result = call_action(mod.get_credential_action, conn, _ns(
            integration="m449-probe",
        ))
        assert is_ok(result), result
        assert result["integration"] == "m449-probe"
        assert result["exists"] is True
        assert result["redacted_preview"] == "K7mQ...UwE6"
        assert "value" not in result
        assert "K7mQ9xVz2RtY4UwE6" not in json.dumps(result)

        missing = call_action(mod.get_credential_action, conn, _ns(
            integration="m449-absent",
        ))
        assert is_ok(missing), missing
        assert missing["exists"] is False

        with open(store, "rb") as fh:
            assert fh.read() == file_before
        assert _dump(db_path, self.TABLES) == before

    def test_missing_integration_refused_without_write(
        self, conn, db_path, cred_sandbox,
    ):
        store = os.path.join(str(cred_sandbox), "credentials.json.enc")
        assert not os.path.exists(store)
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.get_credential_action, conn, _ns(
            integration=None,
        ))
        assert is_error(result), result
        assert "--integration is required" in result.get("message", "")
        assert not os.path.exists(store)
        assert _dump(db_path, self.TABLES) == before


# ── get-custom-field-values: STORED ROWS readback ───────────────────────────

class TestGetCustomFieldValuesDepth:
    TABLES = ("custom_field", "custom_field_value", "audit_log") + _LEDGERS

    def _seed(self, conn):
        added = call_action(mod.add_custom_field_action, conn, _ns(
            table="customer", field_name="m449_tier", field_type="text",
        ))
        assert is_ok(added), added
        stored = call_action(mod.set_custom_field_value_action, conn, _ns(
            table="customer", row_id="cust-m449-01",
            field_name="m449_tier", value="Gold",
        ))
        assert is_ok(stored), stored

    def test_returns_exact_stored_values_with_no_writes(self, conn, db_path):
        # Readback of STORED ROWS: the values returned must equal the stored
        # rows exactly, and the read itself must write nothing (this action
        # does NOT reach the ledger either).
        self._seed(conn)
        before = _dump(db_path, self.TABLES)

        result = call_action(mod.get_custom_field_values_action, conn, _ns(
            table="customer", row_id="cust-m449-01", field_name=None,
        ))
        assert is_ok(result), result
        assert result["table"] == "customer"
        assert result["row_id"] == "cust-m449-01"
        assert result["custom_fields"] == {"m449_tier": "Gold"}

        rows = _row_where(db_path, "custom_field_value",
                          "doc_id", "cust-m449-01")
        assert len(rows) == 1
        assert rows[0]["table_name"] == "customer"
        assert rows[0]["field_name"] == "m449_tier"
        assert rows[0]["value"] == "Gold"

        single = call_action(mod.get_custom_field_values_action, conn, _ns(
            table="customer", row_id="cust-m449-01",
            field_name="m449_tier",
        ))
        assert is_ok(single), single
        assert single["custom_fields"] == {"m449_tier": "Gold"}

        assert _dump(db_path, self.TABLES) == before

    def test_missing_table_refused_without_write(self, conn, db_path):
        self._seed(conn)
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.get_custom_field_values_action, conn, _ns(
            table=None, row_id=None, field_name=None,
        ))
        assert is_error(result), result
        assert "--table and --row-id are required" in result.get("message", "")
        assert _dump(db_path, self.TABLES) == before


# ── get-schema-version: STORED ROW readback ─────────────────────────────────

class TestGetSchemaVersionDepth:
    TABLES = ("schema_version", "audit_log") + _LEDGERS

    def test_returns_exact_stored_version_with_no_writes(self, conn, db_path):
        # Readback of a STORED ROW: the response must equal the stored row
        # exactly, and the read itself must write nothing (no ledger either).
        expected = _row_where(db_path, "schema_version",
                              "module", "erpclaw-setup")[0]
        before = _dump(db_path, self.TABLES)

        result = call_action(mod.get_schema_version, conn, _ns(
            module="erpclaw-setup",
        ))
        assert is_ok(result), result
        assert result["module"] == expected["module"]
        assert result["version"] == expected["version"]
        assert result["updated_at"] == expected["updated_at"]

        assert _dump(db_path, self.TABLES) == before

    def test_unknown_module_refused_without_write(self, conn, db_path):
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.get_schema_version, conn, _ns(
            module="m449-ghost-module",
        ))
        assert is_error(result), result
        assert "No schema version found for module 'm449-ghost-module'" in \
            result.get("message", "")
        assert _dump(db_path, self.TABLES) == before


# ── import-master-key-from-backup: FILE EFFECT ──────────────────────────────

class TestImportMasterKeyFromBackupDepth:
    TABLES = ("audit_log",) + _LEDGERS

    def test_installs_exact_key_bytes_and_leaves_db_alone(
        self, conn, db_path, cred_sandbox, tmp_path,
    ):
        # The master key lives on disk, not in the database: no DB row exists
        # for it and none is written (no ledger either). The effect is the
        # installed file's bytes equalling the wrapped key exactly.
        master_key = os.urandom(32)
        backup = _make_backup(tmp_path, "m449-correct-horse", master_key)
        before = _dump(db_path, self.TABLES)

        result = call_action(mod.import_master_key_from_backup_action, conn,
                             _ns(backup_path=backup,
                                  passphrase="m449-correct-horse"))
        assert is_ok(result), result

        import erpclaw_lib.master_key as mk_mod
        with open(mk_mod.MASTER_KEY_PATH, "rb") as fh:
            assert fh.read() == master_key

        assert _dump(db_path, self.TABLES) == before

    def test_wrong_passphrase_refused_without_write(
        self, conn, db_path, cred_sandbox, tmp_path,
    ):
        backup = _make_backup(tmp_path, "m449-correct-horse", os.urandom(32))
        before = _dump(db_path, self.TABLES)
        result = call_action(mod.import_master_key_from_backup_action, conn,
                             _ns(backup_path=backup,
                                  passphrase="m449-wrong-horse"))
        assert is_error(result), result
        assert "passphrase did not match" in result.get("message", "")

        import erpclaw_lib.master_key as mk_mod
        assert not os.path.exists(mk_mod.MASTER_KEY_PATH)
        assert _dump(db_path, self.TABLES) == before


# ── catalog sanity through the seam ─────────────────────────────────────────

class TestSeamCatalogDepth:
    def test_owned_tables_visible_through_seam(self, conn, db_path):
        from erpclaw_lib import seam
        for table in ("account_type_registry", "voucher_type_registry",
                      "user_role", "exchange_rate", "custom_field",
                      "custom_field_value", "schema_version"):
            assert seam.table_exists(table, db_path), table
        assert "rate" in seam.column_names("exchange_rate", db_path)
        assert "is_active" in seam.column_names(
            "account_type_registry", db_path)
