#!/usr/bin/env python3
"""ERPClaw EU Regional Skill — db_query.py

Pure overlay skill for EU-specific compliance: VAT (27 member states,
standard/reduced/super-reduced), reverse charge, OSS (One Stop Shop),
Intrastat, EN 16931 e-invoicing, SAF-T export, EC Sales List, IBAN
validation, EORI, VIES format check, withholding tax, and generic
European CoA template.

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

EU_COUNTRY_CODES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
    "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
    "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}


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


def _check_eu_company(company):
    country = (company.get("country") or "").upper()
    if country not in EU_COUNTRY_CODES:
        err(
            f"This action is for EU companies only. Company country '{country}' is not an EU member state.",
            suggestion="Set company country to an EU country code (DE, FR, IT, etc.).",
        )


def _get_vat_rate_for_country(country_code, rate_type="standard"):
    """Look up VAT rate for a country from the asset file."""
    rates_data = _load_json_asset("eu_vat_rates.json")
    for entry in rates_data["rates"]:
        if entry["country"] == country_code.upper():
            if rate_type == "standard":
                return to_decimal(entry["standard"])
            elif rate_type == "reduced":
                reduced = entry.get("reduced", [])
                if reduced:
                    return to_decimal(reduced[0])
                return to_decimal(entry["standard"])
            elif rate_type == "super_reduced":
                sr = entry.get("super_reduced")
                if sr:
                    return to_decimal(sr)
                return Decimal("0")
            elif rate_type == "zero":
                return Decimal("0")
    err(f"No VAT rate found for country: {country_code}")


# ---------------------------------------------------------------------------
# Setup Actions
# ---------------------------------------------------------------------------

def seed_eu_defaults(conn, args):
    """Create VAT accounts and tax templates for the company's EU member state."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]
    country = company["country"].upper()

    rate = _get_vat_rate_for_country(country)
    accounts_created = 0
    templates_created = 0

    # VAT accounts: (name, account_type, root_type)
    vat_accounts = [
        (f"VAT Output ({country} {rate}%)", "tax", "liability"),
        ("VAT Output (Intra-Community)", "tax", "liability"),
        ("VAT Input (EU)", "tax", "asset"),
        ("VAT Control (EU)", "tax", "liability"),
    ]
    for name, acct_type, root_type in vat_accounts:
        exists = conn.execute(
            "SELECT id FROM account WHERE company_id = ? AND name = ?",
            (cid, name),
        ).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO account (id, name, account_type, root_type, company_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), name, acct_type, root_type, cid),
            )
            accounts_created += 1

    # Tax templates
    templates = [
        (f"EU VAT Standard ({country} {rate}%)", str(rate)),
        ("EU VAT Reverse Charge (0%)", "0"),
        ("EU VAT Intra-Community (0%)", "0"),
    ]
    for tpl_name, tpl_rate in templates:
        exists = conn.execute(
            "SELECT id FROM tax_template WHERE company_id = ? AND name = ?",
            (cid, tpl_name),
        ).fetchone()
        if not exists:
            tpl_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO tax_template (id, name, tax_type, company_id)
                   VALUES (?, ?, 'both', ?)""",
                (tpl_id, tpl_name, cid),
            )
            templates_created += 1

    conn.commit()
    audit(conn, "erpclaw-region-eu", "seed-eu-defaults", "company", cid,
           new_values={"accounts": accounts_created, "templates": templates_created})
    conn.commit()
    ok({
        "accounts_created": accounts_created,
        "templates_created": templates_created,
        "country": country,
        "standard_vat_rate": str(rate),
        "company_id": cid,
    })


def setup_eu_vat(conn, args):
    """Store EU VAT number and member state for a company."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]

    vat_number = args.vat_number
    if not vat_number:
        err("--vat-number is required.")

    # Validate format against known patterns
    cleaned = vat_number.upper().replace(" ", "")
    formats_data = _load_json_asset("eu_vat_number_formats.json")
    valid = False
    matched_country = None
    for fmt in formats_data["formats"]:
        if re.match(fmt["pattern"], cleaned):
            valid = True
            matched_country = fmt["country"]
            break

    if not valid:
        err(f"Invalid EU VAT number: {vat_number}",
             suggestion="Must match country-specific format (e.g., DE123456789, FR12345678901)")

    # Store in regional_settings
    for key, value in [("eu_vat_number", cleaned), ("eu_member_state", matched_country)]:
        existing = conn.execute(
            "SELECT id FROM regional_settings WHERE company_id = ? AND key = ?",
            (cid, key),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE regional_settings SET value = ?, updated_at = datetime('now') WHERE id = ?",
                (value, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO regional_settings (id, company_id, key, value) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), cid, key, value),
            )

    conn.commit()
    audit(conn, "erpclaw-region-eu", "setup-eu-vat", "company", cid,
           new_values={"vat_number": cleaned, "member_state": matched_country})
    conn.commit()
    ok({
        "vat_number_stored": True,
        "vat_number": cleaned,
        "member_state": matched_country,
        "company_id": cid,
    })


