"""Part B - VALUE tests for the journal read/delete actions (m491 depth).

The five actions below each reached `tested: true` on a routability-only
assertion in testing/integration/contract/test_erpclaw_contract.py
(test_delete_journal_entry_exists, test_delete_recurring_template_exists,
test_get_journal_entry_exists, test_get_recurring_template_exists,
test_list_journal_entries_exists):

    assert "Unknown action" not in result.get("error", "")

That proves the action is routed, not what it does. Every pin here drives the
REAL action against a fresh core DB and asserts the exact rows left behind -
Decimal money compared as exact strings, never float, never round - plus one
refusal path per action that must leave the database byte-identical.

Ledger reach, per action (so a later reader does not add an assertion that
cannot hold):
- get-journal-entry: no ledger reach. Reads journal_entry/journal_entry_line
  only; the gl_entry table is snapshotted byte-identical around the call.
- list-journal-entries: no ledger reach. Reads journal_entry (+ a
  journal_entry_line subquery when --account-id is passed); gl_entry is
  snapshotted byte-identical around the calls.
- delete-journal-entry: no ledger reach on the draft path (a draft has no GL
  rows to reverse). The submitted sibling's two balanced, uncancelled GL legs
  are asserted intact to prove the delete did not touch the ledger.
- get-recurring-template: no ledger reach. Reads one
  recurring_journal_template row; gl_entry is snapshotted byte-identical.
- delete-recurring-template: no ledger reach. Deletes one template row; the
  journal_entry/gl_entry tables are asserted untouched.
"""
import json
from decimal import Decimal

from journals_helpers import (call_action, is_error, is_ok, load_db_query,
                              ns)

mod = load_db_query()

D = Decimal
DATE = "2026-06-20"

_SNAPSHOT_TABLES = ("journal_entry", "journal_entry_line",
                    "recurring_journal_template", "gl_entry", "audit_log")


def _msg(result: dict) -> str:
    return result.get("message", "") + result.get("error", "")


def _is_refusal(result: dict) -> bool:
    # err() refusals carry status == "error"; the resolve_company_id refusal
    # inside list-journal-entries carries an "error" key with no status key.
    return result.get("status") == "error" or "error" in result


def _lines(env, *specs):
    """Cost-center every line so P&L accounts clear GL validation step 6."""
    return json.dumps([
        {"account_id": a, "debit": d, "credit": c, "cost_center_id": env["cc"]}
        for a, d, c in specs])


def _add(conn, env, lines, entry_type="journal", remark="B-depth"):
    return call_action(mod.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date=DATE, entry_type=entry_type,
        remark=remark, lines=lines, cwip_asset_id=None))


def _submit(conn, je_id):
    return call_action(mod.submit_journal_entry, conn, ns(journal_entry_id=je_id))


def _snapshot(conn) -> dict:
    """Byte-level picture of every table these actions may touch."""
    snap = {}
    for table in _SNAPSHOT_TABLES:
        rows = conn.execute(
            "SELECT * FROM %s ORDER BY id" % table).fetchall()
        snap[table] = [[None if v is None else str(v)
                        for v in dict(r).values()] for r in rows]
    return snap


def _mk_template(conn, env, name, amount="100.00", frequency="monthly",
                 start="2026-03-01"):
    lines = _lines(env, (env["cash"], amount, "0"),
                   (env["expense"], "0", amount))
    result = call_action(mod.add_recurring_template, conn, ns(
        company_id=env["company_id"], template_name=name, frequency=frequency,
        start_date=start, end_date=None, entry_type="journal",
        auto_submit=None, lines=lines, remark=None))
    assert is_ok(result), result
    return result["template_id"]


def _list(conn, env, **over):
    kw = {"company_id": env["company_id"], "company_name": None,
          "je_status": None, "entry_type": None, "from_date": None,
          "to_date": None, "account_id": None, "limit": "20", "offset": "0"}
    kw.update(over)
    return call_action(mod.list_journal_entries, conn, ns(**kw))


# -- get-journal-entry: stored-row signal ------------------------------------

