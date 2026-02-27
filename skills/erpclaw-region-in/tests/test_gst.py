"""Tests for GST computation, setup, and seed actions."""
import sqlite3
import uuid

import pytest
from conftest import run_action


class TestComputeGST:
    def test_intra_state_cgst_sgst(self, tmp_db):
        """Test 9: Same seller/buyer state → CGST+SGST 50/50 split."""
        out, rc = run_action(
            tmp_db, "compute-gst",
            amount="10000", gst_rate="18",
            seller_state="27", buyer_state="27",
        )
        assert rc == 0
        assert out["intra_state"] is True
        assert out["cgst_amount"] == "900.00"
        assert out["sgst_amount"] == "900.00"
        assert out["igst_amount"] == "0"
        assert out["total_tax"] == "1800.00"
        assert out["total_with_tax"] == "11800.00"

    def test_inter_state_igst(self, tmp_db):
        """Test 10: Different seller/buyer states → full IGST."""
        out, rc = run_action(
            tmp_db, "compute-gst",
            amount="10000", gst_rate="18",
            seller_state="27", buyer_state="29",
        )
        assert rc == 0
        assert out["intra_state"] is False
        assert out["igst_amount"] == "1800.00"
        assert out["cgst_amount"] == "0"
        assert out["sgst_amount"] == "0"
        assert out["total_tax"] == "1800.00"

    def test_gst_5_percent(self, tmp_db):
        """GST at 5% rate."""
        out, rc = run_action(
            tmp_db, "compute-gst",
            amount="20000", gst_rate="5",
            seller_state="33", buyer_state="33",
        )
        assert rc == 0
        assert out["cgst_amount"] == "500.00"
        assert out["sgst_amount"] == "500.00"
        assert out["total_tax"] == "1000.00"

    def test_gst_40_percent_luxury(self, tmp_db):
        """GST at 40% luxury rate."""
        out, rc = run_action(
            tmp_db, "compute-gst",
            amount="5000", gst_rate="40",
            seller_state="27", buyer_state="29",
        )
        assert rc == 0
        assert out["igst_amount"] == "2000.00"
        assert out["total_with_tax"] == "7000.00"

    def test_gst_hsn_lookup(self, tmp_db):
        """Test 11: HSN code determines correct rate."""
        # HSN 0402 is "Milk and cream, concentrated" at 5%
        out, rc = run_action(
            tmp_db, "compute-gst",
            amount="1000", hsn_code="0402",
            seller_state="27", buyer_state="27",
        )
        assert rc == 0
        assert out["gst_rate"] == "5"
        assert out["cgst_amount"] == "25.00"
        assert out["sgst_amount"] == "25.00"

    def test_gst_nil_rate(self, tmp_db):
        """Nil-rated item → zero tax."""
        out, rc = run_action(
            tmp_db, "compute-gst",
            amount="5000", gst_rate="0",
            seller_state="27", buyer_state="29",
        )
        assert rc == 0
        assert out["total_tax"] == "0.00"


class TestListHSNCodes:
    def test_list_all(self, tmp_db):
        """List HSN codes returns results."""
        out, rc = run_action(tmp_db, "list-hsn-codes")
        assert rc == 0
        assert out["total_count"] > 0
        assert len(out["codes"]) > 0

    def test_search_by_text(self, tmp_db):
        """Search HSN codes by description."""
        out, rc = run_action(tmp_db, "list-hsn-codes", search="milk")
        assert rc == 0
        assert out["total_count"] > 0
        for c in out["codes"]:
            assert "milk" in c["description"].lower()

    def test_filter_by_rate(self, tmp_db):
        """Filter HSN codes by GST rate."""
        out, rc = run_action(tmp_db, "list-hsn-codes", gst_rate="0")
        assert rc == 0
        assert out["total_count"] > 0
        for c in out["codes"]:
            assert c["rate"] == "0"


class TestSeedIndiaDefaults:
    def test_seed_creates_accounts(self, india_company):
        """Test 1: seed-india-defaults creates GST accounts."""
        db_path, company_id = india_company
        out, rc = run_action(db_path, "seed-india-defaults", company_id=company_id)
        assert rc == 0
        assert out["created"]["accounts"] >= 6  # At least CGST/SGST/IGST in/out

        # Verify accounts exist
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        accounts = conn.execute(
            "SELECT name FROM account WHERE company_id = ? AND name LIKE '%GST%'",
            (company_id,),
        ).fetchall()
        conn.close()
        names = [a["name"] for a in accounts]
        assert "CGST Input" in names
        assert "SGST Output" in names
        assert "IGST Input" in names

    def test_seed_creates_templates(self, india_company):
        """Test 2: seed-india-defaults creates tax templates."""
        db_path, company_id = india_company
        out, rc = run_action(db_path, "seed-india-defaults", company_id=company_id)
        assert rc == 0
        assert out["created"]["templates"] >= 3  # At least 5%, 18%, 40%

    def test_seed_idempotent(self, india_company):
        """Test 3: Running seed twice doesn't duplicate data."""
        db_path, company_id = india_company
        out1, _ = run_action(db_path, "seed-india-defaults", company_id=company_id)
        out2, _ = run_action(db_path, "seed-india-defaults", company_id=company_id)
        assert out2["created"]["accounts"] == 0
        assert out2["created"]["templates"] == 0

    def test_seed_rejects_us_company(self, us_company):
        """Seed action rejects non-India company."""
        db_path, company_id = us_company
        out, rc = run_action(db_path, "seed-india-defaults", company_id=company_id)
        assert rc == 1
        assert "not India" in out["message"] or "not IN" in out["message"]


class TestSetupGST:
    def test_setup_gst_stores_gstin(self, india_company):
        """Test 4: setup-gst stores GSTIN and state code."""
        db_path, company_id = india_company
        # Seed first to create accounts
        run_action(db_path, "seed-india-defaults", company_id=company_id)
        out, rc = run_action(
            db_path, "setup-gst",
            company_id=company_id,
            gstin="27AABCU9603R1ZN",
            state_code="27",
        )
        assert rc == 0
        assert out["gstin"] == "27AABCU9603R1ZN"
        assert out["state_name"] == "Maharashtra"

        # Verify stored in regional_settings
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        gstin = conn.execute(
            "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'gstin'",
            (company_id,),
        ).fetchone()
        conn.close()
        assert gstin["value"] == "27AABCU9603R1ZN"

    def test_setup_gst_validates_gstin(self, india_company):
        """Setup rejects invalid GSTIN."""
        db_path, company_id = india_company
        out, rc = run_action(
            db_path, "setup-gst",
            company_id=company_id,
            gstin="INVALIDGSTIN123",
            state_code="27",
        )
        assert rc == 1

    def test_setup_gst_state_mismatch(self, india_company):
        """Setup rejects when state code doesn't match GSTIN prefix."""
        db_path, company_id = india_company
        out, rc = run_action(
            db_path, "setup-gst",
            company_id=company_id,
            gstin="27AABCU9603R1ZN",
            state_code="29",
        )
        assert rc == 1
        assert "does not match" in out["message"]
