"""Month-end close automation v1 (floor-o040).

Covers the read-only ``journal-month-end-close-preview`` and the bounded
``journal-run-month-end-close`` over the existing recurring-template and
journal lifecycles, against a throwaway database: preview isolation and
read-only behavior, exact stored money on every returned amount, one
template processed exactly once with a truthful lifecycle, and every
refusal (wrong company, inactive, future-due, duplicate, unknown, missing
or closed fiscal year, existing draft) leaving no partial writes.

Money discipline: Decimal in Python, TEXT columns, exact comparisons.
Never float.
"""
import json
import uuid
from decimal import Decimal

import pytest
from journals_helpers import (
    build_journals_env, call_action, is_error, is_ok, load_db_query, ns,
)

mod = load_db_query()

MONTH_END = "2026-03-31"

_SNAPSHOT_TABLES = ("company", "account", "cost_center", "fiscal_year",
                    "journal_entry", "journal_entry_line",
                    "recurring_journal_template", "gl_entry",
                    "naming_series", "audit_log", "billing_run",
                    "billing_run_target")


def _state(conn):
    out = {}
    for table in _SNAPSHOT_TABLES:
        rows = conn.execute("SELECT * FROM %s" % table).fetchall()
        out[table] = sorted(
            json.dumps({k: (None if v is None else str(v))
                        for k, v in dict(row).items()}, sort_keys=True)
            for row in rows)
    return out


def _lines(env, amount="100.00"):
    return json.dumps([
        {"account_id": env["cash"], "debit": amount, "credit": "0",
         "cost_center_id": env["cc"]},
        {"account_id": env["expense"], "debit": "0", "credit": amount,
         "cost_center_id": env["cc"]},
    ])


def _mk_template(conn, env, start="2026-03-01", end=None, auto_submit=None,
                 amount="100.00", name=None):
    result = call_action(mod.add_recurring_template, conn, ns(
        company_id=env["company_id"],
        template_name=name or "T-%s" % uuid.uuid4().hex[:6],
        frequency="monthly", start_date=start, end_date=end,
        entry_type="journal", auto_submit=auto_submit,
        lines=_lines(env, amount), remark=None,
    ))
    assert is_ok(result), result
    return result["template_id"]


def _mk_journal(conn, env, posting_date="2026-03-10", amount="250.75"):
    result = call_action(mod.add_journal_entry, conn, ns(
        company_id=env["company_id"], posting_date=posting_date,
        entry_type="journal", remark="close probe",
        lines=_lines(env, amount),
    ))
    assert is_ok(result), result
    return result["journal_entry_id"]


def _preview(conn, company_id=None, date=MONTH_END, **kw):
    args = {"company_id": company_id, "as_of_date": date}
    args.update(kw)
    return call_action(mod.journal_month_end_close_preview, conn, ns(**args))


def _run(conn, company_id, template_ids, date=MONTH_END, **kw):
    args = {"company_id": company_id, "as_of_date": date,
            "template_ids": json.dumps(template_ids)}
    args.update(kw)
    return call_action(mod.journal_run_month_end_close, conn, ns(**args))


def _je_count(conn):
    return conn.execute("SELECT COUNT(*) AS c FROM journal_entry").fetchone()["c"]


def _gl_count(conn):
    return conn.execute("SELECT COUNT(*) AS c FROM gl_entry").fetchone()["c"]


def _next_run(conn, template_id):
    return conn.execute(
        "SELECT next_run_date AS d FROM recurring_journal_template WHERE id = ?",
        (template_id,)).fetchone()["d"]


def _close_fy(conn, company_id, closed):
    conn.execute("UPDATE fiscal_year SET is_closed = ? WHERE company_id = ?",
                 (1 if closed else 0, company_id))
    conn.commit()


def _drop_fy(conn, company_id):
    conn.execute("DELETE FROM fiscal_year WHERE company_id = ?",
                 (company_id,))
    conn.commit()


def _assert_money(value, expected):
    assert isinstance(value, str), value
    assert Decimal(value) == Decimal(expected), (value, expected)


