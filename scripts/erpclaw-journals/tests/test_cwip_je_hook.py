"""Tests for the S3 CWIP journal-entry hook (AVA-43).

`--cwip-asset-id` on add-journal-entry tags the JE; on submit, the JE's debit to a
capital_work_in_progress account is recorded as a cwip_cost_accumulation row in the
same transaction (and the manual-JE CWIP guard is lifted for this sanctioned path).
Absent the flag, behaviour is unchanged (regression).
"""
import json
import pytest
from decimal import Decimal
from journals_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, seed_cwip_asset,
)

mod = load_db_query()


def _lines(env, *specs):
    # Tag every line with a cost center so P&L accounts pass GL Step 6.
    return json.dumps([
        {"account_id": a, "debit": d, "credit": c, "cost_center_id": env["cc"]}
        for a, d, c in specs])


def _add_je(conn, env, lines, cwip_asset_id=None, entry_type="journal"):
    return call_action(mod.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date="2026-06-20",
        entry_type=entry_type, remark="CWIP cost", lines=lines,
        cwip_asset_id=cwip_asset_id))


def _submit(conn, je_id):
    return call_action(mod.submit_journal_entry, conn, ns(journal_entry_id=je_id))


class TestCwipJeHook:
    def test_happy_records_accumulation_against_cwip_leg(self, conn, env):
        asset_id = seed_cwip_asset(conn, env["company_id"])
        lines = _lines(env, (env["cwip"], "300.00", "0"), (env["cash"], "0", "300.00"))
        add = _add_je(conn, env, lines, cwip_asset_id=asset_id)
        assert is_ok(add), add
        sub = _submit(conn, add["journal_entry_id"])
        assert is_ok(sub), sub
        assert sub["cwip_asset_id"] == asset_id
        assert sub.get("cwip_accumulation_id")
        accum = conn.execute(
            "SELECT accumulated_amount, source_voucher_type, gl_entry_id "
            "FROM cwip_cost_accumulation WHERE asset_id = ?", (asset_id,)).fetchone()
        assert Decimal(accum["accumulated_amount"]) == Decimal("300.00")
        assert accum["source_voucher_type"] == "journal_entry"
        # gl_entry_id points at the DR CWIP leg.
        leg = conn.execute("SELECT account_id, debit FROM gl_entry WHERE id = ?",
                           (accum["gl_entry_id"],)).fetchone()
        assert leg["account_id"] == env["cwip"]
        assert Decimal(leg["debit"]) == Decimal("300.00")
        book = conn.execute("SELECT current_book_value FROM asset WHERE id = ?",
                            (asset_id,)).fetchone()[0]
        assert Decimal(book) == Decimal("300.00")

    def test_reject_if_asset_not_under_construction(self, conn, env):
        asset_id = seed_cwip_asset(conn, env["company_id"], status="in_use")
        lines = _lines(env, (env["cwip"], "100.00", "0"), (env["cash"], "0", "100.00"))
        add = _add_je(conn, env, lines, cwip_asset_id=asset_id)
        assert is_error(add)
        assert "under_construction" in (add.get("error", "") + add.get("message", ""))

    def test_reject_if_no_cwip_debit_leg(self, conn, env):
        # JE tagged to a CWIP asset but debits a non-CWIP account → rejected at submit.
        asset_id = seed_cwip_asset(conn, env["company_id"])
        lines = _lines(env, (env["expense"], "100.00", "0"), (env["cash"], "0", "100.00"))
        add = _add_je(conn, env, lines, cwip_asset_id=asset_id)
        assert is_ok(add), add
        sub = _submit(conn, add["journal_entry_id"])
        assert is_error(sub)
        assert "capital_work_in_progress" in (sub.get("error", "") + sub.get("message", ""))

    def test_untagged_je_cannot_debit_cwip(self, conn, env):
        # Backward compat: the manual-JE CWIP guard still blocks an untagged JE.
        lines = _lines(env, (env["cwip"], "100.00", "0"), (env["cash"], "0", "100.00"))
        add = _add_je(conn, env, lines, cwip_asset_id=None)
        assert is_ok(add), add
        sub = _submit(conn, add["journal_entry_id"])
        assert is_error(sub)

    def test_cancel_unwinds_accumulation(self, conn, env):
        asset_id = seed_cwip_asset(conn, env["company_id"])
        lines = _lines(env, (env["cwip"], "200.00", "0"), (env["cash"], "0", "200.00"))
        add = _add_je(conn, env, lines, cwip_asset_id=asset_id)
        je_id = add["journal_entry_id"]
        assert is_ok(_submit(conn, je_id))
        cancel = call_action(mod.cancel_journal_entry, conn, ns(journal_entry_id=je_id))
        assert is_ok(cancel), cancel
        status = conn.execute(
            "SELECT status FROM cwip_cost_accumulation WHERE asset_id = ?",
            (asset_id,)).fetchone()[0]
        assert status == "reversed"
        book = conn.execute("SELECT current_book_value FROM asset WHERE id = ?",
                            (asset_id,)).fetchone()[0]
        assert Decimal(book) == Decimal("0")

    def test_backward_compat_plain_je_unchanged(self, conn, env):
        lines = _lines(env, (env["expense"], "50.00", "0"), (env["cash"], "0", "50.00"))
        add = _add_je(conn, env, lines)
        assert is_ok(add)
        sub = _submit(conn, add["journal_entry_id"])
        assert is_ok(sub)
        assert "cwip_accumulation_id" not in sub
        n = conn.execute("SELECT COUNT(*) FROM cwip_cost_accumulation").fetchone()[0]
        assert n == 0
