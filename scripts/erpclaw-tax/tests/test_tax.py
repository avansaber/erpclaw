"""Tests for erpclaw-tax actions.

Covers every entry in the ACTIONS dispatch table of db_query.py
(18 actions found; 18 covered — see test_every_action_has_a_test).
Each test exercises the real handler against a fresh database and asserts
on persisted rows or exact computed values, never only on a status key.

Ledger note: no action in this module writes gl_entry — the module only
persists tax-domain rows (templates, rules, categories, withholding data)
or computes in memory (calculate-tax, resolve-tax-template and the other
read paths). There is therefore no ledger assertion to make; where an
action does not reach the ledger this is stated so a later reader does
not add one that cannot hold.

Money note: every monetary assertion compares exact Decimal values as
strings. No float, no approximate comparison, no round().
"""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tax_helpers import (
    call_action, ns, is_error, is_ok, snapshot,
    seed_company, seed_account, seed_customer, seed_supplier,
    seed_withholding_entry, tl, load_db_query,
)

mod = load_db_query()


def test_every_action_has_a_test():
    """The ACTIONS table holds 18 entries; each one is named by a test."""
    actions = list(mod.ACTIONS.keys())
    assert len(actions) == 18, (
        "ACTIONS table changed size: %d: %s" % (len(actions), sorted(actions)))
    text = Path(__file__).read_text()
    missing = [a for a in actions if ("# action: %s" % a) not in text]
    assert not missing, "actions without a test: %s" % missing


def _add_template(conn, company_id, account_id, name="Tmpl",
                  tax_type="sales", lines=None, is_default=None):
    if lines is None:
        lines = [tl(account_id, "10")]
    return call_action(mod.add_tax_template, conn, ns(
        name=name, tax_type=tax_type, company_id=company_id,
        lines=json.dumps(lines), is_default=is_default))


# ──────────────────────────────────────────────────────────────────────────────
# add-tax-template
# ──────────────────────────────────────────────────────────────────────────────

class TestAddTaxTemplate:
    def test_create_persists_template_and_lines(self, conn):
        # action: add-tax-template
        # Does not reach the ledger: persists tax_template + tax_template_line.
        cid = seed_company(conn)
        acct = seed_account(conn, cid, "Sales Tax Payable")
        before = snapshot(conn)
        result = _add_template(
            conn, cid, acct, name="VAT 10",
            lines=[tl(acct, "10"), tl(acct, "2", row_order=1)])
        assert is_ok(result)
        assert result["line_count"] == 2

        row = conn.execute(
            "SELECT * FROM tax_template WHERE id=?",
            (result["tax_template_id"],)).fetchone()
        assert row["name"] == "VAT 10"
        assert row["tax_type"] == "sales"
        assert row["company_id"] == cid
        assert row["is_default"] == 0

        lines = conn.execute(
            "SELECT * FROM tax_template_line WHERE tax_template_id=? ORDER BY row_order",
            (result["tax_template_id"],)).fetchall()
        assert len(lines) == 2
        assert Decimal(str(lines[0]["rate"])) == Decimal("10.00")
        assert lines[0]["charge_type"] == "on_net_total"
        assert lines[0]["add_deduct"] == "add"
        assert Decimal(str(lines[1]["rate"])) == Decimal("2.00")
        assert snapshot(conn)["tax_template"] == before["tax_template"] + 1
        assert snapshot(conn)["tax_template_line"] == before["tax_template_line"] + 2

    def test_rate_rounding_boundaries_half_up(self, conn):
        # action: add-tax-template
        # Stored rates pass through round_currency (HALF_UP to 2dp):
        # 7.555 rounds up while 7.554 rounds down; 2.675 rounds up.
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        result = _add_template(conn, cid, acct, name="Bounds", lines=[
            tl(acct, "7.555"), tl(acct, "7.554", row_order=1),
            tl(acct, "2.675", row_order=2)])
        assert is_ok(result)
        rates = [r["rate"] for r in conn.execute(
            "SELECT rate FROM tax_template_line WHERE tax_template_id=? ORDER BY row_order",
            (result["tax_template_id"],)).fetchall()]
        assert Decimal(str(rates[0])) == Decimal("7.56")
        assert Decimal(str(rates[1])) == Decimal("7.55")
        assert Decimal(str(rates[2])) == Decimal("2.68")

    def test_is_default_clears_previous_default(self, conn):
        # action: add-tax-template
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        r1 = _add_template(conn, cid, acct, name="DEF1", tax_type="purchase",
                           is_default=True)
        r2 = _add_template(conn, cid, acct, name="DEF2", tax_type="purchase",
                           is_default=True)
        assert is_ok(r1) and is_ok(r2)
        flags = {r["name"]: r["is_default"] for r in conn.execute(
            "SELECT name, is_default FROM tax_template WHERE company_id=?", (cid,))}
        assert flags == {"DEF1": 0, "DEF2": 1}

    def test_unknown_account_refused_and_unchanged(self, conn):
        # action: add-tax-template
        cid = seed_company(conn)
        before = snapshot(conn)
        result = _add_template(conn, cid, "no-such-account", name="Bad")
        assert is_error(result)
        assert "account not found" in result["message"]
        assert snapshot(conn) == before

    def test_duplicate_name_raises_and_unchanged(self, conn):
        # action: add-tax-template
        # DOCUMENTED DEFECT (no production change per task): the
        # UNIQUE(name, company_id) constraint has no handler-level guard, so a
        # duplicate name escapes as an unhandled sqlite3.IntegrityError
        # instead of a clean {"status": "error"} refusal.
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        assert is_ok(_add_template(conn, cid, acct, name="DUP"))
        before = snapshot(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _add_template(conn, cid, acct, name="DUP")
        assert snapshot(conn) == before
        count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM tax_template WHERE name='DUP' AND company_id=?",
            (cid,)).fetchone()["cnt"]
        assert count == 1