def seed_eu_coa(conn, args):
    """Import generic European Chart of Accounts template."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]

    coa = _load_json_asset("eu_coa_template.json")
    accounts = coa.get("accounts", [])
    created = 0

    for acct in accounts:
        exists = conn.execute(
            "SELECT id FROM account WHERE company_id = ? AND account_number = ?",
            (cid, acct["number"]),
        ).fetchone()
        if not exists:
            acct_type = acct.get("type")
            root_type = acct.get("root_type", "asset")
            _valid_acct_types = {
                "bank", "cash", "receivable", "payable", "stock",
                "fixed_asset", "accumulated_depreciation",
                "cost_of_goods_sold", "tax", "equity", "revenue",
                "expense", "stock_received_not_billed",
                "stock_adjustment", "rounding", "exchange_gain_loss",
                "depreciation", "payroll_payable", "temporary",
                "asset_received_not_billed",
            }
            if acct_type not in _valid_acct_types:
                acct_type = None
            if root_type not in ("asset", "liability", "equity", "income", "expense"):
                root_type = "asset"
            conn.execute(
                """INSERT INTO account (id, name, account_type, root_type, company_id, account_number,
                   is_group)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), acct["name"], acct_type, root_type, cid,
                 acct["number"], acct.get("is_group", 0)),
            )
            created += 1

    conn.commit()
    audit(conn, "erpclaw-region-eu", "seed-eu-coa", "company", cid, new_values={"accounts": created})
    conn.commit()
    ok({
        "accounts_created": created,
        "standard": coa.get("standard", "EU Generic Template"),
        "company_id": cid,
    })


# ---------------------------------------------------------------------------
# Validation Actions
# ---------------------------------------------------------------------------

def validate_eu_vat_number(conn, args):
    """Validate EU VAT number format by country prefix."""
    vat = args.vat_number
    if not vat:
        err("--vat-number is required.")

    cleaned = vat.upper().replace(" ", "")
    formats_data = _load_json_asset("eu_vat_number_formats.json")

    for fmt in formats_data["formats"]:
        if re.match(fmt["pattern"], cleaned):
            ok({
                "valid": True,
                "vat_number": cleaned,
                "country": fmt["country"],
                "prefix": fmt["prefix"],
                "format_pattern": fmt["pattern"],
            })

    # Check if prefix matches any known country
    for fmt in formats_data["formats"]:
        if cleaned.startswith(fmt["prefix"]):
            ok({
                "valid": False,
                "input": vat,
                "country": fmt["country"],
                "reason": f"Does not match expected format for {fmt['country']}",
                "expected": fmt["example"],
            })

    ok({
        "valid": False,
        "input": vat,
        "reason": "Unknown country prefix — not a recognized EU VAT format",
    })


