#!/usr/bin/env python3
"""ERPClaw UK Regional Skill — db_query.py

Pure overlay skill for UK-specific compliance: VAT (standard/reduced/zero/
flat rate), payroll (PAYE, NI, student loan, pension), RTI forms (FPS, EPS,
P60, P45), CIS deductions, FRS 102 CoA, and ID validation (VAT number,
UTR, NINO, CRN).

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


def _check_uk_company(company):
    country = (company.get("country") or "").upper()
    if country not in ("GB", "UK", "UNITED KINGDOM"):
        err(
            "This action is for UK companies only. Company country must be GB.",
            suggestion="Create a UK company with erpclaw-setup first.",
        )


def _get_company_region(conn, company):
    """Get region from company record or regional_settings."""
    region = company.get("region", "")
    if not region:
        row = conn.execute(
            "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'region'",
            (company["id"],),
        ).fetchone()
        if row:
            region = row["value"]
    return (region or "ENG").upper()


def _resolve_periods(args):
    """Convert pay period string to number of periods per year."""
    raw = getattr(args, "pay_period", None) or "annual"
    mapping = {
        "annual": 1, "monthly": 12, "biweekly": 26, "weekly": 52,
        "4weekly": 13, "fortnightly": 26,
    }
    raw_lower = raw.lower()
    if raw_lower in mapping:
        return mapping[raw_lower]
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 12  # default monthly


# ---------------------------------------------------------------------------
# Setup Actions
# ---------------------------------------------------------------------------

def seed_uk_defaults(conn, args):
    """Create VAT input/output accounts and standard/reduced/zero tax templates."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]

    accounts_created = 0
    templates_created = 0

    # VAT accounts: (name, account_type, root_type)
    vat_accounts = [
        ("VAT Output (Standard 20%)", "tax", "liability"),
        ("VAT Output (Reduced 5%)", "tax", "liability"),
        ("VAT Output (Zero 0%)", "tax", "liability"),
        ("VAT Input (UK)", "tax", "asset"),
        ("VAT Control (UK)", "tax", "liability"),
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
        ("UK VAT Standard (20%)", "20"),
        ("UK VAT Reduced (5%)", "5"),
        ("UK VAT Zero (0%)", "0"),
    ]
    for tpl_name, rate in templates:
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
            # Get the output account
            acct = conn.execute(
                "SELECT id FROM account WHERE company_id = ? AND name LIKE ? LIMIT 1",
                (cid, f"VAT Output%{rate}%"),
            ).fetchone()
            if acct:
                conn.execute(
                    """INSERT INTO tax_template_line (id, tax_template_id, tax_account_id, rate)
                       VALUES (?, ?, ?, ?)""",
                    (str(uuid.uuid4()), tpl_id, acct["id"], rate),
                )
            templates_created += 1

    conn.commit()
    audit(conn, "erpclaw-region-uk", "seed-uk-defaults", "company", cid,
           new_values={"accounts": accounts_created, "templates": templates_created})
    conn.commit()
    ok({
        "accounts_created": accounts_created,
        "templates_created": templates_created,
        "company_id": cid,
    })


def setup_vat(conn, args):
    """Store VAT number and MTD flag for a UK company."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]

    vat_number = args.vat_number
    if not vat_number:
        err("--vat-number is required.")

    # Validate VAT number
    cleaned = vat_number.upper().replace(" ", "")
    if cleaned.startswith("GB"):
        digits = cleaned[2:]
    else:
        digits = cleaned
    if not re.match(r"^\d{9}$", digits):
        err(f"Invalid UK VAT number: {vat_number}. Must be GB + 9 digits.",
             suggestion="Format: GB123456789 or 123456789")

    formatted = f"GB{digits}"

    # Store in regional_settings
    for key, value in [("vat_number", formatted), ("mtd_enabled", "true")]:
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
    audit(conn, "erpclaw-region-uk", "setup-vat", "company", cid,
           new_values={"vat_number": formatted})
    conn.commit()
    ok({
        "vat_number_stored": True,
        "vat_number": formatted,
        "mtd_enabled": True,
        "company_id": cid,
    })


def seed_uk_coa(conn, args):
    """Import FRS 102 Chart of Accounts."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]

    coa = _load_json_asset("uk_coa_frs102.json")
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
    audit(conn, "erpclaw-region-uk", "seed-uk-coa", "company", cid, new_values={"accounts": created})
    conn.commit()
    ok({
        "accounts_created": created,
        "standard": coa.get("standard", "FRS 102"),
        "company_id": cid,
    })