# ──────────────────────────────────────────────────────────────────────────────
# update-tax-template
# ──────────────────────────────────────────────────────────────────────────────

class TestUpdateTaxTemplate:
    def test_rename_persists(self, conn):
        # action: update-tax-template
        # Does not reach the ledger: updates the tax_template row.
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="Old")["tax_template_id"]
        result = call_action(mod.update_tax_template, conn, ns(
            tax_template_id=tid, name="New"))
        assert is_ok(result)
        assert result["updated_fields"] == ["name"]
        row = conn.execute(
            "SELECT name FROM tax_template WHERE id=?", (tid,)).fetchone()
        assert row["name"] == "New"

    def test_replace_lines_persists(self, conn):
        # action: update-tax-template
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="Upd",
                            lines=[tl(acct, "10")])["tax_template_id"]
        result = call_action(mod.update_tax_template, conn, ns(
            tax_template_id=tid,
            lines=json.dumps([tl(acct, "12"), tl(acct, "3",
                                                 charge_type="on_previous_row_total",
                                                 row_order=1)])))
        assert is_ok(result)
        assert "lines" in result["updated_fields"]
        rows = conn.execute(
            "SELECT rate, charge_type FROM tax_template_line "
            "WHERE tax_template_id=? ORDER BY row_order", (tid,)).fetchall()
        assert [(r["rate"], r["charge_type"]) for r in rows] == [
            ("12.00", "on_net_total"), ("3.00", "on_previous_row_total")]
        assert Decimal(str(rows[0]["rate"])) == Decimal("12.00")
        assert Decimal(str(rows[1]["rate"])) == Decimal("3.00")

    def test_unknown_id_refused_and_unchanged(self, conn):
        # action: update-tax-template
        before = snapshot(conn)
        result = call_action(mod.update_tax_template, conn, ns(
            tax_template_id="no-such-template", name="X"))
        assert is_error(result)
        assert "not found" in result["message"]
        assert snapshot(conn) == before

    def test_no_fields_refused_and_unchanged(self, conn):
        # action: update-tax-template
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="Same")["tax_template_id"]
        before = snapshot(conn)
        result = call_action(mod.update_tax_template, conn, ns(
            tax_template_id=tid))
        assert is_error(result)
        assert "No fields to update" in result["message"]
        assert snapshot(conn) == before
        assert conn.execute(
            "SELECT name FROM tax_template WHERE id=?", (tid,)).fetchone()["name"] == "Same"


# ──────────────────────────────────────────────────────────────────────────────
# get-tax-template
# ──────────────────────────────────────────────────────────────────────────────

class TestGetTaxTemplate:
    def test_get_returns_template_with_lines(self, conn):
        # action: get-tax-template
        # Read-only: returns the stored template plus its lines with the
        # GL account name resolved.
        cid = seed_company(conn)
        acct = seed_account(conn, cid, "State Tax")
        tid = _add_template(conn, cid, acct, name="GetMe",
                            lines=[tl(acct, "6")])["tax_template_id"]
        before = snapshot(conn)
        result = call_action(mod.get_tax_template, conn, ns(tax_template_id=tid))
        assert is_ok(result)
        assert result["name"] == "GetMe"
        assert result["tax_type"] == "sales"
        assert len(result["lines"]) == 1
        assert result["lines"][0]["tax_account_id"] == acct
        assert result["lines"][0]["account_name"] == "State Tax"
        assert Decimal(str(result["lines"][0]["rate"])) == Decimal("6.00")
        assert snapshot(conn) == before

    def test_unknown_id_refused(self, conn):
        # action: get-tax-template
        before = snapshot(conn)
        result = call_action(mod.get_tax_template, conn, ns(
            tax_template_id="no-such-template"))
        assert is_error(result)
        assert "not found" in result["message"]
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# list-tax-templates
# ──────────────────────────────────────────────────────────────────────────────

class TestListTaxTemplates:
    def test_pagination_and_type_filter(self, conn):
        # action: list-tax-templates
        # Read-only: 'both'-type templates match either sales or purchase filter.
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        _add_template(conn, cid, acct, name="S1", tax_type="sales")
        _add_template(conn, cid, acct, name="S2", tax_type="sales")
        _add_template(conn, cid, acct, name="P1", tax_type="purchase")
        _add_template(conn, cid, acct, name="B1", tax_type="both")

        page1 = call_action(mod.list_tax_templates, conn, ns(
            company_id=cid, limit="2", offset="0"))
        assert is_ok(page1)
        assert page1["total_count"] == 4
        assert len(page1["templates"]) == 2
        assert page1["has_more"] is True

        page2 = call_action(mod.list_tax_templates, conn, ns(
            company_id=cid, limit="2", offset="2"))
        assert len(page2["templates"]) == 2
        assert page2["has_more"] is False

        sales = call_action(mod.list_tax_templates, conn, ns(
            company_id=cid, tax_type="sales"))
        assert sorted(t["name"] for t in sales["templates"]) == ["B1", "S1", "S2"]
        assert sales["total_count"] == 3

        purch = call_action(mod.list_tax_templates, conn, ns(
            company_id=cid, tax_type="purchase"))
        assert sorted(t["name"] for t in purch["templates"]) == ["B1", "P1"]

    def test_unknown_company_name_refused_and_unchanged(self, conn):
        # action: list-tax-templates
        before = snapshot(conn)
        result = call_action(mod.list_tax_templates, conn, ns(
            company_name="No Such Company"))
        # resolve_company_id refuses with an {"error": ...} envelope that
        # carries no "status" key (shared-lib shape, unlike err()).
        assert result.get("status") != "ok"
        assert "not found" in result.get("error", "")
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# delete-tax-template
# ──────────────────────────────────────────────────────────────────────────────