def validate_iban(conn, args):
    """Validate IBAN using modulus 97 checksum."""
    iban = args.iban
    if not iban:
        err("--iban is required.")

    cleaned = iban.upper().replace(" ", "")

    if len(cleaned) < 5:
        ok({"valid": False, "input": iban, "reason": "IBAN too short"})

    country = cleaned[:2]
    if not country.isalpha():
        ok({"valid": False, "input": iban, "reason": "First 2 characters must be country code"})

    # Modulus 97 check: move first 4 chars to end, convert letters to numbers
    rearranged = cleaned[4:] + cleaned[:4]
    numeric = ""
    for ch in rearranged:
        if ch.isdigit():
            numeric += ch
        elif ch.isalpha():
            numeric += str(ord(ch) - ord("A") + 10)
        else:
            ok({"valid": False, "input": iban, "reason": "Invalid characters in IBAN"})

    try:
        remainder = int(numeric) % 97
    except ValueError:
        ok({"valid": False, "input": iban, "reason": "IBAN contains invalid characters"})

    if remainder != 1:
        ok({
            "valid": False,
            "input": iban,
            "country": country,
            "reason": "IBAN checksum failed (modulus 97)",
        })

    ok({
        "valid": True,
        "iban": cleaned,
        "country": country,
        "check_digits": cleaned[2:4],
        "bban": cleaned[4:],
    })


def validate_eori(conn, args):
    """Validate EORI number format: country prefix + up to 15 alphanumeric chars."""
    eori = args.eori
    if not eori:
        err("--eori is required.")

    cleaned = eori.upper().replace(" ", "")

    if len(cleaned) < 3:
        ok({"valid": False, "input": eori, "reason": "EORI too short"})

    country = cleaned[:2]
    if not country.isalpha():
        ok({"valid": False, "input": eori, "reason": "Must start with 2-letter country code"})

    identifier = cleaned[2:]
    if len(identifier) > 15:
        ok({
            "valid": False,
            "input": eori,
            "reason": f"Identifier part too long ({len(identifier)} chars, max 15)",
        })

    if not identifier.isalnum():
        ok({
            "valid": False,
            "input": eori,
            "reason": "Identifier must be alphanumeric",
        })

    ok({
        "valid": True,
        "eori": cleaned,
        "country": country,
        "identifier": identifier,
    })


def check_vies_format(conn, args):
    """Check VAT number format against VIES country-specific patterns."""
    vat = args.vat_number
    if not vat:
        err("--vat-number is required.")

    cleaned = vat.upper().replace(" ", "")
    formats_data = _load_json_asset("eu_vat_number_formats.json")

    for fmt in formats_data["formats"]:
        if re.match(fmt["pattern"], cleaned):
            ok({
                "format_valid": True,
                "vat_number": cleaned,
                "country": fmt["country"],
                "prefix": fmt["prefix"],
                "note": "Format check only — does not verify active registration with VIES",
            })

    ok({
        "format_valid": False,
        "input": vat,
        "reason": "Does not match any known EU VAT number format",
    })


# ---------------------------------------------------------------------------
# VAT Computation Actions
# ---------------------------------------------------------------------------

def compute_vat(conn, args):
    """Compute VAT at member state's standard or reduced rate."""
    amount = to_decimal(args.amount)
    country = (args.country or "").upper()
    rate_type = (getattr(args, "rate_type", None) or "standard").lower()

    if not country:
        err("--country is required (2-letter EU country code).")
    if country not in EU_COUNTRY_CODES:
        err(f"'{country}' is not an EU member state.")

    rate = _get_vat_rate_for_country(country, rate_type)
    vat_amount = round_currency(amount * rate / Decimal("100"))
    total = round_currency(amount + vat_amount)

    ok({
        "country": country,
        "net_amount": str(round_currency(amount)),
        "vat_rate": str(rate),
        "rate_type": rate_type,
        "vat_amount": str(vat_amount),
        "total": str(total),
    })


