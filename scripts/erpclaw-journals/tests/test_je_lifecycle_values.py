"""Part A — VALUE tests for the journal-entry lifecycle actions (Wave G F21).

`update-journal-entry`, `duplicate-journal-entry`, `amend-journal-entry` and
`create-intercompany-je` all reached `tested: true` in the necessity register on
one assertion each, and it was always the same one:

    assert "Unknown action" not in result.get("error", "")

That is the M38 shape. Each of these four actions rewrites `journal_entry_line`
rows, and `amend-journal-entry` reverses posted `gl_entry` rows, so a routability
assert says nothing about whether the books end up right. The classifier that
found them is `scripts/test_depth_audit.py`; the register row is
`planning/wave_g/F21_TEST_DEPTH_REGISTER_2026-08-11.json`.

Every pin below drives the REAL action against a fresh core DB and asserts exact
Decimals on the rows the action left behind, plus one refusal path per action.
"""
import json
from decimal import Decimal

import pytest
from journals_helpers import (call_action, is_error, is_ok, load_db_query,
                              ns, seed_account, _uuid)

mod = load_db_query()

D = Decimal
DATE = "2026-06-20"


def _msg(result: dict) -> str:
    return result.get("message", "") + result.get("error", "")


def _lines(env, *specs):
    """Cost-center every line so P&L accounts clear GL validation step 6."""
    return json.dumps([
        {"account_id": a, "debit": d, "credit": c, "cost_center_id": env["cc"]}
        for a, d, c in specs])


def _add(conn, env, lines, entry_type="journal", remark="F21"):
    return call_action(mod.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date=DATE, entry_type=entry_type,
        remark=remark, lines=lines, cwip_asset_id=None))


def _submit(conn, je_id):
    return call_action(mod.submit_journal_entry, conn, ns(journal_entry_id=je_id))


def _je_row(conn, je_id):
    return conn.execute(
        "SELECT status, total_debit, total_credit, posting_date, entry_type, "
        "amended_from, naming_series FROM journal_entry WHERE id = ?",
        (je_id,)).fetchone()


def _je_lines(conn, je_id):
    return conn.execute(
        "SELECT account_id, debit, credit FROM journal_entry_line "
        "WHERE journal_entry_id = ? ORDER BY debit DESC", (je_id,)).fetchall()


def _gl_for(conn, voucher_id):
    return conn.execute(
        "SELECT account_id, debit, credit, is_cancelled FROM gl_entry "
        "WHERE voucher_id = ? ORDER BY created_at, debit DESC",
        (voucher_id,)).fetchall()


# ── update-journal-entry ─────────────────────────────────────────────────────

class TestUpdateJournalEntry:
    def test_replacing_lines_rewrites_totals_and_leaves_no_orphans(self, conn, env):
        add = _add(conn, env, _lines(env, (env["expense"], "300.00", "0"),
                                     (env["cash"], "0", "300.00")))
        assert is_ok(add), add
        je_id = add["journal_entry_id"]

        upd = call_action(mod.update_journal_entry, conn, ns(
            journal_entry_id=je_id, posting_date=None, entry_type=None,
            remark=None,
            lines=_lines(env, (env["expense"], "450.00", "0"),
                         (env["cash"], "0", "450.00"))))
        assert is_ok(upd), upd
        assert upd["updated_fields"] == ["lines"]

        row = _je_row(conn, je_id)
        assert D(row["total_debit"]) == D("450.00")
        assert D(row["total_credit"]) == D("450.00")
        assert row["status"] == "draft"

        lines = _je_lines(conn, je_id)
        assert len(lines) == 2, "the old pair must be deleted, not appended to"
        assert D(lines[0]["debit"]) == D("450.00")
        assert D(lines[0]["credit"]) == D("0")
        assert lines[0]["account_id"] == env["expense"]
        assert D(lines[1]["credit"]) == D("450.00")
        assert lines[1]["account_id"] == env["cash"]

        # No line row anywhere in the table belongs to a deleted parent.
        orphans = conn.execute(
            "SELECT COUNT(*) FROM journal_entry_line l "
            "WHERE NOT EXISTS (SELECT 1 FROM journal_entry j WHERE j.id = "
            "l.journal_entry_id)").fetchone()[0]
        assert orphans == 0

    def test_unbalanced_replacement_is_refused_and_writes_nothing(self, conn, env):
        add = _add(conn, env, _lines(env, (env["expense"], "300.00", "0"),
                                     (env["cash"], "0", "300.00")))
        je_id = add["journal_entry_id"]

        bad = call_action(mod.update_journal_entry, conn, ns(
            journal_entry_id=je_id, posting_date=None, entry_type=None,
            remark=None,
            lines=_lines(env, (env["expense"], "500.00", "0"),
                         (env["cash"], "0", "400.00"))))
        assert is_error(bad)
        assert "must equal" in _msg(bad), _msg(bad)

        row = _je_row(conn, je_id)
        assert D(row["total_debit"]) == D("300.00"), "the refused update must not land"
        lines = _je_lines(conn, je_id)
        assert [(D(l["debit"]), D(l["credit"])) for l in lines] == [
            (D("300.00"), D("0")), (D("0"), D("300.00"))]

    def test_submitted_entry_cannot_be_updated(self, conn, env):
        add = _add(conn, env, _lines(env, (env["expense"], "100.00", "0"),
                                     (env["cash"], "0", "100.00")))
        je_id = add["journal_entry_id"]
        assert is_ok(_submit(conn, je_id))

        bad = call_action(mod.update_journal_entry, conn, ns(
            journal_entry_id=je_id, posting_date="2026-07-01", entry_type=None,
            remark=None, lines=None))
        assert is_error(bad)
        assert "draft" in _msg(bad)
        assert _je_row(conn, je_id)["posting_date"] == DATE