class TestDeleteTaxTemplate:
    def test_delete_removes_template_and_lines(self, conn):
        # action: delete-tax-template
        # Does not reach the ledger: deletes tax_template + its lines.
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="Gone")["tax_template_id"]
        result = call_action(mod.delete_tax_template, conn, ns(
            tax_template_id=tid))
        assert is_ok(result)
        assert result["deleted"] is True
        assert conn.execute(
            "SELECT COUNT(*) AS cnt FROM tax_template WHERE id=?",
            (tid,)).fetchone()["cnt"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS cnt FROM tax_template_line WHERE tax_template_id=?",
            (tid,)).fetchone()["cnt"] == 0

    def test_unknown_id_refused_and_unchanged(self, conn):
        # action: delete-tax-template
        before = snapshot(conn)
        result = call_action(mod.delete_tax_template, conn, ns(
            tax_template_id="no-such-template"))
        assert is_error(result)
        assert "not found" in result["message"]
        assert snapshot(conn) == before

    def test_blocked_by_tax_rule_and_unchanged(self, conn):
        # action: delete-tax-template
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="Ruled")["tax_template_id"]
        cust = seed_customer(conn, cid)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=tid, tax_type="sales", priority=1,
            customer_id=cust)))
        before = snapshot(conn)
        result = call_action(mod.delete_tax_template, conn, ns(
            tax_template_id=tid))
        assert is_error(result)
        assert "tax rule" in result["message"]
        assert snapshot(conn) == before
        assert conn.execute(
            "SELECT COUNT(*) AS cnt FROM tax_template WHERE id=?",
            (tid,)).fetchone()["cnt"] == 1

    def test_blocked_by_item_tax_template_and_unchanged(self, conn):
        # action: delete-tax-template
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="ItemLinked")["tax_template_id"]
        assert is_ok(call_action(mod.add_item_tax_template, conn, ns(
            item_id="ITEM-1", tax_template_id=tid, tax_rate="5")))
        before = snapshot(conn)
        result = call_action(mod.delete_tax_template, conn, ns(
            tax_template_id=tid))
        assert is_error(result)
        assert "item tax template" in result["message"]
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# resolve-tax-template
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveTaxTemplate:
    def _sales_setup(self, conn):
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="RuleTmpl")["tax_template_id"]
        cust = seed_customer(conn, cid)
        return cid, tid, cust

    def test_customer_rule_match(self, conn):
        # action: resolve-tax-template
        # Read-only: returns the matching template, not a persisted change.
        cid, tid, cust = self._sales_setup(conn)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=tid, tax_type="sales", priority=1,
            customer_id=cust)))
        before = snapshot(conn)
        result = call_action(mod.resolve_tax_template, conn, ns(
            party_type="customer", party_id=cust, company_id=cid))
        assert is_ok(result)
        assert result["tax_template_id"] == tid
        assert result["template_name"] == "RuleTmpl"
        assert result["is_exempt"] is False
        assert snapshot(conn) == before

    def test_lowest_priority_number_wins(self, conn):
        # action: resolve-tax-template
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        low = _add_template(conn, cid, acct, name="Low")["tax_template_id"]
        high = _add_template(conn, cid, acct, name="High")["tax_template_id"]
        cust = seed_customer(conn, cid)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=low, tax_type="sales", priority=20,
            customer_id=cust)))
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=high, tax_type="sales", priority=3,
            customer_id=cust)))
        result = call_action(mod.resolve_tax_template, conn, ns(
            party_type="customer", party_id=cust, company_id=cid))
        assert result["tax_template_id"] == high
        assert result["template_name"] == "High"

    def test_supplier_rule_match(self, conn):
        # action: resolve-tax-template
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="SupT",
                            tax_type="purchase")["tax_template_id"]
        sup = seed_supplier(conn, cid)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=tid, tax_type="purchase", priority=1,
            supplier_id=sup)))
        result = call_action(mod.resolve_tax_template, conn, ns(
            party_type="supplier", party_id=sup, company_id=cid))
        assert result["tax_template_id"] == tid
        assert result["template_name"] == "SupT"

    def test_state_and_category_rule_match_and_mismatch(self, conn):
        # action: resolve-tax-template
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="SSTmpl")["tax_template_id"]
        cat = call_action(mod.add_tax_category, conn, ns(
            name="CAT-SS", description="state test"))["tax_category_id"]
        cust = seed_customer(conn, cid)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=tid, tax_type="sales", priority=1,
            shipping_state="CA", tax_category_id=cat)))
        match = call_action(mod.resolve_tax_template, conn, ns(
            party_type="customer", party_id=cust, company_id=cid,
            shipping_address=json.dumps({"state": "CA"}),
            tax_category_id=cat))
        assert match["tax_template_id"] == tid
        # Wrong state and no company default: no template resolves.
        mismatch = call_action(mod.resolve_tax_template, conn, ns(
            party_type="customer", party_id=cust, company_id=cid,
            shipping_address=json.dumps({"state": "NY"}),
            tax_category_id=cat))
        assert mismatch["tax_template_id"] is None
        assert mismatch["template_name"] is None

    def test_exempt_customer_flagged_but_still_resolves(self, conn):
        # action: resolve-tax-template
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="ExTmpl")["tax_template_id"]
        cust = seed_customer(conn, cid, exempt=True)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=tid, tax_type="sales", priority=1,
            customer_id=cust)))
        result = call_action(mod.resolve_tax_template, conn, ns(
            party_type="customer", party_id=cust, company_id=cid))
        assert result["is_exempt"] is True
        assert result["tax_template_id"] == tid

    def test_default_fallback_without_rules(self, conn):
        # action: resolve-tax-template
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="DefPur",
                            tax_type="purchase",
                            is_default=True)["tax_template_id"]
        sup = seed_supplier(conn, cid)
        result = call_action(mod.resolve_tax_template, conn, ns(
            party_type="supplier", party_id=sup, company_id=cid))
        assert result["tax_template_id"] == tid
        assert result["template_name"] == "DefPur"

    def test_item_override_listed(self, conn):
        # action: resolve-tax-template
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        best = _add_template(conn, cid, acct, name="Best")["tax_template_id"]
        other = _add_template(conn, cid, acct, name="Other")["tax_template_id"]
        cust = seed_customer(conn, cid)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=best, tax_type="sales", priority=1,
            customer_id=cust)))
        assert is_ok(call_action(mod.add_item_tax_template, conn, ns(
            item_id="ITEM-7", tax_template_id=other, tax_rate="5")))
        result = call_action(mod.resolve_tax_template, conn, ns(
            party_type="customer", party_id=cust, company_id=cid))
        assert result["tax_template_id"] == best
        assert {"item_id": "ITEM-7",
                "tax_template_id": other} in result["item_overrides"]

    def test_unregistered_party_type_refused_and_unchanged(self, conn):
        # action: resolve-tax-template
        cid = seed_company(conn)
        cust = seed_customer(conn, cid)
        before = snapshot(conn)
        result = call_action(mod.resolve_tax_template, conn, ns(
            party_type="alien", party_id=cust, company_id=cid))
        assert is_error(result)
        assert "not registered" in result["message"]
        assert snapshot(conn) == before

    def test_missing_party_type_refused_and_unchanged(self, conn):
        # action: resolve-tax-template
        cid = seed_company(conn)
        before = snapshot(conn)
        result = call_action(mod.resolve_tax_template, conn, ns(
            party_id="whatever", company_id=cid))
        assert is_error(result)
        assert "party-type" in result["message"]
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# calculate-tax (pure: no database writes, does not reach the ledger)
# ──────────────────────────────────────────────────────────────────────────────