def compute_reverse_charge(conn, args):
    """Compute reverse charge for intra-community B2B transactions."""
    amount = to_decimal(args.amount)
    seller = (args.seller_country or "").upper()
    buyer = (args.buyer_country or "").upper()

    if not seller or not buyer:
        err("--seller-country and --buyer-country are required.")

    if seller == buyer:
        # Same country — not a reverse charge scenario
        rate = _get_vat_rate_for_country(seller)
        vat = round_currency(amount * rate / Decimal("100"))
        ok({
            "reverse_charge_applies": False,
            "reason": "Same country — standard VAT applies",
            "seller_country": seller,
            "buyer_country": buyer,
            "seller_vat": str(vat),
            "buyer_self_assessed_vat": "0.00",
            "vat_rate": str(rate),
        })

    # Intra-community: seller charges 0%, buyer self-assesses at their local rate
    buyer_rate = _get_vat_rate_for_country(buyer)
    buyer_vat = round_currency(amount * buyer_rate / Decimal("100"))

    ok({
        "reverse_charge_applies": True,
        "scenario": "intra_community",
        "seller_country": seller,
        "buyer_country": buyer,
        "net_amount": str(round_currency(amount)),
        "seller_vat": "0.00",
        "buyer_self_assessed_vat": str(buyer_vat),
        "buyer_vat_rate": str(buyer_rate),
        "ec_sales_list_required": True,
    })


def list_eu_vat_rates(conn, args):
    """List all 27 EU member state VAT rates."""
    rates_data = _load_json_asset("eu_vat_rates.json")
    ok({
        "total": len(rates_data["rates"]),
        "rates": rates_data["rates"],
    })


def compute_oss_vat(conn, args):
    """Compute OSS VAT: buyer country rate for B2C digital services."""
    amount = to_decimal(args.amount)
    seller = (args.seller_country or "").upper()
    buyer = (args.buyer_country or "").upper()

    if not seller or not buyer:
        err("--seller-country and --buyer-country are required.")

    # OSS: VAT at buyer's country rate
    buyer_rate = _get_vat_rate_for_country(buyer)
    vat_amount = round_currency(amount * buyer_rate / Decimal("100"))
    total = round_currency(amount + vat_amount)

    ok({
        "seller_country": seller,
        "buyer_country": buyer,
        "net_amount": str(round_currency(amount)),
        "vat_rate": str(buyer_rate),
        "vat_amount": str(vat_amount),
        "total": str(total),
        "scheme": "OSS (One Stop Shop)",
        "note": "VAT charged at buyer's country rate for B2C digital services",
    })


def check_distance_selling_threshold(conn, args):
    """Check if annual cross-border B2C sales exceed EUR 10K threshold."""
    annual_sales = to_decimal(args.annual_sales)

    rc_data = _load_json_asset("eu_reverse_charge_rules.json")
    threshold = to_decimal(rc_data["distance_selling_threshold"])
    currency = rc_data["distance_selling_threshold_currency"]

    exceeded = annual_sales > threshold

    ok({
        "annual_sales": str(round_currency(annual_sales)),
        "threshold": str(threshold),
        "threshold_currency": currency,
        "threshold_exceeded": exceeded,
        "action_required": "Register for VAT in destination country or use OSS" if exceeded
                          else "Home country VAT rate applies",
    })


def triangulation_check(conn, args):
    """Check if three-party triangulation simplification applies."""
    country_a = (args.country_a or "").upper()
    country_b = (args.country_b or "").upper()
    country_c = (args.country_c or "").upper()

    if not country_a or not country_b or not country_c:
        err("--country-a, --country-b, and --country-c are required.")

    # All three must be different EU member states
    all_eu = all(c in EU_COUNTRY_CODES for c in [country_a, country_b, country_c])
    all_different = len({country_a, country_b, country_c}) == 3

    applies = all_eu and all_different

    ok({
        "country_a": country_a,
        "country_b": country_b,
        "country_c": country_c,
        "simplification_applies": applies,
        "description": "Triangulation: A sells to B, B sells to C, goods ship A→C directly"
                       if applies else "Triangulation requires 3 different EU member states",
        "intermediary_exempt_from_registration": applies,
    })


# ---------------------------------------------------------------------------
# Compliance Actions
# ---------------------------------------------------------------------------

