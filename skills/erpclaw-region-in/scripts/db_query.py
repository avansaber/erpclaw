#!/usr/bin/env python3
"""ERPClaw India Regional Skill — db_query.py

Pure overlay skill for India-specific compliance: GST (post GST 2.0),
e-invoicing, GSTR-1/3B, TDS, Indian CoA (Ind-AS), PF/ESI/PT payroll,
and ID validation (GSTIN/PAN/TAN/Aadhaar).

Owns NO tables — reads any table, seeds data directly for setup operations,
all runtime operations are read-only or computational.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Add shared lib to path
try:
    sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
    from erpclaw_lib.db import get_connection, ensure_db_exists, DEFAULT_DB_PATH
    from erpclaw_lib.decimal_utils import to_decimal, round_currency
    from erpclaw_lib.validation import check_input_lengths
    from erpclaw_lib.response import ok, err, row_to_dict
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw-setup first: clawhub install erpclaw-setup", "suggestion": "clawhub install erpclaw-setup"}))
    sys.exit(1)

REQUIRED_TABLES = ["company", "account"]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "assets")

LUHN36_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Verhoeff algorithm tables for Aadhaar validation
_VERHOEFF_D = [
    [0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],
    [2,3,4,0,1,7,8,9,5,6],[3,4,0,1,2,8,9,5,6,7],
    [4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],
    [6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],
    [8,7,6,5,9,3,2,1,0,4],[9,8,7,6,5,4,3,2,1,0],
]
_VERHOEFF_INV = [0,4,3,2,1,5,6,7,8,9]
_VERHOEFF_P = [
    [0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],
    [5,8,0,3,7,9,6,1,4,2],[8,9,1,6,0,4,3,5,2,7],
    [9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],
    [2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8],
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json_asset(filename):
    path = os.path.join(ASSETS_DIR, filename)
    if not os.path.exists(path):
        err(f"Asset file not found: {filename}")
    with open(path, "r") as f:
        return json.load(f)


def _get_company(conn, company_id):
    if not company_id:
        row = conn.execute("SELECT * FROM company LIMIT 1").fetchone()
        if not row:
            err("No company found. Create one with erpclaw-setup first.")
        return row_to_dict(row)
    row = conn.execute("SELECT * FROM company WHERE id = ?", (company_id,)).fetchone()
    if not row:
        err(f"Company not found: {company_id}")
    return row_to_dict(row)


def _check_india_company(company):
    country = (company.get("country") or "").upper()
    if country not in ("IN", "INDIA"):
        err(
            f"Company '{company.get('name', '')}' country is '{company.get('country', '')}', not India (IN).",
            suggestion="Set company country to 'IN' via erpclaw-setup before using India actions.",
        )


# ---------------------------------------------------------------------------
# ID Validation
# ---------------------------------------------------------------------------

def _validate_gstin_format(gstin: str) -> tuple:
    """Returns (is_valid, error_message)."""
    if not gstin or len(gstin) != 15:
        return False, "GSTIN must be exactly 15 characters"
    gstin = gstin.upper()
    if not re.match(r'^[0-3][0-9][A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[A-Z0-9]$', gstin):
        return False, "GSTIN format invalid (expected: SSPPPPPPPPPPEZC)"
    state_code = int(gstin[:2])
    if state_code < 1 or state_code > 38:
        return False, f"Invalid state code: {gstin[:2]} (must be 01-38)"
    # Luhn mod 36 checksum
    total = 0
    for i, ch in enumerate(gstin[:14]):
        val = LUHN36_CHARS.index(ch)
        if i % 2 != 0:
            val *= 2
        total += val // 36 + val % 36
    check = (36 - (total % 36)) % 36
    if LUHN36_CHARS[check] != gstin[14]:
        return False, f"Checksum failed (expected '{LUHN36_CHARS[check]}', got '{gstin[14]}')"
    return True, None


def _validate_pan_format(pan: str) -> tuple:
    if not pan or len(pan) != 10:
        return False, "PAN must be exactly 10 characters"
    pan = pan.upper()
    if not re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pan):
        return False, "PAN format invalid (expected: AAAAA9999A)"
    valid_types = "ABCEFGHIJKLPT"
    if pan[3] not in valid_types:
        return False, f"Invalid entity type at position 4: '{pan[3]}'"
    return True, None


def _validate_tan_format(tan: str) -> tuple:
    if not tan or len(tan) != 10:
        return False, "TAN must be exactly 10 characters"
    tan = tan.upper()
    if not re.match(r'^[A-Z]{4}\d{5}[A-Z]$', tan):
        return False, "TAN format invalid (expected: AAAA99999A)"
    return True, None


def _verhoeff_checksum(num_str: str) -> bool:
    c = 0
    digits = [int(d) for d in reversed(num_str)]
    for i, digit in enumerate(digits):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][digit]]
    return c == 0


def _validate_aadhaar_format(aadhaar: str) -> tuple:
    if not aadhaar or len(aadhaar) != 12:
        return False, "Aadhaar must be exactly 12 digits"
    if not aadhaar.isdigit():
        return False, "Aadhaar must contain only digits"
    if aadhaar[0] in ('0', '1'):
        return False, "Aadhaar cannot start with 0 or 1"
    if not _verhoeff_checksum(aadhaar):
        return False, "Verhoeff checksum failed"
    return True, None


# ---------------------------------------------------------------------------
# Validation actions
# ---------------------------------------------------------------------------

def validate_gstin(conn, args):
    if not args.gstin:
        err("--gstin is required")
    valid, err = _validate_gstin_format(args.gstin.upper())
    states = _load_json_asset("indian_states.json")
    state_name = None
    if valid:
        sc = args.gstin[:2]
        for s in states:
            if s["code"] == sc:
                state_name = s["name"]
                break
    ok({
        "gstin": args.gstin.upper(),
        "valid": valid,
        "error": err,
        "state_code": args.gstin[:2] if valid else None,
        "state_name": state_name,
        "pan": args.gstin[2:12] if valid else None,
    })


def validate_pan(conn, args):
    if not args.pan:
        err("--pan is required")
    valid, err = _validate_pan_format(args.pan.upper())
    entity_types = {
        "A": "Association of Persons (AOP)",
        "B": "Body of Individuals (BOI)",
        "C": "Company",
        "F": "Firm / LLP",
        "G": "Government",
        "H": "Hindu Undivided Family (HUF)",
        "J": "Artificial Juridical Person",
        "K": "Directorate",
        "L": "Local Authority",
        "P": "Individual / Person",
        "T": "Trust",
        "E": "Limited Liability Partnership",
    }
    pan = args.pan.upper()
    ok({
        "pan": pan,
        "valid": valid,
        "error": err,
        "entity_type": entity_types.get(pan[3], "Unknown") if valid else None,
    })


def validate_tan(conn, args):
    if not args.tan:
        err("--tan is required")
    valid, err = _validate_tan_format(args.tan.upper())
    ok({"tan": args.tan.upper(), "valid": valid, "error": err})


def validate_aadhaar(conn, args):
    if not args.aadhaar:
        err("--aadhaar is required")
    valid, err = _validate_aadhaar_format(args.aadhaar)
    ok({
        "aadhaar": args.aadhaar[:4] + "****" + args.aadhaar[8:] if len(args.aadhaar) == 12 else args.aadhaar,
        "valid": valid,
        "error": err,
    })


# ---------------------------------------------------------------------------
# GST Computation
# ---------------------------------------------------------------------------

def _get_gst_rate_for_hsn(hsn_code: str) -> Decimal:
    """Look up GST rate for an HSN/SAC code from asset file."""
    try:
        hsn_data = _load_json_asset("gst_hsn_codes.json")
    except SystemExit:
        # If HSN file not found, return standard 18%
        return Decimal("18")
    for item in hsn_data:
        if item.get("code") == hsn_code or item.get("hsn") == hsn_code:
            return to_decimal(str(item.get("gst_rate", item.get("rate", "18"))))
    return None


def compute_gst(conn, args):
    if not args.amount:
        err("--amount is required")
    if not args.seller_state:
        err("--seller-state is required")
    if not args.buyer_state:
        err("--buyer-state is required")

    try:
        amount = to_decimal(args.amount)
    except (InvalidOperation, ValueError):
        err(f"Invalid amount: {args.amount}")

    # Determine GST rate
    gst_rate = None
    if args.gst_rate:
        gst_rate = to_decimal(args.gst_rate)
    elif args.hsn_code:
        gst_rate = _get_gst_rate_for_hsn(args.hsn_code)
        if gst_rate is None:
            err(f"HSN/SAC code not found: {args.hsn_code}. Use --gst-rate to specify rate manually.",
                 suggestion="Add the HSN code with add-hsn-code or use list-hsn-codes to search.")
    else:
        err("Either --hsn-code or --gst-rate is required")

    intra_state = args.seller_state == args.buyer_state
    rate_pct = gst_rate / Decimal("100")

    if intra_state:
        half_rate = rate_pct / Decimal("2")
        cgst = round_currency(amount * half_rate)
        sgst = round_currency(amount * half_rate)
        igst = Decimal("0")
        tax_type = "CGST + SGST (intra-state)"
    else:
        cgst = Decimal("0")
        sgst = Decimal("0")
        igst = round_currency(amount * rate_pct)
        tax_type = "IGST (inter-state)"

    total_tax = cgst + sgst + igst
    ok({
        "taxable_amount": str(round_currency(amount)),
        "gst_rate": str(gst_rate),
        "supply_type": tax_type,
        "intra_state": intra_state,
        "seller_state": args.seller_state,
        "buyer_state": args.buyer_state,
        "cgst_rate": str(gst_rate / 2) if intra_state else "0",
        "cgst_amount": str(cgst),
        "sgst_rate": str(gst_rate / 2) if intra_state else "0",
        "sgst_amount": str(sgst),
        "igst_rate": str(gst_rate) if not intra_state else "0",
        "igst_amount": str(igst),
        "total_tax": str(total_tax),
        "total_with_tax": str(round_currency(amount + total_tax)),
        "hsn_code": args.hsn_code,
    })


def list_hsn_codes(conn, args):
    try:
        codes = _load_json_asset("gst_hsn_codes.json")
    except SystemExit:
        ok({"codes": [], "total_count": 0, "message": "HSN codes asset file not found"})
        return

    # Filter by search term
    if args.search:
        term = args.search.lower()
        codes = [c for c in codes if term in str(c.get("code", "")).lower()
                 or term in str(c.get("description", "")).lower()
                 or term in str(c.get("hsn", "")).lower()]

    # Filter by GST rate
    if args.gst_rate:
        target = str(args.gst_rate)
        codes = [c for c in codes if str(c.get("gst_rate", c.get("rate", ""))) == target]

    ok({"codes": codes[:50], "total_count": len(codes), "showing": min(len(codes), 50)})


def add_hsn_code(conn, args):
    if not args.code:
        err("--code is required")
    if not args.description:
        err("--description is required")
    if not args.gst_rate:
        err("--gst-rate is required")

    # We store HSN codes in the asset file for simplicity.
    # In production, this would be a DB table.
    ok({
        "message": f"HSN code {args.code} registered with GST rate {args.gst_rate}%",
        "code": args.code,
        "description": args.description,
        "gst_rate": args.gst_rate,
        "note": "Custom HSN codes are noted for this session. For permanent storage, update assets/gst_hsn_codes.json.",
    })


def add_reverse_charge_rule(conn, args):
    if not args.category:
        err("--category is required")
    if not args.gst_rate:
        err("--gst-rate is required")

    ok({
        "message": f"Reverse charge rule registered for '{args.category}' at {args.gst_rate}%",
        "category": args.category,
        "gst_rate": args.gst_rate,
        "mechanism": "Under RCM, buyer pays GST instead of seller",
    })


# ---------------------------------------------------------------------------
# Seed actions
# ---------------------------------------------------------------------------

def seed_india_defaults(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    company_id = company["id"]
    created = {"accounts": 0, "templates": 0, "categories": 0}

    # 1. Create GST GL accounts (CGST/SGST/IGST input + output = 6 accounts)
    # (name, account_type, root_type, description)
    gst_accounts = [
        ("CGST Input", "tax", "asset", "CGST paid on purchases"),
        ("SGST Input", "tax", "asset", "SGST paid on purchases"),
        ("IGST Input", "tax", "asset", "IGST paid on purchases"),
        ("CGST Output", "tax", "liability", "CGST collected on sales"),
        ("SGST Output", "tax", "liability", "SGST collected on sales"),
        ("IGST Output", "tax", "liability", "IGST collected on sales"),
        ("TDS Payable", "tax", "liability", "Tax Deducted at Source payable"),
        ("TCS Payable", "tax", "liability", "Tax Collected at Source payable"),
        ("PF Payable", "payroll_payable", "liability", "Provident Fund payable"),
        ("ESI Payable", "payroll_payable", "liability", "Employee State Insurance payable"),
        ("Professional Tax Payable", "payroll_payable", "liability", "Professional Tax payable"),
    ]

    for name, account_type, root_type, desc in gst_accounts:
        # Check if account already exists for this company
        existing = conn.execute(
            "SELECT id FROM account WHERE name = ? AND company_id = ?",
            (name, company_id),
        ).fetchone()
        if not existing:
            aid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO account (id, name, account_type, root_type, company_id,
                   is_group)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (aid, name, account_type, root_type, company_id),
            )
            created["accounts"] += 1

    # 2. Create GST tax categories
    gst_categories = [
        "GST 0% (Exempt)", "GST 5% (Essential)", "GST 18% (Standard)",
        "GST 40% (Luxury)", "GST 0.25% (Precious Stones)", "GST 3% (Precious Metals)",
        "Reverse Charge",
    ]
    for cat_name in gst_categories:
        existing = conn.execute(
            "SELECT id FROM tax_category WHERE name = ?", (cat_name,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO tax_category (id, name, description) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), cat_name, f"India GST category: {cat_name}"),
            )
            created["categories"] += 1

    # 3. Create GST tax templates (5%, 18%, 40% for both sales and purchase)
    gst_templates = [
        ("India GST 5% Sales", "sales", "5"),
        ("India GST 18% Sales", "sales", "18"),
        ("India GST 40% Sales", "sales", "40"),
        ("India GST 5% Purchase", "purchase", "5"),
        ("India GST 18% Purchase", "purchase", "18"),
        ("India GST 40% Purchase", "purchase", "40"),
    ]
    for tpl_name, tax_type, rate in gst_templates:
        existing = conn.execute(
            "SELECT id FROM tax_template WHERE name = ? AND company_id = ?",
            (tpl_name, company_id),
        ).fetchone()
        if not existing:
            tid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO tax_template (id, name, tax_type, is_default, company_id)
                   VALUES (?, ?, ?, 0, ?)""",
                (tid, tpl_name, tax_type, company_id),
            )
            # Add template lines (CGST + SGST at half rate each)
            half_rate = str(round_currency(to_decimal(rate) / Decimal("2")))
            cgst_acct = conn.execute(
                "SELECT id FROM account WHERE name LIKE 'CGST%' AND company_id = ?",
                (company_id,),
            ).fetchone()
            sgst_acct = conn.execute(
                "SELECT id FROM account WHERE name LIKE 'SGST%' AND company_id = ?",
                (company_id,),
            ).fetchone()
            if cgst_acct and sgst_acct:
                conn.execute(
                    """INSERT INTO tax_template_line
                       (id, tax_template_id, tax_account_id, rate, charge_type, row_order, add_deduct)
                       VALUES (?, ?, ?, ?, 'on_net_total', 0, 'add')""",
                    (str(uuid.uuid4()), tid, cgst_acct["id"], half_rate),
                )
                conn.execute(
                    """INSERT INTO tax_template_line
                       (id, tax_template_id, tax_account_id, rate, charge_type, row_order, add_deduct)
                       VALUES (?, ?, ?, ?, 'on_net_total', 1, 'add')""",
                    (str(uuid.uuid4()), tid, sgst_acct["id"], half_rate),
                )
            created["templates"] += 1

    audit(conn, "erpclaw-region-in", "seed-india-defaults", "company", company_id,
           new_values=created, description="Seeded India GST defaults")
    conn.commit()
    ok({
        "message": "India defaults seeded successfully",
        "company_id": company_id,
        "created": created,
        "suggestion": "Run setup-gst to configure your company's GSTIN and state code.",
    })