class TestCalculateTax:
    def test_on_net_total_exact_split(self, conn):
        # action: calculate-tax
        # Pure computation: the snapshot proves nothing was written.
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="Ten")["tax_template_id"]
        before = snapshot(conn)
        result = call_action(mod.calculate_tax, conn, ns(
            tax_template_id=tid, items=json.dumps([
                {"item_id": "i1", "net_amount": "100.00"},
                {"item_id": "i2", "net_amount": "200.00"}])))
        assert is_ok(result)
        assert Decimal(str(result["net_total"])) == Decimal("300.00")
        assert Decimal(str(result["total_tax"])) == Decimal("30.00")
        assert Decimal(str(result["grand_total"])) == Decimal("330.00")
        assert len(result["tax_lines"]) == 1
        assert Decimal(str(result["tax_lines"][0]["amount"])) == Decimal("30.00")
        by_item = {p["item_id"]: p["tax_amount"]
                   for p in result["per_item_tax"]}
        assert Decimal(str(by_item["i1"])) == Decimal("10.00")
        assert Decimal(str(by_item["i2"])) == Decimal("20.00")
        assert snapshot(conn) == before

    def test_rounding_boundaries_half_up(self, conn):
        # action: calculate-tax
        # 10% of 33.35 is 3.335 -> rounds up to 3.34; 10% of 33.33 is
        # 3.333 -> rounds down to 3.33 (ROUND_HALF_UP to 2dp).
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="TenB")["tax_template_id"]
        up = call_action(mod.calculate_tax, conn, ns(
            tax_template_id=tid, items=json.dumps(
                [{"item_id": "i1", "net_amount": "33.35"}])))
        down = call_action(mod.calculate_tax, conn, ns(
            tax_template_id=tid, items=json.dumps(
                [{"item_id": "i1", "net_amount": "33.33"}])))
        assert Decimal(str(up["total_tax"])) == Decimal("3.34")
        assert Decimal(str(up["grand_total"])) == Decimal("36.69")
        assert Decimal(str(down["total_tax"])) == Decimal("3.33")
        assert Decimal(str(down["grand_total"])) == Decimal("36.66")

    def test_cascade_actual_and_deduct(self, conn):
        # action: calculate-tax
        # 100.00 net: +10% on net (10.00), +5% on previous row total
        # (5% of 110.00 = 5.50), actual 2.50, then -2% deduct (-2.00).
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="Casc", lines=[
            tl(acct, "10"),
            tl(acct, "5", charge_type="on_previous_row_total", row_order=1),
            tl(acct, "2.50", charge_type="actual", row_order=2),
            tl(acct, "2", add_deduct="deduct", row_order=3),
        ])["tax_template_id"]
        result = call_action(mod.calculate_tax, conn, ns(
            tax_template_id=tid, items=json.dumps(
                [{"item_id": "i1", "net_amount": "100.00"}])))
        amounts = [Decimal(str(line["amount"])) for line in result["tax_lines"]]
        assert amounts == [Decimal("10.00"), Decimal("5.50"),
                           Decimal("2.50"), Decimal("-2.00")]
        assert Decimal(str(result["total_tax"])) == Decimal("16.00")
        assert Decimal(str(result["grand_total"])) == Decimal("116.00")

    def test_on_previous_row_amount(self, conn):
        # action: calculate-tax
        # 200.00 net: +10% (20.00), then 50% of the previous row's
        # amount (50% of 20.00 = 10.00).
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="PrevAmt", lines=[
            tl(acct, "10"),
            tl(acct, "50", charge_type="on_previous_row_amount", row_order=1),
        ])["tax_template_id"]
        result = call_action(mod.calculate_tax, conn, ns(
            tax_template_id=tid, items=json.dumps(
                [{"item_id": "i1", "net_amount": "200.00"}])))
        assert Decimal(str(result["total_tax"])) == Decimal("30.00")
        assert Decimal(str(result["grand_total"])) == Decimal("230.00")

    def test_on_item_quantity(self, conn):
        # action: calculate-tax
        # Base is total quantity (2 + 4 = 6): 3% of 6 = 0.18, split by
        # net-amount share to 0.09 per item.
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="Qty", lines=[
            tl(acct, "3", charge_type="on_item_quantity"),
        ])["tax_template_id"]
        result = call_action(mod.calculate_tax, conn, ns(
            tax_template_id=tid, items=json.dumps([
                {"item_id": "i1", "net_amount": "50.00", "qty": "2"},
                {"item_id": "i2", "net_amount": "50.00", "qty": "4"}])))
        assert Decimal(str(result["total_tax"])) == Decimal("0.18")
        assert Decimal(str(result["grand_total"])) == Decimal("100.18")
        by_item = {p["item_id"]: p["tax_amount"]
                   for p in result["per_item_tax"]}
        assert Decimal(str(by_item["i1"])) == Decimal("0.09")
        assert Decimal(str(by_item["i2"])) == Decimal("0.09")

    def test_item_overrides_excluded_from_split(self, conn):
        # action: calculate-tax
        # The overridden item keeps 0.00 while the header total is unchanged.
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="Ovr")["tax_template_id"]
        result = call_action(mod.calculate_tax, conn, ns(
            tax_template_id=tid,
            items=json.dumps([{"item_id": "i1", "net_amount": "100.00"},
                              {"item_id": "i2", "net_amount": "100.00"}]),
            item_overrides=json.dumps(
                [{"item_id": "i2", "override_template_id": "other"}])))
        assert Decimal(str(result["total_tax"])) == Decimal("20.00")
        by_item = {p["item_id"]: p["tax_amount"]
                   for p in result["per_item_tax"]}
        assert Decimal(str(by_item["i1"])) == Decimal("10.00")
        assert Decimal(str(by_item["i2"])) == Decimal("0.00")

    def test_unknown_template_refused(self, conn):
        # action: calculate-tax
        before = snapshot(conn)
        result = call_action(mod.calculate_tax, conn, ns(
            tax_template_id="no-such-template", items=json.dumps(
                [{"item_id": "i1", "net_amount": "10.00"}])))
        assert is_error(result)
        assert "No template lines found" in result["message"]
        assert snapshot(conn) == before

    def test_missing_items_refused(self, conn):
        # action: calculate-tax
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="NoItems")["tax_template_id"]
        before = snapshot(conn)
        result = call_action(mod.calculate_tax, conn, ns(
            tax_template_id=tid))
        assert is_error(result)
        assert "non-empty JSON array" in result["message"]
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# add-tax-category / list-tax-categories
# ──────────────────────────────────────────────────────────────────────────────