class TestGetJournalEntry:
    def test_response_matches_stored_header_and_both_lines_exactly(
            self, conn, env):
        add = _add(conn, env, _lines(env, (env["expense"], "300.00", "0"),
                                     (env["cash"], "0", "300.00")),
                   remark="get-pin")
        assert is_ok(add), add
        je_id = add["journal_entry_id"]
        assert is_ok(_submit(conn, je_id)), "pin reads a submitted entry"
        before = _snapshot(conn)

        got = call_action(mod.get_journal_entry, conn,
                          ns(journal_entry_id=je_id))
        assert is_ok(got), got
        assert _snapshot(conn) == before, "a read must write nothing"

        stored = dict(conn.execute(
            "SELECT * FROM journal_entry WHERE id = ?",
            (je_id,)).fetchone())
        assert got["id"] == je_id == stored["id"]
        assert got["naming_series"] == stored["naming_series"]
        assert got["posting_date"] == "2026-06-20" == stored["posting_date"]
        assert got["entry_type"] == "journal" == stored["entry_type"]
        assert got["document_status"] == "submitted" == stored["status"]
        assert got["total_debit"] == "300.00" == stored["total_debit"]
        assert got["total_credit"] == "300.00" == stored["total_credit"]
        assert D(got["total_debit"]) == D("300.00")
        assert D(got["total_credit"]) == D("300.00")
        assert got["remark"] == "get-pin" == stored["remark"]
        assert got["company_id"] == env["company_id"] == stored["company_id"]

        stored_lines = [dict(r) for r in conn.execute(
            "SELECT account_id, debit, credit, cost_center_id "
            "FROM journal_entry_line WHERE journal_entry_id = ? "
            "ORDER BY debit DESC", (je_id,)).fetchall()]
        assert len(got["lines"]) == 2 == len(stored_lines)
        by_acct = {l["account_id"]: l for l in got["lines"]}
        assert D(by_acct[env["expense"]]["debit"]) == D("300.00")
        assert D(by_acct[env["expense"]]["credit"]) == D("0")
        assert D(by_acct[env["cash"]]["credit"]) == D("300.00")
        assert D(by_acct[env["cash"]]["debit"]) == D("0")
        for stored_line in stored_lines:
            echoed = by_acct[stored_line["account_id"]]
            assert D(echoed["debit"]) == D(stored_line["debit"])
            assert D(echoed["credit"]) == D(stored_line["credit"])
            assert echoed["cost_center_id"] == stored_line["cost_center_id"]
        # No ledger reach: the two posted GL legs sit untouched (snapshot
        # equality above already proves byte-identical; count stated plainly).
        assert before["gl_entry"] and len(before["gl_entry"]) == 2

    def test_unknown_id_is_refused_and_writes_nothing(self, conn, env):
        add = _add(conn, env, _lines(env, (env["expense"], "300.00", "0"),
                                     (env["cash"], "0", "300.00")))
        assert is_ok(add), add
        before = _snapshot(conn)

        bogus = "00000000-0000-0000-0000-000000000000"
        bad = call_action(mod.get_journal_entry, conn,
                          ns(journal_entry_id=bogus))
        assert is_error(bad)
        assert "not found" in _msg(bad), _msg(bad)
        assert bogus in _msg(bad), "the refusal must name what is missing"
        assert conn.execute("SELECT COUNT(*) FROM journal_entry WHERE id = ?",
                            (bogus,)).fetchone()[0] == 0
        assert _snapshot(conn) == before


# -- list-journal-entries: stored-row signal ---------------------------------