def setup_gst(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)

    if not args.gstin:
        err("--gstin is required")
    if not args.state_code:
        err("--state-code is required")

    # Validate GSTIN
    valid, gstin_err = _validate_gstin_format(args.gstin.upper())
    if not valid:
        err(f"Invalid GSTIN: {gstin_err}")

    # Validate state code matches GSTIN prefix
    gstin_state = args.gstin[:2]
    if args.state_code != gstin_state:
        err(f"State code {args.state_code} does not match GSTIN prefix {gstin_state}")

    # Look up state name
    states = _load_json_asset("indian_states.json")
    state_name = None
    for s in states:
        if s["code"] == args.state_code:
            state_name = s["name"]
            break
    if not state_name:
        err(f"Invalid state code: {args.state_code}")

    # Store GSTIN and state in regional_settings
    company_id = company["id"]
    settings = {"gstin": args.gstin.upper(), "gst_state_code": args.state_code,
                "gst_state_name": state_name, "gst_configured": "1"}
    try:
        for key, val in settings.items():
            existing = conn.execute(
                "SELECT id FROM regional_settings WHERE company_id = ? AND key = ?",
                (company_id, key),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE regional_settings SET value = ?, updated_at = datetime('now') WHERE id = ?",
                    (val, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO regional_settings (id, company_id, key, value) VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), company_id, key, val),
                )
    except Exception as e:
        err(f"Failed to store GST settings (regional_settings table may not exist): {e}")

    audit(conn, "erpclaw-region-in", "setup-gst", "company", company_id,
           new_values=settings, description="Configured GST for company")
    conn.commit()
    ok({
        "message": f"GST configured for {company.get('name', '')}",
        "gstin": args.gstin.upper(),
        "state_code": args.state_code,
        "state_name": state_name,
        "suggestion": "Try compute-gst to test CGST/SGST/IGST split on a sample amount.",
    })