# ── duplicate-journal-entry ──────────────────────────────────────────────────

class TestDuplicateJournalEntry:
    def test_duplicate_copies_every_line_and_starts_a_fresh_draft(self, conn, env):
        add = _add(conn, env, _lines(env, (env["expense"], "125.50", "0"),
                                     (env["cash"], "0", "125.50")))
        src_id = add["journal_entry_id"]
        assert is_ok(_submit(conn, src_id))

        dup = call_action(mod.duplicate_journal_entry, conn, ns(
            journal_entry_id=src_id, posting_date="2026-07-15"))
        assert is_ok(dup), dup
        new_id = dup["new_journal_entry_id"]
        assert new_id != src_id

        new = _je_row(conn, new_id)
        assert new["status"] == "draft"
        assert new["posting_date"] == "2026-07-15"
        assert D(new["total_debit"]) == D("125.50")
        assert D(new["total_credit"]) == D("125.50")
        assert new["naming_series"] != _je_row(conn, src_id)["naming_series"]

        assert [(l["account_id"], D(l["debit"]), D(l["credit"]))
                for l in _je_lines(conn, new_id)] == \
               [(l["account_id"], D(l["debit"]), D(l["credit"]))
                for l in _je_lines(conn, src_id)]

        # The source is untouched and the copy posted no GL of its own.
        assert _je_row(conn, src_id)["status"] == "submitted"
        assert len(_gl_for(conn, new_id)) == 0

    def test_duplicating_an_unknown_entry_is_refused(self, conn, env):
        bad = call_action(mod.duplicate_journal_entry, conn, ns(
            journal_entry_id="no-such-je", posting_date=DATE))
        assert is_error(bad)
        assert "not found" in _msg(bad)
        assert conn.execute("SELECT COUNT(*) FROM journal_entry").fetchone()[0] == 0


# ── amend-journal-entry ──────────────────────────────────────────────────────

class TestAmendJournalEntry:
    def test_amend_reverses_the_posted_gl_and_links_the_new_draft(self, conn, env):
        add = _add(conn, env, _lines(env, (env["expense"], "300.00", "0"),
                                     (env["cash"], "0", "300.00")))
        src_id = add["journal_entry_id"]
        assert is_ok(_submit(conn, src_id))
        assert len(_gl_for(conn, src_id)) == 2

        amend = call_action(mod.amend_journal_entry, conn, ns(
            journal_entry_id=src_id, posting_date=None, remark="corrected",
            lines=_lines(env, (env["expense"], "275.00", "0"),
                         (env["cash"], "0", "275.00"))))
        assert is_ok(amend), amend
        new_id = amend["new_journal_entry_id"]

        # The original is amended, and its GL nets to exactly zero: the original
        # pair plus a mirrored reversal pair. Immutable-GL rule: reverse, never edit.
        assert _je_row(conn, src_id)["status"] == "amended"
        gl = _gl_for(conn, src_id)
        assert len(gl) == 4, [dict(g) for g in gl]
        net = sum((D(g["debit"]) - D(g["credit"]) for g in gl), D("0"))
        assert net == D("0")
        assert sum((D(g["debit"]) for g in gl), D("0")) == D("600.00")
        assert sum((D(g["credit"]) for g in gl), D("0")) == D("600.00")

        # The replacement is a draft carrying the new amounts and the back-link,
        # and it has posted nothing yet.
        new = _je_row(conn, new_id)
        assert new["status"] == "draft"
        assert new["amended_from"] == src_id
        assert D(new["total_debit"]) == D("275.00")
        assert new["posting_date"] == DATE, "posting date carries over when not given"
        assert len(_gl_for(conn, new_id)) == 0

    def test_amending_a_draft_is_refused(self, conn, env):
        add = _add(conn, env, _lines(env, (env["expense"], "10.00", "0"),
                                     (env["cash"], "0", "10.00")))
        bad = call_action(mod.amend_journal_entry, conn, ns(
            journal_entry_id=add["journal_entry_id"], posting_date=None,
            remark=None, lines=None))
        assert is_error(bad)
        assert "submitted" in _msg(bad)
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_entry").fetchone()[0] == 1