def seed_uk_payroll(conn, args):
    """Register PAYE, NI, student loan, and pension salary components."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]

    components = [
        ("PAYE Income Tax", "deduction", "Pay As You Earn income tax", 1),
        ("Employee NI", "deduction", "National Insurance - employee contribution", 1),
        ("Employer NI", "employer_contribution", "National Insurance - employer contribution", 1),
        ("Student Loan", "deduction", "Student loan repayment", 0),
        ("Employee Pension", "deduction", "Employee pension contribution (NEST/auto-enrollment)", 1),
        ("Employer Pension", "employer_contribution", "Employer pension contribution (NEST/auto-enrollment)", 1),
        ("Basic Salary", "earning", "Basic monthly/weekly salary", 0),
        ("Overtime", "earning", "Overtime payments", 0),
    ]
    created = 0
    for name, comp_type, desc, is_statutory in components:
        exists = conn.execute(
            "SELECT id FROM salary_component WHERE name = ?",
            (name,),
        ).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO salary_component (id, name, component_type, description, is_statutory)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), name, comp_type, desc, is_statutory),
            )
            created += 1

    conn.commit()
    audit(conn, "erpclaw-region-uk", "seed-uk-payroll", "company", cid,
           new_values={"components": created})
    conn.commit()
    ok({
        "components_created": created,
        "company_id": cid,
    })


# ---------------------------------------------------------------------------
# Validation Actions
# ---------------------------------------------------------------------------

def validate_vat_number(conn, args):
    """Validate UK VAT number format: GB + 9 digits."""
    vat = args.vat_number
    if not vat:
        err("--vat-number is required.")

    cleaned = vat.upper().replace(" ", "")
    if cleaned.startswith("GB"):
        digits = cleaned[2:]
    else:
        digits = cleaned

    if not re.match(r"^\d{9}$", digits):
        return ok({
            "valid": False,
            "input": vat,
            "reason": "Must be GB prefix + exactly 9 digits",
        })

    formatted = f"GB{digits}"

    # Modulus 97 check (HMRC uses two algorithms: pre-1998 and post-1998)
    # Weighted sum: 8*d1 + 7*d2 + 6*d3 + 5*d4 + 4*d5 + 3*d6 + 2*d7
    # Check digits d8d9 make (total - check_value) divisible by 97
    weights = [8, 7, 6, 5, 4, 3, 2]
    d = [int(c) for c in digits]
    total = sum(w * v for w, v in zip(weights, d[:7]))
    check_value = int(digits[7:9])

    # Two valid algorithms for HMRC VAT numbers
    valid_mod = False
    # Pre-1998: (total - check_value) % 97 == 0
    if (total - check_value) % 97 == 0:
        valid_mod = True
    # Post-1998: (total + 55 - check_value) % 97 == 0
    elif (total + 55 - check_value) % 97 == 0:
        valid_mod = True

    if not valid_mod:
        return ok({
            "valid": False,
            "input": vat,
            "formatted": formatted,
            "reason": "Modulus 97 check digit validation failed",
        })

    ok({
        "valid": True,
        "input": vat,
        "formatted": formatted,
        "digits": digits,
    })


def validate_utr(conn, args):
    """Validate UK Unique Taxpayer Reference: exactly 10 digits."""
    utr = args.utr
    if not utr:
        err("--utr is required.")

    cleaned = utr.replace(" ", "")
    if re.match(r"^\d{10}$", cleaned):
        ok({
            "valid": True,
            "utr": cleaned,
        })
    else:
        ok({
            "valid": False,
            "input": utr,
            "reason": "UTR must be exactly 10 digits",
        })


def validate_nino(conn, args):
    """Validate UK National Insurance Number: 2 letters + 6 digits + suffix letter."""
    nino = args.nino
    if not nino:
        err("--nino is required.")

    cleaned = nino.upper().replace(" ", "")

    # Format: 2 letters + 6 digits + 1 letter (A/B/C/D)
    if not re.match(r"^[A-Z]{2}\d{6}[A-D]$", cleaned):
        ok({
            "valid": False,
            "input": nino,
            "reason": "Must be 2 letters + 6 digits + suffix letter (A-D)",
        })

    # Invalid prefixes: D, F, I, Q, U, V as first letter
    # BG, GB, NK, KN, TN, NT, ZZ are also invalid
    prefix = cleaned[:2]
    invalid_first = set("DFIQUV")
    invalid_second = set("DFIQUVO")
    invalid_pairs = {"BG", "GB", "NK", "KN", "TN", "NT", "ZZ"}

    if cleaned[0] in invalid_first or cleaned[1] in invalid_second or prefix in invalid_pairs:
        ok({
            "valid": False,
            "input": nino,
            "reason": f"Invalid NINO prefix: {prefix}",
        })

    ok({
        "valid": True,
        "nino": cleaned,
        "formatted": f"{cleaned[:2]} {cleaned[2:4]} {cleaned[4:6]} {cleaned[6:8]} {cleaned[8]}",
    })


def validate_crn(conn, args):
    """Validate Companies House Registration Number: 8 characters."""
    crn = args.crn
    if not crn:
        err("--crn is required.")

    cleaned = crn.upper().replace(" ", "")

    # CRN: 8 characters total
    # Can be 8 digits, or 2-letter prefix + 6 digits
    # Prefixes: SC (Scotland), NI (N Ireland), OC/SO (LLP), IP/SP/IC/SI/NP (various)
    if len(cleaned) != 8:
        ok({
            "valid": False,
            "input": crn,
            "reason": "CRN must be exactly 8 characters",
        })

    # All digits or letter prefix + digits
    if re.match(r"^\d{8}$", cleaned):
        ok({"valid": True, "crn": cleaned, "type": "England/Wales"})
    elif re.match(r"^[A-Z]{2}\d{6}$", cleaned):
        prefix = cleaned[:2]
        prefix_types = {
            "SC": "Scotland", "NI": "Northern Ireland", "OC": "LLP (England/Wales)",
            "SO": "LLP (Scotland)", "IP": "Industrial & Provident", "SP": "Scottish Partnership",
            "IC": "ICVC", "SI": "Scottish ICVC", "NP": "Northern Ireland LLP",
            "R0": "Royal Charter", "RC": "Royal Charter (Scotland)",
        }
        ctype = prefix_types.get(prefix, "Other")
        ok({"valid": True, "crn": cleaned, "type": ctype, "prefix": prefix})
    else:
        ok({
            "valid": False,
            "input": crn,
            "reason": "CRN must be 8 digits or 2-letter prefix + 6 digits",
        })


# ---------------------------------------------------------------------------
# VAT Computation Actions
# ---------------------------------------------------------------------------

def compute_vat(conn, args):
    """Compute VAT at standard (20%), reduced (5%), or zero (0%) rate."""
    amount = to_decimal(args.amount)
    rate_type = (getattr(args, "rate_type", None) or "standard").lower()

    rates = _load_json_asset("uk_vat_rates.json")
    rate_map = {
        "standard": to_decimal(rates["standard_rate"]),
        "reduced": to_decimal(rates["reduced_rate"]),
        "zero": to_decimal(rates["zero_rate"]),
    }
    if rate_type not in rate_map:
        err(f"Unknown rate type: {rate_type}. Use standard, reduced, or zero.")

    rate = rate_map[rate_type]
    vat_amount = round_currency(amount * rate / Decimal("100"))
    total = round_currency(amount + vat_amount)

    ok({
        "net_amount": str(round_currency(amount)),
        "vat_rate": str(rate),
        "vat_amount": str(vat_amount),
        "total": str(total),
        "rate_type": rate_type,
    })


def compute_vat_inclusive(conn, args):
    """Reverse-calculate VAT from a gross amount."""
    gross = to_decimal(args.gross_amount)
    rate_type = (getattr(args, "rate_type", None) or "standard").lower()

    rates = _load_json_asset("uk_vat_rates.json")
    rate_map = {
        "standard": to_decimal(rates["standard_rate"]),
        "reduced": to_decimal(rates["reduced_rate"]),
        "zero": to_decimal(rates["zero_rate"]),
    }
    if rate_type not in rate_map:
        err(f"Unknown rate type: {rate_type}. Use standard, reduced, or zero.")

    rate = rate_map[rate_type]
    net = round_currency(gross * Decimal("100") / (Decimal("100") + rate))
    vat_amount = round_currency(gross - net)

    ok({
        "gross_amount": str(round_currency(gross)),
        "net_amount": str(net),
        "vat_rate": str(rate),
        "vat_amount": str(vat_amount),
        "rate_type": rate_type,
    })


def list_vat_rates(conn, args):
    """List all UK VAT rates and flat rate scheme categories."""
    rates = _load_json_asset("uk_vat_rates.json")
    categories = _load_json_asset("uk_vat_categories.json")

    ok({
        "standard_rate": rates["standard_rate"],
        "reduced_rate": rates["reduced_rate"],
        "zero_rate": rates["zero_rate"],
        "flat_rate_categories_count": len(rates.get("flat_rate_categories", [])),
        "supply_categories": categories.get("categories", []),
    })


def compute_flat_rate_vat(conn, args):
    """Compute VAT under the Flat Rate Scheme."""
    gross = to_decimal(args.gross_turnover)
    category = args.category
    first_year = (getattr(args, "first_year", None) or "").lower() in ("true", "1", "yes")

    if not category:
        err("--category is required for flat rate VAT computation.")

    rates = _load_json_asset("uk_vat_rates.json")
    flat_rates = rates.get("flat_rate_categories", [])
    discount = to_decimal(rates.get("first_year_discount", "1"))

    # Find category rate
    flat_rate = None
    for cat in flat_rates:
        if cat["category"].lower() == category.lower():
            flat_rate = to_decimal(cat["rate"])
            break

    if flat_rate is None:
        err(f"Unknown flat rate category: {category}")

    if first_year:
        flat_rate = flat_rate - discount

    vat_due = round_currency(gross * flat_rate / Decimal("100"))

    ok({
        "gross_turnover": str(round_currency(gross)),
        "category": category,
        "flat_rate": str(flat_rate),
        "first_year_discount": first_year,
        "vat_due": str(vat_due),
    })


# ---------------------------------------------------------------------------
# Payroll Actions
# ---------------------------------------------------------------------------

def compute_paye(conn, args):
    """Compute PAYE income tax using progressive bands."""
    annual = to_decimal(args.annual_income)
    region = (getattr(args, "region", None) or "ENG").upper()

    bands_data = _load_json_asset("uk_income_tax_bands.json")
    pa = to_decimal(bands_data["personal_allowance"])
    pa_taper_threshold = to_decimal(bands_data["pa_taper_threshold"])

    # Personal allowance taper: reduced by £1 for every £2 over £100,000
    if annual > pa_taper_threshold:
        reduction = (annual - pa_taper_threshold) / Decimal("2")
        pa = max(Decimal("0"), pa - reduction)

    # Select bands based on region
    if region == "SCO":
        band_list = bands_data["scotland"]["bands"]
    else:
        band_list = bands_data["england_wales_ni"]["bands"]

    # Compute tax on taxable income
    taxable = max(Decimal("0"), annual - pa)
    tax = Decimal("0")
    marginal_rate = Decimal("0")

    # Process bands (skip personal allowance band, compute on subsequent bands)
    remaining = taxable
    for band in band_list:
        rate = to_decimal(band["rate"])
        if rate == Decimal("0"):
            continue  # skip PA band
        band_from = to_decimal(band["from"])
        band_to = to_decimal(band["to"]) if band["to"] != "0" else Decimal("999999999")

        # Band width (relative to personal allowance)
        band_width = band_to - band_from
        if band["to"] == "0":  # unlimited top band
            band_width = remaining

        taxable_in_band = min(remaining, band_width)
        if taxable_in_band <= 0:
            break
        tax += round_currency(taxable_in_band * rate / Decimal("100"))
        marginal_rate = rate
        remaining -= taxable_in_band

    net_tax = round_currency(tax)

    ok({
        "annual_income": str(round_currency(annual)),
        "personal_allowance": str(round_currency(pa)),
        "taxable_income": str(round_currency(taxable)),
        "net_tax": str(net_tax),
        "marginal_rate": str(marginal_rate),
        "region": region,
    })


def compute_ni(conn, args):
    """Compute National Insurance contributions (employee + employer)."""
    annual = to_decimal(args.annual_income)

    ni_data = _load_json_asset("uk_ni_rates.json")
    emp = ni_data["class_1_employee"]
    empr = ni_data["class_1_employer"]

    pt = to_decimal(emp["primary_threshold_annual"])
    uel = to_decimal(emp["upper_earnings_limit_annual"])
    main_rate = to_decimal(emp["rate_main"])
    above_uel_rate = to_decimal(emp["rate_above_uel"])

    st = to_decimal(empr["secondary_threshold_annual"])
    employer_rate = to_decimal(empr["rate"])

    # Employee NI
    employee_ni = Decimal("0")
    if annual > pt:
        taxable_main = min(annual, uel) - pt
        employee_ni += taxable_main * main_rate / Decimal("100")
        if annual > uel:
            employee_ni += (annual - uel) * above_uel_rate / Decimal("100")
    employee_ni = round_currency(employee_ni)

    # Employer NI
    employer_ni = Decimal("0")
    if annual > st:
        employer_ni = round_currency((annual - st) * employer_rate / Decimal("100"))

    ok({
        "annual_income": str(round_currency(annual)),
        "employee_ni": str(employee_ni),
        "employer_ni": str(employer_ni),
        "employee_rate_main": str(main_rate),
        "employee_rate_above_uel": str(above_uel_rate),
        "employer_rate": str(employer_rate),
    })


def compute_student_loan(conn, args):
    """Compute student loan deduction by plan type."""
    annual = to_decimal(args.annual_income)
    plan = (args.plan or "1").upper()

    sl_data = _load_json_asset("uk_student_loan_thresholds.json")
    plans = sl_data.get("plans", [])

    plan_info = None
    for p in plans:
        if p["plan"].upper() == plan:
            plan_info = p
            break

    if not plan_info:
        err(f"Unknown student loan plan: {plan}. Valid: 1, 2, 4, 5, PG")

    threshold = to_decimal(plan_info["annual_threshold"])
    rate = to_decimal(plan_info["rate"])

    if annual <= threshold:
        ok({
            "plan": plan,
            "annual_income": str(round_currency(annual)),
            "threshold": str(threshold),
            "rate": str(rate),
            "annual_deduction": "0.00",
            "monthly_deduction": "0.00",
        })

    annual_deduction = round_currency((annual - threshold) * rate / Decimal("100"))
    monthly_deduction = round_currency(annual_deduction / Decimal("12"))

    ok({
        "plan": plan,
        "annual_income": str(round_currency(annual)),
        "threshold": str(threshold),
        "rate": str(rate),
        "annual_deduction": str(annual_deduction),
        "monthly_deduction": str(monthly_deduction),
    })


def compute_pension(conn, args):
    """Compute NEST auto-enrollment pension contributions."""
    annual = to_decimal(args.annual_salary)

    pension_data = _load_json_asset("uk_pension_rates.json")
    qe = pension_data["qualifying_earnings"]
    lower = to_decimal(qe["lower_limit_annual"])
    upper = to_decimal(qe["upper_limit_annual"])
    emp_rate = to_decimal(pension_data["minimum_contributions"]["employer_rate"])
    ee_rate = to_decimal(pension_data["minimum_contributions"]["employee_rate"])

    if annual < lower:
        ok({
            "annual_salary": str(round_currency(annual)),
            "qualifying_earnings": "0.00",
            "employee_contribution": "0.00",
            "employer_contribution": "0.00",
            "total_contribution": "0.00",
            "employee_rate": str(ee_rate),
            "employer_rate": str(emp_rate),
        })

    qualifying = min(annual, upper) - lower
    employee_contrib = round_currency(qualifying * ee_rate / Decimal("100"))
    employer_contrib = round_currency(qualifying * emp_rate / Decimal("100"))
    total = round_currency(employee_contrib + employer_contrib)

    ok({
        "annual_salary": str(round_currency(annual)),
        "qualifying_earnings": str(round_currency(qualifying)),
        "employee_contribution": str(employee_contrib),
        "employer_contribution": str(employer_contrib),
        "total_contribution": str(total),
        "employee_rate": str(ee_rate),
        "employer_rate": str(emp_rate),
    })


def uk_payroll_summary(conn, args):
    """Monthly payroll summary with per-employee breakdown."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]
    month = args.month or args.period
    year = args.year

    if not month or not year:
        err("--month and --year are required.")

    # Find salary slips for the period
    period_prefix = f"{year}-{int(month):02d}"
    slips = conn.execute(
        """SELECT ss.*, e.full_name, e.ssn
           FROM salary_slip ss
           JOIN employee e ON ss.employee_id = e.id
           WHERE ss.company_id = ? AND ss.period_start LIKE ? AND ss.status = 'submitted'""",
        (cid, f"{period_prefix}%"),
    ).fetchall()

    employees = []
    total_gross = Decimal("0")
    total_deductions = Decimal("0")
    total_net = Decimal("0")

    for slip in slips:
        s = row_to_dict(slip)
        gross = to_decimal(s["gross_pay"])
        deductions = to_decimal(s["total_deductions"])
        net = to_decimal(s["net_pay"])
        total_gross += gross
        total_deductions += deductions
        total_net += net
        employees.append({
            "employee_name": s["full_name"],
            "gross_pay": str(round_currency(gross)),
            "deductions": str(round_currency(deductions)),
            "net_pay": str(round_currency(net)),
        })

    ok({
        "report": "UK Payroll Summary",
        "period": period_prefix,
        "employee_count": len(employees),
        "employees": employees,
        "total_gross": str(round_currency(total_gross)),
        "total_deductions": str(round_currency(total_deductions)),
        "total_net": str(round_currency(total_net)),
    })


