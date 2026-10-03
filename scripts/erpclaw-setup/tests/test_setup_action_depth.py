"""Behavioural depth for three erpclaw-setup actions whose only prior coverage
was routability (the contract suite proves each can be reached, not what it
does to the database):

  - tutorial
  - unlink-telegram-user
  - update-regional-settings

Every test reads the affected rows back through the seam stack (a second
connection from `erpclaw_lib.db.get_connection`, statements built with
`erpclaw_lib.query`, catalog questions through `erpclaw_lib.seam`) and compares
exact stored values. None of these actions touches money or posts ledger legs,
so no balance assertion can hold for any of them; each class says so explicitly
so a later reader does not add one.

The other two task actions already have module-level tests (`set-password` in
test_rbac.py, `validate-registry-completeness` in test_registry_admin.py) and
are deepened in place there rather than here.
"""
from datetime import datetime, timezone

import pytest

from setup_helpers import call_action, ns, seed_company, is_error, is_ok, load_db_query, read_all, read_one, open_reader, freeze_snapshot  # noqa: E501 (binds erpclaw_lib to this tree)
from erpclaw_lib import seam  # noqa: E402 (catalog questions go through the seam)

mod = load_db_query()

_TUTORIAL_TABLES = ("company", "account", "cost_center", "fiscal_year", "audit_log")

EXPECTED_TUTORIAL_NUMBERS = (
    "1000", "1100", "1200", "1300", "1400",
    "2000", "2100", "2150", "2200", "2300", "2310", "2320",
    "4000", "4100",
    "5000", "5100", "5200", "5210", "5300",
    "3000", "3100",
)


class TestTutorial:
    def test_creates_demo_company_with_exact_rows(self, conn, db_path):
        """Behavioural depth for tutorial: Acme Corp exists afterwards with the
        exact seeded values, 21 accounts, one cost center, one fiscal year, and
        exactly one audit row; unrelated tables are untouched.

        Ledger: tutorial seeds master data only; it posts no ledger legs, so
        no balance assertion can hold here.
        """
        result = call_action(mod.tutorial, conn, ns())
        assert is_ok(result)
        assert result["company_name"] == "Acme Corp"
        assert result["accounts_created"] == 21
        reader = open_reader(db_path)
        try:
            companies = read_all(reader, "company", seam.column_names("company", db_path))
            assert len(companies) == 1
            co = companies[0]
            assert co["name"] == "Acme Corp"
            assert co["abbr"] == "AC"
            assert co["default_currency"] == "USD"
            assert co["country"] == "United States"
            accounts = read_all(reader, "account", seam.column_names("account", db_path))
            assert len(accounts) == result["accounts_created"] == 21
            assert sorted(a["account_number"] for a in accounts) == sorted(EXPECTED_TUTORIAL_NUMBERS)
            by_number = {a["account_number"]: a for a in accounts}
            assert {a["company_id"] for a in accounts} == {co["id"]}
            assert by_number["1100"]["balance_direction"] == "debit_normal"
            assert by_number["2100"]["balance_direction"] == "credit_normal"
            assert co["default_receivable_account_id"] == by_number["1300"]["id"]
            assert co["default_payable_account_id"] == by_number["2100"]["id"]
            assert co["default_income_account_id"] == by_number["4100"]["id"]
            assert co["default_expense_account_id"] == by_number["5200"]["id"]
            assert co["default_bank_account_id"] == by_number["1100"]["id"]
            assert co["default_cash_account_id"] == by_number["1200"]["id"]
            centers = read_all(reader, "cost_center", seam.column_names("cost_center", db_path))
            assert len(centers) == 1
            assert centers[0]["name"] == "Main"
            assert co["default_cost_center_id"] == centers[0]["id"]
            years = read_all(reader, "fiscal_year", seam.column_names("fiscal_year", db_path))
            year = datetime.now(timezone.utc).strftime("%Y")
            assert len(years) == 1
            assert years[0]["name"] == "FY %s" % year
            assert years[0]["start_date"] == "%s-01-01" % year
            assert years[0]["end_date"] == "%s-12-31" % year
            assert years[0]["is_closed"] in (0, "0")
            audits = read_all(reader, "audit_log", seam.column_names("audit_log", db_path))
            assert len(audits) == 1
            assert audits[0]["skill"] == "erpclaw-setup"
            assert audits[0]["action"] == "create"
            assert audits[0]["entity_type"] == "company"
            assert audits[0]["entity_id"] == co["id"]
            assert read_all(reader, "erp_user", ["id"]) == []
            assert read_all(reader, "currency", ["code"]) == []
        finally:
            reader.close()

    def test_second_run_writes_nothing(self, conn, db_path):
        """Idempotency: re-running tutorial returns the same company and leaves
        every observed table byte-identical (no duplicated demo data).

        tutorial has no input-validation branch (it never rejects), so this
        no-second-write proof stands in for the refusal case: the action that
        cannot refuse must at least never half-write twice.
        """
        first = call_action(mod.tutorial, conn, ns())
        assert is_ok(first)
        reader = open_reader(db_path)
        try:
            before = freeze_snapshot(reader, db_path, _TUTORIAL_TABLES)
            second = call_action(mod.tutorial, conn, ns())
            assert is_ok(second)
            assert second["company_id"] == first["company_id"]
            assert second["accounts"] == 21
            assert freeze_snapshot(reader, db_path, _TUTORIAL_TABLES) == before
        finally:
            reader.close()


