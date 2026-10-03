"""Tests for the M0 registry-administration actions (erpclaw-setup).

add/list/deactivate-account-type, add/list/deactivate-voucher-type,
validate-registry-completeness.
"""
import argparse
import pytest
from setup_helpers import call_action, seed_company, is_ok, is_error, load_db_query, read_one, open_reader, freeze_snapshot
from erpclaw_lib.query import Q, Table, P  # noqa: E402 (plant rows via builders, not literals)
from erpclaw_lib import seam  # noqa: E402 (catalog questions go through the seam)

mod = load_db_query()


def _ns(**kw):
    base = dict(account_type=None, voucher_type=None, target_table=None,
                label=None, skill_name=None, include_inactive=False, name=None,
                root_type=None, account_number=None, parent_id=None, currency=None,
                is_group=False, company_id=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _call(fn, conn, **kw):
    return call_action(getattr(mod, fn), conn, _ns(**kw))


class TestAccountTypeRegistry:
    def test_add_list_deactivate(self, conn):
        before = _call("list_account_types", conn)["count"]
        r = _call("add_account_type", conn, account_type="crypto_wallet", label="Crypto Wallet")
        assert is_ok(r) and r["result"] == "registered"
        assert _call("list_account_types", conn)["count"] == before + 1
        # duplicate rejected
        assert is_error(_call("add_account_type", conn, account_type="crypto_wallet"))
        # deactivate (unused) -> drops from active list
        assert is_ok(_call("deactivate_account_type", conn, account_type="crypto_wallet"))
        assert _call("list_account_types", conn)["count"] == before
        # still visible with --include-inactive
        assert _call("list_account_types", conn, include_inactive=True)["count"] == before + 1

    def test_deactivate_blocked_when_in_use(self, conn):
        cid = seed_company(conn)
        # 'bank' is seeded + active; create an account using it, then deactivation must block
        conn.execute("INSERT INTO account (id, name, root_type, account_type, company_id) "
                     "VALUES ('acc-x', 'Bank', 'asset', 'bank', ?)", (cid,))
        conn.commit()
        assert is_error(_call("deactivate_account_type", conn, account_type="bank"))

    def test_missing_arg(self, conn):
        assert is_error(_call("add_account_type", conn))


class TestVoucherTypeRegistry:
    def test_add_list_deactivate(self, conn):
        r = _call("add_voucher_type", conn, voucher_type="rebate", target_table="gl_entry", label="Rebate")
        assert is_ok(r) and r["result"] == "registered"
        gl_list = _call("list_voucher_types", conn, target_table="gl_entry")
        assert "rebate" in {v["voucher_type"] for v in gl_list["voucher_types"]}
        assert is_ok(_call("deactivate_voucher_type", conn, voucher_type="rebate", target_table="gl_entry"))

    def test_bad_target_table(self, conn):
        assert is_error(_call("add_voucher_type", conn, voucher_type="x", target_table="bogus"))

    def test_deactivate_blocked_when_in_use(self, conn):
        cid = seed_company(conn)
        conn.execute("INSERT INTO account (id, name, root_type, account_type, company_id) "
                     "VALUES ('a-vt', 'Cash', 'asset', 'cash', ?)", (cid,))
        conn.execute("INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
                     "voucher_type, voucher_id) VALUES ('g-vt', '2026-05-31', 'a-vt', '1', '0', 'journal_entry', 'JE-X')")
        conn.commit()
        assert is_error(_call("deactivate_voucher_type", conn, voucher_type="journal_entry", target_table="gl_entry"))


_VALIDATE_CANDIDATES = ("account", "gl_entry", "payment_entry",
                         "payment_ledger_entry", "asset", "account_type_registry",
                         "party_type_registry", "voucher_type_registry",
                         "asset_status_registry", "audit_log")


def _validate_tables(db_path):
    """Tables the diagnostic can observe, restricted to ones that exist.

    Asked through the seam catalog so the list stays truthful on backends or
    installs where an optional table is absent.
    """
    return [t for t in _VALIDATE_CANDIDATES if seam.table_exists(t, db_path)]


class TestValidateRegistryCompleteness:
    def test_complete_on_fresh_db(self, conn, db_path):
        """Behavioural depth: the diagnostic reports a fresh database complete
        AND writes nothing — the observed tables are byte-identical after.

        Ledger: validate-registry-completeness is read-only; it posts no legs,
        so no balance assertion can hold here.
        """
        reader = open_reader(db_path)
        try:
            assert seam.table_exists("account_type_registry", db_path)
            tables = _validate_tables(db_path)
            before = freeze_snapshot(reader, db_path, tables)
            r = _call("validate_registry_completeness", conn)
            assert is_ok(r) and r["complete"] is True
            assert r["unregistered_in_use"] == {}
            assert freeze_snapshot(reader, db_path, tables) == before
        finally:
            reader.close()

    def test_flags_unregistered_value_in_use(self, conn, db_path):
        """Behavioural depth: the flag names an exact stored value, the flagged
        row is read back from the database (not just the response), every other
        bucket is exactly empty, and the diagnostic itself wrote nothing.
        """
        cid = seed_company(conn)
        # an account_type that is NOT registered (CHECK is gone, so the plant succeeds)
        t = Table("account")
        q = Q.into(t).columns("id", "name", "root_type", "account_type", "company_id").insert(P(), P(), P(), P(), P())
        conn.execute(q.get_sql(), ("a-unreg", "Weird", "asset", "totally_unregistered", cid))
        conn.commit()
        reader = open_reader(db_path)
        try:
            tables = _validate_tables(db_path)
            before = freeze_snapshot(reader, db_path, tables)
            r = _call("validate_registry_completeness", conn)
            assert r["complete"] is False
            assert r["unregistered_in_use"] == {"account_type": ["totally_unregistered"]}
            stored = read_one(reader, "account",
                              ["id", "name", "root_type", "account_type", "company_id"],
                              "a-unreg")
            assert stored["account_type"] == "totally_unregistered"
            assert stored["company_id"] == cid
            assert freeze_snapshot(reader, db_path, tables) == before
        finally:
            reader.close()