class TestAddTaxCategory:
    def test_create_persists(self, conn):
        # action: add-tax-category
        # Does not reach the ledger: persists a tax_category row.
        before = snapshot(conn)
        result = call_action(mod.add_tax_category, conn, ns(
            name="Services", description="service tax"))
        assert is_ok(result)
        row = conn.execute(
            "SELECT * FROM tax_category WHERE id=?",
            (result["tax_category_id"],)).fetchone()
        assert row["name"] == "Services"
        assert row["description"] == "service tax"
        assert snapshot(conn)["tax_category"] == before["tax_category"] + 1

    def test_missing_name_refused_and_unchanged(self, conn):
        # action: add-tax-category
        before = snapshot(conn)
        result = call_action(mod.add_tax_category, conn, ns(description="x"))
        assert is_error(result)
        assert "name" in result["message"]
        assert snapshot(conn) == before

    def test_duplicate_name_raises_and_unchanged(self, conn):
        # action: add-tax-category
        # DOCUMENTED DEFECT (no production change per task): the
        # UNIQUE(name) constraint has no handler-level guard, so a duplicate
        # escapes as an unhandled sqlite3.IntegrityError instead of a clean
        # {"status": "error"} refusal. Same shape as the add-tax-template one.
        assert is_ok(call_action(mod.add_tax_category, conn, ns(name="DUPC")))
        before = snapshot(conn)
        with pytest.raises(sqlite3.IntegrityError):
            call_action(mod.add_tax_category, conn, ns(name="DUPC"))
        assert snapshot(conn) == before


class TestListTaxCategories:
    def test_list_pagination(self, conn):
        # action: list-tax-categories
        # Read-only: ordered by name with total_count / has_more paging.
        for name in ("C0", "C1", "C2"):
            assert is_ok(call_action(mod.add_tax_category, conn, ns(name=name)))
        page = call_action(mod.list_tax_categories, conn, ns(
            limit="2", offset="1"))
        assert is_ok(page)
        assert [c["name"] for c in page["categories"]] == ["C1", "C2"]
        assert page["total_count"] == 3
        assert page["has_more"] is False

    def test_no_validated_input_to_refuse(self, conn):
        # action: list-tax-categories
        # Limitation record: this action validates no input, so there is no
        # refusal path to test. An out-of-range offset is a successful empty
        # page, and the database is unchanged either way.
        before = snapshot(conn)
        result = call_action(mod.list_tax_categories, conn, ns(
            limit="20", offset="99"))
        assert is_ok(result)
        assert result["categories"] == []
        assert result["total_count"] == 0
        assert result["has_more"] is False
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# add-tax-rule / list-tax-rules
# ──────────────────────────────────────────────────────────────────────────────