class TestListJournalEntries:
    def _seed_three(self, conn, env):
        alpha = _add(conn, env, _lines(env, (env["expense"], "300.00", "0"),
                                       (env["cash"], "0", "300.00")),
                     remark="alpha")
        beta = _add(conn, env, _lines(env, (env["expense"], "150.50", "0"),
                                      (env["cash"], "0", "150.50")),
                    remark="beta")
        gamma = _add(conn, env, _lines(env, (env["expense"], "75.25", "0"),
                                       (env["cash"], "0", "75.25")),
                     remark="gamma")
        for r in (alpha, beta, gamma):
            assert is_ok(r), r
        assert is_ok(_submit(conn, beta["journal_entry_id"]))
        return (alpha["journal_entry_id"], beta["journal_entry_id"],
                gamma["journal_entry_id"])

    def test_lists_exact_stored_rows_with_filters_and_pagination(
            self, conn, env):
        alpha, beta, gamma = self._seed_three(conn, env)
        before = _snapshot(conn)

        full = _list(conn, env)
        assert is_ok(full), full
        assert full["total_count"] == 3
        assert full["has_more"] is False
        assert {e["id"] for e in full["entries"]} == {alpha, beta, gamma}
        for entry in full["entries"]:
            stored = dict(conn.execute(
                "SELECT id, naming_series, posting_date, entry_type, status, "
                "total_debit, total_credit, remark FROM journal_entry "
                "WHERE id = ?", (entry["id"],)).fetchone())
            assert entry["id"] == stored["id"]
            assert entry["naming_series"] == stored["naming_series"]
            assert entry["posting_date"] == stored["posting_date"]
            assert entry["entry_type"] == stored["entry_type"]
            assert entry["status"] == stored["status"]
            assert entry["total_debit"] == stored["total_debit"]
            assert entry["total_credit"] == stored["total_credit"]
            assert D(entry["total_debit"]) == D(stored["total_debit"])
            assert entry["remark"] == stored["remark"]
        by_id = {e["id"]: e for e in full["entries"]}
        assert by_id[alpha]["remark"] == "alpha"
        assert by_id[alpha]["status"] == "draft"
        assert by_id[alpha]["total_debit"] == "300.00"
        assert by_id[beta]["status"] == "submitted"
        assert by_id[beta]["total_debit"] == "150.50"
        assert by_id[gamma]["total_credit"] == "75.25"

        drafts = _list(conn, env, je_status="draft")
        assert is_ok(drafts), drafts
        assert drafts["total_count"] == 2
        assert {e["id"] for e in drafts["entries"]} == {alpha, gamma}

        submitted = _list(conn, env, je_status="submitted")
        assert submitted["total_count"] == 1
        assert submitted["entries"][0]["id"] == beta

        typed = _list(conn, env, entry_type="journal")
        assert typed["total_count"] == 3
        assert _list(conn, env, entry_type="opening")["total_count"] == 0

        on_cash = _list(conn, env, account_id=env["cash"])
        assert on_cash["total_count"] == 3
        window = _list(conn, env, from_date="2026-06-01", to_date="2026-06-30")
        assert window["total_count"] == 3
        empty = _list(conn, env, from_date="2026-07-01", to_date="2026-07-31")
        assert empty["total_count"] == 0
        assert empty["entries"] == []

        page1 = _list(conn, env, limit="2", offset="0")
        assert len(page1["entries"]) == 2
        assert page1["total_count"] == 3
        assert page1["has_more"] is True
        page2 = _list(conn, env, limit="2", offset="2")
        assert len(page2["entries"]) == 1
        assert page2["has_more"] is False
        assert ({e["id"] for e in page1["entries"]}
                | {e["id"] for e in page2["entries"]}) == {alpha, beta, gamma}

        assert _snapshot(conn) == before, "a read must write nothing"
        # No ledger reach: list reads journal_entry rows only, so the
        # submitted entry's posted legs are byte-identical (see snapshot).

    def test_unknown_company_name_is_refused_and_writes_nothing(
            self, conn, env):
        self._seed_three(conn, env)
        before = _snapshot(conn)

        bad = call_action(mod.list_journal_entries, conn, ns(
            company_id=None, company_name="No Such Co", je_status=None,
            entry_type=None, from_date=None, to_date=None, account_id=None,
            limit="20", offset="0"))
        assert _is_refusal(bad), bad
        assert "No Such Co" in _msg(bad), _msg(bad)
        real_names = [r[0] for r in conn.execute(
            "SELECT name FROM company").fetchall()]
        for offered in bad.get("available_companies", []):
            assert offered in real_names, "offered names must be real"
        assert _snapshot(conn) == before


# -- delete-journal-entry: stored-row + ledger-untouched signal --------------