class TestUnlinkTelegramUser:
    def test_unknown_telegram_user_refuses_truthfully_and_writes_nothing(self, conn, db_path):
        """Refusal: an id nobody linked reports exactly that, and the database
        is byte-identical afterwards.

        FINDING (defect, deliberately not fixed): this refusal is
        vacuous-by-construction on this tree — no fixture can reach the success
        path through the module's own actions because link-telegram-user
        crashes (see below) and erp_user has no telegram_user_id column.
        """
        call_action(mod.add_user, conn, ns(
            name="tg_user", email="tg@t.com",
            full_name=None, company_id=None,
        ))
        reader = open_reader(db_path)
        try:
            before = freeze_snapshot(reader, db_path, ("erp_user", "audit_log"))
            result = call_action(mod.unlink_telegram_user, conn, ns(telegram_user_id="424242"))
            assert is_error(result)
            assert result["message"] == "No user linked to Telegram user 424242"
            assert freeze_snapshot(reader, db_path, ("erp_user", "audit_log")) == before
        finally:
            reader.close()

    def test_missing_arg_refuses_and_writes_nothing(self, conn, db_path):
        """Refusal on missing input: truthful message, byte-identical database."""
        reader = open_reader(db_path)
        try:
            before = freeze_snapshot(reader, db_path, ("erp_user", "audit_log"))
            result = call_action(mod.unlink_telegram_user, conn, ns(telegram_user_id=None))
            assert is_error(result)
            assert result["message"] == "--telegram-user-id is required"
            assert freeze_snapshot(reader, db_path, ("erp_user", "audit_log")) == before
        finally:
            reader.close()

    def test_link_crashes_on_missing_column_and_writes_nothing(self, conn, db_path):
        """FINDING (defect, deliberately not fixed): link-telegram-user cannot
        run on this tree. erp_user has no telegram_user_id column (absent from
        the schema and from every migration), so the link dies with
        "no such column" before writing anything.

        Ledger: neither telegram action posts ledger legs, so no balance
        assertion can hold here.
        """
        created = call_action(mod.add_user, conn, ns(
            name="tg_link_user", email="tl@t.com",
            full_name=None, company_id=None,
        ))
        assert "telegram_user_id" not in seam.column_names("erp_user", db_path)
        reader = open_reader(db_path)
        try:
            before = freeze_snapshot(reader, db_path, ("erp_user", "audit_log"))
            with pytest.raises(Exception, match="no such column"):
                call_action(mod.link_telegram_user, conn, ns(
                    user_id=created["user_id"], telegram_user_id="12345"))
            assert freeze_snapshot(reader, db_path, ("erp_user", "audit_log")) == before
        finally:
            reader.close()