# ---------------------------------------------------------------------------
# Compliance / Form Actions
# ---------------------------------------------------------------------------

def generate_vat_return(conn, args):
    """Generate 9-box VAT return (MTD-compatible)."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]
    period = args.period or args.month
    year = args.year

    if not period or not year:
        err("--period and --year are required.")

    # Date range for the period (monthly)
    month = int(period)
    date_from = f"{year}-{month:02d}-01"
    if month == 12:
        date_to = f"{int(year) + 1}-01-01"
    else:
        date_to = f"{year}-{month + 1:02d}-01"

    # Box 1: VAT due on sales
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total
           FROM sales_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date < ? AND status = 'submitted'""",
        (cid, date_from, date_to),
    ).fetchone()
    box1 = round_currency(to_decimal(str(row["total"])))

    # Box 2: VAT due on acquisitions (EU — typically 0 post-Brexit)
    box2 = Decimal("0")

    # Box 3: Total VAT due
    box3 = round_currency(box1 + box2)

    # Box 4: VAT reclaimed on purchases
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total
           FROM purchase_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date < ? AND status = 'submitted'""",
        (cid, date_from, date_to),
    ).fetchone()
    box4 = round_currency(to_decimal(str(row["total"])))

    # Box 5: Net VAT (Box 3 - Box 4)
    box5 = round_currency(box3 - box4)

    # Box 6: Total sales (ex VAT)
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(total_amount AS REAL)), 0) as total
           FROM sales_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date < ? AND status = 'submitted'""",
        (cid, date_from, date_to),
    ).fetchone()
    box6 = round_currency(to_decimal(str(row["total"])))

    # Box 7: Total purchases (ex VAT)
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(total_amount AS REAL)), 0) as total
           FROM purchase_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date < ? AND status = 'submitted'""",
        (cid, date_from, date_to),
    ).fetchone()
    box7 = round_currency(to_decimal(str(row["total"])))

    # Box 8: Total supplies to EU (post-Brexit NI protocol only)
    box8 = Decimal("0")

    # Box 9: Total acquisitions from EU
    box9 = Decimal("0")

    ok({
        "report": "VAT Return",
        "period": f"{year}-{month:02d}",
        "box1_vat_due_sales": str(box1),
        "box2_vat_due_acquisitions": str(box2),
        "box3_total_vat_due": str(box3),
        "box4_vat_reclaimed": str(box4),
        "box5_net_vat": str(box5),
        "box6_total_sales_ex_vat": str(box6),
        "box7_total_purchases_ex_vat": str(box7),
        "box8_total_supplies_eu": str(box8),
        "box9_total_acquisitions_eu": str(box9),
    })


def generate_mtd_payload(conn, args):
    """Generate HMRC MTD-compatible JSON payload."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
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

    # Sales VAT
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as vat,
                  COALESCE(SUM(CAST(total_amount AS REAL)), 0) as net
           FROM sales_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date < ? AND status = 'submitted'""",
        (cid, date_from, date_to),
    ).fetchone()
    vat_due_sales = round_currency(to_decimal(str(row["vat"])))
    total_sales = round_currency(to_decimal(str(row["net"])))

    # Purchase VAT
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as vat,
                  COALESCE(SUM(CAST(total_amount AS REAL)), 0) as net
           FROM purchase_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date < ? AND status = 'submitted'""",
        (cid, date_from, date_to),
    ).fetchone()
    vat_reclaimed = round_currency(to_decimal(str(row["vat"])))
    total_purchases = round_currency(to_decimal(str(row["net"])))

    net_vat = round_currency(vat_due_sales - vat_reclaimed)

    ok({
        "periodKey": f"{year}-{month:02d}",
        "vatDueSales": float(vat_due_sales),
        "vatDueAcquisitions": 0,
        "totalVatDue": float(vat_due_sales),
        "vatReclaimedCurrPeriod": float(vat_reclaimed),
        "netVatDue": float(abs(net_vat)),
        "totalValueSalesExVAT": int(total_sales),
        "totalValuePurchasesExVAT": int(total_purchases),
        "totalValueGoodsSuppliedExVAT": 0,
        "totalAcquisitionsExVAT": 0,
        "finalised": True,
    })