class TestAddTaxRule:
    def test_create_persists(self, conn):
        # action: add-tax-rule
        # Does not reach the ledger: persists a tax_rule row.
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="Ruled2")["tax_template_id"]
        cust = seed_customer(conn, cid)
        before = snapshot(conn)
        result = call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=tid, tax_type="sales", priority=7,
            customer_id=cust))
        assert is_ok(result)
        row = conn.execute(
            "SELECT * FROM tax_rule WHERE id=?",
            (result["tax_rule_id"],)).fetchone()
        assert row["tax_template_id"] == tid
        assert row["tax_type"] == "sales"
        assert row["customer_id"] == cust
        assert row["priority"] == 7
        assert row["company_id"] == cid
        assert snapshot(conn)["tax_rule"] == before["tax_rule"] + 1

    def test_no_filter_refused_and_unchanged(self, conn):
        # action: add-tax-rule
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="NoFilt")["tax_template_id"]
        before = snapshot(conn)
        result = call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=tid, tax_type="sales", priority=5))
        assert is_error(result)
        assert "filter condition" in result["message"]
        assert snapshot(conn) == before

    def test_bad_tax_type_refused_and_unchanged(self, conn):
        # action: add-tax-rule
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="BadType")["tax_template_id"]
        cust = seed_customer(conn, cid)
        before = snapshot(conn)
        result = call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=tid, tax_type="both", priority=5,
            customer_id=cust))
        assert is_error(result)
        assert "sales" in result["message"] and "purchase" in result["message"]
        assert snapshot(conn) == before


class TestListTaxRules:
    def test_list_returns_template_names_in_priority_order(self, conn):
        # action: list-tax-rules
        # Read-only: joins the template name and orders by priority.
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        first = _add_template(conn, cid, acct, name="First")["tax_template_id"]
        second = _add_template(conn, cid, acct, name="Second")["tax_template_id"]
        cust = seed_customer(conn, cid)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=first, tax_type="sales", priority=9,
            customer_id=cust)))
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=second, tax_type="sales", priority=1,
            customer_id=cust)))
        result = call_action(mod.list_tax_rules, conn, ns(company_id=cid))
        assert is_ok(result)
        assert result["total_count"] == 2
        assert [r["template_name"] for r in result["rules"]] == ["Second", "First"]

    def test_unknown_company_refused_and_unchanged(self, conn):
        # action: list-tax-rules
        before = snapshot(conn)
        result = call_action(mod.list_tax_rules, conn, ns(
            company_name="No Such Company"))
        # Same resolve_company_id {"error": ...} envelope as above.
        assert result.get("status") != "ok"
        assert "not found" in result.get("error", "")
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# add-item-tax-template
# ──────────────────────────────────────────────────────────────────────────────

class TestAddItemTaxTemplate:
    def test_create_persists_linkage(self, conn):
        # action: add-item-tax-template
        # Does not reach the ledger: persists an item_tax_template row.
        cid = seed_company(conn)
        tid = _add_template(conn, cid, seed_account(conn, cid),
                            name="ItemT")["tax_template_id"]
        before = snapshot(conn)
        result = call_action(mod.add_item_tax_template, conn, ns(
            item_id="ITEM-9", tax_template_id=tid, tax_rate="5.5"))
        assert is_ok(result)
        row = conn.execute(
            "SELECT * FROM item_tax_template WHERE id=?",
            (result["item_tax_template_id"],)).fetchone()
        assert row["item_id"] == "ITEM-9"
        assert row["tax_template_id"] == tid
        assert Decimal(str(row["tax_rate"])) == Decimal("5.5")
        assert snapshot(conn)["item_tax_template"] == before["item_tax_template"] + 1

    def test_unknown_template_refused_and_unchanged(self, conn):
        # action: add-item-tax-template
        before = snapshot(conn)
        result = call_action(mod.add_item_tax_template, conn, ns(
            item_id="ITEM-1", tax_template_id="no-such-template"))
        assert is_error(result)
        assert "not found" in result["message"]
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# add-tax-withholding-category / get-withholding-details
# ──────────────────────────────────────────────────────────────────────────────

class TestAddTaxWithholdingCategory:
    def test_create_persists_category_and_default_group(self, conn):
        # action: add-tax-withholding-category
        # Does not reach the ledger: persists the category plus one
        # 'Default' withholding group holding the exact rate.
        cid = seed_company(conn)
        seed_account(conn, cid, "Withholding Payable",
                     root_type="liability", account_type="payable")
        before = snapshot(conn)
        result = call_action(mod.add_tax_withholding_category, conn, ns(
            name="NEC", wh_rate="10", threshold_amount="600",
            form_type="1099-NEC", company_id=cid))
        assert is_ok(result)
        cat = conn.execute(
            "SELECT * FROM tax_withholding_category WHERE id=?",
            (result["category_id"],)).fetchone()
        assert cat["name"] == "NEC"
        assert cat["category_code"] == "1099-NEC"
        assert Decimal(str(cat["cumulative_threshold"])) == Decimal("600")
        groups = conn.execute(
            "SELECT * FROM tax_withholding_group WHERE category_id=?",
            (result["category_id"],)).fetchall()
        assert len(groups) == 1
        assert groups[0]["group_name"] == "Default"
        assert Decimal(str(groups[0]["rate"])) == Decimal("10")
        assert groups[0]["effective_from"] == "2020-01-01"
        assert snapshot(conn)["tax_withholding_category"] == \
            before["tax_withholding_category"] + 1
        assert snapshot(conn)["tax_withholding_group"] == \
            before["tax_withholding_group"] + 1

    def test_bad_form_type_refused_and_unchanged(self, conn):
        # action: add-tax-withholding-category
        cid = seed_company(conn)
        before = snapshot(conn)
        result = call_action(mod.add_tax_withholding_category, conn, ns(
            name="Bad", wh_rate="10", threshold_amount="600",
            form_type="W-2", company_id=cid))
        assert is_error(result)
        assert "form-type" in result["message"]
        assert snapshot(conn) == before

    def test_no_liability_account_creates_category_without_group(self, conn):
        # action: add-tax-withholding-category
        # Limitation record: with no tax/payable account in the company the
        # handler still reports success but writes no withholding group, so
        # the category can never supply a rate.
        cid = seed_company(conn)
        result = call_action(mod.add_tax_withholding_category, conn, ns(
            name="NoAcct", wh_rate="10", threshold_amount="600",
            form_type="1099-MISC", company_id=cid))
        assert is_ok(result)
        assert conn.execute(
            "SELECT COUNT(*) AS cnt FROM tax_withholding_group WHERE category_id=?",
            (result["category_id"],)).fetchone()["cnt"] == 0