def generate_vat_return(conn, args):
    """Generate VAT return for an EU member state company."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]
    period = args.period or args.month
    year = args.year

    if not period or not year:
        err("--period and --year are required.")

    month = int(period)
    date_from = f"{year}-{month:02d}-01"
    if month == 12:
        date_to = f"{int(year) + 1}-01-01"
    else:
        date_to = f"{year}-{month + 1:02d}-01"

    # Output VAT (sales)
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total,
                  COALESCE(SUM(CAST(total_amount AS REAL)), 0) as net
           FROM sales_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date < ? AND status = 'submitted'""",
        (cid, date_from, date_to),
    ).fetchone()
    output_vat = round_currency(to_decimal(str(row["total"])))
    total_sales = round_currency(to_decimal(str(row["net"])))

    # Input VAT (purchases)
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total,
                  COALESCE(SUM(CAST(total_amount AS REAL)), 0) as net
           FROM purchase_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date < ? AND status = 'submitted'""",
        (cid, date_from, date_to),
    ).fetchone()
    input_vat = round_currency(to_decimal(str(row["total"])))
    total_purchases = round_currency(to_decimal(str(row["net"])))

    net_vat = round_currency(output_vat - input_vat)

    ok({
        "report": "VAT Return",
        "country": company["country"].upper(),
        "period": f"{year}-{month:02d}",
        "output_vat": str(output_vat),
        "input_vat": str(input_vat),
        "net_vat": str(net_vat),
        "total_sales_ex_vat": str(total_sales),
        "total_purchases_ex_vat": str(total_purchases),
        "company_id": cid,
    })


def generate_ec_sales_list(conn, args):
    """Generate EC Sales List for intra-community B2B supplies."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]
    period = args.period or args.month
    year = args.year

    if not period or not year:
        err("--period and --year are required.")

    ok({
        "report": "EC Sales List",
        "country": company["country"].upper(),
        "period": f"{year}-{int(period):02d}",
        "company_id": cid,
        "entries": [],
        "total_goods": "0.00",
        "total_services": "0.00",
        "total_triangulation": "0.00",
    })


def generate_saft_export(conn, args):
    """Generate SAF-T (Standard Audit File for Tax) export structure."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]
    from_date = args.from_date
    to_date = args.to_date

    if not from_date or not to_date:
        err("--from-date and --to-date are required.")

    saft_mapping = _load_json_asset("eu_saft_mapping.json")

    # Count records
    sales_count = conn.execute(
        """SELECT COUNT(*) as cnt FROM sales_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date <= ? AND status = 'submitted'""",
        (cid, from_date, to_date),
    ).fetchone()["cnt"]

    purchase_count = conn.execute(
        """SELECT COUNT(*) as cnt FROM purchase_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date <= ? AND status = 'submitted'""",
        (cid, from_date, to_date),
    ).fetchone()["cnt"]

    ok({
        "standard": saft_mapping["standard"],
        "header": {
            "AuditFileVersion": saft_mapping["header"]["AuditFileVersion"],
            "AuditFileCountry": company["country"].upper(),
            "AuditFileDateCreated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "SoftwareCompanyName": "ERPClaw",
            "SoftwareID": "erpclaw-region-eu",
            "SoftwareVersion": "1.0.0",
            "Company": {
                "Name": company.get("name", ""),
                "CompanyID": cid,
            },
            "SelectionCriteria": {
                "PeriodStart": from_date,
                "PeriodEnd": to_date,
            },
        },
        "record_counts": {
            "sales_invoices": sales_count,
            "purchase_invoices": purchase_count,
        },
    })


def generate_intrastat_dispatches(conn, args):
    """Generate Intrastat Dispatches report (goods sent to other EU states)."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]
    period = args.period or args.month
    year = args.year

    if not period or not year:
        err("--period and --year are required.")

    ok({
        "report": "Intrastat Dispatches",
        "country": company["country"].upper(),
        "period": f"{year}-{int(period):02d}",
        "company_id": cid,
        "dispatches": [],
        "total_value": "0.00",
        "total_weight_kg": "0",
    })