def generate_ec_sales_list(conn, args):
    """Generate EC Sales List (NI Protocol — for Northern Ireland businesses)."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]
    period = args.period or args.month
    year = args.year

    if not period or not year:
        err("--period and --year are required.")

    ok({
        "report": "EC Sales List",
        "period": f"{year}-{int(period):02d}",
        "company_id": cid,
        "note": "Applicable to Northern Ireland businesses under the NI Protocol only",
        "entries": [],
        "total_supplies": "0.00",
    })


def generate_fps(conn, args):
    """Generate Full Payment Submission (FPS) for HMRC RTI."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]
    month = args.month or args.period
    year = args.year

    if not month or not year:
        err("--month and --year are required.")

    period_prefix = f"{year}-{int(month):02d}"

    # Get employees with salary slips for the period
    rows = conn.execute(
        """SELECT e.id, e.full_name, e.first_name, e.last_name, e.ssn,
                  ss.gross_pay, ss.total_deductions, ss.net_pay
           FROM salary_slip ss
           JOIN employee e ON ss.employee_id = e.id
           WHERE ss.company_id = ? AND ss.period_start LIKE ? AND ss.status = 'submitted'""",
        (cid, f"{period_prefix}%"),
    ).fetchall()

    employees = []
    for row in rows:
        r = row_to_dict(row)
        nino = r.get("ssn", "")
        masked = f"{nino[:2]}****{nino[-1]}" if len(nino) >= 9 else nino
        employees.append({
            "employee_name": r["full_name"],
            "nino_masked": masked,
            "gross_pay": r["gross_pay"],
            "tax_deducted": r["total_deductions"],
            "net_pay": r["net_pay"],
        })

    ok({
        "form": "FPS",
        "period": period_prefix,
        "company_id": cid,
        "employee_count": len(employees),
        "employees": employees,
    })


