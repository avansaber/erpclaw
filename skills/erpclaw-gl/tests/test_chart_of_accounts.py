"""Tests for chart of accounts actions.

Test IDs: GL-COA-01 through GL-COA-06
"""
import db_query
from helpers import _call_action, create_test_company


# ---------------------------------------------------------------------------
# GL-COA-01: setup-chart-of-accounts with us_gaap creates ~94 accounts
# ---------------------------------------------------------------------------
def test_setup_chart_of_accounts_creates_accounts(fresh_db):
    company_id = create_test_company(fresh_db)
    result = _call_action(
        db_query.setup_chart_of_accounts, fresh_db,
        company_id=company_id, template="us_gaap",
    )
    assert result["status"] == "ok"
    assert result["accounts_created"] == 94
    assert result["template"] == "us_gaap"

    # Verify accounts in DB
    count = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM account WHERE company_id = ?",
        (company_id,),
    ).fetchone()["cnt"]
    assert count == 94


# ---------------------------------------------------------------------------
# GL-COA-02: setup-chart-of-accounts is idempotent (2nd call creates 0)
# ---------------------------------------------------------------------------
def test_setup_chart_of_accounts_idempotent(fresh_db):
    company_id = create_test_company(fresh_db)

    first = _call_action(
        db_query.setup_chart_of_accounts, fresh_db,
        company_id=company_id, template="us_gaap",
    )
    assert first["accounts_created"] == 94

    second = _call_action(
        db_query.setup_chart_of_accounts, fresh_db,
        company_id=company_id, template="us_gaap",
    )
    assert second["status"] == "ok"
    assert second["accounts_created"] == 0


# ---------------------------------------------------------------------------
# GL-COA-03: add-account with required fields
# ---------------------------------------------------------------------------
def test_add_account(fresh_db):
    company_id = create_test_company(fresh_db)
    result = _call_action(
        db_query.add_account, fresh_db,
        company_id=company_id, name="Test Bank Account",
        root_type="asset", account_type="bank",
        account_number="1100",
    )
    assert result["status"] == "ok"
    assert "account_id" in result
    assert result["name"] == "Test Bank Account"
    assert result["account_number"] == "1100"

    # Verify in DB
    row = fresh_db.execute(
        "SELECT * FROM account WHERE id = ?", (result["account_id"],)
    ).fetchone()
    assert row["root_type"] == "asset"
    assert row["account_type"] == "bank"
    assert row["balance_direction"] == "debit_normal"


# ---------------------------------------------------------------------------
# GL-COA-04: list-accounts filters by root_type
# ---------------------------------------------------------------------------
def test_list_accounts_filter_root_type(fresh_db):
    company_id = create_test_company(fresh_db)
    _call_action(
        db_query.setup_chart_of_accounts, fresh_db,
        company_id=company_id, template="us_gaap",
    )

    result = _call_action(
        db_query.list_accounts, fresh_db,
        company_id=company_id, root_type="asset",
    )
    assert result["status"] == "ok"
    assert len(result["accounts"]) > 0
    for acct in result["accounts"]:
        assert acct["root_type"] == "asset"


# ---------------------------------------------------------------------------
# GL-COA-05: get-account returns balance info
# ---------------------------------------------------------------------------
def test_get_account_with_balance(fresh_db):
    company_id = create_test_company(fresh_db)
    result = _call_action(
        db_query.add_account, fresh_db,
        company_id=company_id, name="Cash",
        root_type="asset", account_number="1000",
    )
    acct_id = result["account_id"]

    get_result = _call_action(
        db_query.get_account, fresh_db,
        account_id=acct_id,
    )
    assert get_result["status"] == "ok"
    assert get_result["account"]["id"] == acct_id
    assert get_result["account"]["name"] == "Cash"
    # No GL entries yet, balance should be 0
    assert get_result["account"]["balance"] == "0"
    assert "debit_total" in get_result["account"]
    assert "credit_total" in get_result["account"]


# ---------------------------------------------------------------------------
# GL-COA-06: freeze-account and unfreeze-account
# ---------------------------------------------------------------------------
def test_freeze_unfreeze_account(fresh_db):
    company_id = create_test_company(fresh_db)
    result = _call_action(
        db_query.add_account, fresh_db,
        company_id=company_id, name="Frozen Account",
        root_type="asset", account_number="1999",
    )
    acct_id = result["account_id"]

    # Freeze
    freeze = _call_action(db_query.freeze_account, fresh_db, account_id=acct_id)
    assert freeze["status"] == "ok"
    assert freeze["is_frozen"] is True

    row = fresh_db.execute(
        "SELECT is_frozen FROM account WHERE id = ?", (acct_id,)
    ).fetchone()
    assert row["is_frozen"] == 1

    # Unfreeze
    unfreeze = _call_action(db_query.unfreeze_account, fresh_db, account_id=acct_id)
    assert unfreeze["status"] == "ok"
    assert unfreeze["is_frozen"] is False

    row = fresh_db.execute(
        "SELECT is_frozen FROM account WHERE id = ?", (acct_id,)
    ).fetchone()
    assert row["is_frozen"] == 0
