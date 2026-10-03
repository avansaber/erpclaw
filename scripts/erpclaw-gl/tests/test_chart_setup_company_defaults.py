"""Setup-chart fills company default accounts via update-company (m721).

After `setup-chart-of-accounts --template us_gaap`, a fresh company's six
default-account columns are filled by delegating to setup's `update-company`
through `erpclaw_lib.cross_skill.call_skill_action` — the gl module never
writes the company table itself.
"""
import argparse
import importlib.util
import os

from gl_helpers import (
    call_action, ns, is_ok,
    seed_company, seed_account, load_db_query, SETUP_DIR,
)

import erpclaw_lib.cross_skill as cross_skill
from erpclaw_lib.cross_skill import CrossSkillError

mod = load_db_query()

EXPECTED_NUMBERS = {
    "default_receivable_account_id": "1121",
    "default_payable_account_id": "2111",
    "default_income_account_id": "4110",
    "default_expense_account_id": "5350",
    "default_bank_account_id": "1112",
    "default_cash_account_id": "1111",
}

EXPECTED_FLAGS = {
    "default_receivable_account_id": "--default-receivable-account-id",
    "default_payable_account_id": "--default-payable-account-id",
    "default_income_account_id": "--default-income-account-id",
    "default_expense_account_id": "--default-expense-account-id",
    "default_bank_account_id": "--default-bank-account-id",
    "default_cash_account_id": "--default-cash-account-id",
}

DEFAULT_COLUMNS = list(EXPECTED_NUMBERS)


def _account_id(conn, company_id, number):
    row = conn.execute(
        "SELECT id FROM account WHERE account_number=? AND company_id=?",
        (number, company_id),
    ).fetchone()
    assert row is not None, f"account {number} missing for company"
    return row["id"]


def _company_row(conn, company_id):
    return conn.execute(
        "SELECT default_receivable_account_id, default_payable_account_id,"
        " default_income_account_id, default_expense_account_id,"
        " default_bank_account_id, default_cash_account_id"
        " FROM company WHERE id=?",
        (company_id,),
    ).fetchone()


class _Recorder:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result if result is not None else {"status": "ok"}
        self.error = error

    def __call__(self, skill, action, args=None, db_path=None, timeout=30):
        self.calls.append({
            "skill": skill, "action": action,
            "args": dict(args or {}), "db_path": db_path,
        })
        if self.error is not None:
            raise self.error
        return self.result


def test_fresh_company_sets_six_defaults(conn, monkeypatch):
    cid = seed_company(conn)
    fake = _Recorder()
    monkeypatch.setattr(cross_skill, "call_skill_action", fake)
    result = call_action(mod.setup_chart_of_accounts, conn, ns(
        company_id=cid, template="us_gaap",
    ))
    assert is_ok(result)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["skill"] == "erpclaw"
    assert call["action"] == "update-company"
    assert call["args"].get("--company-id") == cid
    assert call["db_path"] is None
    for column, number in EXPECTED_NUMBERS.items():
        flag = EXPECTED_FLAGS[column]
        assert call["args"].get(flag) == _account_id(conn, cid, number), flag
    assert result["company_defaults_set"] == dict(EXPECTED_NUMBERS)


def test_existing_values_are_kept(conn, monkeypatch):
    cid = seed_company(conn)
    other = seed_account(conn, cid, name="Other Bank", root_type="asset",
                         account_type="bank", account_number="9999")
    conn.execute(
        "UPDATE company SET default_bank_account_id=? WHERE id=?",
        (other, cid),
    )
    conn.commit()
    fake = _Recorder()
    monkeypatch.setattr(cross_skill, "call_skill_action", fake)
    result = call_action(mod.setup_chart_of_accounts, conn, ns(
        company_id=cid, template="us_gaap",
    ))
    assert is_ok(result)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert "--default-bank-account-id" not in call["args"]
    assert len([k for k in call["args"] if k != "--company-id"]) == 5
    for column, number in EXPECTED_NUMBERS.items():
        if column == "default_bank_account_id":
            continue
        assert call["args"].get(EXPECTED_FLAGS[column]) == _account_id(conn, cid, number)
    assert "default_bank_account_id" not in result["company_defaults_set"]
    assert result["company_defaults_set"] == {
        c: n for c, n in EXPECTED_NUMBERS.items()
        if c != "default_bank_account_id"
    }


def test_nothing_to_do(conn, monkeypatch):
    cid = seed_company(conn)
    conn.execute(
        "UPDATE company SET default_receivable_account_id='r',"
        " default_payable_account_id='p', default_income_account_id='i',"
        " default_expense_account_id='e', default_bank_account_id='b',"
        " default_cash_account_id='c' WHERE id=?",
        (cid,),
    )
    conn.commit()
    fake = _Recorder()
    monkeypatch.setattr(cross_skill, "call_skill_action", fake)
    result = call_action(mod.setup_chart_of_accounts, conn, ns(
        company_id=cid, template="us_gaap",
    ))
    assert is_ok(result)
    assert fake.calls == []
    assert result["company_defaults_set"] == {}


def test_child_failure_keeps_chart(conn, monkeypatch):
    cid = seed_company(conn)
    fake = _Recorder(error=CrossSkillError("boom"))
    monkeypatch.setattr(cross_skill, "call_skill_action", fake)
    result = call_action(mod.setup_chart_of_accounts, conn, ns(
        company_id=cid, template="us_gaap",
    ))
    assert is_ok(result)
    assert result["company_defaults_set"] == {}
    assert "boom" in result["company_defaults_error"]
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM account WHERE company_id=?", (cid,)
    ).fetchone()["cnt"]
    assert count == result["accounts_created"]
    assert result["accounts_created"] > 0
    for number in EXPECTED_NUMBERS.values():
        assert _account_id(conn, cid, number)

    other_cid = seed_company(conn)
    ok_fake = _Recorder()
    monkeypatch.setattr(cross_skill, "call_skill_action", ok_fake)
    normal = call_action(mod.setup_chart_of_accounts, conn, ns(
        company_id=other_cid, template="us_gaap",
    ))
    assert result["accounts_created"] == normal["accounts_created"]


def test_end_to_end_through_setup_update_company(conn, monkeypatch):
    setup_path = os.path.join(SETUP_DIR, "db_query.py")
    spec = importlib.util.spec_from_file_location("db_query_setup_e2e", setup_path)
    setup_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup_mod)

    cid = seed_company(conn)

    def fake_through_setup(skill, action, args=None, db_path=None, timeout=30):
        assert skill == "erpclaw"
        assert action == "update-company"
        kwargs = {}
        for key, value in (args or {}).items():
            kwargs[key.lstrip("-").replace("-", "_")] = value
        return call_action(setup_mod.update_company, conn, argparse.Namespace(**kwargs))

    monkeypatch.setattr(cross_skill, "call_skill_action", fake_through_setup)
    result = call_action(mod.setup_chart_of_accounts, conn, ns(
        company_id=cid, template="us_gaap",
    ))
    assert is_ok(result)
    assert result["company_defaults_set"] == dict(EXPECTED_NUMBERS)
    row = _company_row(conn, cid)
    for column, number in EXPECTED_NUMBERS.items():
        assert row[column] == _account_id(conn, cid, number), column