def generate_eps(conn, args):
    """Generate Employer Payment Summary (EPS)."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]
    month = args.month or args.period
    year = args.year

    if not month or not year:
        err("--month and --year are required.")

    period_prefix = f"{year}-{int(month):02d}"

    # Aggregate payroll for the period
    row = conn.execute(
        """SELECT COUNT(*) as emp_count,
                  COALESCE(SUM(CAST(gross_pay AS REAL)), 0) as total_gross,
                  COALESCE(SUM(CAST(total_deductions AS REAL)), 0) as total_deductions,
                  COALESCE(SUM(CAST(net_pay AS REAL)), 0) as total_net
           FROM salary_slip
           WHERE company_id = ? AND period_start LIKE ? AND status = 'submitted'""",
        (cid, f"{period_prefix}%"),
    ).fetchone()

    ok({
        "form": "EPS",
        "period": period_prefix,
        "company_id": cid,
        "employee_count": row["emp_count"],
        "total_gross": str(round_currency(to_decimal(str(row["total_gross"])))),
        "total_deductions": str(round_currency(to_decimal(str(row["total_deductions"])))),
        "total_net": str(round_currency(to_decimal(str(row["total_net"])))),
        "employment_allowance_claimed": False,
    })


def generate_p60(conn, args):
    """Generate P60 end-of-year certificate for an employee."""
    employee_id = args.employee_id
    tax_year = args.tax_year or args.year

    if not employee_id:
        err("--employee-id is required.")

    employee = conn.execute(
        "SELECT * FROM employee WHERE id = ?", (employee_id,)
    ).fetchone()
    if not employee:
        err(f"Employee not found: {employee_id}")
    emp = row_to_dict(employee)

    nino = emp.get("ssn", "")
    masked = f"{nino[:2]}****{nino[-1]}" if len(nino) >= 9 else nino

    # Sum salary slips for the tax year (April to March)
    if tax_year:
        ty = int(tax_year)
        date_from = f"{ty}-04-06"
        date_to = f"{ty + 1}-04-05"
    else:
        date_from = "1900-01-01"
        date_to = "2099-12-31"

    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(gross_pay AS REAL)), 0) as total_gross,
                  COALESCE(SUM(CAST(total_deductions AS REAL)), 0) as total_tax,
                  COALESCE(SUM(CAST(net_pay AS REAL)), 0) as total_net
           FROM salary_slip
           WHERE employee_id = ? AND period_start >= ? AND period_start <= ? AND status = 'submitted'""",
        (employee_id, date_from, date_to),
    ).fetchone()

    ok({
        "form": "P60",
        "tax_year": f"{tax_year}/{int(tax_year) + 1}" if tax_year else "N/A",
        "employee_name": emp.get("full_name", ""),
        "nino_masked": masked,
        "total_pay": str(round_currency(to_decimal(str(row["total_gross"])))),
        "total_tax_deducted": str(round_currency(to_decimal(str(row["total_tax"])))),
        "total_net_pay": str(round_currency(to_decimal(str(row["total_net"])))),
    })


