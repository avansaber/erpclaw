"""Journal entries carry accounting dimensions from draft to ledger (m687).

A journal entry is tagged with accounting dimensions as a whole and per
line. Tags are checked against the dimension registry when the draft is
written, stored on the draft, and copied onto every ledger row the entry
posts, so profit-and-loss --group-by reports ordinary journal entries. A
line's own tag wins per key; untagged keys come from the entry header.

Every test runs on a fresh database from the journals suite's fixtures
(conn, env). `department` (text, active) ships on every fresh install and
is used as-is; `fund` (text, not required) is registered per test.
"""
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from journals_helpers import (  # noqa: E402
    call_action, is_error, is_ok, load_db_query, ns, seed_account, _uuid)

mod = load_db_query()

DATE = "2026-06-20"


def _msg(result):
    return result.get("message", "") + result.get("error", "")


def _jns(**kw):
    base = dict(
        company_id=None, company_name=None, posting_date=None,
        entry_type=None, remark=None, lines=None, journal_entry_id=None,
        dimensions=None, dimension_key=None, dimension_value=None,
        cwip_asset_id=None, source_company_id=None, target_company_id=None,
        amount=None, description=None, template_id=None, template_name=None,
        frequency=None, start_date=None, end_date=None, auto_submit=None,
        template_status=None, amended_from=None, as_of_date=None,
        resume_run_id=None, je_status=None, account_id=None,
        from_date=None, to_date=None, limit="20", offset="0")
    base.update(kw)
    return ns(**base)


def _register_fund(conn, required_on=None):
    conn.execute(
        "INSERT INTO dimension_registry (id, key, label, data_type, "
        "referenced_table, allowed_values_json, "
        "is_required_on_account_types_json, is_active) "
        "VALUES (?, 'fund', 'Fund', 'text', NULL, NULL, ?, 1)",
        (_uuid(), json.dumps(required_on) if required_on is not None else None))
    conn.commit()


def _entry_lines(env, line1_dims="__absent__", line2_dims="__absent__",
                 amount="100.00"):
    first = {"account_id": env["expense"], "debit": amount, "credit": "0",
             "cost_center_id": env["cc"]}
    if line1_dims != "__absent__":
        first["dimensions"] = line1_dims
    second = {"account_id": env["cash"], "debit": "0", "credit": amount}
    if line2_dims != "__absent__":
        second["dimensions"] = line2_dims
    return json.dumps([first, second])


def _add(conn, env, lines, dimensions=None, keys=None, values=None,
         remark="DIM"):
    return call_action(mod.add_journal_entry, conn, _jns(
        company_id=env["company_id"], posting_date=DATE, entry_type="journal",
        remark=remark, lines=lines, cwip_asset_id=None,
        dimensions=dimensions, dimension_key=keys, dimension_value=values))


def _submit(conn, je_id):
    return call_action(mod.submit_journal_entry, conn,
                       _jns(journal_entry_id=je_id))


def _je_dims(conn, je_id):
    header = conn.execute(
        "SELECT dimensions_json FROM journal_entry WHERE id = ?",
        (je_id,)).fetchone()["dimensions_json"]
    rows = conn.execute(
        "SELECT dimensions_json FROM journal_entry_line "
        "WHERE journal_entry_id = ? ORDER BY debit DESC",
        (je_id,)).fetchall()
    return header, [r["dimensions_json"] for r in rows]


def _gl_dims(conn, voucher_id):
    rows = conn.execute(
        "SELECT account_id, debit, credit, dimensions_json FROM gl_entry "
        "WHERE voucher_id = ? ORDER BY debit DESC",
        (voucher_id,)).fetchall()
    return [(r["account_id"], r["debit"], r["credit"], r["dimensions_json"])
            for r in rows]