class TestPreviewClean:
    def test_clean_company_can_close_and_writes_nothing(self, conn, env):
        before = _state(conn)
        first = _preview(conn, env["company_id"])
        assert is_ok(first), first
        assert first["company_id"] == env["company_id"]
        assert first["month_end_date"] == MONTH_END
        assert first["can_close"] is True
        assert first["draft_journal_count"] == 0
        assert first["draft_journal_entries"] == []
        assert first["due_template_count"] == 0
        assert first["due_recurring_templates"] == []
        assert first["fiscal_year_state"] == "open"
        assert first["fiscal_year"]["fiscal_year_id"] == env["fiscal_year_id"]
        assert first["fiscal_year"]["is_closed"] is False
        assert first["blockers"] == []
        second = _preview(conn, env["company_id"])
        assert is_ok(second), second
        assert first == second
        assert _state(conn) == before

    def test_preview_requires_company_and_date(self, conn, env):
        before = _state(conn)
        missing_company = _preview(conn, None)
        assert missing_company == {
            "status": "error", "message": "--company-id is required"}
        unknown = _preview(conn, "bogus-company")
        assert unknown == {
            "status": "error", "message": "Company not found: bogus-company"}
        bad_date = _preview(conn, env["company_id"], date="not-a-date")
        assert bad_date == {
            "status": "error",
            "message": "Invalid --as-of-date 'not-a-date': expected YYYY-MM-DD"}
        bad_month = _preview(conn, env["company_id"], date="2026-13-40")
        assert bad_month == {
            "status": "error",
            "message": "Invalid --as-of-date '2026-13-40': expected YYYY-MM-DD"}
        missing_date = _preview(conn, env["company_id"], date=None)
        assert missing_date == {
            "status": "error", "message": "--as-of-date is required"}
        for result in (missing_company, unknown, bad_date, bad_month,
                       missing_date):
            assert is_error(result)
        assert _state(conn) == before

    def test_month_end_date_alias(self, conn, env):
        via_alias = call_action(mod.journal_month_end_close_preview, conn, ns(
            company_id=env["company_id"], month_end_date=MONTH_END))
        assert is_ok(via_alias), via_alias
        assert via_alias == _preview(conn, env["company_id"])
        conflict = call_action(mod.journal_month_end_close_preview, conn, ns(
            company_id=env["company_id"], as_of_date=MONTH_END,
            month_end_date="2026-04-30"))
        assert is_error(conflict)
        assert "differ" in conflict["message"]