def generate_p45(conn, args):
    """Generate P45 leaver certificate."""
    employee_id = args.employee_id

    if not employee_id:
        err("--employee-id is required.")

    employee = conn.execute(
        "SELECT * FROM employee WHERE id = ?", (employee_id,)
    ).fetchone()
    if not employee:
        err(f"Employee not found: {employee_id}")
    emp = row_to_dict(employee)

    nino = emp.get("ssn", "")
    masked = f"{nino[:2]}****{nino[-1]}" if len(nino) >= 9 else nino
    leaving_date = emp.get("date_of_leaving", "")

    # Total pay and tax up to leaving date
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(gross_pay AS REAL)), 0) as total_gross,
                  COALESCE(SUM(CAST(total_deductions AS REAL)), 0) as total_tax
           FROM salary_slip
           WHERE employee_id = ? AND status = 'submitted'""",
        (employee_id,),
    ).fetchone()

    ok({
        "form": "P45",
        "employee_name": emp.get("full_name", ""),
        "nino_masked": masked,
        "leaving_date": leaving_date,
        "total_pay_to_date": str(round_currency(to_decimal(str(row["total_gross"])))),
        "total_tax_to_date": str(round_currency(to_decimal(str(row["total_tax"])))),
    })


def compute_cis_deduction(conn, args):
    """Compute Construction Industry Scheme deduction."""
    amount = to_decimal(args.amount)
    cis_rate_type = (getattr(args, "cis_rate", None) or "standard").lower()

    rate_map = {
        "standard": Decimal("20"),
        "higher": Decimal("30"),
        "gross": Decimal("0"),
    }
    if cis_rate_type not in rate_map:
        err(f"Unknown CIS rate: {cis_rate_type}. Use standard, higher, or gross.")

    rate = rate_map[cis_rate_type]
    deduction = round_currency(amount * rate / Decimal("100"))
    net_payment = round_currency(amount - deduction)

    ok({
        "gross_amount": str(round_currency(amount)),
        "deduction_rate": str(rate),
        "deduction_amount": str(deduction),
        "net_payment": str(net_payment),
        "cis_type": cis_rate_type,
    })


# ---------------------------------------------------------------------------
# Report Actions
# ---------------------------------------------------------------------------

def uk_tax_summary(conn, args):
    """UK tax dashboard: VAT + payroll totals."""
    company = _get_company(conn, args.company_id)
    _check_uk_company(company)
    cid = company["id"]
    from_date = args.from_date
    to_date = args.to_date

    if not from_date or not to_date:
        err("--from-date and --to-date are required.")

    # VAT collected (sales)
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total
           FROM sales_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date <= ? AND status = 'submitted'""",
        (cid, from_date, to_date),
    ).fetchone()
    vat_collected = round_currency(to_decimal(str(row["total"])))

    # VAT reclaimed (purchases)
    row = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total
           FROM purchase_invoice
           WHERE company_id = ? AND posting_date >= ? AND posting_date <= ? AND status = 'submitted'""",
        (cid, from_date, to_date),
    ).fetchone()
    vat_reclaimed = round_currency(to_decimal(str(row["total"])))

    net_vat = round_currency(vat_collected - vat_reclaimed)

    # Payroll totals
    row = conn.execute(
        """SELECT COUNT(*) as slip_count,
                  COALESCE(SUM(CAST(gross_pay AS REAL)), 0) as total_gross,
                  COALESCE(SUM(CAST(total_deductions AS REAL)), 0) as total_deductions,
                  COALESCE(SUM(CAST(net_pay AS REAL)), 0) as total_net
           FROM salary_slip
           WHERE company_id = ? AND period_start >= ? AND period_start <= ? AND status = 'submitted'""",
        (cid, from_date, to_date),
    ).fetchone()

    ok({
        "report": "UK Tax Summary",
        "period": f"{from_date} to {to_date}",
        "vat_collected": str(vat_collected),
        "vat_reclaimed": str(vat_reclaimed),
        "net_vat": str(net_vat),
        "payroll_slips": row["slip_count"],
        "total_gross_pay": str(round_currency(to_decimal(str(row["total_gross"])))),
        "total_deductions": str(round_currency(to_decimal(str(row["total_deductions"])))),
        "total_net_pay": str(round_currency(to_decimal(str(row["total_net"])))),
        "company_id": cid,
    })


def available_reports(conn, args):
    """List all available UK reports."""
    ok({
        "reports": [
            {"name": "VAT Return", "action": "generate-vat-return", "description": "9-box MTD-compatible VAT return"},
            {"name": "MTD Payload", "action": "generate-mtd-payload", "description": "HMRC MTD JSON structure"},
            {"name": "EC Sales List", "action": "generate-ec-sales-list", "description": "NI Protocol sales to EU"},
            {"name": "FPS", "action": "generate-fps", "description": "Full Payment Submission (RTI)"},
            {"name": "EPS", "action": "generate-eps", "description": "Employer Payment Summary"},
            {"name": "P60", "action": "generate-p60", "description": "End-of-year certificate"},
            {"name": "P45", "action": "generate-p45", "description": "Leaver certificate"},
            {"name": "UK Tax Summary", "action": "uk-tax-summary", "description": "VAT + PAYE + NI dashboard"},
            {"name": "UK Payroll Summary", "action": "uk-payroll-summary", "description": "Monthly payroll breakdown"},
        ],
    })


def status(conn, args):
    """Show skill status and configuration."""
    asset_files = [
        "uk_vat_rates.json", "uk_vat_categories.json", "uk_ni_rates.json",
        "uk_income_tax_bands.json", "uk_student_loan_thresholds.json",
        "uk_pension_rates.json", "uk_coa_frs102.json", "uk_regions.json",
    ]
    assets = {}
    for f in asset_files:
        assets[f] = os.path.exists(os.path.join(ASSETS_DIR, f))

    result = {
        "skill": "erpclaw-region-uk",
        "version": "1.0.0",
        "description": "UK Regional Compliance (VAT, PAYE, NI, RTI, CIS)",
        "asset_files": assets,
    }

    # Check company config if company_id provided
    if args.company_id:
        try:
            company = _get_company(conn, args.company_id)
            _check_uk_company(company)
            cid = company["id"]
            try:
                vat_row = conn.execute(
                    "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'vat_number'",
                    (cid,),
                ).fetchone()
                result["vat_configured"] = vat_row is not None
            except Exception:
                result["vat_configured"] = False
            result["company_id"] = cid
            result["company_name"] = company.get("name", "")
        except SystemExit:
            pass

    result["status"] = "ok"
    print(json.dumps(result, indent=2))
    sys.exit(0)


# ---------------------------------------------------------------------------
# Action Router
# ---------------------------------------------------------------------------

ACTIONS = {
    "seed-uk-defaults": seed_uk_defaults,
    "setup-vat": setup_vat,
    "seed-uk-coa": seed_uk_coa,
    "seed-uk-payroll": seed_uk_payroll,
    "validate-vat-number": validate_vat_number,
    "validate-utr": validate_utr,
    "validate-nino": validate_nino,
    "validate-crn": validate_crn,
    "compute-vat": compute_vat,
    "compute-vat-inclusive": compute_vat_inclusive,
    "list-vat-rates": list_vat_rates,
    "compute-flat-rate-vat": compute_flat_rate_vat,
    "generate-vat-return": generate_vat_return,
    "generate-mtd-payload": generate_mtd_payload,
    "generate-ec-sales-list": generate_ec_sales_list,
    "compute-paye": compute_paye,
    "compute-ni": compute_ni,
    "compute-student-loan": compute_student_loan,
    "compute-pension": compute_pension,
    "uk-payroll-summary": uk_payroll_summary,
    "generate-fps": generate_fps,
    "generate-eps": generate_eps,
    "generate-p60": generate_p60,
    "generate-p45": generate_p45,
    "compute-cis-deduction": compute_cis_deduction,
    "uk-tax-summary": uk_tax_summary,
    "available-reports": available_reports,
    "status": status,
}


def main():
    parser = argparse.ArgumentParser(description="ERPClaw UK Regional Skill")
    parser.add_argument("--action", required=True, help="Action to perform")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    parser.add_argument("--company-id", default=None, help="Company ID")

    # VAT flags
    parser.add_argument("--amount", default=None, help="Amount for tax computation")
    parser.add_argument("--gross-amount", default=None, help="Gross amount for inclusive VAT")
    parser.add_argument("--rate-type", default=None, help="VAT rate type (standard/reduced/zero)")
    parser.add_argument("--vat-number", default=None, help="UK VAT number")
    parser.add_argument("--gross-turnover", default=None, help="Gross turnover for flat rate VAT")
    parser.add_argument("--category", default=None, help="Flat rate scheme category")
    parser.add_argument("--first-year", default=None, help="First year discount flag")

    # Payroll flags
    parser.add_argument("--annual-income", default=None, help="Annual income for tax/NI")
    parser.add_argument("--annual-salary", default=None, help="Annual salary for pension")
    parser.add_argument("--plan", default=None, help="Student loan plan (1/2/4/5/PG)")
    parser.add_argument("--region", default=None, help="UK region (ENG/SCO/WAL/NIR)")
    parser.add_argument("--pay-period", default=None, help="Pay period (annual/monthly/weekly)")

    # Compliance flags
    parser.add_argument("--period", default=None, help="Period number (month)")
    parser.add_argument("--month", default=None, help="Month number")
    parser.add_argument("--year", default=None, help="Year")
    parser.add_argument("--tax-year", default=None, help="Tax year (e.g., 2025)")
    parser.add_argument("--employee-id", default=None, help="Employee ID")

    # Validation flags
    parser.add_argument("--utr", default=None, help="Unique Taxpayer Reference")
    parser.add_argument("--nino", default=None, help="National Insurance Number")
    parser.add_argument("--crn", default=None, help="Companies House Registration Number")

    # Report flags
    parser.add_argument("--from-date", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to-date", default=None, help="End date (YYYY-MM-DD)")

    # CIS flags
    parser.add_argument("--cis-rate", default=None, help="CIS rate (standard/higher/gross)")

    args, _unknown = parser.parse_known_args()
    action_name = args.action.lower()

    if action_name not in ACTIONS:
        err(f"Unknown action: {action_name}",
             suggestion=f"Available actions: {', '.join(sorted(ACTIONS.keys()))}")

    # Connect to DB
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