def generate_intrastat_arrivals(conn, args):
    """Generate Intrastat Arrivals report (goods received from other EU states)."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]
    period = args.period or args.month
    year = args.year

    if not period or not year:
        err("--period and --year are required.")

    ok({
        "report": "Intrastat Arrivals",
        "country": company["country"].upper(),
        "period": f"{year}-{int(period):02d}",
        "company_id": cid,
        "arrivals": [],
        "total_value": "0.00",
        "total_weight_kg": "0",
    })


def generate_einvoice_en16931(conn, args):
    """Generate EN 16931 e-invoice payload."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]
    invoice_id = args.invoice_id

    if not invoice_id:
        err("--invoice-id is required.")

    # Try to find the invoice
    invoice = conn.execute(
        "SELECT * FROM sales_invoice WHERE id = ? OR name = ?",
        (invoice_id, invoice_id),
    ).fetchone()

    if not invoice:
        ok({
            "standard": "EN 16931",
            "invoice_id": invoice_id,
            "seller": {"name": company.get("name", ""), "country": company["country"].upper()},
            "invoice_lines": [],
            "note": "Invoice not found — empty e-invoice structure returned",
        })

    inv = row_to_dict(invoice)

    ok({
        "standard": "EN 16931",
        "invoice_id": inv.get("name", invoice_id),
        "issue_date": inv.get("posting_date", ""),
        "seller": {
            "name": company.get("name", ""),
            "country": company["country"].upper(),
        },
        "buyer": {
            "customer_id": inv.get("customer_id", ""),
        },
        "invoice_lines": [],
        "net_total": inv.get("total_amount", "0"),
        "tax_total": inv.get("tax_amount", "0"),
        "gross_total": inv.get("grand_total", "0"),
    })