class TestPreviewBlockers:
    def test_draft_entries_block_with_exact_amounts(self, conn, env):
        first = _mk_journal(conn, env, posting_date="2026-03-10",
                            amount="250.75")
        second = _mk_journal(conn, env, posting_date="2026-03-01",
                             amount="19.99")
        future = _mk_journal(conn, env, posting_date="2026-04-01",
                             amount="7.50")
        result = _preview(conn, env["company_id"])
        assert is_ok(result), result
        assert result["can_close"] is False
        assert result["draft_journal_count"] == 2
        entries = result["draft_journal_entries"]
        by_id = {e["journal_entry_id"]: e for e in entries}
        assert set(by_id) == {first, second}
        assert future not in by_id
        _assert_money(by_id[first]["total_debit"], "250.75")
        _assert_money(by_id[first]["total_credit"], "250.75")
        _assert_money(by_id[second]["total_debit"], "19.99")
        _assert_money(by_id[second]["total_credit"], "19.99")
        assert by_id[first]["posting_date"] == "2026-03-10"
        assert by_id[second]["posting_date"] == "2026-03-01"
        dates = [e["posting_date"] for e in entries]
        assert dates == sorted(dates)
        assert [b["code"] for b in result["blockers"]] == [
            "draft_journal_entries"]
        blocker = result["blockers"][0]
        assert blocker["count"] == 2
        assert blocker["journal_entry_ids"] == [
            e["journal_entry_id"] for e in entries]

    def test_due_templates_block_and_scope_to_company(self, conn, env):
        other = build_journals_env(conn)
        due = _mk_template(conn, env, amount="100.00")
        paused = _mk_template(conn, env, start="2026-03-01")
        call_action(mod.update_recurring_template, conn, ns(
            template_id=paused, template_name=None, frequency=None,
            end_date=None, entry_type=None, remark=None, auto_submit=None,
            lines=None, template_status="paused"))
        future = _mk_template(conn, env, start="2026-05-01")
        foreign = _mk_template(conn, other, start="2026-03-01")
        before = _state(conn)
        result = _preview(conn, env["company_id"])
        assert is_ok(result), result
        assert result["can_close"] is False
        assert result["due_template_count"] == 1
        listed = result["due_recurring_templates"]
        assert [t["template_id"] for t in listed] == [due]
        assert listed[0]["next_run_date"] == "2026-03-01"
        assert paused not in [t["template_id"] for t in listed]
        assert future not in [t["template_id"] for t in listed]
        assert foreign not in [t["template_id"] for t in listed]
        assert [b["code"] for b in result["blockers"]] == [
            "due_recurring_templates"]
        blocker = result["blockers"][0]
        assert blocker["count"] == 1
        assert blocker["template_ids"] == [due]
        far = _preview(conn, other["company_id"])
        assert is_ok(far), far
        assert [t["template_id"] for t in far["due_recurring_templates"]] == [
            foreign]
        assert _state(conn) == before

    def test_missing_fiscal_year_blocks(self, conn, env):
        _drop_fy(conn, env["company_id"])
        result = _preview(conn, env["company_id"])
        assert is_ok(result), result
        assert result["can_close"] is False
        assert result["fiscal_year_state"] == "missing"
        assert result["fiscal_year"] is None
        assert result["blockers"] == [{"code": "fiscal_year_missing"}]

    def test_closed_fiscal_year_blocks(self, conn, env):
        _close_fy(conn, env["company_id"], True)
        result = _preview(conn, env["company_id"])
        assert is_ok(result), result
        assert result["can_close"] is False
        assert result["fiscal_year_state"] == "closed"
        assert result["fiscal_year"]["fiscal_year_id"] == env["fiscal_year_id"]
        assert result["fiscal_year"]["is_closed"] is True
        assert result["blockers"] == [{
            "code": "fiscal_year_closed",
            "fiscal_year_id": env["fiscal_year_id"],
            "fiscal_year_name": result["fiscal_year"]["name"],
        }]

    def test_blocker_order_is_stable(self, conn, env):
        _mk_journal(conn, env, amount="42.50")
        due = _mk_template(conn, env)
        _drop_fy(conn, env["company_id"])
        result = _preview(conn, env["company_id"])
        assert is_ok(result), result
        assert [b["code"] for b in result["blockers"]] == [
            "draft_journal_entries", "due_recurring_templates",
            "fiscal_year_missing"]
        assert result["blockers"][0]["count"] == 1
        assert result["blockers"][1]["template_ids"] == [due]
        again = _preview(conn, env["company_id"])
        assert again == result


class TestPreviewIsolation:
    def test_other_company_dirt_stays_out_and_reads_change_nothing(
            self, conn, env):
        other = build_journals_env(conn)
        foreign_draft = _mk_journal(conn, other, amount="88.88")
        foreign_template = _mk_template(conn, other)
        before = _state(conn)
        near = _preview(conn, env["company_id"])
        assert is_ok(near), near
        assert near["can_close"] is True
        assert near["draft_journal_entries"] == []
        assert near["due_recurring_templates"] == []
        far = _preview(conn, other["company_id"])
        assert is_ok(far), far
        assert far["can_close"] is False
        assert [e["journal_entry_id"]
                for e in far["draft_journal_entries"]] == [foreign_draft]
        _assert_money(far["draft_journal_entries"][0]["total_debit"], "88.88")
        assert [t["template_id"]
                for t in far["due_recurring_templates"]] == [foreign_template]
        assert _state(conn) == before