class TestGetWithholdingDetails:
    def _category(self, conn, cid):
        seed_account(conn, cid, "WH Payable",
                     root_type="liability", account_type="payable")
        return call_action(mod.add_tax_withholding_category, conn, ns(
            name="NEC", wh_rate="10", threshold_amount="600",
            form_type="1099-NEC", company_id=cid))["category_id"]

    def test_1099_vendor_ytd_and_backup_math(self, conn):
        # action: get-withholding-details
        # Read-only: aggregates entries. The entries are seeded directly
        # (see seed_withholding_entry) because record-1099-payment cannot
        # write them — DEFECT-01 in CHANGES.md. A 1099 vendor with no W-9 on
        # file owes 24% backup withholding on YTD payments: 700.00 -> 168.00.
        cid = seed_company(conn)
        cat = self._category(conn, cid)
        sup = seed_supplier(conn, cid, is_1099=True, tax_id="12-3456789")
        seed_withholding_entry(conn, sup, cat, "2026", "700.00")
        before = snapshot(conn)
        result = call_action(mod.get_withholding_details, conn, ns(
            supplier_id=sup, tax_year="2026", company_id=cid))
        assert is_ok(result)
        assert result["is_1099_vendor"] is True
        assert Decimal(str(result["ytd_payments"])) == Decimal("700.00")
        assert Decimal(str(result["backup_withholding_rate"])) == Decimal("24.0")
        assert Decimal(str(result["withholding_amount"])) == Decimal("168.00")
        # Limitation record: the supplier row cannot carry its withholding
        # category (no such column — DEFECT-01), so the category name, rate
        # and threshold always read back empty/zero here.
        assert result["withholding_category"] == ""
        assert result["threshold_exceeded"] is False
        assert snapshot(conn) == before

    def test_clean_vendor_reads_zero(self, conn):
        # action: get-withholding-details
        cid = seed_company(conn)
        self._category(conn, cid)
        sup = seed_supplier(conn, cid)
        result = call_action(mod.get_withholding_details, conn, ns(
            supplier_id=sup, tax_year="2026", company_id=cid))
        assert is_ok(result)
        assert result["is_1099_vendor"] is False
        assert Decimal(str(result["ytd_payments"])) == Decimal("0.00")
        assert Decimal(str(result["withholding_amount"])) == Decimal("0.00")
        assert result["w9_on_file"] is False

    def test_unknown_supplier_refused(self, conn):
        # action: get-withholding-details
        cid = seed_company(conn)
        before = snapshot(conn)
        result = call_action(mod.get_withholding_details, conn, ns(
            supplier_id="no-such-supplier", tax_year="2026",
            company_id=cid))
        assert is_error(result)
        assert "not found" in result["message"]
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# record-withholding-entry / record-1099-payment
# ──────────────────────────────────────────────────────────────────────────────

class TestRecordWithholdingEntry:
    def test_valid_call_refused_missing_supplier_link(self, conn):
        # action: record-withholding-entry
        # DOCUMENTED DEFECT DEFECT-01 (no production change per task): the
        # supplier table has no tax_withholding_category_id column, so the
        # handler's lookup always misses and every valid call is refused with
        # "Supplier has no withholding category assigned". The refusal is
        # truthful about what it checked, and nothing is half-written.
        cid = seed_company(conn)
        seed_account(conn, cid, "WH Payable",
                     root_type="liability", account_type="payable")
        assert is_ok(call_action(mod.add_tax_withholding_category, conn, ns(
            name="NEC", wh_rate="10", threshold_amount="600",
            form_type="1099-NEC", company_id=cid)))
        sup = seed_supplier(conn, cid)
        before = snapshot(conn)
        result = call_action(mod.record_withholding_entry, conn, ns(
            supplier_id=sup, voucher_type="purchase_invoice",
            voucher_id="PI-1", withholding_amount="50", tax_year="2026"))
        assert is_error(result)
        assert "no withholding category assigned" in result["message"]
        assert snapshot(conn) == before
        assert conn.execute(
            "SELECT COUNT(*) AS cnt FROM tax_withholding_entry"
            ).fetchone()["cnt"] == 0

    def test_unknown_supplier_refused_and_unchanged(self, conn):
        # action: record-withholding-entry
        before = snapshot(conn)
        result = call_action(mod.record_withholding_entry, conn, ns(
            supplier_id="no-such-supplier", voucher_type="purchase_invoice",
            voucher_id="PI-1", withholding_amount="50", tax_year="2026"))
        assert is_error(result)
        assert "not found" in result["message"]
        assert snapshot(conn) == before

    def test_missing_voucher_refused_and_unchanged(self, conn):
        # action: record-withholding-entry
        cid = seed_company(conn)
        sup = seed_supplier(conn, cid)
        before = snapshot(conn)
        result = call_action(mod.record_withholding_entry, conn, ns(
            supplier_id=sup, withholding_amount="50", tax_year="2026"))
        assert is_error(result)
        assert "voucher-type" in result["message"]
        assert snapshot(conn) == before


class TestRecord1099Payment:
    def test_valid_call_refused_missing_supplier_link(self, conn):
        # action: record-1099-payment
        # DOCUMENTED DEFECT DEFECT-01 (no production change per task): same
        # missing supplier.tax_withholding_category_id column as
        # record-withholding-entry, so the happy path is unreachable and the
        # handler always refuses without writing.
        cid = seed_company(conn)
        sup = seed_supplier(conn, cid)
        before = snapshot(conn)
        result = call_action(mod.record_1099_payment, conn, ns(
            supplier_id=sup, ple_amount="700", tax_year="2026",
            voucher_type="purchase_invoice", voucher_id="PI-1"))
        assert is_error(result)
        assert "no withholding category assigned" in result["message"]
        assert snapshot(conn) == before

    def test_unknown_supplier_refused_and_unchanged(self, conn):
        # action: record-1099-payment
        before = snapshot(conn)
        result = call_action(mod.record_1099_payment, conn, ns(
            supplier_id="no-such-supplier", ple_amount="700",
            tax_year="2026", voucher_type="purchase_invoice",
            voucher_id="PI-1"))
        assert is_error(result)
        assert "not found" in result["message"]
        assert snapshot(conn) == before