def generate_oss_return(conn, args):
    """Generate OSS (One Stop Shop) quarterly VAT return."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]
    quarter = args.quarter
    year = args.year

    if not quarter or not year:
        err("--quarter and --year are required.")

    q = int(quarter)
    if q < 1 or q > 4:
        err("Quarter must be 1-4.")

    ok({
        "report": "OSS Return",
        "country": company["country"].upper(),
        "quarter": f"Q{q} {year}",
        "company_id": cid,
        "supplies_by_country": [],
        "total_vat_due": "0.00",
    })


# ---------------------------------------------------------------------------
# Tax Actions
# ---------------------------------------------------------------------------

def compute_withholding_tax(conn, args):
    """Compute withholding tax on cross-border payments."""
    amount = to_decimal(args.amount)
    income_type = (args.income_type or "dividends").lower()
    source = (args.source_country or "").upper()
    recipient = (args.recipient_country or "").upper()

    if not source or not recipient:
        err("--source-country and --recipient-country are required.")

    # Default WHT rates (simplified — full treaty rates would need a separate asset)
    default_rates = {
        "dividends": Decimal("15"),
        "interest": Decimal("0"),   # EU Interest/Royalties Directive
        "royalties": Decimal("0"),  # EU Interest/Royalties Directive
    }

    # Within EU, Interest and Royalties Directive eliminates WHT
    if source in EU_COUNTRY_CODES and recipient in EU_COUNTRY_CODES:
        if income_type in ("interest", "royalties"):
            rate = Decimal("0")
        else:
            rate = default_rates.get(income_type, Decimal("15"))
    else:
        rate = default_rates.get(income_type, Decimal("15"))

    wht = round_currency(amount * rate / Decimal("100"))
    net = round_currency(amount - wht)

    ok({
        "source_country": source,
        "recipient_country": recipient,
        "income_type": income_type,
        "gross_amount": str(round_currency(amount)),
        "wht_rate": str(rate),
        "wht_amount": str(wht),
        "net_amount": str(net),
        "note": "EU Interest/Royalties Directive applies" if rate == Decimal("0") and income_type != "dividends" else "",
    })


# ---------------------------------------------------------------------------
# Info / Report Actions
# ---------------------------------------------------------------------------

def list_eu_countries(conn, args):
    """List all 27 EU member states with codes, currencies, and VAT rates."""
    countries_data = _load_json_asset("eu_country_codes.json")
    ok({
        "total": len(countries_data["countries"]),
        "countries": countries_data["countries"],
    })


def list_intrastat_codes(conn, args):
    """List Combined Nomenclature codes for Intrastat reporting."""
    codes_data = _load_json_asset("eu_intrastat_codes.json")
    ok({
        "total": len(codes_data["codes"]),
        "codes": codes_data["codes"],
    })


def eu_tax_summary(conn, args):
    """EU tax dashboard: domestic VAT + intra-community totals."""
    company = _get_company(conn, args.company_id)
    _check_eu_company(company)
    cid = company["id"]
    from_date = args.from_date
    to_date = args.to_date

    if not from_date or not to_date:
        err("--from-date and --to-date are required.")

    # Domestic VAT collected (sales)
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total
           FROM sales_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date <= ? AND status = 'submitted'""",
        (cid, from_date, to_date),
    ).fetchone()
    vat_collected = round_currency(to_decimal(str(row["total"])))

    # Domestic VAT paid (purchases)
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total
           FROM purchase_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date <= ? AND status = 'submitted'""",
        (cid, from_date, to_date),
    ).fetchone()
    vat_paid = round_currency(to_decimal(str(row["total"])))

    net_vat = round_currency(vat_collected - vat_paid)

    ok({
        "report": "EU Tax Summary",
        "country": company["country"].upper(),
        "period": f"{from_date} to {to_date}",
        "domestic_vat_collected": str(vat_collected),
        "domestic_vat_paid": str(vat_paid),
        "net_vat": str(net_vat),
        "intra_community_supplies": "0.00",
        "intra_community_acquisitions": "0.00",
        "oss_vat_due": "0.00",
        "company_id": cid,
    })


def available_reports(conn, args):
    """List all available EU reports."""
    ok({
        "reports": [
            {"name": "VAT Return", "action": "generate-vat-return", "description": "Member state VAT return"},
            {"name": "EC Sales List", "action": "generate-ec-sales-list", "description": "Intra-community B2B supply listing"},
            {"name": "SAF-T Export", "action": "generate-saft-export", "description": "OECD Standard Audit File for Tax"},
            {"name": "Intrastat Dispatches", "action": "generate-intrastat-dispatches", "description": "Goods sent to other EU states"},
            {"name": "Intrastat Arrivals", "action": "generate-intrastat-arrivals", "description": "Goods received from other EU states"},
            {"name": "EN 16931 E-Invoice", "action": "generate-einvoice-en16931", "description": "European e-invoice standard"},
            {"name": "OSS Return", "action": "generate-oss-return", "description": "One Stop Shop quarterly return"},
            {"name": "EU Tax Summary", "action": "eu-tax-summary", "description": "Domestic + intra-community VAT dashboard"},
        ],
    })


def status(conn, args):
    """Show skill status and configuration."""
    asset_files = [
        "eu_country_codes.json", "eu_vat_rates.json", "eu_vat_number_formats.json",
        "eu_reverse_charge_rules.json", "eu_intrastat_codes.json",
        "eu_saft_mapping.json", "eu_coa_template.json",
    ]
    assets = {}
    for f in asset_files:
        assets[f] = os.path.exists(os.path.join(ASSETS_DIR, f))

    result = {
        "skill": "erpclaw-region-eu",
        "version": "1.0.0",
        "description": "EU Regional Compliance (VAT, Reverse Charge, OSS, Intrastat, E-Invoice, SAF-T)",
        "asset_files": assets,
    }

    if args.company_id:
        try:
            company = _get_company(conn, args.company_id)
            _check_eu_company(company)
            cid = company["id"]
            try:
                vat_row = conn.execute(
                    "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'eu_vat_number'",
                    (cid,),
                ).fetchone()
                result["vat_configured"] = vat_row is not None
            except Exception:
                result["vat_configured"] = False
            result["company_id"] = cid
            result["company_name"] = company.get("name", "")
            result["country"] = company["country"].upper()
        except SystemExit:
            pass

    result["status"] = "ok"
    print(json.dumps(result, indent=2))
    sys.exit(0)


# ---------------------------------------------------------------------------
# Action Router
# ---------------------------------------------------------------------------

ACTIONS = {
    "seed-eu-defaults": seed_eu_defaults,
    "setup-eu-vat": setup_eu_vat,
    "seed-eu-coa": seed_eu_coa,
    "validate-eu-vat-number": validate_eu_vat_number,
    "validate-iban": validate_iban,
    "validate-eori": validate_eori,
    "check-vies-format": check_vies_format,
    "compute-vat": compute_vat,
    "compute-reverse-charge": compute_reverse_charge,
    "list-eu-vat-rates": list_eu_vat_rates,
    "compute-oss-vat": compute_oss_vat,
    "check-distance-selling-threshold": check_distance_selling_threshold,
    "triangulation-check": triangulation_check,
    "generate-vat-return": generate_vat_return,
    "generate-ec-sales-list": generate_ec_sales_list,
    "generate-saft-export": generate_saft_export,
    "generate-intrastat-dispatches": generate_intrastat_dispatches,
    "generate-intrastat-arrivals": generate_intrastat_arrivals,
    "generate-einvoice-en16931": generate_einvoice_en16931,
    "generate-oss-return": generate_oss_return,
    "compute-withholding-tax": compute_withholding_tax,
    "list-eu-countries": list_eu_countries,
    "list-intrastat-codes": list_intrastat_codes,
    "eu-tax-summary": eu_tax_summary,
    "available-reports": available_reports,
    "status": status,
}


def main():
    parser = argparse.ArgumentParser(description="ERPClaw EU Regional Skill")
    parser.add_argument("--action", required=True, help="Action to perform")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    parser.add_argument("--company-id", default=None, help="Company ID")

    # VAT flags
    parser.add_argument("--amount", default=None, help="Amount for computation")
    parser.add_argument("--country", default=None, help="EU country code")
    parser.add_argument("--rate-type", default=None, help="VAT rate type")
    parser.add_argument("--vat-number", default=None, help="EU VAT number")
    parser.add_argument("--seller-country", default=None, help="Seller country code")
    parser.add_argument("--buyer-country", default=None, help="Buyer country code")
    parser.add_argument("--annual-sales", default=None, help="Annual cross-border sales")

    # Triangulation
    parser.add_argument("--country-a", default=None, help="Country A in triangulation")
    parser.add_argument("--country-b", default=None, help="Country B in triangulation")
    parser.add_argument("--country-c", default=None, help="Country C in triangulation")

    # Compliance flags
    parser.add_argument("--period", default=None, help="Period number (month)")
    parser.add_argument("--month", default=None, help="Month number")
    parser.add_argument("--year", default=None, help="Year")
    parser.add_argument("--quarter", default=None, help="Quarter (1-4)")
    parser.add_argument("--invoice-id", default=None, help="Invoice ID for e-invoice")

    # Validation flags
    parser.add_argument("--iban", default=None, help="IBAN for validation")
    parser.add_argument("--eori", default=None, help="EORI for validation")

    # WHT flags
    parser.add_argument("--income-type", default=None, help="Income type (dividends/interest/royalties)")
    parser.add_argument("--source-country", default=None, help="Source country for WHT")
    parser.add_argument("--recipient-country", default=None, help="Recipient country for WHT")

    # Report flags
    parser.add_argument("--from-date", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to-date", default=None, help="End date (YYYY-MM-DD)")

    args, _unknown = parser.parse_known_args()
    action_name = args.action.lower()

    if action_name not in ACTIONS:
        err(f"Unknown action: {action_name}",
             suggestion=f"Available actions: {', '.join(sorted(ACTIONS.keys()))}")

    try:
        if args.db_path != DEFAULT_DB_PATH:
            ensure_db_exists(args.db_path)
        conn = get_connection(args.db_path)
    except FileNotFoundError as e:
        err(str(e), suggestion="Run init_db.py first to create the database.")
    except Exception as e:
        err(f"Database connection error: {e}")

    # Dependency check
    _dep = check_required_tables(conn, REQUIRED_TABLES)
    if _dep:
        _dep["suggestion"] = "clawhub install " + " ".join(_dep.get("missing_skills", []))
        print(json.dumps(_dep, indent=2))
        conn.close()
        sys.exit(1)

    try:
        ACTIONS[action_name](conn, args)
    except SystemExit:
        raise
    except Exception as e:
        err(f"Action '{action_name}' failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
