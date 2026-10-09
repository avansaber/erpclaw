"""Explicit-rate translation of an entity's posted functional-currency books."""
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from advacct_helpers import _ConnWrapper, build_advacct_env, call_action, load_db_query, ns, seed_recognition_ledger
from erpclaw_lib.db import get_connection
from erpclaw_lib.gl_posting import insert_gl_entries, reverse_gl_entries
from erpclaw_lib.query import P, Q, Table
from erpclaw_lib.seam import table_names

mod = load_db_query()


def insert(conn, name, values):
    table = Table(name)
    conn.execute(Q.into(table).columns(*values).insert(*[P() for _ in values]).get_sql(), tuple(values.values()))


@pytest.fixture
def books(db_path):
    conn = _ConnWrapper(get_connection(db_path))
    env = build_advacct_env(conn)
    child = env["company2_id"]
    env.update(seed_recognition_ledger(conn, child))
    company, account = Table("company"), Table("account")
    conn.execute(Q.update(company).set(company.default_currency, P()).where(company.id == P()).get_sql(), ("EUR", child))
    conn.execute(Q.update(account).set(account.currency, P()).where(account.company_id == P()).get_sql(), ("EUR", child))
    for name, root in (("cash", "asset"), ("capital", "equity"), ("retained", "equity"),
                       ("debt", "liability"), ("income", "income"), ("expense", "expense")):
        env[name] = str(uuid4())
        insert(conn, "account", {"id": env[name], "name": name, "root_type": root,
            "account_type": "temporary", "currency": "EUR", "is_group": 0, "company_id": child,
            "balance_direction": "debit_normal" if root in ("asset", "expense") else "credit_normal"})
    group = call_action(mod.add_consolidation_group, conn, ns(company_id=env["company_id"],
        name="Reviewed group", parent_company_id=env["company_id"], consolidation_currency="USD"))
    env["group_id"] = group["id"]
    member = call_action(mod.add_group_entity, conn, ns(company_id=env["company_id"], group_id=group["id"],
        entity_company_id=child, entity_name="Foreign entity", ownership_pct="80", functional_currency="EUR", consolidation_method="full"))
    env["member_id"] = member["id"]
    conn.commit()
    post(conn, env, [("cash", "500.00", "0.00"), ("expense", "100.00", "0.00"),
                    ("capital", "0.00", "100.00"), ("retained", "0.00", "100.00"),
                    ("debt", "0.00", "100.00"), ("income", "0.00", "300.00")])
    yield conn, env
    conn.close()


def post(conn, env, amounts, day="2026-06-30", rate="1"):
    voucher = str(uuid4())
    rows = [{"account_id": env[name], "debit": debit, "credit": credit,
             "currency": "EUR", "exchange_rate": rate, "cost_center_id": env["cost_center_id"]}
            for name, debit, credit in amounts]
    insert_gl_entries(conn, rows, voucher_type="journal_entry", voucher_id=voucher,
                      posting_date=day, company_id=env["company2_id"])
    conn.commit()
    return voucher


def flags(env, **changes):
    policy = [{"account_id": env[name], "basis": basis} for name, basis in
              (("cash", "closing"), ("debt", "closing"), ("income", "average"), ("expense", "average"))]
    policy += [{"account_id": env["capital"], "basis": "historical", "equity_class": "capital", "rate": "1.00"},
               {"account_id": env["retained"], "basis": "carry", "equity_class": "retained-earnings", "reporting_balance": "-90.00"}]
    values = dict(company_id=env["company_id"], group_id=env["group_id"], entity_company_id=env["company2_id"],
                  start_date="2026-01-01", period_date="2026-06-30", closing_rate="1.20", average_rate="1.10",
                  review_reference="Approved worksheet", translation_policy=json.dumps(policy))
    values.update(changes)
    return ns(**values)


def snapshot(conn):
    return {name: [tuple(row) for row in conn.execute(Q.from_(Table(name)).select("*").get_sql()).fetchall()]
            for name in sorted(table_names())}


def test_real_books_translation_balances_and_does_not_write(books):
    conn, env = books
    before = snapshot(conn)
    result = call_action(mod.consolidation_translation_report, conn, flags(env))
    assert result["status"] == "ok", result
    assert result["translated_debits_before_cta"] == "710.00"
    assert result["translated_credits_before_cta"] == "640.00"
    assert result["balancing_cta_credit"] == "70.00"
    assert result["balancing_cta_debit"] == "0.00"
    assert result["balanced_debits"] == result["balanced_credits"] == "710.00"
    assert result["functional_currency"] == "EUR" and result["reporting_currency"] == "USD"
    assert result["stored"] is False and result["posted"] is False
    assert {row["account_id"]: row["translated_balance"] for row in result["rows"]}[env["retained"]] == "-90.00"
    assert snapshot(conn) == before


def test_cta_debit_sign_and_no_ownership_scaling(books):
    conn, env = books
    result = call_action(mod.consolidation_translation_report, conn, flags(env, closing_rate="0.80"))
    assert result["balancing_cta_debit"] == "90.00"
    assert result["balancing_cta_credit"] == "0.00"
    assert result["balanced_debits"] == result["balanced_credits"] == "600.00"
    assert next(row for row in result["rows"] if row["account_id"] == env["cash"])["source_balance"] == "500.00"