# ──────────────────────────────────────────────────────────────────────────────
# generate-1099-data / status
# ──────────────────────────────────────────────────────────────────────────────

class TestGenerate1099Data:
    def _seed_vendors(self, conn, cid):
        seed_account(conn, cid, "WH Payable",
                     root_type="liability", account_type="payable")
        nec = call_action(mod.add_tax_withholding_category, conn, ns(
            name="NEC", wh_rate="10", threshold_amount="600",
            form_type="1099-NEC", company_id=cid))["category_id"]
        misc = call_action(mod.add_tax_withholding_category, conn, ns(
            name="MISC", wh_rate="5", threshold_amount="600",
            form_type="1099-MISC", company_id=cid))["category_id"]
        # Entries are seeded directly: record-1099-payment cannot write them
        # (DEFECT-01 in CHANGES.md), so this is the only way to exercise the
        # aggregation, threshold and box logic of this read path.
        big_nec = seed_supplier(conn, cid, name="Big NEC", tax_id="12-3456789")
        seed_withholding_entry(conn, big_nec, nec, "2026", "700.00")
        big_misc = seed_supplier(conn, cid, name="Big MISC", tax_id="98-7654321")
        seed_withholding_entry(conn, big_misc, misc, "2026", "800.00")
        small = seed_supplier(conn, cid, name="Small")
        seed_withholding_entry(conn, small, nec, "2026", "100.00")
        return big_nec, big_misc

    def test_threshold_filter_and_boxes(self, conn):
        # action: generate-1099-data
        # Read-only: vendors under the 600.00 cumulative threshold are
        # excluded; 1099-NEC totals land in box_1 while 1099-MISC reads 0.00.
        cid = seed_company(conn)
        big_nec, big_misc = self._seed_vendors(conn, cid)
        before = snapshot(conn)
        result = call_action(mod.generate_1099_data, conn, ns(
            tax_year="2026", company_id=cid))
        assert is_ok(result)
        vendors = sorted(result["vendors"], key=lambda v: v["name"])
        assert [v["name"] for v in vendors] == ["Big MISC", "Big NEC"]
        nec = next(v for v in vendors if v["supplier_id"] == big_nec)
        assert nec["tin"] == "12-3456789"
        assert Decimal(str(nec["total_paid"])) == Decimal("700.00")
        assert nec["form_type"] == "1099-NEC"
        assert Decimal(str(nec["box_1"])) == Decimal("700.00")
        misc = next(v for v in vendors if v["supplier_id"] == big_misc)
        assert Decimal(str(misc["total_paid"])) == Decimal("800.00")
        assert misc["form_type"] == "1099-MISC"
        assert Decimal(str(misc["box_1"])) == Decimal("0.00")
        assert snapshot(conn) == before

    def test_empty_year_returns_no_vendors(self, conn):
        # action: generate-1099-data
        cid = seed_company(conn)
        result = call_action(mod.generate_1099_data, conn, ns(
            tax_year="2026", company_id=cid))
        assert is_ok(result)
        assert result["vendors"] == []

    def test_unknown_company_refused(self, conn):
        # action: generate-1099-data
        before = snapshot(conn)
        result = call_action(mod.generate_1099_data, conn, ns(
            tax_year="2026", company_name="No Such Company"))
        # Same resolve_company_id {"error": ...} envelope as above.
        assert result.get("status") != "ok"
        assert "not found" in result.get("error", "")
        assert snapshot(conn) == before


class TestStatus:
    def test_counts(self, conn):
        # action: status
        # Read-only: counts templates, rules and withholding categories for
        # the company; ytd_1099_vendors counts distinct vendors with entries
        # in the current calendar year only.
        cid = seed_company(conn)
        acct = seed_account(conn, cid)
        tid = _add_template(conn, cid, acct, name="Stat")["tax_template_id"]
        cust = seed_customer(conn, cid)
        assert is_ok(call_action(mod.add_tax_rule, conn, ns(
            tax_template_id=tid, tax_type="sales", priority=1,
            customer_id=cust)))
        seed_account(conn, cid, "WH Payable",
                     root_type="liability", account_type="payable")
        cat = call_action(mod.add_tax_withholding_category, conn, ns(
            name="NEC", wh_rate="10", threshold_amount="600",
            form_type="1099-NEC", company_id=cid))["category_id"]
        year = str(datetime.now(timezone.utc).year)
        current = seed_supplier(conn, cid, name="Current")
        seed_withholding_entry(conn, current, cat, year, "700.00")
        old = seed_supplier(conn, cid, name="Old")
        seed_withholding_entry(conn, old, cat, "2001", "900.00")
        before = snapshot(conn)
        result = call_action(mod.status_action, conn, ns(company_id=cid))
        assert is_ok(result)
        assert result["templates"] == 1
        assert result["rules"] == 1
        assert result["withholding_categories"] == 1
        assert result["ytd_1099_vendors"] == 1
        assert snapshot(conn) == before

    def test_unknown_company_refused(self, conn):
        # action: status
        before = snapshot(conn)
        result = call_action(mod.status_action, conn, ns(
            company_name="No Such Company"))
        # Same resolve_company_id {"error": ...} envelope as above.
        assert result.get("status") != "ok"
        assert "not found" in result.get("error", "")
        assert snapshot(conn) == before