class TestRunHappyPath:
    def test_one_template_processed_exactly_once(self, conn, env):
        first = _mk_template(conn, env, amount="100.00")
        second = _mk_template(conn, env, start="2026-03-01", amount="55.55")
        audits_before = len(conn.execute("SELECT id FROM audit_log").fetchall())
        result = _run(conn, env["company_id"], [first])
        assert is_ok(result), result
        assert result["company_id"] == env["company_id"]
        assert result["month_end_date"] == MONTH_END
        assert result["processed"] == 1
        assert len(result["created_journal_ids"]) == 1
        created = result["created_journals"][0]
        assert created["journal_entry_id"] == result["created_journal_ids"][0]
        assert created["template_id"] == first
        assert created["je_status"] == "draft"
        assert created["posting_date"] == "2026-03-01"
        _assert_money(created["total_debit"], "100.00")
        _assert_money(created["total_credit"], "100.00")
        assert result["state"] == "incomplete"
        fresh = _preview(conn, env["company_id"])
        assert is_ok(fresh), fresh
        del fresh["status"]
        assert result["preview"] == fresh
        assert [e["journal_entry_id"]
                for e in result["preview"]["draft_journal_entries"]] == [
                    created["journal_entry_id"]]

        stored = conn.execute(
            "SELECT * FROM journal_entry WHERE id = ?",
            (created["journal_entry_id"],)).fetchone()
        assert stored["status"] == created["je_status"] == "draft"
        assert Decimal(str(stored["total_debit"])) == Decimal("100.00")
        assert Decimal(str(stored["total_credit"])) == Decimal("100.00")
        assert stored["company_id"] == env["company_id"]
        assert _next_run(conn, first) == "2026-04-01"
        assert _next_run(conn, second) == "2026-03-01"
        assert _je_count(conn) == 1
        audits_after = conn.execute("SELECT * FROM audit_log").fetchall()
        assert len(audits_after) == audits_before + 1
        assert audits_after[-1]["action"] == "journal-run-month-end-close"

        rerun = _run(conn, env["company_id"], [first])
        assert is_error(rerun)
        assert created["journal_entry_id"] in rerun["message"]
        assert _je_count(conn) == 1
        assert _next_run(conn, first) == "2026-04-01"

    def test_named_subset_leaves_other_templates_alone(self, conn, env):
        first = _mk_template(conn, env, start="2026-03-01", amount="10.00")
        second = _mk_template(conn, env, start="2026-03-01", amount="20.00")
        result = _run(conn, env["company_id"], [second, first])
        assert is_ok(result), result
        assert result["processed"] == 2
        assert result["created_journals"][0]["template_id"] == second
        assert result["created_journals"][1]["template_id"] == first
        _assert_money(result["created_journals"][0]["total_debit"], "20.00")
        _assert_money(result["created_journals"][1]["total_debit"], "10.00")
        assert _je_count(conn) == 2
        assert _next_run(conn, first) == "2026-04-01"
        assert _next_run(conn, second) == "2026-04-01"

    def test_auto_submit_run_reaches_complete(self, conn, env):
        tid = _mk_template(conn, env, auto_submit=True, amount="150.25")
        gl_before = _gl_count(conn)
        result = _run(conn, env["company_id"], [tid])
        assert is_ok(result), result
        assert result["processed"] == 1
        created = result["created_journals"][0]
        assert created["je_status"] == "submitted"
        _assert_money(created["total_debit"], "150.25")
        _assert_money(created["total_credit"], "150.25")
        stored = conn.execute(
            "SELECT * FROM journal_entry WHERE id = ?",
            (created["journal_entry_id"],)).fetchone()
        assert stored["status"] == "submitted"
        assert Decimal(str(stored["total_debit"])) == Decimal("150.25")
        assert _gl_count(conn) == gl_before + 2
        assert result["state"] == "complete"
        assert result["preview"]["can_close"] is True
        assert result["preview"]["blockers"] == []