class TestDeleteJournalEntry:
    def test_deleting_draft_removes_header_and_lines_and_spares_the_rest(
            self, conn, env):
        doomed = _add(conn, env, _lines(env, (env["expense"], "300.00", "0"),
                                        (env["cash"], "0", "300.00")),
                      remark="doomed")
        kept = _add(conn, env, _lines(env, (env["expense"], "150.50", "0"),
                                      (env["cash"], "0", "150.50")),
                    remark="kept")
        assert is_ok(doomed) and is_ok(kept), (doomed, kept)
        doomed_id, kept_id = doomed["journal_entry_id"], kept["journal_entry_id"]
        assert is_ok(_submit(conn, kept_id)), kept_id
        gl_before = [dict(r) for r in conn.execute(
            "SELECT account_id, debit, credit, is_cancelled FROM gl_entry "
            "WHERE voucher_id = ? ORDER BY debit DESC", (kept_id,)).fetchall()]
        assert len(gl_before) == 2, "the kept entry must have posted"

        gone = call_action(mod.delete_journal_entry, conn,
                           ns(journal_entry_id=doomed_id))
        assert is_ok(gone), gone
        assert gone["deleted"] is True

        assert conn.execute("SELECT COUNT(*) FROM journal_entry WHERE id = ?",
                            (doomed_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM journal_entry_line "
                            "WHERE journal_entry_id = ?",
                            (doomed_id,)).fetchone()[0] == 0
        orphans = conn.execute(
            "SELECT COUNT(*) FROM journal_entry_line l "
            "WHERE NOT EXISTS (SELECT 1 FROM journal_entry j WHERE j.id = "
            "l.journal_entry_id)").fetchone()[0]
        assert orphans == 0

        kept_row = dict(conn.execute(
            "SELECT status, total_debit, total_credit, remark "
            "FROM journal_entry WHERE id = ?", (kept_id,)).fetchone())
        assert kept_row["status"] == "submitted"
        assert D(kept_row["total_debit"]) == D("150.50")
        assert D(kept_row["total_credit"]) == D("150.50")
        assert kept_row["remark"] == "kept"
        kept_lines = [dict(r) for r in conn.execute(
            "SELECT account_id, debit, credit FROM journal_entry_line "
            "WHERE journal_entry_id = ? ORDER BY debit DESC",
            (kept_id,)).fetchall()]
        assert len(kept_lines) == 2
        assert kept_lines[0]["account_id"] == env["expense"]
        assert D(kept_lines[0]["debit"]) == D("150.50")
        assert kept_lines[1]["account_id"] == env["cash"]
        assert D(kept_lines[1]["credit"]) == D("150.50")

        # Ledger untouched: both posted legs still present, uncancelled,
        # and balanced. A draft delete must never reach gl_entry.
        gl_after = [dict(r) for r in conn.execute(
            "SELECT account_id, debit, credit, is_cancelled FROM gl_entry "
            "WHERE voucher_id = ? ORDER BY debit DESC", (kept_id,)).fetchall()]
        assert gl_after == gl_before
        assert all(g["is_cancelled"] in (0, False) for g in gl_after)
        assert sum((D(g["debit"]) for g in gl_after), D("0")) == D("150.50")
        assert sum((D(g["credit"]) for g in gl_after), D("0")) == D("150.50")
        assert conn.execute(
            "SELECT COUNT(*) FROM gl_entry").fetchone()[0] == 2

    def test_deleting_submitted_is_refused_and_changes_nothing(
            self, conn, env):
        add = _add(conn, env, _lines(env, (env["expense"], "150.50", "0"),
                                     (env["cash"], "0", "150.50")))
        assert is_ok(add), add
        je_id = add["journal_entry_id"]
        assert is_ok(_submit(conn, je_id)), je_id
        before = _snapshot(conn)

        bad = call_action(mod.delete_journal_entry, conn,
                          ns(journal_entry_id=je_id))
        assert is_error(bad)
        assert "submitted" in _msg(bad), _msg(bad)
        assert "draft" in _msg(bad), "the refusal must state the real rule"
        row = dict(conn.execute("SELECT status, total_debit FROM journal_entry "
                                "WHERE id = ?", (je_id,)).fetchone())
        assert row["status"] == "submitted"
        assert D(row["total_debit"]) == D("150.50")
        assert _snapshot(conn) == before


# -- get-recurring-template: stored-row signal --------------------------------

class TestGetRecurringTemplate:
    def test_response_matches_stored_template_and_lines_exactly(
            self, conn, env):
        tid = _mk_template(conn, env, "T-get", amount="250.75")
        before = _snapshot(conn)

        got = call_action(mod.get_recurring_template, conn,
                          ns(template_id=tid))
        assert is_ok(got), got
        assert _snapshot(conn) == before, "a read must write nothing"

        stored = dict(conn.execute(
            "SELECT * FROM recurring_journal_template WHERE id = ?",
            (tid,)).fetchone())
        assert got["id"] == tid == stored["id"]
        assert got["name"] == "T-get" == stored["name"]
        assert got["frequency"] == "monthly" == stored["frequency"]
        assert got["start_date"] == "2026-03-01" == stored["start_date"]
        assert got["next_run_date"] == stored["next_run_date"]
        assert got["entry_type"] == "journal" == stored["entry_type"]
        assert got["company_id"] == env["company_id"] == stored["company_id"]
        assert got["document_status"] == "active" == stored["status"]

        stored_lines = json.loads(stored["lines"])
        assert len(got["lines"]) == 2 == len(stored_lines)
        for echoed, raw in zip(
                sorted(got["lines"], key=lambda l: l["credit"]),
                sorted(stored_lines, key=lambda l: l["credit"])):
            assert echoed["account_id"] == raw["account_id"]
            assert D(echoed["debit"]) == D(raw["debit"])
            assert D(echoed["credit"]) == D(raw["credit"])
        amounts = sorted(D(l["debit"]) for l in got["lines"])
        assert amounts == [D("0"), D("250.75")]
        # No ledger reach: fetching a template posts nothing (snapshot
        # equality above proves gl_entry byte-identical).

    def test_unknown_id_is_refused_and_writes_nothing(self, conn, env):
        _mk_template(conn, env, "T-keep")
        before = _snapshot(conn)

        bogus = "00000000-0000-0000-0000-000000000000"
        bad = call_action(mod.get_recurring_template, conn,
                          ns(template_id=bogus))
        assert is_error(bad)
        assert "not found" in _msg(bad), _msg(bad)
        assert bogus in _msg(bad), "the refusal must name what is missing"
        assert conn.execute("SELECT COUNT(*) FROM recurring_journal_template "
                            "WHERE id = ?", (bogus,)).fetchone()[0] == 0
        assert _snapshot(conn) == before


# -- delete-recurring-template: stored-row signal -----------------------------

class TestDeleteRecurringTemplate:
    def test_delete_removes_the_row_and_spares_its_sibling(
            self, conn, env):
        doomed = _mk_template(conn, env, "T-doomed", amount="100.00")
        kept = _mk_template(conn, env, "T-kept", amount="200.00")
        kept_stored = dict(conn.execute(
            "SELECT * FROM recurring_journal_template WHERE id = ?",
            (kept,)).fetchone())
        counts_before = {
            "journal_entry": conn.execute(
                "SELECT COUNT(*) FROM journal_entry").fetchone()[0],
            "gl_entry": conn.execute(
                "SELECT COUNT(*) FROM gl_entry").fetchone()[0]}

        gone = call_action(mod.delete_recurring_template, conn,
                           ns(template_id=doomed))
        assert is_ok(gone), gone
        assert gone["deleted"] is True

        assert conn.execute("SELECT COUNT(*) FROM recurring_journal_template "
                            "WHERE id = ?", (doomed,)).fetchone()[0] == 0
        missing = call_action(mod.get_recurring_template, conn,
                              ns(template_id=doomed))
        assert is_error(missing), "the deleted row must stay gone"

        kept_after = dict(conn.execute(
            "SELECT * FROM recurring_journal_template WHERE id = ?",
            (kept,)).fetchone())
        assert kept_after == kept_stored, "the sibling row must not move"
        sibling_lines = json.loads(kept_after["lines"])
        assert sorted(D(l["debit"]) for l in sibling_lines) == \
            [D("0"), D("200.00")]
        # No ledger reach and no journal side effects from deleting a
        # template: both tables hold exactly what they held before.
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_entry").fetchone()[0] == \
            counts_before["journal_entry"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM gl_entry").fetchone()[0] == \
            counts_before["gl_entry"] == 0

    def test_unknown_id_is_refused_and_writes_nothing(self, conn, env):
        kept = _mk_template(conn, env, "T-kept")
        before = _snapshot(conn)

        bogus = "00000000-0000-0000-0000-000000000000"
        bad = call_action(mod.delete_recurring_template, conn,
                          ns(template_id=bogus))
        assert is_error(bad)
        assert "not found" in _msg(bad), _msg(bad)
        assert bogus in _msg(bad), "the refusal must name what is missing"
        assert conn.execute("SELECT COUNT(*) FROM recurring_journal_template "
                            "WHERE id = ?", (kept,)).fetchone()[0] == 1
        assert _snapshot(conn) == before