def _snapshot(conn, company_id):
    counts = (
        conn.execute("SELECT COUNT(*) FROM journal_entry").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM journal_entry_line").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
    series = conn.execute(
        "SELECT entity_type, prefix, current_value FROM naming_series "
        "WHERE company_id = ? ORDER BY entity_type, prefix",
        (company_id,)).fetchall()
    return (counts, [tuple(r) for r in series])


def _load_reports():
    scripts = os.path.dirname(os.path.dirname(_TESTS_DIR))
    rep_tests = os.path.join(scripts, "erpclaw-reports", "tests")
    # The m634 module imports payments_helpers, which lives in the payments
    # suite (added to sys.path by the reports conftest under pytest).
    pay_tests = os.path.join(scripts, "erpclaw-payments", "tests")
    for entry in (rep_tests, pay_tests):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    path = os.path.join(rep_tests, "test_m634_exact_money_arithmetic.py")
    spec = importlib.util.spec_from_file_location(
        "test_m634_exact_money_arithmetic_dims", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 1. tagged entry ──────────────────────────────────────────────────────────

def test_tagged_entry_threads_merged_dimensions(conn, env):
    _register_fund(conn)
    add = _add(conn, env,
               _entry_lines(env, {"department": "Ops", "fund": "F1"}),
               dimensions='{"department": "Engineering"}')
    assert is_ok(add), add
    je_id = add["journal_entry_id"]

    header, stored = _je_dims(conn, je_id)
    assert header == '{"department": "Engineering"}'
    assert stored == ['{"department": "Ops", "fund": "F1"}', '{}']

    sub = _submit(conn, je_id)
    assert is_ok(sub), sub
    rows = _gl_dims(conn, je_id)
    assert len(rows) == 2
    assert rows[0][0] == env["expense"]
    assert rows[0][3] == '{"department": "Ops", "fund": "F1"}'
    assert rows[1][0] == env["cash"]
    assert rows[1][3] == '{"department": "Engineering"}'


# ── 2. untagged entry ────────────────────────────────────────────────────────

def test_untagged_entry_posts_empty_dimensions(conn, env):
    _register_fund(conn)
    add = _add(conn, env, _entry_lines(env))
    assert is_ok(add), add
    je_id = add["journal_entry_id"]

    header, stored = _je_dims(conn, je_id)
    assert header == '{}'
    assert stored == ['{}', '{}']

    sub = _submit(conn, je_id)
    assert is_ok(sub), sub
    rows = _gl_dims(conn, je_id)
    assert len(rows) == 2
    assert [r[3] for r in rows] == ['{}', '{}']


# ── 3. pairs form ────────────────────────────────────────────────────────────

def test_pairs_form_stores_header_object(conn, env):
    _register_fund(conn)
    add = _add(conn, env, _entry_lines(env), keys=["department"],
               values=["Engineering"])
    assert is_ok(add), add
    header, _stored = _je_dims(conn, add["journal_entry_id"])
    assert header == '{"department": "Engineering"}'


# ── 4. refusals ──────────────────────────────────────────────────────────────

def _refuse(conn, env, before, lines, dimensions=None, keys=None, values=None):
    bad = _add(conn, env, lines, dimensions=dimensions, keys=keys,
               values=values)
    assert is_error(bad), bad
    after = _snapshot(conn, env["company_id"])
    assert after == before, (before, after)
    return _msg(bad)


def test_dimension_refusals_write_nothing(conn, env):
    _register_fund(conn)
    plain = _entry_lines(env)

    # Control (ok-with-no-tags): an untagged entry is accepted, before and
    # after the change.
    ok_add = _add(conn, env, plain)
    assert is_ok(ok_add), ok_add
    header, _stored = _je_dims(conn, ok_add["journal_entry_id"])
    assert header == '{}'

    before = _snapshot(conn, env["company_id"])
    text = _refuse(conn, env, before, plain,
                   dimensions='{"nosuchdim": "x"}')
    assert text == ("Unknown or inactive dimension 'nosuchdim'; "
                    "run list-dimensions"), text

    before = _snapshot(conn, env["company_id"])
    text = _refuse(conn, env, before, plain, keys=["department"],
                   values=["Engineering", "Extra"])
    assert text == ("--dimension-key and --dimension-value must be given "
                    "in pairs"), text

    before = _snapshot(conn, env["company_id"])
    text = _refuse(conn, env, before, plain,
                   dimensions='{"department": "Engineering"}',
                   keys=["department"], values=["Ops"])
    assert text == ("Dimension 'department' given twice with "
                    "different values"), text

    before = _snapshot(conn, env["company_id"])
    text = _refuse(conn, env, before, _entry_lines(env, "oops"))
    assert text == "Line 1: dimensions must be a JSON object", text

    before = _snapshot(conn, env["company_id"])
    text = _refuse(conn, env, before, _entry_lines(env, None))
    assert text == "Line 1: dimensions must be a JSON object", text

    conn.execute("UPDATE dimension_registry SET "
                 "is_required_on_account_types_json = ? WHERE key = 'fund'",
                 (json.dumps(["expense"]),))
    conn.commit()
    expense_name = conn.execute(
        "SELECT name FROM account WHERE id = ?",
        (env["expense"],)).fetchone()["name"]
    before = _snapshot(conn, env["company_id"])
    text = _refuse(conn, env, before, plain)
    assert text == ("Dimension 'fund' is required for account '%s' "
                    "(account_type 'expense')" % expense_name), text
    conn.execute("UPDATE dimension_registry SET "
                 "is_required_on_account_types_json = NULL WHERE key = 'fund'")
    conn.commit()


# ── 5. cancel nets to zero per dimension value ───────────────────────────────

def test_cancel_nets_to_zero_per_dimension_value(conn, env):
    _register_fund(conn)
    add = _add(conn, env,
               _entry_lines(env, {"department": "Ops", "fund": "F1"}),
               dimensions='{"department": "Engineering"}')
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    assert is_ok(_submit(conn, je_id)), je_id
    cancelled = call_action(mod.cancel_journal_entry, conn,
                            _jns(journal_entry_id=je_id))
    assert is_ok(cancelled), cancelled

    rows = conn.execute(
        "SELECT account_id, debit, credit, dimensions_json FROM gl_entry "
        "WHERE voucher_id = ?", (je_id,)).fetchall()
    assert len(rows) == 4
    nets = {}
    for row in rows:
        key = (row["account_id"], row["dimensions_json"])
        nets[key] = (nets.get(key, Decimal("0")) + Decimal(row["debit"])
                     - Decimal(row["credit"]))
    assert nets, "cancellation must leave reversal rows behind"
    for key, net in nets.items():
        assert net == Decimal("0.00"), (key, net)


# ── 6. reports ───────────────────────────────────────────────────────────────

def test_reports_group_journal_entries_by_department(conn, env):
    _register_fund(conn)
    first = _add(conn, env,
                 _entry_lines(env, {"department": "Ops", "fund": "F1"}),
                 dimensions='{"department": "Engineering"}')
    assert is_ok(first), first
    assert is_ok(_submit(conn, first["journal_entry_id"]))
    second = _add(conn, env, _entry_lines(env, amount="50.00"))
    assert is_ok(second), second
    assert is_ok(_submit(conn, second["journal_entry_id"]))

    m634 = _load_reports()
    pnl = call_action(
        m634.REP.profit_and_loss, conn,
        m634._base_ns(company_id=env["company_id"],
                       from_date="2026-01-01", to_date="2026-12-31",
                       group_by="department"))
    assert is_ok(pnl), pnl
    by_dept = {g["department"]: g for g in pnl["groups"]}
    assert by_dept["Ops"]["expenses"] == "100.00", pnl["groups"]
    assert by_dept["Ops"]["revenue"] == "0.00", pnl["groups"]
    assert by_dept["(untagged)"]["expenses"] == "50.00", pnl["groups"]

    trial = call_action(
        m634.REP.multi_dim_trial_balance, conn,
        m634._base_ns(company_id=env["company_id"],
                       to_date="2026-12-31", group_by="department"))
    assert is_ok(trial), trial
    assert any(g["department"] is None for g in trial["groups"]), trial


# ── 7. update ────────────────────────────────────────────────────────────────

def test_update_replaces_and_clears_header_object(conn, env):
    _register_fund(conn)
    add = _add(conn, env, _entry_lines(env))
    assert is_ok(add), add
    je_id = add["journal_entry_id"]

    upd = call_action(mod.update_journal_entry, conn, _jns(
        journal_entry_id=je_id, dimensions='{"department": "Sales"}'))
    assert is_ok(upd), upd
    header, _stored = _je_dims(conn, je_id)
    assert header == '{"department": "Sales"}'
    audit_row = conn.execute(
        "SELECT old_values, new_values FROM audit_log "
        "WHERE entity_type = 'journal_entry' AND entity_id = ? "
        "AND action = 'update-journal-entry' "
        "ORDER BY rowid DESC LIMIT 1", (je_id,)).fetchone()
    assert json.loads(audit_row["old_values"])["dimensions_json"] == '{}'
    assert (json.loads(audit_row["new_values"])["dimensions_json"]
            == '{"department": "Sales"}')

    cleared = call_action(mod.update_journal_entry, conn, _jns(
        journal_entry_id=je_id, dimensions='{}'))
    assert is_ok(cleared), cleared
    header, _stored = _je_dims(conn, je_id)
    assert header == '{}'


# ── 8. amend and duplicate ───────────────────────────────────────────────────

def test_amend_and_duplicate_keep_header_and_line_objects(conn, env):
    _register_fund(conn)
    add = _add(conn, env,
               _entry_lines(env, {"department": "Ops", "fund": "F1"}),
               dimensions='{"department": "Engineering"}')
    assert is_ok(add), add
    je_id = add["journal_entry_id"]
    assert is_ok(_submit(conn, je_id))
    want_header, want_lines = _je_dims(conn, je_id)

    dup = call_action(mod.duplicate_journal_entry, conn,
                      _jns(journal_entry_id=je_id))
    assert is_ok(dup), dup
    assert _je_dims(conn, dup["new_journal_entry_id"]) == (
        want_header, want_lines)

    amended = call_action(mod.amend_journal_entry, conn,
                          _jns(journal_entry_id=je_id))
    assert is_ok(amended), amended
    new_id = amended["new_journal_entry_id"]
    assert _je_dims(conn, new_id) == (want_header, want_lines)

    assert is_ok(_submit(conn, new_id))
    amended2 = call_action(mod.amend_journal_entry, conn, _jns(
        journal_entry_id=new_id, dimensions='{"department": "Sales"}'))
    assert is_ok(amended2), amended2
    new2_id = amended2["new_journal_entry_id"]
    header2, lines2 = _je_dims(conn, new2_id)
    assert header2 == '{"department": "Sales"}'
    assert lines2 == want_lines


# ── 9. recurring ─────────────────────────────────────────────────────────────

def test_recurring_template_carries_dimensions(conn, env):
    _register_fund(conn)
    tmpl = call_action(mod.add_recurring_template, conn, _jns(
        company_id=env["company_id"], template_name="T9", frequency="monthly",
        start_date=DATE, entry_type="journal", auto_submit=True,
        remark="T9", dimensions='{"department": "Engineering"}',
        lines=_entry_lines(env, {"department": "Ops"})))
    assert is_ok(tmpl), tmpl
    template_id = tmpl["template_id"]

    want_header = conn.execute(
        "SELECT dimensions_json FROM recurring_journal_template WHERE id = ?",
        (template_id,)).fetchone()["dimensions_json"]
    assert want_header == '{"department": "Engineering"}'
    want_lines = json.loads(conn.execute(
        "SELECT lines FROM recurring_journal_template WHERE id = ?",
        (template_id,)).fetchone()["lines"])
    assert want_lines[0]["dimensions"] == {"department": "Ops"}

    run = call_action(mod.process_recurring, conn, _jns(
        company_id=env["company_id"], as_of_date=DATE))
    assert is_ok(run), run
    assert run["generated"] == 1, run
    entry = run["results"][0]
    assert entry["je_status"] == "submitted", entry
    je_id = entry["journal_entry_id"]

    header, stored = _je_dims(conn, je_id)
    assert header == want_header
    assert [json.loads(text) for text in stored] == [
        line.get("dimensions", {}) for line in want_lines]

    rows = _gl_dims(conn, je_id)
    assert len(rows) == 2
    assert rows[0][3] == '{"department": "Ops"}'
    assert rows[1][3] == '{"department": "Engineering"}'


# ── 10. intercompany ─────────────────────────────────────────────────────────

def test_intercompany_pair_shares_header_object(conn, env):
    _register_fund(conn)
    seed_account(conn, env["company_id"], "Service Revenue",
                 "income", "revenue", "4000")
    peer_id = _uuid()
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country, "
        "fiscal_year_start_month) VALUES (?, ?, ?, 'USD', 'United States', 1)",
        (peer_id, "Peer Co %s" % peer_id[:6], "PC%s" % peer_id[:4]))
    conn.commit()
    peer_expense = seed_account(conn, peer_id, "Peer Purchases",
                                "expense", "expense", "5000")

    made = call_action(mod.create_intercompany_je, conn, _jns(
        source_company_id=env["company_id"], target_company_id=peer_id,
        amount="250.00", posting_date=DATE, description="IC10",
        dimensions='{"department": "Engineering"}'))
    assert is_ok(made), made
    for je_id in (made["source_je_id"], made["target_je_id"]):
        header = conn.execute(
            "SELECT dimensions_json FROM journal_entry WHERE id = ?",
            (je_id,)).fetchone()["dimensions_json"]
        assert header == '{"department": "Engineering"}', je_id
    assert peer_expense is not None