class TestRunRefusals:
    def _clean_run_state(self, conn):
        return (_je_count(conn), _gl_count(conn))

    def test_wrong_company_template_refuses(self, conn, env):
        other = build_journals_env(conn)
        foreign = _mk_template(conn, other)
        before_state = _state(conn)
        journals_before, gl_before = self._clean_run_state(conn)
        result = _run(conn, env["company_id"], [foreign])
        assert is_error(result)
        assert "another company" in result["message"]
        assert _je_count(conn) == journals_before
        assert _gl_count(conn) == gl_before
        assert _next_run(conn, foreign) == "2026-03-01"
        keep = {k: v for k, v in _state(conn).items() if k != "audit_log"}
        want = {k: v for k, v in before_state.items() if k != "audit_log"}
        assert keep == want

    def test_paused_and_completed_templates_refuse(self, conn, env):
        paused = _mk_template(conn, env)
        call_action(mod.update_recurring_template, conn, ns(
            template_id=paused, template_name=None, frequency=None,
            end_date=None, entry_type=None, remark=None, auto_submit=None,
            lines=None, template_status="paused"))
        done = _mk_template(conn, env)
        conn.execute(
            "UPDATE recurring_journal_template SET status = 'completed' "
            "WHERE id = ?", (done,))
        conn.commit()
        for tid, status in ((paused, "paused"), (done, "completed")):
            journals_before, gl_before = self._clean_run_state(conn)
            result = _run(conn, env["company_id"], [tid])
            assert is_error(result), (tid, result)
            assert status in result["message"]
            assert _je_count(conn) == journals_before
            assert _gl_count(conn) == gl_before
            assert _next_run(conn, tid) == "2026-03-01"

    def test_future_due_template_refuses(self, conn, env):
        tid = _mk_template(conn, env, start="2026-05-01")
        journals_before, gl_before = self._clean_run_state(conn)
        result = _run(conn, env["company_id"], [tid])
        assert is_error(result)
        assert "not due" in result["message"]
        assert _je_count(conn) == journals_before
        assert _gl_count(conn) == gl_before
        assert _next_run(conn, tid) == "2026-05-01"

    def test_duplicate_unknown_and_bad_lists_refuse(self, conn, env):
        tid = _mk_template(conn, env)
        journals_before, gl_before = self._clean_run_state(conn)
        duplicate = _run(conn, env["company_id"], [tid, tid])
        assert is_error(duplicate)
        assert "Duplicate" in duplicate["message"]
        unknown = _run(conn, env["company_id"], ["no-such-template"])
        assert is_error(unknown)
        assert "not found" in unknown["message"]
        empty = _run(conn, env["company_id"], [])
        assert is_error(empty)
        assert "non-empty" in empty["message"]
        missing = call_action(mod.journal_run_month_end_close, conn, ns(
            company_id=env["company_id"], as_of_date=MONTH_END))
        assert is_error(missing)
        assert "--template-ids is required" in missing["message"]
        broken = call_action(mod.journal_run_month_end_close, conn, ns(
            company_id=env["company_id"], as_of_date=MONTH_END,
            template_ids="not-json"))
        assert is_error(broken)
        assert "valid JSON" in broken["message"]
        assert _je_count(conn) == journals_before
        assert _gl_count(conn) == gl_before
        assert _next_run(conn, tid) == "2026-03-01"

    def test_missing_fiscal_year_refuses(self, conn, env):
        tid = _mk_template(conn, env)
        _drop_fy(conn, env["company_id"])
        journals_before, gl_before = self._clean_run_state(conn)
        result = _run(conn, env["company_id"], [tid])
        assert is_error(result)
        assert "fiscal year" in result["message"].lower()
        assert _je_count(conn) == journals_before
        assert _gl_count(conn) == gl_before
        assert _next_run(conn, tid) == "2026-03-01"

    def test_closed_fiscal_year_refuses(self, conn, env):
        tid = _mk_template(conn, env)
        _close_fy(conn, env["company_id"], True)
        journals_before, gl_before = self._clean_run_state(conn)
        result = _run(conn, env["company_id"], [tid])
        assert is_error(result)
        assert "closed" in result["message"]
        assert _je_count(conn) == journals_before
        assert _gl_count(conn) == gl_before
        assert _next_run(conn, tid) == "2026-03-01"

    def test_existing_draft_entry_refuses(self, conn, env):
        tid = _mk_template(conn, env)
        draft = _mk_journal(conn, env, amount="33.33")
        journals_before, gl_before = self._clean_run_state(conn)
        result = _run(conn, env["company_id"], [tid])
        assert is_error(result)
        assert draft in result["message"]
        assert _je_count(conn) == journals_before
        assert _gl_count(conn) == gl_before
        assert _next_run(conn, tid) == "2026-03-01"
        stored = conn.execute(
            "SELECT status FROM journal_entry WHERE id = ?",
            (draft,)).fetchone()
        assert stored["status"] == "draft"
