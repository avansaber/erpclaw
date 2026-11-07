"""Stock seed/role words and truthful setup cards; no database or bootstrap."""
from datetime import date
from types import SimpleNamespace

import pytest

from erpclaw_core import core_contracts, ledger, models, setup
from erpclaw_core.common import Refusal


# Stock setup grants and counter-account roles used by the seed cards.
SEEDS = [
    ("2100", "Accounts Payable", "liability", "payable", False, "2000", "credit"),
    ("3900", "Opening Balance Equity", "equity", "opening_balance", False, "3000", "credit"),
    ("5200", "Stock Adjustment", "expense", "stock_adjustment", False, "5000", "debit"),
    ("5210", "Inventory Write-offs", "expense", "stock_write_off", False, "5000", "debit"),
]
WORDS = {"payable": "accounts payable", "opening_balance": "opening balance equity",
         "stock_adjustment": "stock adjustment", "stock_write_off": "inventory write-off"}


@pytest.mark.parametrize("seed", SEEDS, ids=[row[3] for row in SEEDS])
def test_new_company_seed_has_the_exact_role_parent_and_normal_side(seed):
    number, name, root, role, group, parent, side = seed
    rows = {row[0]: row for row in setup.CHART}
    assert rows[number] == (number, name, root, role, group, parent)
    assert rows[parent][2] == root and rows[parent][4] is True
    assert [row[0] for row in setup.CHART].index(parent) < [row[0] for row in setup.CHART].index(number)
    assert role in models.ACCOUNT_TYPES
    context = SimpleNamespace(get_current_parameters=lambda: {"root_type": root})
    assert setup.SIDES.get(number, models._side_of_root(context)) == side
    if role in models.ROLE_SIDES:
        assert models.ROLE_SIDES[role] == (root, side)


def test_seed_keeps_one_bank_no_cash_and_unique_account_numbers():
    assert len(setup.CHART) == len({row[0] for row in setup.CHART}) == 22
    assert [(r[0], r[1]) for r in setup.CHART if r[3] == "bank"] == [("1100", "Bank")]
    assert not any(r[0] == "1050" or r[1] == "Cash on Hand" for r in setup.CHART)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class RoleRead:
    """A SELECT-only result double; no SQL is executed."""
    def __init__(self, role, rows):
        self.role, self.rows, self.queries = role, rows, []

    def execute(self, statement):
        assert statement.is_select
        assert statement.get_final_froms() == [models.account]
        params = statement.compile().params
        assert params == {"company_id_1": "company-a", "account_type_1": self.role}
        self.queries.append(statement)
        return Rows(self.rows)


def role_context(role, rows):
    return SimpleNamespace(company_id="company-a", company_name="Old Books", conn=RoleRead(role, rows))


@pytest.mark.parametrize("role", WORDS)
def test_old_books_without_role_get_the_typed_plain_refusal_without_backfill(role):
    ctx = role_context(role, [])
    with pytest.raises(Refusal) as caught:
        ledger.account_by_type(ctx, role)
    assert caught.value.code == f"no_{role}_account"
    assert caught.value.message == f"Old Books has no {WORDS[role]} account."
    assert len(ctx.conn.queries) == 1 and ctx.conn.rows == []


@pytest.mark.parametrize("role", WORDS)
def test_existing_custom_numbered_live_role_is_preserved(role):
    live = {"id": "owner-account", "number": "9998", "name": "Owner chosen", "frozen": False}
    frozen = {"id": "old-account", "number": "0010", "name": "Retired", "frozen": True}
    ctx = role_context(role, [frozen, live])
    assert ledger.account_by_type(ctx, role) == live
    assert ctx.conn.rows == [frozen, live]


@pytest.mark.parametrize("role", WORDS)
def test_frozen_role_still_refuses_without_replacement(role):
    ctx = role_context(role, [{"number": "9998", "name": "Owner chosen", "frozen": True}])
    with pytest.raises(Refusal) as caught:
        ledger.account_by_type(ctx, role)
    assert caught.value.code == "frozen_account"
    assert caught.value.message == "9998 Owner chosen is frozen."


@pytest.mark.parametrize("role", WORDS)
def test_ambiguous_roles_still_refuse_in_plain_words(role):
    ctx = role_context(role, [{"number": "9998", "frozen": False}, {"number": "9999", "frozen": False}])
    with pytest.raises(Refusal) as caught:
        ledger.account_by_type(ctx, role)
    assert caught.value.code == f"several_{role}_accounts"
    assert caught.value.message == f"Old Books has more than one {WORDS[role]} account; say which one."


class Scalar:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class SetupRead:
    """Company-scoped status/count doubles; no SQL connection or bootstrap."""
    def __init__(self, company, count, status="pending"):
        self.company, self.count, self.status = company, count, status
        self.queries = []

    def execute(self, statement):
        assert statement.is_select
        tables = statement.get_final_froms()
        params = statement.compile().params
        self.queries.append(statement)
        if tables == [models.company]:
            assert params == {"id_1": self.company}
            return Scalar(self.status)
        assert tables == [models.account]
        assert params == {"company_id_1": self.company}
        assert [column.name for column in statement.selected_columns] == ["count"]
        return Scalar(self.count)


def preview(count, company="company-a", status="pending"):
    ctx = SimpleNamespace(company_id=company, company_name="", currency="", today=date(2026, 11, 4),
                          conn=SetupRead(company, count, status))
    inp = core_contracts.SetUpCompany(name="Owner's Books", currency="USD", fiscal_year_start_month=1,
                                     date_order="MDY")
    return core_contracts.preview_set_up(ctx, inp), ctx


@pytest.mark.parametrize("company,count", [("old-pending", 18), ("fresh-pending", 22), ("owner-custom", 23)])
def test_setup_card_reports_its_companys_persisted_chart_without_changing_it(company, count):
    result, ctx = preview(count, company)
    assert result.card.books[0] == (f"Your chart of accounts: {count} accounts for a small business (bank, "
                                    "receivables, sales, cost of goods, and the rest); more are added when you need them.")
    assert result.card.books[1] == core_contracts.NOTHING_POSTED
    assert len(ctx.conn.queries) == 2 and ctx.conn.count == count


def test_account_count_is_bound_into_the_setup_preview_state_token():
    before, _ = preview(18)
    after, _ = preview(22)
    same, _ = preview(18)
    assert before.state_token != after.state_token
    assert before.state_token == same.state_token
    assert before.card.books != after.card.books


def test_ready_books_remain_refused_before_an_account_count_is_read(monkeypatch):
    execute = SetupRead.execute

    def status_only(self, statement):
        assert statement.get_final_froms() == [models.company], "ready books must not read the chart"
        return execute(self, statement)

    monkeypatch.setattr(SetupRead, "execute", status_only)
    with pytest.raises(Refusal) as caught:
        preview(18, status="ready")
    assert caught.value.code == "books_ready"