class TestUpdateRegionalSettings:
    def test_sets_and_updates_exact_rows(self, conn, db_path):
        """Behavioural depth: the exact (company, key, value) rows are stored,
        a second call upserts (no duplicate rows), and each call leaves exactly
        one audit row.

        Ledger: regional settings are display metadata; the action posts no
        ledger legs, so no balance assertion can hold here.
        """
        cid = seed_company(conn)
        result = call_action(mod.update_regional_settings, conn, ns(
            company_id=cid, date_format="DD-MM-YYYY", number_format="#,##0.00",
            default_tax_template_id=None,
        ))
        assert is_ok(result)
        assert sorted(result["updated"]) == ["date_format", "number_format"]
        reader = open_reader(db_path)
        try:
            cols = seam.column_names("regional_settings", db_path)
            rows = read_all(reader, "regional_settings", cols)
            assert {(r["key"], r["value"]) for r in rows} == {
                ("date_format", "DD-MM-YYYY"), ("number_format", "#,##0.00")}
            assert {r["company_id"] for r in rows} == {cid}
            again = call_action(mod.update_regional_settings, conn, ns(
                company_id=cid, date_format="MM/DD/YYYY", number_format=None,
                default_tax_template_id=None,
            ))
            assert is_ok(again)
            assert again["updated"] == ["date_format"]
            rows2 = read_all(reader, "regional_settings", cols)
            assert len(rows2) == 2
            assert {r["key"]: r["value"] for r in rows2} == {
                "date_format": "MM/DD/YYYY", "number_format": "#,##0.00"}
            audit_cols = seam.column_names("audit_log", db_path)
            writes = [r for r in read_all(reader, "audit_log", audit_cols)
                      if r["entity_type"] == "regional_settings" and r["entity_id"] == cid]
            assert len(writes) == 2
            assert {w["action"] for w in writes} == {"update"}
        finally:
            reader.close()

    def test_defaults_to_the_only_company(self, conn, db_path):
        """Behavioural depth for the company fallback: with no --company-id the
        write lands on the single existing company, with the exact value."""
        cid = seed_company(conn)
        result = call_action(mod.update_regional_settings, conn, ns(
            company_id=None, date_format="YYYY-MM-DD", number_format=None,
            default_tax_template_id=None,
        ))
        assert is_ok(result)
        assert result["updated"] == ["date_format"]
        reader = open_reader(db_path)
        try:
            rows = read_all(reader, "regional_settings",
                            seam.column_names("regional_settings", db_path))
            assert len(rows) == 1
            assert rows[0]["company_id"] == cid
            assert rows[0]["key"] == "date_format"
            assert rows[0]["value"] == "YYYY-MM-DD"
        finally:
            reader.close()

    def test_no_settings_refuses_and_writes_nothing(self, conn, db_path):
        """Refusal: empty settings are rejected with a truthful message and the
        database is byte-identical afterwards (company included)."""
        cid = seed_company(conn)
        reader = open_reader(db_path)
        try:
            before = freeze_snapshot(reader, db_path, ("regional_settings", "company", "audit_log"))
            result = call_action(mod.update_regional_settings, conn, ns(
                company_id=cid, date_format=None, number_format=None,
                default_tax_template_id=None,
            ))
            assert is_error(result)
            assert result["message"] == "No settings to update. Use --date-format, --number-format, etc."
            assert freeze_snapshot(reader, db_path, ("regional_settings", "company", "audit_log")) == before
        finally:
            reader.close()

    def test_no_company_refuses_truthfully_and_writes_nothing(self, conn, db_path):
        """Refusal on the fallback path: with no company anywhere the action
        says so (pointing at tutorial) instead of writing an orphan row."""
        reader = open_reader(db_path)
        try:
            before = freeze_snapshot(reader, db_path, ("regional_settings", "company", "audit_log"))
            result = call_action(mod.update_regional_settings, conn, ns(
                company_id=None, date_format="DD-MM-YYYY", number_format=None,
                default_tax_template_id=None,
            ))
            assert is_error(result)
            assert result["message"] == "No company found"
            assert freeze_snapshot(reader, db_path, ("regional_settings", "company", "audit_log")) == before
        finally:
            reader.close()
