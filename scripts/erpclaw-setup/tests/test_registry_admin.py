"""Tests for the M0 registry-administration actions (erpclaw-setup).

add/list/deactivate-account-type, add/list/deactivate-voucher-type,
validate-registry-completeness.
"""
import argparse
import pytest
from setup_helpers import call_action, seed_company, is_ok, is_error, load_db_query

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


class TestValidateRegistryCompleteness:
    def test_complete_on_fresh_db(self, conn):
        r = _call("validate_registry_completeness", conn)
        assert is_ok(r) and r["complete"] is True
        assert r["unregistered_in_use"] == {}

    def test_flags_unregistered_value_in_use(self, conn):
        cid = seed_company(conn)
        # an account_type that is NOT registered (CHECK is gone, so raw insert succeeds)
        conn.execute("INSERT INTO account (id, name, root_type, account_type, company_id) "
                     "VALUES ('a-unreg', 'Weird', 'asset', 'totally_unregistered', ?)", (cid,))
        conn.commit()
        r = _call("validate_registry_completeness", conn)
        assert r["complete"] is False
        assert "totally_unregistered" in r["unregistered_in_use"].get("account_type", [])