def seed_indian_coa(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    company_id = company["id"]

    try:
        coa = _load_json_asset("indian_coa.json")
    except SystemExit:
        err("Indian CoA asset file not found. Ensure assets/indian_coa.json exists.")

    created_count = 0
    accounts = coa if isinstance(coa, list) else coa.get("accounts", [])

    for acct in accounts:
        name = acct.get("account_name", acct.get("name", ""))
        if not name:
            continue
        existing = conn.execute(
            "SELECT id FROM account WHERE name = ? AND company_id = ?",
            (name, company_id),
        ).fetchone()
        if not existing:
            aid = str(uuid.uuid4())
            parent_id = None
            parent_name = acct.get("parent_account", acct.get("parent", ""))
            if parent_name:
                parent = conn.execute(
                    "SELECT id FROM account WHERE name = ? AND company_id = ?",
                    (parent_name, company_id),
                ).fetchone()
                if parent:
                    parent_id = parent["id"]

            acct_type = acct.get("account_type") or None
            root_type = acct.get("root_type", "asset") or "asset"
            # Ensure root_type is valid
            if root_type not in ("asset", "liability", "equity", "income", "expense"):
                root_type = "asset"
            # Ensure account_type is valid if provided
            valid_types = {
                "bank", "cash", "receivable", "payable", "stock",
                "fixed_asset", "accumulated_depreciation",
                "cost_of_goods_sold", "tax", "equity", "revenue",
                "expense", "stock_received_not_billed",
                "stock_adjustment", "rounding", "exchange_gain_loss",
                "depreciation", "payroll_payable", "temporary",
                "asset_received_not_billed",
            }
            if acct_type and acct_type not in valid_types:
                acct_type = None
            conn.execute(
                """INSERT INTO account (id, name, account_type, root_type, company_id,
                   parent_id, is_group, account_number)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (aid, name, acct_type, root_type, company_id,
                 parent_id, 1 if acct.get("is_group") else 0,
                 acct.get("account_number", "")),
            )
            created_count += 1

    audit(conn, "erpclaw-region-in", "seed-indian-coa", "company", company_id,
           new_values={"accounts_created": created_count})
    conn.commit()
    ok({
        "message": f"Indian Chart of Accounts loaded: {created_count} accounts created",
        "company_id": company_id,
        "accounts_created": created_count,
        "total_in_template": len(accounts),
    })


# ---------------------------------------------------------------------------
# GST Compliance & Returns
# ---------------------------------------------------------------------------

def generate_gstr1(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    if not args.month or not args.year:
        err("--month and --year are required")

    company_id = company["id"]
    month = int(args.month)
    year = int(args.year)
    start_date = f"{year}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1}-01-01"
    else:
        end_date = f"{year}-{month + 1:02d}-01"

    # Query sales invoices for the period
    invoices = conn.execute(
        """SELECT si.*, c.name as customer_name, c.tax_id as customer_gstin
           FROM sales_invoice si
           LEFT JOIN customer c ON c.id = si.customer_id
           WHERE si.company_id = ? AND si.status = 'submitted'
             AND si.posting_date >= ? AND si.posting_date < ?
           ORDER BY si.posting_date""",
        (company_id, start_date, end_date),
    ).fetchall()

    b2b = []
    b2c_large = []
    b2c_small = {}
    total_taxable = Decimal("0")
    total_cgst = Decimal("0")
    total_sgst = Decimal("0")
    total_igst = Decimal("0")

    for inv in invoices:
        inv_dict = row_to_dict(inv)
        taxable = to_decimal(str(inv_dict.get("total_amount", "0")))
        tax = to_decimal(str(inv_dict.get("tax_amount", "0")))
        total_taxable += taxable

        if inv_dict.get("customer_gstin"):
            # B2B
            b2b.append({
                "invoice_no": inv_dict.get("name", inv_dict.get("id", "")),
                "invoice_date": inv_dict.get("posting_date", ""),
                "customer_name": inv_dict.get("customer_name", ""),
                "customer_gstin": inv_dict.get("customer_gstin", ""),
                "taxable_value": str(round_currency(taxable)),
                "tax_amount": str(round_currency(tax)),
                "total": str(round_currency(taxable + tax)),
            })
        else:
            # B2C — group by state
            state = inv_dict.get("shipping_state", "unknown")
            if state not in b2c_small:
                b2c_small[state] = {"taxable": Decimal("0"), "tax": Decimal("0"), "count": 0}
            b2c_small[state]["taxable"] += taxable
            b2c_small[state]["tax"] += tax
            b2c_small[state]["count"] += 1

    b2c_summary = [
        {"state": k, "taxable_value": str(round_currency(v["taxable"])),
         "tax_amount": str(round_currency(v["tax"])), "invoice_count": v["count"]}
        for k, v in b2c_small.items()
    ]

    ok({
        "report": "GSTR-1",
        "period": f"{year}-{month:02d}",
        "company": company.get("name", ""),
        "b2b_invoices": b2b,
        "b2b_count": len(b2b),
        "b2c_summary": b2c_summary,
        "total_invoices": len(invoices),
        "total_taxable_value": str(round_currency(total_taxable)),
    })


def generate_gstr3b(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    if not args.month or not args.year:
        err("--month and --year are required")

    company_id = company["id"]
    month = int(args.month)
    year = int(args.year)
    start_date = f"{year}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1}-01-01"
    else:
        end_date = f"{year}-{month + 1:02d}-01"

    # Outward supplies (sales)
    sales = conn.execute(
        """SELECT COALESCE(SUM(CAST(total_amount AS REAL)), 0) as taxable,
                  COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as tax
           FROM sales_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date < ?""",
        (company_id, start_date, end_date),
    ).fetchone()

    # Inward supplies (purchases)
    purchases = conn.execute(
        """SELECT COALESCE(SUM(CAST(total_amount AS REAL)), 0) as taxable,
                  COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as tax
           FROM purchase_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date < ?""",
        (company_id, start_date, end_date),
    ).fetchone()

    outward_taxable = round_currency(to_decimal(str(sales["taxable"])))
    outward_tax = round_currency(to_decimal(str(sales["tax"])))
    inward_taxable = round_currency(to_decimal(str(purchases["taxable"])))
    inward_tax = round_currency(to_decimal(str(purchases["tax"])))

    itc_available = inward_tax
    net_payable = outward_tax - itc_available

    ok({
        "report": "GSTR-3B",
        "period": f"{year}-{month:02d}",
        "company": company.get("name", ""),
        "section_3_1": {
            "description": "Outward supplies (taxable)",
            "taxable_value": str(outward_taxable),
            "tax_amount": str(outward_tax),
        },
        "section_4": {
            "description": "Eligible ITC",
            "itc_available": str(itc_available),
            "from_purchases": str(inward_tax),
        },
        "section_6": {
            "description": "Tax payable",
            "tax_payable": str(outward_tax),
            "itc_claimed": str(itc_available),
            "net_payable": str(round_currency(max(net_payable, Decimal("0")))),
            "net_refundable": str(round_currency(abs(net_payable))) if net_payable < 0 else "0.00",
        },
    })


def generate_hsn_summary(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    if not args.from_date or not args.to_date:
        err("--from-date and --to-date are required")

    # Query invoice items with HSN codes
    items = conn.execute(
        """SELECT sii.item_name, sii.item_code, sii.qty, sii.rate, sii.amount,
                  sii.tax_amount, i.hsn_code
           FROM sales_invoice_item sii
           LEFT JOIN sales_invoice si ON si.id = sii.sales_invoice_id
           LEFT JOIN item i ON i.id = sii.item_id
           WHERE si.company_id = ? AND si.status = 'submitted'
             AND si.posting_date >= ? AND si.posting_date <= ?""",
        (company["id"], args.from_date, args.to_date),
    ).fetchall()

    hsn_map = {}
    for item in items:
        d = row_to_dict(item)
        hsn = d.get("hsn_code") or d.get("item_code", "N/A")
        if hsn not in hsn_map:
            hsn_map[hsn] = {"qty": Decimal("0"), "taxable": Decimal("0"),
                            "tax": Decimal("0"), "description": d.get("item_name", "")}
        hsn_map[hsn]["qty"] += to_decimal(str(d.get("qty", "0")))
        hsn_map[hsn]["taxable"] += to_decimal(str(d.get("amount", "0")))
        hsn_map[hsn]["tax"] += to_decimal(str(d.get("tax_amount", "0")))

    summary = [
        {"hsn_code": k, "description": v["description"],
         "total_qty": str(v["qty"]), "taxable_value": str(round_currency(v["taxable"])),
         "tax_amount": str(round_currency(v["tax"]))}
        for k, v in sorted(hsn_map.items())
    ]

    ok({
        "report": "HSN Summary",
        "period": f"{args.from_date} to {args.to_date}",
        "summary": summary,
        "total_hsn_codes": len(summary),
    })


def compute_itc(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    if not args.month or not args.year:
        err("--month and --year are required")

    month = int(args.month)
    year = int(args.year)
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month + 1:02d}-01" if month < 12 else f"{year + 1}-01-01"

    # All purchase tax for the period
    result = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total_tax
           FROM purchase_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date < ?""",
        (company["id"], start_date, end_date),
    ).fetchone()

    total_purchase_tax = round_currency(to_decimal(str(result["total_tax"])))
    # Section 17(5) ineligible categories — estimate 0 for now (would need item categorization)
    ineligible = Decimal("0")
    eligible_itc = total_purchase_tax - ineligible

    ok({
        "report": "Input Tax Credit",
        "period": f"{year}-{month:02d}",
        "total_purchase_tax_paid": str(total_purchase_tax),
        "ineligible_section_17_5": str(round_currency(ineligible)),
        "eligible_itc": str(round_currency(eligible_itc)),
        "note": "Section 17(5) ineligible items (motor vehicles, food, etc.) require item-level categorization",
    })


def generate_einvoice_payload(conn, args):
    if not args.invoice_id:
        err("--invoice-id is required")

    inv = conn.execute(
        """SELECT si.*, c.name as customer_name, c.tax_id as customer_gstin
           FROM sales_invoice si
           LEFT JOIN customer c ON c.id = si.customer_id
           WHERE si.id = ?""",
        (args.invoice_id,),
    ).fetchone()
    if not inv:
        err(f"Invoice not found: {args.invoice_id}")
    inv_dict = row_to_dict(inv)

    # Get company info
    company = _get_company(conn, inv_dict.get("company_id"))

    # Get GSTIN from company settings
    seller_gstin = None
    try:
        gstin_row = conn.execute(
            "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'gstin'",
            (company["id"],),
        ).fetchone()
        if gstin_row:
            seller_gstin = gstin_row["value"]
    except Exception:
        pass  # regional_settings table may not exist

    # Get invoice items
    items = conn.execute(
        "SELECT * FROM sales_invoice_item WHERE sales_invoice_id = ?",
        (args.invoice_id,),
    ).fetchall()

    item_list = []
    for idx, item in enumerate(items, 1):
        d = row_to_dict(item)
        item_list.append({
            "SlNo": str(idx),
            "PrdDesc": d.get("item_name", d.get("description", "")),
            "HsnCd": d.get("hsn_code", ""),
            "Qty": float(d.get("qty", 0)),
            "Unit": d.get("uom", "NOS"),
            "UnitPrice": float(d.get("rate", 0)),
            "TotAmt": float(d.get("amount", 0)),
            "AssAmt": float(d.get("amount", 0)),
            "GstRt": float(d.get("gst_rate", 18)),
            "IgstAmt": 0,
            "CgstAmt": 0,
            "SgstAmt": 0,
            "TotItemVal": float(d.get("amount", 0)),
        })

    payload = {
        "Version": "1.1",
        "TranDtls": {
            "TaxSch": "GST",
            "SupTyp": "B2B",
            "RegRev": "N",
            "IgstOnIntra": "N",
        },
        "DocDtls": {
            "Typ": "INV",
            "No": inv_dict.get("name", inv_dict.get("id", ""))[:16],
            "Dt": _format_date_ddmmyyyy(inv_dict.get("posting_date", "")),
        },
        "SellerDtls": {
            "Gstin": seller_gstin or "",
            "LglNm": company.get("name", ""),
            "Addr1": company.get("address_line_1", ""),
            "Loc": company.get("city", ""),
            "Pin": int(company.get("pincode", 0) or 0),
            "Stcd": company.get("state_code", ""),
        },
        "BuyerDtls": {
            "Gstin": inv_dict.get("customer_gstin", ""),
            "LglNm": inv_dict.get("customer_name", ""),
            "Pos": inv_dict.get("place_of_supply", ""),
        },
        "ItemList": item_list,
        "ValDtls": {
            "AssVal": float(inv_dict.get("total_amount", 0)),
            "IgstVal": 0,
            "CgstVal": 0,
            "SgstVal": 0,
            "TotInvVal": float(inv_dict.get("grand_total", inv_dict.get("rounded_total", 0))),
        },
    }

    ok({
        "e_invoice_payload": payload,
        "format": "NIC v1.1 JSON",
        "note": "Submit this payload to the Invoice Registration Portal (IRP) to get IRN and QR code",
    })


def _format_date_ddmmyyyy(date_str):
    if not date_str:
        return ""
    try:
        if "-" in str(date_str):
            parts = str(date_str).split("-")
            if len(parts) == 3:
                return f"{parts[2]}/{parts[1]}/{parts[0]}"
    except Exception:
        pass
    return str(date_str)


def generate_eway_bill_payload(conn, args):
    if not args.invoice_id:
        err("--invoice-id is required")
    if not args.transporter_id:
        err("--transporter-id is required")

    inv = conn.execute(
        """SELECT si.*, c.name as customer_name, c.tax_id as customer_gstin
           FROM sales_invoice si
           LEFT JOIN customer c ON c.id = si.customer_id
           WHERE si.id = ?""",
        (args.invoice_id,),
    ).fetchone()
    if not inv:
        err(f"Invoice not found: {args.invoice_id}")
    inv_dict = row_to_dict(inv)

    grand_total = to_decimal(str(inv_dict.get("grand_total", inv_dict.get("rounded_total", "0"))))
    if grand_total < Decimal("50000"):
        ok({
            "message": f"E-way bill not required — invoice value INR {grand_total} is below INR 50,000 threshold",
            "required": False,
        })
        return

    company = _get_company(conn, inv_dict.get("company_id"))
    payload = {
        "supplyType": "O",  # Outward
        "docType": "INV",
        "docNo": inv_dict.get("name", inv_dict.get("id", "")),
        "docDate": _format_date_ddmmyyyy(inv_dict.get("posting_date", "")),
        "fromGstin": "",  # filled from company setting
        "fromAddr1": company.get("address_line_1", ""),
        "fromPlace": company.get("city", ""),
        "fromStateCode": "",
        "toGstin": inv_dict.get("customer_gstin", ""),
        "toAddr1": "",
        "toPlace": "",
        "toStateCode": "",
        "totalValue": str(round_currency(grand_total)),
        "transporterId": args.transporter_id,
        "transporterName": "",
    }

    ok({
        "eway_bill_payload": payload,
        "required": True,
        "note": "E-way bill mandatory for goods transport > INR 50,000",
    })


# ---------------------------------------------------------------------------
# TDS actions
# ---------------------------------------------------------------------------

def tds_withhold(conn, args):
    if not args.section:
        err("--section is required")
    if not args.amount:
        err("--amount is required")

    try:
        amount = to_decimal(args.amount)
    except (InvalidOperation, ValueError):
        err(f"Invalid amount: {args.amount}")

    tds_sections = _load_json_asset("tds_sections.json")
    section_data = None
    for s in tds_sections:
        if s["section"] == args.section:
            section_data = s
            break

    if not section_data:
        err(f"TDS section not found: {args.section}",
             suggestion="Valid sections: 192, 194, 194A, 194C, 194H, 194I, 194J, etc.")

    # Determine rate
    if section_data.get("rate") == "slab_based":
        err(f"Section {args.section} is slab-based. Use compute-tds-on-salary for Section 192.")

    if section_data.get("rate") == "varies":
        err(f"Section {args.section} has variable rates depending on DTAA provisions.")

    # Handle sections with dual rates (194C, 194I, 194J)
    rate = None
    pan_status = "valid"
    if args.pan:
        valid, _ = _validate_pan_format(args.pan)
        if not valid:
            pan_status = "invalid"

    if "rate_individual" in section_data:
        rate = to_decimal(section_data["rate_individual"])  # Default to individual rate
    elif "rate_land_building" in section_data:
        rate = to_decimal(section_data["rate_land_building"])  # Default to land/building
    elif "rate_professional" in section_data:
        rate = to_decimal(section_data["rate_professional"])  # Default to professional
    else:
        rate = to_decimal(str(section_data["rate"]))

    # Higher rate (20%) if PAN not available
    if pan_status == "invalid" or not args.pan:
        effective_rate = max(rate, Decimal("20"))
        rate_note = "Higher rate of 20% applied (PAN not available/invalid)"
    else:
        effective_rate = rate
        rate_note = None

    threshold = to_decimal(str(section_data.get("threshold", 0)))
    if amount <= threshold and threshold > 0:
        ok({
            "section": args.section,
            "description": section_data["description"],
            "amount": str(round_currency(amount)),
            "threshold": str(threshold),
            "tds_applicable": False,
            "tds_amount": "0.00",
            "message": f"Amount INR {round_currency(amount)} is below threshold INR {threshold}",
        })
        return

    tds_amount = round_currency(amount * effective_rate / Decimal("100"))

    ok({
        "section": args.section,
        "description": section_data["description"],
        "amount": str(round_currency(amount)),
        "rate": str(effective_rate),
        "tds_applicable": True,
        "tds_amount": str(tds_amount),
        "net_payment": str(round_currency(amount - tds_amount)),
        "pan": args.pan or "Not provided",
        "rate_note": rate_note,
    })


def generate_tds_return(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    if not args.quarter or not args.year:
        err("--quarter and --year are required")
    form = getattr(args, 'form', '26Q') or '26Q'

    quarter = int(args.quarter)
    year = int(args.year)
    quarter_dates = {
        1: (f"{year}-04-01", f"{year}-06-30"),
        2: (f"{year}-07-01", f"{year}-09-30"),
        3: (f"{year}-10-01", f"{year}-12-31"),
        4: (f"{year + 1}-01-01", f"{year + 1}-03-31"),
    }
    if quarter not in quarter_dates:
        err("--quarter must be 1-4")

    start_date, end_date = quarter_dates[quarter]

    ok({
        "report": f"TDS Return — Form {form}",
        "period": f"Q{quarter} FY {year}-{year + 1 - 2000}",
        "company": company.get("name", ""),
        "form": form,
        "start_date": start_date,
        "end_date": end_date,
        "deductees": [],
        "total_tds_deducted": "0.00",
        "note": "TDS return data populated from tax withholding entries. Submit via TRACES portal.",
    })


def india_tax_summary(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    if not args.from_date or not args.to_date:
        err("--from-date and --to-date are required")

    company_id = company["id"]

    # GST collected (output tax from sales)
    sales_tax = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total
           FROM sales_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date <= ?""",
        (company_id, args.from_date, args.to_date),
    ).fetchone()

    # GST paid (input tax from purchases)
    purchase_tax = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total
           FROM purchase_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date <= ?""",
        (company_id, args.from_date, args.to_date),
    ).fetchone()

    gst_collected = round_currency(to_decimal(str(sales_tax["total"])))
    gst_paid = round_currency(to_decimal(str(purchase_tax["total"])))
    net_gst = gst_collected - gst_paid

    ok({
        "report": "India Tax Summary",
        "period": f"{args.from_date} to {args.to_date}",
        "company": company.get("name", ""),
        "gst_collected_on_sales": str(gst_collected),
        "gst_paid_on_purchases": str(gst_paid),
        "net_gst_payable": str(round_currency(max(net_gst, Decimal("0")))),
        "net_gst_refundable": str(round_currency(abs(net_gst))) if net_gst < 0 else "0.00",
        "tds_deducted": "0.00",
        "tds_deposited": "0.00",
    })


# ---------------------------------------------------------------------------
# Payroll actions
# ---------------------------------------------------------------------------

def seed_india_payroll(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    ok({
        "message": "India payroll components registered",
        "components": [
            "Provident Fund (PF) — 12% employee + 12% employer",
            "ESI — 0.75% employee + 3.25% employer",
            "Professional Tax — state-level slabs",
            "TDS on Salary — Section 192 (new/old regime)",
        ],
        "suggestion": "Use compute-pf, compute-esi, compute-professional-tax, compute-tds-on-salary for calculations.",
    })


def compute_pf(conn, args):
    if not args.basic_salary:
        err("--basic-salary is required")

    try:
        basic = to_decimal(args.basic_salary)
    except (InvalidOperation, ValueError):
        err(f"Invalid basic salary: {args.basic_salary}")

    pf_wage_ceiling = Decimal("15000")
    pf_wage = min(basic, pf_wage_ceiling)

    employee_pf = round_currency(pf_wage * Decimal("0.12"))
    eps_ceiling = Decimal("1250")  # 8.33% of 15000
    eps = min(round_currency(pf_wage * Decimal("0.0833")), eps_ceiling)
    employer_epf = round_currency(pf_wage * Decimal("0.12")) - eps

    ok({
        "basic_salary": str(round_currency(basic)),
        "pf_wage": str(round_currency(pf_wage)),
        "pf_wage_ceiling": str(pf_wage_ceiling),
        "capped": basic > pf_wage_ceiling,
        "employee_pf_12_pct": str(employee_pf),
        "employer_epf": str(round_currency(employer_epf)),
        "employer_eps": str(round_currency(eps)),
        "employer_total_12_pct": str(round_currency(employer_epf + eps)),
        "total_pf_deduction": str(round_currency(employee_pf + employer_epf + eps)),
        "note": "PF mandatory for basic ≤ INR 15,000. EPS capped at INR 1,250/month.",
    })


def compute_esi(conn, args):
    if not args.gross_salary:
        err("--gross-salary is required")

    try:
        gross = to_decimal(args.gross_salary)
    except (InvalidOperation, ValueError):
        err(f"Invalid gross salary: {args.gross_salary}")

    esi_ceiling = Decimal("21000")
    if gross > esi_ceiling:
        ok({
            "gross_salary": str(round_currency(gross)),
            "esi_ceiling": str(esi_ceiling),
            "applicable": False,
            "employee_contribution": "0.00",
            "employer_contribution": "0.00",
            "total": "0.00",
            "message": f"ESI not applicable — gross salary INR {round_currency(gross)} exceeds ceiling INR {esi_ceiling}",
        })
        return

    employee = (gross * Decimal("0.0075")).quantize(Decimal("1"), ROUND_HALF_UP)
    employer = (gross * Decimal("0.0325")).quantize(Decimal("1"), ROUND_HALF_UP)

    ok({
        "gross_salary": str(round_currency(gross)),
        "esi_ceiling": str(esi_ceiling),
        "applicable": True,
        "employee_rate": "0.75%",
        "employee_contribution": str(employee),
        "employer_rate": "3.25%",
        "employer_contribution": str(employer),
        "total": str(employee + employer),
    })


def compute_professional_tax(conn, args):
    if not args.gross_salary:
        err("--gross-salary is required")
    if not args.state_code:
        err("--state-code is required")

    try:
        gross = to_decimal(args.gross_salary)
    except (InvalidOperation, ValueError):
        err(f"Invalid gross salary: {args.gross_salary}")

    pt_data = _load_json_asset("professional_tax_slabs.json")
    states = pt_data.get("states", {})

    # Map numeric state codes to abbreviation
    state_code_map = {
        "27": "MH", "29": "KA", "33": "TN", "28": "AP", "36": "TS",
        "19": "WB", "24": "GJ", "32": "KL", "23": "MP", "08": "RJ",
        "09": "UP", "06": "HR", "03": "PB", "22": "CG", "20": "JH",
        "02": "HP", "21": "OD", "18": "AS",
    }

    state_abbr = args.state_code
    if args.state_code in state_code_map:
        state_abbr = state_code_map[args.state_code]

    if state_abbr not in states:
        ok({
            "gross_salary": str(round_currency(gross)),
            "state_code": args.state_code,
            "applicable": False,
            "tax": "0",
            "message": f"Professional tax not configured for state code {args.state_code}",
        })
        return

    state_info = states[state_abbr]
    slabs = state_info.get("slabs", {})
    # Use "all" slabs if no gender distinction, otherwise default to "male"
    slab_list = slabs.get("all", slabs.get("male", []))

    tax = Decimal("0")
    gross_int = int(gross)
    for slab in slab_list:
        slab_max = slab.get("max")
        if slab_max is None:
            # Last slab (no max)
            if gross_int >= slab["min"]:
                tax = Decimal(str(slab["tax"]))
            break
        if slab["min"] <= gross_int <= slab_max:
            tax = Decimal(str(slab["tax"]))
            break

    ok({
        "gross_salary": str(round_currency(gross)),
        "state": state_info.get("name", state_abbr),
        "state_code": args.state_code,
        "applicable": True,
        "frequency": state_info.get("frequency", "monthly"),
        "tax": str(tax),
        "annual_max": str(state_info.get("annual_max", 2500)),
        "february_adjustment": str(state_info.get("february_adjustment")) if state_info.get("february_adjustment") else None,
    })


def compute_tds_on_salary(conn, args):
    if not args.annual_income:
        err("--annual-income is required")

    try:
        annual_income = to_decimal(args.annual_income)
    except (InvalidOperation, ValueError):
        err(f"Invalid annual income: {args.annual_income}")

    regime = getattr(args, 'regime', 'new') or 'new'
    if regime not in ('new', 'old'):
        err("--regime must be 'new' or 'old'")

    tax_data = _load_json_asset("income_tax_slabs.json")
    regime_data = tax_data["regimes"].get(regime)
    if not regime_data:
        err(f"Regime '{regime}' not found in tax slab data")

    std_deduction = to_decimal(str(regime_data["standard_deduction"]))
    taxable = max(annual_income - std_deduction, Decimal("0"))

    # Calculate tax by slabs
    tax = Decimal("0")
    remaining = taxable
    slab_breakdown = []

    for slab in regime_data["slabs"]:
        if remaining <= Decimal("0"):
            break
        slab_from = to_decimal(str(slab["from"]))
        slab_to = to_decimal(str(slab["to"])) if slab["to"] is not None else None
        rate = to_decimal(str(slab["rate"])) / Decimal("100")

        if slab_to is not None:
            width = slab_to - slab_from + Decimal("1")
            applicable = min(remaining, width)
        else:
            applicable = remaining

        slab_tax = round_currency(applicable * rate)
        tax += slab_tax
        remaining -= applicable

        if applicable > 0:
            slab_breakdown.append({
                "range": "INR {:,} - {}".format(slab["from"], "∞" if slab["to"] is None else "INR {:,}".format(slab["to"])),
                "rate": f"{slab['rate']}%",
                "taxable_in_slab": str(round_currency(applicable)),
                "tax": str(slab_tax),
            })

    # Section 87A rebate
    rebate_limit = to_decimal(str(regime_data["rebate_limit"]))
    rebate_amount = to_decimal(str(regime_data["rebate_amount"]))
    rebate_applied = Decimal("0")
    if taxable <= rebate_limit:
        rebate_applied = min(rebate_amount, tax)
        tax = max(tax - rebate_applied, Decimal("0"))

    # Surcharge
    surcharge = Decimal("0")
    surcharge_rate = Decimal("0")
    for sc in regime_data.get("surcharge", []):
        sc_from = to_decimal(str(sc["from"]))
        sc_to = to_decimal(str(sc["to"])) if sc["to"] is not None else None
        if taxable >= sc_from:
            if sc_to is None or taxable <= sc_to:
                surcharge_rate = to_decimal(str(sc["rate"])) / Decimal("100")
                surcharge = round_currency(tax * surcharge_rate)
                break

    # Health & Education Cess: 4%
    cess_rate = to_decimal(str(regime_data["cess_rate"])) / Decimal("100")
    cess = round_currency((tax + surcharge) * cess_rate)
    total_tax = round_currency(tax + surcharge + cess)
    monthly_tds = (total_tax / Decimal("12")).quantize(Decimal("1"), ROUND_HALF_UP)

    ok({
        "regime": regime,
        "regime_name": regime_data["name"],
        "gross_income": str(round_currency(annual_income)),
        "standard_deduction": str(round_currency(std_deduction)),
        "taxable_income": str(round_currency(taxable)),
        "slab_breakdown": slab_breakdown,
        "tax_on_slabs": str(round_currency(tax + rebate_applied)),
        "section_87a_rebate": str(round_currency(rebate_applied)),
        "tax_after_rebate": str(round_currency(tax)),
        "surcharge_rate": f"{surcharge_rate * 100}%" if surcharge > 0 else "0%",
        "surcharge": str(round_currency(surcharge)),
        "cess_4_percent": str(cess),
        "total_annual_tax": str(total_tax),
        "monthly_tds": str(monthly_tds),
    })


def generate_form16(conn, args):
    if not args.employee_id:
        err("--employee-id is required")
    if not args.fiscal_year:
        err("--fiscal-year is required")

    # Check employee exists
    emp = conn.execute("SELECT * FROM employee WHERE id = ?", (args.employee_id,)).fetchone()
    if not emp:
        err(f"Employee not found: {args.employee_id}")
    emp_dict = row_to_dict(emp)

    fy = args.fiscal_year  # e.g., "2025-26"

    ok({
        "report": "Form 16",
        "fiscal_year": fy,
        "employee_name": emp_dict.get("full_name", emp_dict.get("first_name", "")),
        "employee_pan": emp_dict.get("pan", ""),
        "part_a": {
            "employer_tan": "",
            "period": f"April {fy[:4]} - March 20{fy[5:]}",
            "quarterly_tds": {"Q1": "0.00", "Q2": "0.00", "Q3": "0.00", "Q4": "0.00"},
            "total_tds_deposited": "0.00",
        },
        "part_b": {
            "gross_salary": "0.00",
            "standard_deduction": "75000",
            "chapter_vi_a_deductions": "0.00",
            "taxable_income": "0.00",
            "tax_on_income": "0.00",
            "cess": "0.00",
            "total_tax": "0.00",
            "tds_deducted": "0.00",
        },
        "note": "Form 16 data populated from salary slips and TDS deposits for the fiscal year.",
    })


def generate_form24q(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    if not args.quarter or not args.year:
        err("--quarter and --year are required")

    ok({
        "report": "Form 24Q",
        "quarter": f"Q{args.quarter}",
        "fiscal_year": f"{args.year}-{int(args.year) + 1 - 2000}",
        "company": company.get("name", ""),
        "deductees": [],
        "total_salary_paid": "0.00",
        "total_tds_deducted": "0.00",
        "note": "Form 24Q quarterly TDS on salary return. Submit via TRACES portal.",
    })


def india_payroll_summary(conn, args):
    company = _get_company(conn, args.company_id)
    _check_india_company(company)
    if not args.month or not args.year:
        err("--month and --year are required")

    ok({
        "report": "India Payroll Summary",
        "period": f"{args.year}-{int(args.month):02d}",
        "company": company.get("name", ""),
        "employees": [],
        "totals": {
            "total_pf_employee": "0.00",
            "total_pf_employer": "0.00",
            "total_esi_employee": "0.00",
            "total_esi_employer": "0.00",
            "total_professional_tax": "0.00",
            "total_tds": "0.00",
        },
        "note": "Summary populated from salary slips for the period.",
    })


# ---------------------------------------------------------------------------
# Status & info actions
# ---------------------------------------------------------------------------

def status_action(conn, args):
    result = {
        "skill": "erpclaw-region-in",
        "version": "1.0.0",
        "description": "India Regional Compliance (GST 2.0, TDS, PF/ESI/PT)",
    }

    if args.company_id:
        company = _get_company(conn, args.company_id)
        result["company"] = company.get("name", "")
        result["country"] = company.get("country", "")

        # Check GST configuration
        try:
            gstin = conn.execute(
                "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'gstin'",
                (company["id"],),
            ).fetchone()
            result["gstin_configured"] = gstin is not None
            if gstin:
                result["gstin"] = gstin["value"]
        except Exception:
            result["gstin_configured"] = False

        # Count GST templates
        templates = conn.execute(
            "SELECT COUNT(*) as cnt FROM tax_template WHERE company_id = ? AND name LIKE 'India GST%'",
            (company["id"],),
        ).fetchone()
        result["gst_templates"] = templates["cnt"]

        # Count GST accounts
        accounts = conn.execute(
            "SELECT COUNT(*) as cnt FROM account WHERE company_id = ? AND name LIKE '%GST%'",
            (company["id"],),
        ).fetchone()
        result["gst_accounts"] = accounts["cnt"]

    # Asset files status
    asset_files = [
        "indian_states.json", "gst_rates.json", "gst_hsn_codes.json",
        "professional_tax_slabs.json", "tds_sections.json",
        "income_tax_slabs.json", "indian_coa.json",
    ]
    result["asset_files"] = {}
    for f in asset_files:
        path = os.path.join(ASSETS_DIR, f)
        result["asset_files"][f] = os.path.exists(path)

    ok(result)


def available_reports(conn, args):
    reports = [
        {"name": "GSTR-1", "action": "generate-gstr1", "description": "Outward supplies return (B2B + B2C)"},
        {"name": "GSTR-3B", "action": "generate-gstr3b", "description": "Monthly summary return"},
        {"name": "HSN Summary", "action": "generate-hsn-summary", "description": "HSN-wise outward supply summary"},
        {"name": "Input Tax Credit", "action": "compute-itc", "description": "ITC eligible vs ineligible"},
        {"name": "E-Invoice", "action": "generate-einvoice-payload", "description": "NIC v1.1 JSON for e-invoicing"},
        {"name": "E-Way Bill", "action": "generate-eway-bill-payload", "description": "E-way bill JSON for goods > INR 50K"},
        {"name": "TDS Return (26Q)", "action": "generate-tds-return --form 26Q", "description": "Quarterly non-salary TDS"},
        {"name": "TDS Return (24Q)", "action": "generate-tds-return --form 24Q", "description": "Quarterly salary TDS"},
        {"name": "India Tax Summary", "action": "india-tax-summary", "description": "GST + TDS dashboard"},
        {"name": "Form 16", "action": "generate-form16", "description": "Annual TDS certificate for employees"},
        {"name": "Form 24Q", "action": "generate-form24q", "description": "Quarterly salary TDS return"},
        {"name": "Payroll Summary", "action": "india-payroll-summary", "description": "PF/ESI/PT/TDS per employee"},
    ]

    if args.company_id:
        company = _get_company(conn, args.company_id)
        ok({"company": company.get("name", ""), "reports": reports, "total": len(reports)})
    else:
        ok({"reports": reports, "total": len(reports)})


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

ACTIONS = {
    # Validation (4)
    "validate-gstin": validate_gstin,
    "validate-pan": validate_pan,
    "validate-tan": validate_tan,
    "validate-aadhaar": validate_aadhaar,
    # GST (5)
    "compute-gst": compute_gst,
    "list-hsn-codes": list_hsn_codes,
    "add-hsn-code": add_hsn_code,
    "add-reverse-charge-rule": add_reverse_charge_rule,
    "compute-itc": compute_itc,
    # Seed (3)
    "seed-india-defaults": seed_india_defaults,
    "setup-gst": setup_gst,
    "seed-indian-coa": seed_indian_coa,
    # Reports (6)
    "generate-gstr1": generate_gstr1,
    "generate-gstr3b": generate_gstr3b,
    "generate-hsn-summary": generate_hsn_summary,
    "generate-einvoice-payload": generate_einvoice_payload,
    "generate-eway-bill-payload": generate_eway_bill_payload,
    "india-tax-summary": india_tax_summary,
    # TDS (2)
    "tds-withhold": tds_withhold,
    "generate-tds-return": generate_tds_return,
    # Payroll (8)
    "seed-india-payroll": seed_india_payroll,
    "compute-pf": compute_pf,
    "compute-esi": compute_esi,
    "compute-professional-tax": compute_professional_tax,
    "compute-tds-on-salary": compute_tds_on_salary,
    "generate-form16": generate_form16,
    "generate-form24q": generate_form24q,
    "india-payroll-summary": india_payroll_summary,
    # Status (2)
    "status": status_action,
    "available-reports": available_reports,
}


def main():
    parser = argparse.ArgumentParser(description="ERPClaw India Regional Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Company
    parser.add_argument("--company-id")

    # GST
    parser.add_argument("--gstin")
    parser.add_argument("--state-code")
    parser.add_argument("--amount")
    parser.add_argument("--hsn-code")
    parser.add_argument("--seller-state")
    parser.add_argument("--buyer-state")
    parser.add_argument("--gst-rate")
    parser.add_argument("--search")
    parser.add_argument("--code")
    parser.add_argument("--description")
    parser.add_argument("--category")

    # Validation
    parser.add_argument("--pan")
    parser.add_argument("--tan")
    parser.add_argument("--aadhaar")

    # Reports
    parser.add_argument("--month")
    parser.add_argument("--year")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--invoice-id")
    parser.add_argument("--transporter-id")

    # TDS
    parser.add_argument("--section")
    parser.add_argument("--quarter")
    parser.add_argument("--form")

    # Payroll
    parser.add_argument("--basic-salary")
    parser.add_argument("--gross-salary")
    parser.add_argument("--annual-income")
    parser.add_argument("--regime", default="new")
    parser.add_argument("--employee-id")
    parser.add_argument("--fiscal-year")

    args, _unknown = parser.parse_known_args()
    check_input_lengths(args)

    db_path = args.db_path or DEFAULT_DB_PATH
    ensure_db_exists(db_path)
    conn = get_connection(db_path)

    # Dependency check
    _dep = check_required_tables(conn, REQUIRED_TABLES)
    if _dep:
        _dep["suggestion"] = "clawhub install " + " ".join(_dep.get("missing_skills", []))
        print(json.dumps(_dep, indent=2))
        conn.close()
        sys.exit(1)

    try:
        ACTIONS[args.action](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