# ── create-intercompany-je ───────────────────────────────────────────────────

@pytest.fixture
def ic_env(conn, env):
    """The source company gains a revenue account; a second company is created
    with its own cost center, expense account and journal naming series."""
    src_rev = seed_account(conn, env["company_id"], "IC Revenue", "income",
                           "revenue", "4000")
    tgt = _uuid()
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country, "
        "fiscal_year_start_month) VALUES (?, ?, ?, 'USD', 'United States', 1)",
        (tgt, f"Target Co {tgt[:6]}", f"TG{tgt[:4]}"))
    conn.execute(
        "INSERT INTO fiscal_year (id, name, start_date, end_date, company_id) "
        "VALUES (?, ?, '2026-01-01', '2026-12-31', ?)", (_uuid(), f"FY-{tgt[:6]}", tgt))
    conn.execute(
        "INSERT INTO naming_series (id, entity_type, prefix, current_value, "
        "company_id) VALUES (?, 'journal_entry', 'JE-', 0, ?)", (_uuid(), tgt))
    tgt_cc = _uuid()
    conn.execute(
        "INSERT INTO cost_center (id, name, company_id, is_group) "
        "VALUES (?, 'Target CC', ?, 0)", (tgt_cc, tgt))
    conn.commit()
    tgt_exp = seed_account(conn, tgt, "IC Expense", "expense", "expense", "5100")
    return {"source": env["company_id"], "source_revenue": src_rev,
            "target": tgt, "target_expense": tgt_exp}


class TestCreateIntercompanyJe:
    def test_pair_is_balanced_mirrored_and_cross_referenced(self, conn, ic_env):
        res = call_action(mod.create_intercompany_je, conn, ns(
            source_company_id=ic_env["source"], target_company_id=ic_env["target"],
            amount="1250.75", description="Shared services", posting_date=DATE))
        assert is_ok(res), res
        assert res["amount"] == "1250.75"

        src = _je_row(conn, res["source_je_id"])
        tgt = _je_row(conn, res["target_je_id"])
        for row in (src, tgt):
            assert row["status"] == "draft"
            assert row["entry_type"] == "inter_company"
            assert D(row["total_debit"]) == D("1250.75")
            assert D(row["total_debit"]) == D(row["total_credit"])

        # Source: DR Intercompany Receivable / CR Revenue.
        src_lines = _je_lines(conn, res["source_je_id"])
        assert D(src_lines[0]["debit"]) == D("1250.75")
        assert D(src_lines[1]["credit"]) == D("1250.75")
        assert src_lines[1]["account_id"] == ic_env["source_revenue"]
        ic_recv = conn.execute(
            "SELECT root_type, account_type FROM account WHERE id = ?",
            (src_lines[0]["account_id"],)).fetchone()
        assert (ic_recv["root_type"], ic_recv["account_type"]) == ("asset", "receivable")

        # Target: DR Expense / CR Intercompany Payable.
        tgt_lines = _je_lines(conn, res["target_je_id"])
        assert D(tgt_lines[0]["debit"]) == D("1250.75")
        assert tgt_lines[0]["account_id"] == ic_env["target_expense"]
        ic_pay = conn.execute(
            "SELECT root_type, account_type FROM account WHERE id = ?",
            (tgt_lines[1]["account_id"],)).fetchone()
        assert (ic_pay["root_type"], ic_pay["account_type"]) == ("liability", "payable")

        # Each side names the other; neither posts GL until it is submitted.
        src_remark = conn.execute("SELECT remark FROM journal_entry WHERE id = ?",
                                  (res["source_je_id"],)).fetchone()[0]
        assert res["target_naming"] in src_remark
        assert conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0] == 0

    def test_same_company_on_both_sides_is_refused(self, conn, ic_env):
        bad = call_action(mod.create_intercompany_je, conn, ns(
            source_company_id=ic_env["source"], target_company_id=ic_env["source"],
            amount="100.00", description=None, posting_date=DATE))
        assert is_error(bad)
        assert "must be different" in _msg(bad)
        assert conn.execute("SELECT COUNT(*) FROM journal_entry").fetchone()[0] == 0

    def test_non_positive_amount_is_refused(self, conn, ic_env):
        bad = call_action(mod.create_intercompany_je, conn, ns(
            source_company_id=ic_env["source"], target_company_id=ic_env["target"],
            amount="0", description=None, posting_date=DATE))
        assert is_error(bad)
        assert "positive" in _msg(bad)
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_entry_line").fetchone()[0] == 0