def test_cancelled_and_future_books_are_not_read(books):
    conn, env = books
    original = call_action(mod.consolidation_translation_report, conn, flags(env))
    voucher = post(conn, env, [("cash", "1.00", "0.00"), ("income", "0.00", "1.00")])
    reverse_gl_entries(conn, voucher_type="journal_entry", voucher_id=voucher, posting_date="2026-06-30")
    conn.commit()
    post(conn, env, [("cash", "999.00", "0.00"), ("income", "0.00", "999.00")], day="2026-07-01")
    assert call_action(mod.consolidation_translation_report, conn, flags(env)) == original


@pytest.mark.parametrize("changes", [
    {"group_id": "absent"}, {"company_id": "absent"}, {"entity_company_id": "absent"},
    {"start_date": "20260701"}, {"start_date": "2026-07-01"}, {"period_date": "2026-02-30"},
    {"closing_rate": "NaN"}, {"closing_rate": "0"}, {"average_rate": "-1"},
    {"average_rate": "1e2"}, {"closing_rate": "1.0000000000001"}, {"review_reference": " "},
    {"translation_policy": "{}"}, {"translation_policy": "[]"}, {"translation_policy": "bad"},
])
def test_bad_request_refuses_without_writes(books, changes):
    conn, env = books
    before = snapshot(conn)
    assert call_action(mod.consolidation_translation_report, conn, flags(env, **changes))["status"] == "error"
    assert snapshot(conn) == before


@pytest.mark.parametrize("problem", ["missing", "duplicate", "wrong-basis", "capital-carry", "earnings-rate", "extra", "foreign"])
def test_classification_is_exhaustive_disjoint_and_explicit(books, problem):
    conn, env = books
    args = flags(env)
    policy = json.loads(args.translation_policy)
    if problem == "missing":
        policy.pop(0)
    elif problem == "duplicate":
        policy.append(policy[0])
    elif problem == "wrong-basis":
        policy[0]["basis"] = "average"
    elif problem == "capital-carry":
        policy[-2] = {"account_id": env["capital"], "basis": "carry", "equity_class": "capital", "reporting_balance": "-100"}
    elif problem == "earnings-rate":
        policy[-1] = {"account_id": env["retained"], "basis": "historical", "equity_class": "retained-earnings", "rate": "1.20"}
    elif problem == "extra":
        policy[0]["guess"] = "no"
    else:
        policy[0]["account_id"] = "absent"
    args.translation_policy = json.dumps(policy)
    assert call_action(mod.consolidation_translation_report, conn, args)["status"] == "error"


def test_income_opening_balance_requires_close(books):
    conn, env = books
    post(conn, env, [("cash", "1.00", "0.00"), ("income", "0.00", "1.00")], day="2026-01-01")
    result = call_action(mod.consolidation_translation_report, conn, flags(env, start_date="2026-02-01"))
    assert result["message"] == "Income and expense opening balances must be closed before start-date"


def test_scope_requires_owner_and_entity(books, monkeypatch):
    conn, env = books
    from erpclaw_lib import company_scope
    monkeypatch.setattr(company_scope, "resolution_scope", lambda *_: frozenset({env["company_id"]}))
    before = snapshot(conn)
    result = call_action(mod.consolidation_translation_report, conn, flags(env))
    assert result.get("message") == company_scope.REFUSAL_CODE, result
    assert snapshot(conn) == before


def test_each_account_rounds_once_and_cta_retains_rounding(books):
    conn, env = books
    args = flags(env, closing_rate="1.00001", average_rate="1")
    policy = json.loads(args.translation_policy)
    policy[-1]["reporting_balance"] = "-100.00"
    args.translation_policy = json.dumps(policy)
    result = call_action(mod.consolidation_translation_report, conn, args)
    rows = {row["account_id"]: row for row in result["rows"]}
    assert rows[env["cash"]]["translated_balance"] == "500.01"
    assert rows[env["debt"]]["translated_balance"] == "-100.00"
    assert result["balancing_cta_credit"] == "0.01"
    assert Decimal(result["rounding_difference"]) == Decimal("0.00600")
    assert result["balanced_debits"] == result["balanced_credits"] == "600.01"


def test_actual_routed_seeded_readonly_sweep(books, tmp_path, monkeypatch):
    conn, env = books
    conn.commit()
    disposable = tmp_path / "sweep.sqlite"
    conn.execute("VACUUM INTO ?", (str(disposable),))
    root = Path(__file__).resolve().parents[5]
    monkeypatch.syspath_prepend(str(root / "testing"))
    import readonly_sweep
    args = {key.replace("_", "-"): value for key, value in vars(flags(env)).items()}
    result = readonly_sweep._dispatch_once(readonly_sweep._child_argv("consolidation-translation-report", args),
                                          readonly_sweep._child_env, str(disposable))
    assert result["status"] == "ok" and result["findings"] == [], result
