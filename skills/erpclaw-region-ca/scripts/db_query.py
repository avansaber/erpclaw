#!/usr/bin/env python3
"""ERPClaw Canada Regional Skill — db_query.py

Pure overlay skill for Canada-specific compliance: GST/HST/PST/QST,
payroll (CPP/CPP2/QPP/EI, federal+provincial tax), T4/T4A/ROE/PD7A,
Canadian CoA (ASPE), and ID validation (BN/SIN).

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


def _check_ca_company(company):
    country = (company.get("country") or "").upper()
    if country not in ("CA", "CANADA"):
        err(
            f"This action is for Canadian companies only. Company country must be CA.",
            suggestion="Create a Canadian company with erpclaw-setup first.",
        )


def _get_company_province(conn, company):
    """Get province from company record or regional_settings."""
    province = company.get("province", "")
    if province:
        return province.upper()
    try:
        row = conn.execute(
            "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'province'",
            (company["id"],),
        ).fetchone()
        if row:
            return row["value"].upper()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Embedded tax rate data (used when asset files not present)
# ---------------------------------------------------------------------------

_GST_HST_RATES = {
    "AB": {"name": "Alberta", "tax_type": "GST", "gst_rate": "5", "combined_rate": "5"},
    "BC": {"name": "British Columbia", "tax_type": "GST+PST", "gst_rate": "5", "pst_rate": "7", "combined_rate": "12"},
    "MB": {"name": "Manitoba", "tax_type": "GST+RST", "gst_rate": "5", "pst_rate": "7", "combined_rate": "12"},
    "NB": {"name": "New Brunswick", "tax_type": "HST", "gst_rate": "5", "hst_rate": "15", "combined_rate": "15"},
    "NL": {"name": "Newfoundland and Labrador", "tax_type": "HST", "gst_rate": "5", "hst_rate": "15", "combined_rate": "15"},
    "NS": {"name": "Nova Scotia", "tax_type": "HST", "gst_rate": "5", "hst_rate": "15", "combined_rate": "15"},
    "NT": {"name": "Northwest Territories", "tax_type": "GST", "gst_rate": "5", "combined_rate": "5"},
    "NU": {"name": "Nunavut", "tax_type": "GST", "gst_rate": "5", "combined_rate": "5"},
    "ON": {"name": "Ontario", "tax_type": "HST", "gst_rate": "5", "hst_rate": "13", "combined_rate": "13"},
    "PE": {"name": "Prince Edward Island", "tax_type": "HST", "gst_rate": "5", "hst_rate": "15", "combined_rate": "15"},
    "QC": {"name": "Quebec", "tax_type": "GST+QST", "gst_rate": "5", "qst_rate": "9.975", "combined_rate": "14.975"},
    "SK": {"name": "Saskatchewan", "tax_type": "GST+PST", "gst_rate": "5", "pst_rate": "6", "combined_rate": "11"},
    "YT": {"name": "Yukon", "tax_type": "GST", "gst_rate": "5", "combined_rate": "5"},
}

_CPP_RATES = {
    "year": 2025,
    "rate": "5.95",
    "max_pensionable_earnings": "71300",
    "basic_exemption": "3500",
    "max_employee_contribution": "4034.10",
    "cpp2_rate": "4",
    "cpp2_first_ceiling": "71300",
    "cpp2_second_ceiling": "81200",
    "cpp2_max_employee_contribution": "396.00",
}

_QPP_RATES = {
    "year": 2025,
    "rate": "6.40",
    "max_pensionable_earnings": "71300",
    "basic_exemption": "3500",
    "max_employee_contribution": "4340.40",
}

_EI_RATES = {
    "year": 2025,
    "rate": "1.64",
    "max_insurable_earnings": "65700",
    "max_employee_premium": "1077.48",
    "employer_multiplier": "1.4",
    "quebec_rate": "1.32",
    "quebec_max_employee_premium": "867.24",
}

_FEDERAL_TAX_BRACKETS = {
    "year": 2025,
    "basic_personal_amount": "16129",
    "brackets": [
        {"from": 0, "to": 57375, "rate": "15"},
        {"from": 57375, "to": 114750, "rate": "20.5"},
        {"from": 114750, "to": 158468, "rate": "26"},
        {"from": 158468, "to": 220000, "rate": "29"},
        {"from": 220000, "to": None, "rate": "33"},
    ],
}

_PROVINCIAL_TAX_BRACKETS = {
    "ON": {
        "name": "Ontario",
        "basic_personal_amount": "11865",
        "surtax": True,
        "surtax_threshold_1": "4991",
        "surtax_rate_1": "20",
        "surtax_threshold_2": "6387",
        "surtax_rate_2": "36",
        "brackets": [
            {"from": 0, "to": 51446, "rate": "5.05"},
            {"from": 51446, "to": 102894, "rate": "9.15"},
            {"from": 102894, "to": 150000, "rate": "11.16"},
            {"from": 150000, "to": 220000, "rate": "12.16"},
            {"from": 220000, "to": None, "rate": "13.16"},
        ],
    },
    "BC": {
        "name": "British Columbia",
        "basic_personal_amount": "12580",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 47937, "rate": "5.06"},
            {"from": 47937, "to": 95875, "rate": "7.7"},
            {"from": 95875, "to": 110076, "rate": "10.5"},
            {"from": 110076, "to": 133664, "rate": "12.29"},
            {"from": 133664, "to": 181232, "rate": "14.7"},
            {"from": 181232, "to": 252752, "rate": "16.8"},
            {"from": 252752, "to": None, "rate": "20.5"},
        ],
    },
    "AB": {
        "name": "Alberta",
        "basic_personal_amount": "21003",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 148269, "rate": "10"},
            {"from": 148269, "to": 177922, "rate": "12"},
            {"from": 177922, "to": 237230, "rate": "13"},
            {"from": 237230, "to": 355845, "rate": "14"},
            {"from": 355845, "to": None, "rate": "15"},
        ],
    },
    "QC": {
        "name": "Quebec",
        "basic_personal_amount": "18056",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 51780, "rate": "14"},
            {"from": 51780, "to": 103545, "rate": "19"},
            {"from": 103545, "to": 126000, "rate": "24"},
            {"from": 126000, "to": None, "rate": "25.75"},
        ],
    },
    "SK": {
        "name": "Saskatchewan",
        "basic_personal_amount": "18491",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 52057, "rate": "10.5"},
            {"from": 52057, "to": 148734, "rate": "12.5"},
            {"from": 148734, "to": None, "rate": "14.5"},
        ],
    },
    "MB": {
        "name": "Manitoba",
        "basic_personal_amount": "15780",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 47000, "rate": "10.8"},
            {"from": 47000, "to": 100000, "rate": "12.75"},
            {"from": 100000, "to": None, "rate": "17.4"},
        ],
    },
    "NB": {
        "name": "New Brunswick",
        "basic_personal_amount": "13044",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 49958, "rate": "9.4"},
            {"from": 49958, "to": 99916, "rate": "14"},
            {"from": 99916, "to": 185064, "rate": "16"},
            {"from": 185064, "to": None, "rate": "19.5"},
        ],
    },
    "NS": {
        "name": "Nova Scotia",
        "basic_personal_amount": "8481",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 29590, "rate": "8.79"},
            {"from": 29590, "to": 59180, "rate": "14.95"},
            {"from": 59180, "to": 93000, "rate": "16.67"},
            {"from": 93000, "to": 150000, "rate": "17.5"},
            {"from": 150000, "to": None, "rate": "21"},
        ],
    },
    "NL": {
        "name": "Newfoundland and Labrador",
        "basic_personal_amount": "10818",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 43198, "rate": "8.7"},
            {"from": 43198, "to": 86395, "rate": "14.5"},
            {"from": 86395, "to": 154244, "rate": "15.8"},
            {"from": 154244, "to": 215943, "rate": "17.8"},
            {"from": 215943, "to": 275870, "rate": "19.8"},
            {"from": 275870, "to": 551739, "rate": "20.8"},
            {"from": 551739, "to": 1103478, "rate": "21.3"},
            {"from": 1103478, "to": None, "rate": "21.8"},
        ],
    },
    "PE": {
        "name": "Prince Edward Island",
        "basic_personal_amount": "13500",
        "surtax": True,
        "surtax_threshold_1": "12500",
        "surtax_rate_1": "10",
        "brackets": [
            {"from": 0, "to": 32656, "rate": "9.65"},
            {"from": 32656, "to": 64313, "rate": "13.63"},
            {"from": 64313, "to": 105000, "rate": "16.65"},
            {"from": 105000, "to": 140000, "rate": "18"},
            {"from": 140000, "to": None, "rate": "18.75"},
        ],
    },
    "NT": {
        "name": "Northwest Territories",
        "basic_personal_amount": "17373",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 50597, "rate": "5.9"},
            {"from": 50597, "to": 101198, "rate": "8.6"},
            {"from": 101198, "to": 164525, "rate": "12.2"},
            {"from": 164525, "to": None, "rate": "14.05"},
        ],
    },
    "NU": {
        "name": "Nunavut",
        "basic_personal_amount": "18767",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 53268, "rate": "4"},
            {"from": 53268, "to": 106537, "rate": "7"},
            {"from": 106537, "to": 173205, "rate": "9"},
            {"from": 173205, "to": None, "rate": "11.5"},
        ],
    },
    "YT": {
        "name": "Yukon",
        "basic_personal_amount": "16129",
        "surtax": False,
        "brackets": [
            {"from": 0, "to": 57375, "rate": "6.4"},
            {"from": 57375, "to": 114750, "rate": "9"},
            {"from": 114750, "to": 158468, "rate": "10.9"},
            {"from": 158468, "to": 500000, "rate": "12.8"},
            {"from": 500000, "to": None, "rate": "15"},
        ],
    },
}


def _try_load_json_asset(filename):
    """Try to load a JSON asset file, returning None if not found."""
    path = os.path.join(ASSETS_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def _get_gst_hst_rates():
    """Load GST/HST rates as dict keyed by province code.
    Asset file has {"provinces": [...]} list format; normalize to dict."""
    data = _try_load_json_asset("ca_gst_hst_rates.json")
    if data is not None:
        # Asset file: {"provinces": [{"code": "ON", "tax_type": "HST", ...}, ...]}
        if "provinces" in data and isinstance(data["provinces"], list):
            pst_data = _try_load_json_asset("ca_pst_rates.json")
            pst_map = {}
            if pst_data and "provinces" in pst_data:
                for p in pst_data["provinces"]:
                    pst_map[p["code"]] = p.get("rate", "0")
            result = {}
            for prov in data["provinces"]:
                code = prov["code"]
                entry = {
                    "name": prov.get("name", code),
                    "tax_type": prov["tax_type"],
                    "gst_rate": data.get("gst_rate", "5"),
                    "combined_rate": prov["combined_rate"],
                }
                if prov["tax_type"] == "HST":
                    entry["hst_rate"] = prov["combined_rate"]
                    entry["name"] = prov.get("name", _GST_HST_RATES.get(code, {}).get("name", code))
                elif "PST" in prov["tax_type"]:
                    entry["pst_rate"] = pst_map.get(code, "0")
                    entry["name"] = prov.get("name", _GST_HST_RATES.get(code, {}).get("name", code))
                elif "RST" in prov["tax_type"]:
                    entry["pst_rate"] = pst_map.get(code, "7")
                    entry["name"] = prov.get("name", _GST_HST_RATES.get(code, {}).get("name", code))
                elif "QST" in prov["tax_type"]:
                    entry["qst_rate"] = pst_map.get(code, "9.975")
                    entry["name"] = prov.get("name", _GST_HST_RATES.get(code, {}).get("name", code))
                else:
                    entry["name"] = prov.get("name", _GST_HST_RATES.get(code, {}).get("name", code))
                # Fill name from embedded fallback if not in asset
                if entry["name"] == code and code in _GST_HST_RATES:
                    entry["name"] = _GST_HST_RATES[code]["name"]
                result[code] = entry
            return result
        return data
    return _GST_HST_RATES


def _get_cpp_rates():
    """Normalize CPP rates to flat dict format."""
    data = _try_load_json_asset("ca_cpp_rates.json")
    if data is not None:
        # Asset: {"cpp": {"rate": ..., ...}, "cpp2": {...}}
        if "cpp" in data:
            cpp = data["cpp"]
            cpp2 = data.get("cpp2", {})
            return {
                "year": data.get("year", 2026),
                "rate": cpp.get("rate", "5.95"),
                "max_pensionable_earnings": cpp.get("max_pensionable_earnings", "74600"),
                "basic_exemption": cpp.get("basic_exemption", "3500"),
                "max_employee_contribution": cpp.get("max_employee_contribution", "4230.45"),
                "cpp2_rate": cpp2.get("rate", "4"),
                "cpp2_first_ceiling": cpp2.get("first_additional_ceiling", cpp2.get("cpp2_first_ceiling", "74600")),
                "cpp2_second_ceiling": cpp2.get("second_additional_ceiling", cpp2.get("cpp2_second_ceiling", "85000")),
                "cpp2_max_employee_contribution": cpp2.get("max_employee_contribution", "416.00"),
            }
        return data
    return _CPP_RATES


def _get_qpp_rates():
    """Normalize QPP rates to flat dict format."""
    data = _try_load_json_asset("ca_cpp_rates.json")  # QPP is in same file
    if data is not None and "qpp" in data:
        qpp = data["qpp"]
        return {
            "year": data.get("year", 2026),
            "rate": qpp.get("rate", "6.40"),
            "max_pensionable_earnings": qpp.get("max_pensionable_earnings", "74600"),
            "basic_exemption": qpp.get("basic_exemption", "3500"),
            "max_employee_contribution": qpp.get("max_employee_contribution", "4479.30"),
        }
    data2 = _try_load_json_asset("ca_qpp_rates.json")
    if data2 is not None:
        return data2
    return _QPP_RATES


def _get_ei_rates():
    """Normalize EI rates to flat dict format."""
    data = _try_load_json_asset("ca_ei_rates.json")
    if data is not None:
        # Asset: {"employee_rate": ..., "quebec": {"employee_rate": ...}, ...}
        if "employee_rate" in data:
            qc = data.get("quebec", {})
            return {
                "year": data.get("year", 2026),
                "rate": data.get("employee_rate", "1.63"),
                "max_insurable_earnings": data.get("max_insurable_earnings", "68900"),
                "max_employee_premium": data.get("max_employee_premium", "1123.07"),
                "employer_multiplier": data.get("employer_multiple", "1.4"),
                "quebec_rate": qc.get("employee_rate", "1.30"),
                "quebec_max_employee_premium": qc.get("max_employee_premium", "895.70"),
            }
        return data
    return _EI_RATES


def _get_federal_brackets():
    """Normalize federal tax brackets. Asset uses string values and 999999999 for unlimited."""
    data = _try_load_json_asset("ca_federal_tax_brackets.json")
    if data is not None:
        # Normalize brackets: convert "999999999" to None for the last bracket
        if "brackets" in data:
            brackets = []
            for b in data["brackets"]:
                nb = dict(b)
                # Convert string "from"/"to" to int for the embedded format compatibility
                # but keep as-is since _progressive_tax uses to_decimal(str(...))
                if nb.get("to") is not None:
                    to_val = int(str(nb["to"]).replace(",", ""))
                    if to_val >= 999999999:
                        nb["to"] = None
                brackets.append(nb)
            return {
                "year": data.get("year", 2026),
                "basic_personal_amount": data.get("basic_personal_amount", "16452"),
                "brackets": brackets,
            }
        return data
    return _FEDERAL_TAX_BRACKETS


def _get_provincial_brackets():
    """Normalize provincial tax brackets. Asset has {"provinces": {"ON": {...}}}.
    Returns dict keyed by province code with normalized bracket/surtax data."""
    data = _try_load_json_asset("ca_provincial_tax_brackets.json")
    if data is not None:
        provinces_raw = data.get("provinces", data)
        if not isinstance(provinces_raw, dict):
            return _PROVINCIAL_TAX_BRACKETS
        result = {}
        for code, prov in provinces_raw.items():
            entry = {
                "name": prov.get("name", code),
                "basic_personal_amount": prov.get("basic_personal_amount", "0"),
                "surtax": False,
                "brackets": [],
            }
            for b in prov.get("brackets", []):
                nb = dict(b)
                if nb.get("to") is not None:
                    to_val = int(str(nb["to"]).replace(",", ""))
                    if to_val >= 999999999:
                        nb["to"] = None
                entry["brackets"].append(nb)
            # Handle surtax (Ontario has it as nested dict in asset)
            surtax_data = prov.get("surtax")
            if surtax_data and isinstance(surtax_data, dict):
                entry["surtax"] = True
                entry["surtax_threshold_1"] = surtax_data.get("threshold_1", "0")
                entry["surtax_rate_1"] = surtax_data.get("rate_1", "0")
                if "threshold_2" in surtax_data:
                    entry["surtax_threshold_2"] = surtax_data.get("threshold_2", "0")
                    entry["surtax_rate_2"] = surtax_data.get("rate_2", "0")
            elif surtax_data is True:
                entry["surtax"] = True
            result[code] = entry
        return result
    return _PROVINCIAL_TAX_BRACKETS


def _resolve_periods(args):
    """Convert --pay-period string to numeric periods, or use --pay-periods."""
    period_map = {"annual": 1, "monthly": 12, "biweekly": 26, "weekly": 52, "semimonthly": 24}
    if getattr(args, "pay_period", None):
        val = args.pay_period.lower()
        if val in period_map:
            return period_map[val]
        try:
            return int(val)
        except ValueError:
            err(f"Invalid pay-period: {val}. Use annual, monthly, biweekly, weekly, or semimonthly.")
    return int(args.pay_periods or 12)


def _progressive_tax(taxable_income, brackets):
    """Compute progressive tax from a list of bracket dicts."""
    tax = Decimal("0")
    remaining = taxable_income
    breakdown = []
    marginal_rate = Decimal("0")
    for bracket in brackets:
        if remaining <= Decimal("0"):
            break
        b_from = to_decimal(str(bracket["from"]))
        b_to = to_decimal(str(bracket["to"])) if bracket["to"] is not None else None
        rate = to_decimal(str(bracket["rate"])) / Decimal("100")
        if b_to is not None:
            width = b_to - b_from
            applicable = min(remaining, width)
        else:
            applicable = remaining
        slab_tax = round_currency(applicable * rate)
        tax += slab_tax
        remaining -= applicable
        marginal_rate = to_decimal(str(bracket["rate"]))
        if applicable > Decimal("0"):
            breakdown.append({
                "range": "${:,.0f} - {}".format(b_from, "unlimited" if b_to is None else "${:,.0f}".format(b_to)),
                "rate": f"{bracket['rate']}%",
                "taxable_in_bracket": str(round_currency(applicable)),
                "tax": str(slab_tax),
            })
    return tax, breakdown, marginal_rate


# ---------------------------------------------------------------------------
# Validation actions
# ---------------------------------------------------------------------------

_VALID_BN_PROGRAMS = ("RT", "RP", "RC", "RM", "RZ")


def validate_business_number(conn, args):
    """Validate a Canadian Business Number (BN)."""
    if not args.bn:
        err("--bn is required")
    bn = args.bn.strip().upper()

    # Strip any dashes/spaces
    bn_clean = re.sub(r"[\s\-]", "", bn)

    # Base BN: 9 digits
    # Full BN: 9 digits + 2-letter program code + 4-digit reference
    if len(bn_clean) == 9:
        if not bn_clean.isdigit():
            ok({"bn": bn, "valid": False, "error": "BN base must be exactly 9 digits",
                 "format": "9 digits (base) or 9 digits + program code + 4 digits (full)"})
            return
        ok({
            "bn": bn_clean,
            "valid": True,
            "format": "base",
            "base_number": bn_clean,
            "error": None,
            "note": "Base BN (9 digits). Full format is BN + program code (RT/RP/RC/RM/RZ) + 4-digit reference.",
        })
        return

    if len(bn_clean) == 15:
        base = bn_clean[:9]
        program = bn_clean[9:11]
        ref = bn_clean[11:15]
        errors = []
        if not base.isdigit():
            errors.append("First 9 characters must be digits")
        if program not in _VALID_BN_PROGRAMS:
            errors.append(f"Program code '{program}' invalid. Must be one of: {', '.join(_VALID_BN_PROGRAMS)}")
        if not ref.isdigit():
            errors.append("Last 4 characters (reference number) must be digits")
        if errors:
            ok({"bn": bn, "valid": False, "error": "; ".join(errors)})
            return
        ok({
            "bn": bn_clean,
            "valid": True,
            "format": "full",
            "error": None,
            "base_bn": base,
            "program_code": program,
            "program_name": {
                "RT": "GST/HST", "RP": "Payroll", "RC": "Corporate Income Tax",
                "RM": "Import/Export", "RZ": "Information Returns",
            }.get(program, "Unknown"),
            "reference_number": ref,
        })
        return

    ok({
        "bn": bn,
        "valid": False,
        "error": f"BN must be 9 digits (base) or 15 characters (9 digits + 2-letter program + 4 digits). Got {len(bn_clean)} characters.",
        "format": "9 digits (base) or 9 digits + program code + 4 digits (full)",
    })


def validate_sin(conn, args):
    """Validate a Canadian Social Insurance Number (SIN) using Luhn checksum."""
    if not args.sin:
        err("--sin is required")
    sin = re.sub(r"[\s\-]", "", args.sin.strip())

    if len(sin) != 9:
        ok({"sin": args.sin, "valid": False, "error": "SIN must be exactly 9 digits"})
        return
    if not sin.isdigit():
        ok({"sin": args.sin, "valid": False, "error": "SIN must contain only digits"})
        return

    first_digit = int(sin[0])

    # Luhn checksum
    total = 0
    for i, ch in enumerate(sin):
        d = int(ch)
        if i % 2 == 1:  # double every second digit (0-indexed)
            d *= 2
            if d > 9:
                d -= 9
        total += d
    luhn_valid = (total % 10 == 0)

    category = "regular"
    note = ""
    if first_digit == 8:
        category = "temporary_resident"
        note = "Temporary resident SIN (starts with 8)"
    elif first_digit == 9:
        category = "not_issued_to_individuals"

    masked = sin[:3] + "***" + sin[6:]
    result = {
        "sin": masked,
        "valid": luhn_valid,
        "error": None if luhn_valid else "Luhn checksum validation failed",
        "category": category,
        "first_digit": first_digit,
    }
    if note:
        result["note"] = note
    ok(result)


# ---------------------------------------------------------------------------
# Tax Computation — GST/HST/PST/QST
# ---------------------------------------------------------------------------

def compute_gst(conn, args):
    """Compute GST at 5% on an amount."""
    if not args.amount:
        err("--amount is required")
    try:
        amount = to_decimal(args.amount)
    except (InvalidOperation, ValueError):
        err(f"Invalid amount: {args.amount}")

    gst_rate = Decimal("5")
    gst_amount = round_currency(amount * gst_rate / Decimal("100"))
    total = round_currency(amount + gst_amount)

    ok({
        "net_amount": str(round_currency(amount)),
        "gst_rate": "5",
        "gst_amount": str(gst_amount),
        "total": str(total),
    })


def compute_hst(conn, args):
    """Compute HST for an HST province."""
    if not args.amount:
        err("--amount is required")
    if not args.province:
        err("--province is required")
    try:
        amount = to_decimal(args.amount)
    except (InvalidOperation, ValueError):
        err(f"Invalid amount: {args.amount}")

    province = args.province.upper()
    rates = _get_gst_hst_rates()
    prov_data = rates.get(province)
    if not prov_data:
        err(f"Province not found: {province}",
             suggestion="Use a 2-letter province code: ON, NS, NB, NL, PE, etc.")

    tax_type = prov_data.get("tax_type", "")
    if "HST" not in tax_type:
        err(f"{prov_data['name']} ({province}) does not use HST (uses {tax_type}).",
             suggestion=f"Use compute-sales-tax for {province} to get the correct tax breakdown.")

    combined = to_decimal(prov_data["hst_rate"])
    federal_portion = Decimal("5")
    provincial_portion = combined - federal_portion
    hst_amount = round_currency(amount * combined / Decimal("100"))
    federal_amount = round_currency(amount * federal_portion / Decimal("100"))
    provincial_amount = round_currency(amount * provincial_portion / Decimal("100"))
    total = round_currency(amount + hst_amount)

    ok({
        "net_amount": str(round_currency(amount)),
        "province": province,
        "province_name": prov_data["name"],
        "hst_rate": str(combined),
        "hst_amount": str(hst_amount),
        "federal_portion_5pct": str(federal_amount),
        "provincial_portion": str(provincial_amount),
        "provincial_rate": str(provincial_portion),
        "total": str(total),
    })


def compute_pst(conn, args):
    """Compute PST/RST for a province."""
    if not args.amount:
        err("--amount is required")
    if not args.province:
        err("--province is required")
    try:
        amount = to_decimal(args.amount)
    except (InvalidOperation, ValueError):
        err(f"Invalid amount: {args.amount}")

    province = args.province.upper()
    rates = _get_gst_hst_rates()
    prov_data = rates.get(province)
    if not prov_data:
        err(f"Province not found: {province}")

    pst_rate_str = prov_data.get("pst_rate", "0")
    pst_rate = to_decimal(pst_rate_str)
    tax_type = prov_data.get("tax_type", "")

    # Provinces without separate PST
    if "PST" not in tax_type and "RST" not in tax_type:
        ok({
            "net_amount": str(round_currency(amount)),
            "province": province,
            "province_name": prov_data["name"],
            "pst_rate": "0",
            "pst_amount": "0.00",
            "total": str(round_currency(amount)),
            "note": f"{prov_data['name']} does not have a separate PST. Tax type: {tax_type}.",
        })
        return

    pst_amount = round_currency(amount * pst_rate / Decimal("100"))
    total = round_currency(amount + pst_amount)

    ok({
        "net_amount": str(round_currency(amount)),
        "province": province,
        "province_name": prov_data["name"],
        "pst_rate": str(pst_rate),
        "pst_amount": str(pst_amount),
        "total": str(total),
    })


def compute_qst(conn, args):
    """Compute Quebec Sales Tax (QST) at 9.975%."""
    if not args.amount:
        err("--amount is required")
    try:
        amount = to_decimal(args.amount)
    except (InvalidOperation, ValueError):
        err(f"Invalid amount: {args.amount}")

    province = (args.province or "QC").upper()
    if province != "QC":
        err("QST applies only to Quebec (QC).",
             suggestion="Use compute-sales-tax for other provinces.")

    qst_rate = Decimal("9.975")
    qst_amount = round_currency(amount * qst_rate / Decimal("100"))
    total = round_currency(amount + qst_amount)

    ok({
        "net_amount": str(round_currency(amount)),
        "province": "QC",
        "province_name": "Quebec",
        "qst_rate": "9.975",
        "qst_amount": str(qst_amount),
        "total": str(total),
    })


def compute_sales_tax(conn, args):
    """All-in-one: determine which taxes apply for a province and compute total."""
    if not args.amount:
        err("--amount is required")
    if not args.province:
        err("--province is required")
    try:
        amount = to_decimal(args.amount)
    except (InvalidOperation, ValueError):
        err(f"Invalid amount: {args.amount}")

    province = args.province.upper()
    rates = _get_gst_hst_rates()
    prov_data = rates.get(province)
    if not prov_data:
        err(f"Province not found: {province}",
             suggestion="Use a 2-letter province/territory code: AB, BC, MB, NB, NL, NS, NT, NU, ON, PE, QC, SK, YT")

    tax_type = prov_data["tax_type"]
    result = {
        "net_amount": str(round_currency(amount)),
        "province": province,
        "province_name": prov_data["name"],
        "tax_type": tax_type,
        "taxes": [],
    }

    total_tax = Decimal("0")

    if tax_type == "HST":
        hst_rate = to_decimal(prov_data["hst_rate"])
        hst_amount = round_currency(amount * hst_rate / Decimal("100"))
        total_tax += hst_amount
        result["taxes"].append({"name": "HST", "rate": str(hst_rate), "amount": str(hst_amount)})
    elif tax_type == "GST":
        gst_rate = Decimal("5")
        gst_amount = round_currency(amount * gst_rate / Decimal("100"))
        total_tax += gst_amount
        result["taxes"].append({"name": "GST", "rate": "5", "amount": str(gst_amount)})
    elif tax_type == "GST+PST":
        gst_rate = Decimal("5")
        gst_amount = round_currency(amount * gst_rate / Decimal("100"))
        total_tax += gst_amount
        result["gst_amount"] = str(gst_amount)
        result["taxes"].append({"name": "GST", "rate": "5", "amount": str(gst_amount)})
        pst_rate = to_decimal(prov_data["pst_rate"])
        pst_amount = round_currency(amount * pst_rate / Decimal("100"))
        total_tax += pst_amount
        result["pst_amount"] = str(pst_amount)
        result["taxes"].append({"name": "PST", "rate": str(pst_rate), "amount": str(pst_amount)})
    elif tax_type == "GST+RST":
        gst_rate = Decimal("5")
        gst_amount = round_currency(amount * gst_rate / Decimal("100"))
        total_tax += gst_amount
        result["gst_amount"] = str(gst_amount)
        result["taxes"].append({"name": "GST", "rate": "5", "amount": str(gst_amount)})
        rst_rate = to_decimal(prov_data["pst_rate"])
        rst_amount = round_currency(amount * rst_rate / Decimal("100"))
        total_tax += rst_amount
        result["pst_amount"] = str(rst_amount)
        result["taxes"].append({"name": "RST", "rate": str(rst_rate), "amount": str(rst_amount)})
    elif tax_type == "GST+QST":
        gst_rate = Decimal("5")
        gst_amount = round_currency(amount * gst_rate / Decimal("100"))
        total_tax += gst_amount
        result["gst_amount"] = str(gst_amount)
        result["taxes"].append({"name": "GST", "rate": "5", "amount": str(gst_amount)})
        qst_rate = Decimal("9.975")
        qst_amount = round_currency(amount * qst_rate / Decimal("100"))
        total_tax += qst_amount
        result["qst_amount"] = str(qst_amount)
        result["taxes"].append({"name": "QST", "rate": "9.975", "amount": str(qst_amount)})

    combined = to_decimal(prov_data["combined_rate"])
    result["combined_rate"] = str(combined)
    result["total_tax"] = str(round_currency(total_tax))
    result["total_with_tax"] = str(round_currency(amount + total_tax))

    ok(result)


def list_tax_rates(conn, args):
    """List all Canadian provinces/territories with their tax types and rates."""
    rates = _get_gst_hst_rates()
    provinces = []
    for code in sorted(rates.keys()):
        prov = rates[code]
        entry = {
            "province_code": code,
            "province_name": prov["name"],
            "tax_type": prov["tax_type"],
            "gst_rate": prov.get("gst_rate", "5"),
            "combined_rate": prov["combined_rate"],
        }
        if "hst_rate" in prov:
            entry["hst_rate"] = prov["hst_rate"]
        if "pst_rate" in prov:
            entry["pst_rate"] = prov["pst_rate"]
        if "qst_rate" in prov:
            entry["qst_rate"] = prov["qst_rate"]
        provinces.append(entry)

    ok({"provinces": provinces, "total": len(provinces)})


def compute_itc(conn, args):
    """Compute Input Tax Credits from purchase invoices for a period."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)
    if not args.month or not args.year:
        err("--month and --year are required")

    month = int(args.month)
    year = int(args.year)
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month + 1:02d}-01" if month < 12 else f"{year + 1}-01-01"

    result = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as tax_amount,
                  COUNT(*) as invoice_count
           FROM purchase_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date < ?""",
        (company["id"], start_date, end_date),
    ).fetchone()

    total_purchase_tax = round_currency(to_decimal(str(result["tax_amount"])))
    eligible_itc = total_purchase_tax  # Full amount eligible (simplified)

    ok({
        "report": "Input Tax Credits (ITC)",
        "period": f"{year}-{month:02d}",
        "company": company.get("name", ""),
        "invoice_count": result["invoice_count"],
        "total_purchase_tax": str(total_purchase_tax),
        "eligible_itc": str(round_currency(eligible_itc)),
        "note": "ITCs claimed on GST/HST paid on business purchases. Restrictions apply for certain expenses (meals 50%, personal use, etc.)",
    })


# ---------------------------------------------------------------------------
# Seed / Setup actions
# ---------------------------------------------------------------------------

def seed_ca_defaults(conn, args):
    """Seed Canadian GST/HST/PST/QST accounts and tax templates for a company."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)
    company_id = company["id"]
    province = _get_company_province(conn, company)
    created = {"accounts": 0, "templates": 0, "categories": 0}

    # 1. Create GST/HST GL accounts: (name, root_type)
    tax_accounts = [
        ("GST Collected", "liability"),
        ("GST Paid on Purchases", "asset"),
        ("HST Collected", "liability"),
        ("HST Paid on Purchases", "asset"),
    ]
    # Province-specific accounts
    if province in ("BC", "SK"):
        tax_accounts.append(("PST Collected", "liability"))
        tax_accounts.append(("PST Paid on Purchases", "asset"))
    elif province == "MB":
        tax_accounts.append(("RST Collected", "liability"))
        tax_accounts.append(("RST Paid on Purchases", "asset"))
    elif province == "QC":
        tax_accounts.append(("QST Collected", "liability"))
        tax_accounts.append(("QST Paid on Purchases", "asset"))

    for name, root_type in tax_accounts:
        existing = conn.execute(
            "SELECT id FROM account WHERE name = ? AND company_id = ?",
            (name, company_id),
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO account (id, name, account_type, root_type, company_id,
                   is_group)
                   VALUES (?, ?, 'tax', ?, ?, 0)""",
                (str(uuid.uuid4()), name, root_type, company_id),
            )
            created["accounts"] += 1

    # 2. Create tax categories
    categories = [
        "GST 5%", "HST-ON 13%", "HST-Atlantic 15%",
        "PST-BC 7%", "PST-SK 6%", "RST-MB 7%", "QST-QC 9.975%",
        "Zero-rated", "Exempt",
    ]
    for cat_name in categories:
        existing = conn.execute(
            "SELECT id FROM tax_category WHERE name = ?", (cat_name,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO tax_category (id, name, description) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), cat_name, f"Canada tax category: {cat_name}"),
            )
            created["categories"] += 1

    # 3. Create tax templates
    templates = [
        ("Canada GST 5% Sales", "sales", "5"),
        ("Canada GST 5% Purchase", "purchase", "5"),
        ("Canada HST-ON 13% Sales", "sales", "13"),
        ("Canada HST-ON 13% Purchase", "purchase", "13"),
        ("Canada HST-Atlantic 15% Sales", "sales", "15"),
        ("Canada HST-Atlantic 15% Purchase", "purchase", "15"),
        ("Canada PST-BC 7% Sales", "sales", "7"),
        ("Canada PST-SK 6% Sales", "sales", "6"),
        ("Canada RST-MB 7% Sales", "sales", "7"),
        ("Canada QST-QC 9.975% Sales", "sales", "9.975"),
        ("Canada QST-QC 9.975% Purchase", "purchase", "9.975"),
    ]
    for tpl_name, tax_type, rate in templates:
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
            # Add template line with rate
            gst_acct = conn.execute(
                "SELECT id FROM account WHERE name LIKE 'GST%' AND company_id = ? LIMIT 1",
                (company_id,),
            ).fetchone()
            if gst_acct:
                conn.execute(
                    """INSERT INTO tax_template_line
                       (id, tax_template_id, tax_account_id, rate, charge_type, row_order, add_deduct)
                       VALUES (?, ?, ?, ?, 'on_net_total', 0, 'add')""",
                    (str(uuid.uuid4()), tid, gst_acct["id"], rate),
                )
            created["templates"] += 1

    audit(conn, "erpclaw-region-ca", "seed-ca-defaults", "company", company_id,
           new_values=created, description="Seeded Canada GST/HST/PST/QST defaults")
    conn.commit()
    ok({
        "message": "Canada defaults seeded successfully",
        "company_id": company_id,
        "province": province,
        "accounts_created": created["accounts"],
        "templates_created": created["templates"],
        "categories_created": created["categories"],
        "suggestion": "Run setup-gst-hst to configure your company's Business Number and province.",
    })


def setup_gst_hst(conn, args):
    """Store BN and province in regional_settings, validate BN format."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)

    bn_input = args.business_number or args.bn
    if not bn_input:
        err("--business-number (or --bn) is required")
    if not args.province:
        err("--province is required")

    bn = re.sub(r"[\s\-]", "", bn_input.strip().upper())
    province = args.province.upper()

    # Validate BN format (accept 9 or 15 chars)
    if len(bn) == 9:
        if not bn.isdigit():
            err("Base BN must be 9 digits")
    elif len(bn) == 15:
        if not bn[:9].isdigit():
            err("First 9 characters of BN must be digits")
        if bn[9:11] not in _VALID_BN_PROGRAMS:
            err(f"Invalid program code: {bn[9:11]}")
        if not bn[11:15].isdigit():
            err("Last 4 characters must be digits")
    else:
        err("BN must be 9 digits (base) or 15 characters (full)")

    # Validate province
    rates = _get_gst_hst_rates()
    if province not in rates:
        err(f"Invalid province code: {province}",
             suggestion=f"Valid codes: {', '.join(sorted(rates.keys()))}")

    company_id = company["id"]
    settings = {
        "bn": bn,
        "province": province,
        "province_name": rates[province]["name"],
        "tax_type": rates[province]["tax_type"],
        "gst_hst_configured": "1",
    }
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

    audit(conn, "erpclaw-region-ca", "setup-gst-hst", "company", company_id,
           new_values=settings, description="Configured GST/HST for company")
    conn.commit()
    ok({
        "message": f"GST/HST configured for {company.get('name', '')}",
        "business_number_stored": True,
        "bn": bn,
        "province": province,
        "province_name": rates[province]["name"],
        "tax_type": rates[province]["tax_type"],
        "suggestion": "Try compute-sales-tax to test tax calculation for your province.",
    })


def seed_ca_coa(conn, args):
    """Seed Canadian Chart of Accounts (ASPE) for a company."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)
    company_id = company["id"]

    try:
        coa = _load_json_asset("ca_coa_aspe.json")
    except SystemExit:
        # Fallback: create a basic Canadian CoA inline
        coa = [
            {"account_name": "Cash and Cash Equivalents", "account_type": "asset", "account_number": "1001"},
            {"account_name": "Accounts Receivable", "account_type": "asset", "account_number": "1200"},
            {"account_name": "Inventory", "account_type": "asset", "account_number": "1300"},
            {"account_name": "Prepaid Expenses", "account_type": "asset", "account_number": "1400"},
            {"account_name": "Property, Plant and Equipment", "account_type": "asset", "account_number": "1500"},
            {"account_name": "Accumulated Depreciation", "account_type": "asset", "account_number": "1600"},
            {"account_name": "Intangible Assets", "account_type": "asset", "account_number": "1700"},
            {"account_name": "Accounts Payable", "account_type": "liability", "account_number": "2001"},
            {"account_name": "Accrued Liabilities", "account_type": "liability", "account_number": "2100"},
            {"account_name": "GST/HST Payable", "account_type": "liability", "account_number": "2200"},
            {"account_name": "PST Payable", "account_type": "liability", "account_number": "2210"},
            {"account_name": "QST Payable", "account_type": "liability", "account_number": "2220"},
            {"account_name": "Income Tax Payable", "account_type": "liability", "account_number": "2300"},
            {"account_name": "CPP Payable", "account_type": "liability", "account_number": "2310"},
            {"account_name": "EI Payable", "account_type": "liability", "account_number": "2320"},
            {"account_name": "Payroll Tax Payable", "account_type": "liability", "account_number": "2330"},
            {"account_name": "Current Portion of Long-term Debt", "account_type": "liability", "account_number": "2400"},
            {"account_name": "Long-term Debt", "account_type": "liability", "account_number": "2500"},
            {"account_name": "Share Capital", "account_type": "equity", "account_number": "3001"},
            {"account_name": "Retained Earnings", "account_type": "equity", "account_number": "3100"},
            {"account_name": "Dividends", "account_type": "equity", "account_number": "3200"},
            {"account_name": "Revenue - Sales", "account_type": "income", "account_number": "4001"},
            {"account_name": "Revenue - Services", "account_type": "income", "account_number": "4100"},
            {"account_name": "Other Income", "account_type": "income", "account_number": "4200"},
            {"account_name": "Cost of Goods Sold", "account_type": "expense", "account_number": "5001"},
            {"account_name": "Salaries and Wages", "account_type": "expense", "account_number": "5100"},
            {"account_name": "Employee Benefits", "account_type": "expense", "account_number": "5110"},
            {"account_name": "CPP Employer Expense", "account_type": "expense", "account_number": "5120"},
            {"account_name": "EI Employer Expense", "account_type": "expense", "account_number": "5130"},
            {"account_name": "Rent Expense", "account_type": "expense", "account_number": "5200"},
            {"account_name": "Utilities", "account_type": "expense", "account_number": "5300"},
            {"account_name": "Office Supplies", "account_type": "expense", "account_number": "5400"},
            {"account_name": "Professional Fees", "account_type": "expense", "account_number": "5500"},
            {"account_name": "Insurance", "account_type": "expense", "account_number": "5600"},
            {"account_name": "Depreciation Expense", "account_type": "expense", "account_number": "5700"},
            {"account_name": "Travel and Entertainment", "account_type": "expense", "account_number": "5800"},
            {"account_name": "Advertising and Marketing", "account_type": "expense", "account_number": "5900"},
            {"account_name": "Interest Expense", "account_type": "expense", "account_number": "6001"},
            {"account_name": "Bank Charges", "account_type": "expense", "account_number": "6100"},
            {"account_name": "Income Tax Expense", "account_type": "expense", "account_number": "9001"},
        ]

    accounts = coa if isinstance(coa, list) else coa.get("accounts", [])
    created_count = 0

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
            if root_type not in ("asset", "liability", "equity", "income", "expense"):
                root_type = "asset"
            _valid_acct_types = {
                "bank", "cash", "receivable", "payable", "stock",
                "fixed_asset", "accumulated_depreciation",
                "cost_of_goods_sold", "tax", "equity", "revenue",
                "expense", "stock_received_not_billed",
                "stock_adjustment", "rounding", "exchange_gain_loss",
                "depreciation", "payroll_payable", "temporary",
                "asset_received_not_billed",
            }
            if acct_type and acct_type not in _valid_acct_types:
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

    audit(conn, "erpclaw-region-ca", "seed-ca-coa", "company", company_id,
           new_values={"accounts_created": created_count})
    conn.commit()
    ok({
        "message": f"Canadian Chart of Accounts (ASPE) loaded: {created_count} accounts created",
        "company_id": company_id,
        "accounts_created": created_count,
        "total_in_template": len(accounts),
    })


def seed_ca_payroll(conn, args):
    """Seed Canadian payroll salary components (CPP, EI, federal/provincial tax, etc.)."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)
    company_id = company["id"]
    province = _get_company_province(conn, company)
    created_count = 0

    components = [
        ("CPP Employee", "deduction", "Canada Pension Plan — employee contribution", 1),
        ("CPP Employer", "employer_contribution", "Canada Pension Plan — employer contribution", 1),
        ("CPP2 Employee", "deduction", "CPP2 enhanced — employee contribution on earnings above first ceiling", 1),
        ("CPP2 Employer", "employer_contribution", "CPP2 enhanced — employer contribution on earnings above first ceiling", 1),
        ("EI Employee", "deduction", "Employment Insurance — employee premium", 1),
        ("EI Employer", "employer_contribution", "Employment Insurance — employer premium (1.4x employee)", 1),
        ("Federal Income Tax", "deduction", "Federal income tax withheld at source", 1),
        ("Provincial Income Tax", "deduction", "Provincial income tax withheld at source", 1),
    ]
    # Quebec-specific components
    if province == "QC":
        components.extend([
            ("QPP Employee", "deduction", "Quebec Pension Plan — employee contribution (replaces CPP in QC)", 1),
            ("QPP Employer", "employer_contribution", "Quebec Pension Plan — employer contribution", 1),
            ("QPIP Employee", "deduction", "Quebec Parental Insurance Plan — employee premium", 1),
            ("QPIP Employer", "employer_contribution", "Quebec Parental Insurance Plan — employer premium", 1),
        ])

    for name, comp_type, desc, is_statutory in components:
        existing = conn.execute(
            "SELECT id FROM salary_component WHERE name = ?", (name,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO salary_component (id, name, component_type, description, is_statutory) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), name, comp_type, desc, is_statutory),
            )
            created_count += 1

    audit(conn, "erpclaw-region-ca", "seed-ca-payroll", "company", company_id,
           new_values={"components_created": created_count})
    conn.commit()
    ok({
        "message": "Canada payroll components seeded successfully",
        "company_id": company_id,
        "province": province,
        "components_created": created_count,
        "components": [c[0] for c in components],
        "suggestion": "Use compute-cpp, compute-ei, compute-federal-tax, compute-provincial-tax for calculations.",
    })


# ---------------------------------------------------------------------------
# Payroll — CPP / CPP2 / QPP / EI
# ---------------------------------------------------------------------------

def compute_cpp(conn, args):
    """Compute Canada Pension Plan contributions."""
    if not args.gross_salary:
        err("--gross-salary is required (periodic gross pay)")

    try:
        gross = to_decimal(args.gross_salary)
    except (InvalidOperation, ValueError):
        err(f"Invalid gross salary: {args.gross_salary}")

    periods = _resolve_periods(args)
    rates = _get_cpp_rates()

    rate = to_decimal(rates["rate"]) / Decimal("100")
    max_pensionable = to_decimal(rates["max_pensionable_earnings"])
    basic_exemption = to_decimal(rates["basic_exemption"])
    max_contribution = to_decimal(rates["max_employee_contribution"])

    # Annualize
    annual_gross = gross * Decimal(str(periods))
    annual_pensionable = min(annual_gross, max_pensionable)
    annual_taxable = max(annual_pensionable - basic_exemption, Decimal("0"))
    annual_cpp = min(round_currency(annual_taxable * rate), max_contribution)

    # Per-period
    period_cpp = round_currency(annual_cpp / Decimal(str(periods)))
    employer_cpp = period_cpp  # Employer matches employee

    ok({
        "gross_salary": str(round_currency(gross)),
        "pay_periods": periods,
        "annual_gross": str(round_currency(annual_gross)),
        "max_pensionable_earnings": str(max_pensionable),
        "basic_exemption": str(basic_exemption),
        "pensionable_earnings": str(round_currency(annual_taxable)),
        "cpp_rate": rates["rate"],
        "employee_cpp": str(period_cpp),
        "employer_cpp": str(employer_cpp),
        "annual_employee_cpp": str(annual_cpp),
        "annual_max_employee": str(max_contribution),
    })


def compute_cpp2(conn, args):
    """Compute CPP2 (enhanced) contributions on earnings between first and second ceiling."""
    annual_earnings_str = args.annual_earnings or args.gross_salary
    if not annual_earnings_str:
        err("--annual-earnings (or --gross-salary) is required")

    try:
        annual_gross = to_decimal(annual_earnings_str)
    except (InvalidOperation, ValueError):
        err(f"Invalid earnings: {annual_earnings_str}")

    rates = _get_cpp_rates()
    cpp2_rate = to_decimal(rates["cpp2_rate"]) / Decimal("100")
    first_ceiling = to_decimal(rates["cpp2_first_ceiling"])
    second_ceiling = to_decimal(rates["cpp2_second_ceiling"])
    max_cpp2 = to_decimal(rates["cpp2_max_employee_contribution"])

    if annual_gross <= first_ceiling:
        ok({
            "annual_earnings": str(round_currency(annual_gross)),
            "first_ceiling": str(first_ceiling),
            "second_ceiling": str(second_ceiling),
            "cpp2_applicable": False,
            "employee_cpp2": "0.00",
            "employer_cpp2": "0.00",
            "annual_max_employee": str(max_cpp2),
        })
        return

    cpp2_earnings = min(annual_gross, second_ceiling) - first_ceiling
    annual_cpp2 = min(round_currency(cpp2_earnings * cpp2_rate), max_cpp2)

    ok({
        "annual_earnings": str(round_currency(annual_gross)),
        "first_ceiling": str(first_ceiling),
        "second_ceiling": str(second_ceiling),
        "cpp2_earnings": str(round_currency(cpp2_earnings)),
        "cpp2_rate": rates["cpp2_rate"],
        "cpp2_applicable": True,
        "employee_cpp2": str(annual_cpp2),
        "employer_cpp2": str(annual_cpp2),
        "annual_max_employee": str(max_cpp2),
    })


def compute_qpp(conn, args):
    """Compute Quebec Pension Plan contributions (replaces CPP for Quebec employees)."""
    if not args.gross_salary:
        err("--gross-salary is required (periodic gross pay)")

    try:
        gross = to_decimal(args.gross_salary)
    except (InvalidOperation, ValueError):
        err(f"Invalid gross salary: {args.gross_salary}")

    periods = _resolve_periods(args)
    rates = _get_qpp_rates()

    rate = to_decimal(rates["rate"]) / Decimal("100")
    max_pensionable = to_decimal(rates["max_pensionable_earnings"])
    basic_exemption = to_decimal(rates["basic_exemption"])
    max_contribution = to_decimal(rates["max_employee_contribution"])

    annual_gross = gross * Decimal(str(periods))
    annual_pensionable = min(annual_gross, max_pensionable)
    annual_taxable = max(annual_pensionable - basic_exemption, Decimal("0"))
    annual_qpp = min(round_currency(annual_taxable * rate), max_contribution)

    period_qpp = round_currency(annual_qpp / Decimal(str(periods)))
    employer_qpp = period_qpp

    ok({
        "gross_salary": str(round_currency(gross)),
        "pay_periods": periods,
        "annual_gross": str(round_currency(annual_gross)),
        "rate": rates["rate"],
        "employee_qpp": str(period_qpp),
        "employer_qpp": str(employer_qpp),
        "annual_employee_qpp": str(annual_qpp),
        "annual_max_employee": str(max_contribution),
        "note": "QPP replaces CPP for employees working in Quebec. Rate is higher than CPP.",
    })


def compute_ei(conn, args):
    """Compute Employment Insurance premiums."""
    if not args.gross_salary:
        err("--gross-salary is required (periodic gross pay)")

    try:
        gross = to_decimal(args.gross_salary)
    except (InvalidOperation, ValueError):
        err(f"Invalid gross salary: {args.gross_salary}")

    periods = _resolve_periods(args)
    province = (args.province or "ON").upper()
    rates = _get_ei_rates()

    is_quebec = province == "QC"
    rate_key = "quebec_rate" if is_quebec else "rate"
    max_key = "quebec_max_employee_premium" if is_quebec else "max_employee_premium"

    ei_rate = to_decimal(rates[rate_key]) / Decimal("100")
    max_insurable = to_decimal(rates["max_insurable_earnings"])
    max_premium = to_decimal(rates[max_key])
    employer_mult = to_decimal(rates["employer_multiplier"])

    annual_gross = gross * Decimal(str(periods))
    insurable = min(annual_gross, max_insurable)
    annual_employee_ei = min(round_currency(insurable * ei_rate), max_premium)
    annual_employer_ei = round_currency(annual_employee_ei * employer_mult)

    period_employee = round_currency(annual_employee_ei / Decimal(str(periods)))
    period_employer = round_currency(annual_employer_ei / Decimal(str(periods)))

    result = {
        "gross_salary": str(round_currency(gross)),
        "pay_periods": periods,
        "province": province,
        "annual_gross": str(round_currency(annual_gross)),
        "ei_rate": rates[rate_key],
        "employee_ei": str(period_employee),
        "employer_ei": str(period_employer),
        "annual_employee_ei": str(annual_employee_ei),
        "annual_employer_ei": str(annual_employer_ei),
        "annual_max_employee": str(max_premium),
        "employer_multiplier": str(employer_mult),
    }
    if is_quebec:
        result["note"] = "Quebec EI rate is lower because Quebec has its own QPIP (parental insurance)."
    ok(result)


# ---------------------------------------------------------------------------
# Payroll — Federal & Provincial Income Tax
# ---------------------------------------------------------------------------

def compute_federal_tax(conn, args):
    """Compute federal income tax using progressive brackets."""
    if not args.annual_income:
        err("--annual-income is required")

    try:
        annual_income = to_decimal(args.annual_income)
    except (InvalidOperation, ValueError):
        err(f"Invalid annual income: {args.annual_income}")

    data = _get_federal_brackets()
    bpa = to_decimal(data["basic_personal_amount"])
    taxable = max(annual_income - Decimal("0"), Decimal("0"))  # No standard deduction at federal level

    gross_tax, breakdown, marginal_rate = _progressive_tax(taxable, data["brackets"])

    # Basic personal amount credit = 15% of BPA
    personal_credit = round_currency(bpa * Decimal("0.15"))
    net_tax = max(round_currency(gross_tax - personal_credit), Decimal("0"))
    effective_rate = round_currency(net_tax / taxable * Decimal("100")) if taxable > 0 else Decimal("0")

    ok({
        "annual_income": str(round_currency(annual_income)),
        "taxable_income": str(round_currency(taxable)),
        "basic_personal_amount": str(bpa),
        "bracket_breakdown": breakdown,
        "gross_tax": str(round_currency(gross_tax)),
        "personal_credit": str(personal_credit),
        "net_tax": str(net_tax),
        "effective_rate": str(effective_rate),
        "marginal_rate": str(marginal_rate),
    })


def compute_provincial_tax(conn, args):
    """Compute provincial income tax using progressive brackets."""
    if not args.annual_income:
        err("--annual-income is required")
    if not args.province:
        err("--province is required")

    try:
        annual_income = to_decimal(args.annual_income)
    except (InvalidOperation, ValueError):
        err(f"Invalid annual income: {args.annual_income}")

    province = args.province.upper()
    all_brackets = _get_provincial_brackets()
    prov_data = all_brackets.get(province)
    if not prov_data:
        err(f"Provincial tax brackets not found for: {province}",
             suggestion=f"Available provinces: {', '.join(sorted(all_brackets.keys()))}")

    bpa = to_decimal(prov_data["basic_personal_amount"])
    taxable = max(annual_income, Decimal("0"))

    gross_tax, breakdown, marginal_rate = _progressive_tax(taxable, prov_data["brackets"])

    # Provincial personal credit (lowest bracket rate * BPA)
    lowest_rate = to_decimal(prov_data["brackets"][0]["rate"]) / Decimal("100")
    personal_credit = round_currency(bpa * lowest_rate)

    net_before_surtax = max(round_currency(gross_tax - personal_credit), Decimal("0"))

    # Ontario surtax
    surtax = Decimal("0")
    surtax_details = None
    if prov_data.get("surtax"):
        t1 = to_decimal(prov_data.get("surtax_threshold_1", "0"))
        r1 = to_decimal(prov_data.get("surtax_rate_1", "0")) / Decimal("100")
        surtax_1 = Decimal("0")
        if net_before_surtax > t1:
            surtax_1 = round_currency((net_before_surtax - t1) * r1)

        surtax_2 = Decimal("0")
        if "surtax_threshold_2" in prov_data:
            t2 = to_decimal(prov_data["surtax_threshold_2"])
            r2 = to_decimal(prov_data.get("surtax_rate_2", "0")) / Decimal("100")
            if net_before_surtax > t2:
                surtax_2 = round_currency((net_before_surtax - t2) * r2)

        surtax = surtax_1 + surtax_2
        surtax_details = {
            "surtax_1": str(round_currency(surtax_1)),
            "surtax_2": str(round_currency(surtax_2)),
            "total_surtax": str(round_currency(surtax)),
        }

    net_tax = round_currency(net_before_surtax + surtax)

    result = {
        "annual_income": str(round_currency(annual_income)),
        "province": province,
        "province_name": prov_data["name"],
        "taxable_income": str(round_currency(taxable)),
        "basic_personal_amount": str(bpa),
        "bracket_breakdown": breakdown,
        "gross_tax": str(round_currency(gross_tax)),
        "personal_credit": str(personal_credit),
        "tax_after_credit": str(net_before_surtax),
    }
    if surtax_details:
        result["surtax"] = surtax_details
    result["net_tax"] = str(net_tax)

    ok(result)


def compute_total_payroll_deductions(conn, args):
    """Compute all payroll deductions: CPP/QPP, EI, federal tax, provincial tax."""
    if not args.gross_salary:
        err("--gross-salary is required (periodic gross pay)")
    if not args.province:
        err("--province is required")

    try:
        gross = to_decimal(args.gross_salary)
    except (InvalidOperation, ValueError):
        err(f"Invalid gross salary: {args.gross_salary}")

    province = args.province.upper()
    periods = _resolve_periods(args)
    is_quebec = province == "QC"
    annual_gross = gross * Decimal(str(periods))

    # --- CPP or QPP ---
    if is_quebec:
        qpp_rates = _get_qpp_rates()
        pension_rate = to_decimal(qpp_rates["rate"]) / Decimal("100")
        pension_max_pe = to_decimal(qpp_rates["max_pensionable_earnings"])
        pension_exemption = to_decimal(qpp_rates["basic_exemption"])
        pension_max_contrib = to_decimal(qpp_rates["max_employee_contribution"])
        pension_label = "QPP"
    else:
        cpp_rates = _get_cpp_rates()
        pension_rate = to_decimal(cpp_rates["rate"]) / Decimal("100")
        pension_max_pe = to_decimal(cpp_rates["max_pensionable_earnings"])
        pension_exemption = to_decimal(cpp_rates["basic_exemption"])
        pension_max_contrib = to_decimal(cpp_rates["max_employee_contribution"])
        pension_label = "CPP"

    pension_earnings = max(min(annual_gross, pension_max_pe) - pension_exemption, Decimal("0"))
    annual_pension = min(round_currency(pension_earnings * pension_rate), pension_max_contrib)
    period_pension = round_currency(annual_pension / Decimal(str(periods)))

    # --- EI ---
    ei_rates = _get_ei_rates()
    ei_rate_key = "quebec_rate" if is_quebec else "rate"
    ei_max_key = "quebec_max_employee_premium" if is_quebec else "max_employee_premium"
    ei_rate = to_decimal(ei_rates[ei_rate_key]) / Decimal("100")
    ei_max_insurable = to_decimal(ei_rates["max_insurable_earnings"])
    ei_max_premium = to_decimal(ei_rates[ei_max_key])
    insurable = min(annual_gross, ei_max_insurable)
    annual_ei = min(round_currency(insurable * ei_rate), ei_max_premium)
    period_ei = round_currency(annual_ei / Decimal(str(periods)))

    # --- Federal tax ---
    fed_data = _get_federal_brackets()
    fed_bpa = to_decimal(fed_data["basic_personal_amount"])
    fed_gross_tax, _, _ = _progressive_tax(annual_gross, fed_data["brackets"])
    fed_credit = round_currency(fed_bpa * Decimal("0.15"))
    annual_fed_tax = max(round_currency(fed_gross_tax - fed_credit), Decimal("0"))
    period_fed_tax = round_currency(annual_fed_tax / Decimal(str(periods)))

    # --- Provincial tax ---
    prov_brackets = _get_provincial_brackets()
    prov_data = prov_brackets.get(province, prov_brackets.get("ON"))
    prov_bpa = to_decimal(prov_data["basic_personal_amount"])
    prov_gross_tax, _, _ = _progressive_tax(annual_gross, prov_data["brackets"])
    prov_lowest_rate = to_decimal(prov_data["brackets"][0]["rate"]) / Decimal("100")
    prov_credit = round_currency(prov_bpa * prov_lowest_rate)
    net_prov_before_surtax = max(round_currency(prov_gross_tax - prov_credit), Decimal("0"))

    # Ontario surtax
    surtax = Decimal("0")
    if prov_data.get("surtax"):
        t1 = to_decimal(prov_data.get("surtax_threshold_1", "0"))
        r1 = to_decimal(prov_data.get("surtax_rate_1", "0")) / Decimal("100")
        if net_prov_before_surtax > t1:
            surtax += round_currency((net_prov_before_surtax - t1) * r1)
        if "surtax_threshold_2" in prov_data:
            t2 = to_decimal(prov_data["surtax_threshold_2"])
            r2 = to_decimal(prov_data.get("surtax_rate_2", "0")) / Decimal("100")
            if net_prov_before_surtax > t2:
                surtax += round_currency((net_prov_before_surtax - t2) * r2)

    annual_prov_tax = round_currency(net_prov_before_surtax + surtax)
    period_prov_tax = round_currency(annual_prov_tax / Decimal(str(periods)))

    # --- Totals ---
    total_period_deductions = period_pension + period_ei + period_fed_tax + period_prov_tax
    net_pay = round_currency(gross - total_period_deductions)

    pension_key = "qpp" if is_quebec else "cpp"
    ok({
        "gross_pay": str(round_currency(gross)),
        "province": province,
        "pay_periods": periods,
        pension_key: str(period_pension),
        "ei": str(period_ei),
        "federal_tax": str(period_fed_tax),
        "provincial_tax": str(period_prov_tax),
        "total_deductions": str(round_currency(total_period_deductions)),
        "net_pay": str(net_pay),
    })


def ca_payroll_summary(conn, args):
    """Generate a payroll summary for all employees of a Canadian company."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)
    if not args.month or not args.year:
        err("--month and --year are required")

    company_id = company["id"]
    company_province = _get_company_province(conn, company)
    periods = int(args.pay_periods or 12)

    employees = conn.execute(
        "SELECT * FROM employee WHERE company_id = ? AND status = 'active'",
        (company_id,),
    ).fetchall()

    emp_list = []
    totals = {
        "total_gross": Decimal("0"),
        "total_cpp": Decimal("0"),
        "total_ei": Decimal("0"),
        "total_federal_tax": Decimal("0"),
        "total_provincial_tax": Decimal("0"),
        "total_deductions": Decimal("0"),
        "total_net_pay": Decimal("0"),
        "total_employer_cpp": Decimal("0"),
        "total_employer_ei": Decimal("0"),
    }

    for emp_row in employees:
        emp = row_to_dict(emp_row)
        emp_province = (emp.get("province") or company_province or "ON").upper()
        is_quebec = emp_province == "QC"

        # Look up latest salary slip for the period
        period_str = f"{args.year}-{int(args.month):02d}"
        slip = conn.execute(
            "SELECT * FROM salary_slip WHERE employee_id = ? AND period_start = ? AND status = 'submitted'",
            (emp["id"], period_str),
        ).fetchone()

        if slip:
            slip_dict = row_to_dict(slip)
            gross = to_decimal(str(slip_dict.get("gross_pay", "0")))
        else:
            # No salary slip; skip or estimate from 0
            gross = Decimal("0")

        if gross <= Decimal("0"):
            emp_list.append({
                "employee_id": emp["id"],
                "employee_name": emp.get("full_name", emp.get("first_name", "")),
                "province": emp_province,
                "gross": "0.00",
                "deductions": {},
                "net_pay": "0.00",
                "note": "No salary slip found for this period",
            })
            continue

        annual_gross = gross * Decimal(str(periods))

        # CPP/QPP
        if is_quebec:
            qpp_rates = _get_qpp_rates()
            p_rate = to_decimal(qpp_rates["rate"]) / Decimal("100")
            p_max_pe = to_decimal(qpp_rates["max_pensionable_earnings"])
            p_exempt = to_decimal(qpp_rates["basic_exemption"])
            p_max_c = to_decimal(qpp_rates["max_employee_contribution"])
        else:
            cpp_rates = _get_cpp_rates()
            p_rate = to_decimal(cpp_rates["rate"]) / Decimal("100")
            p_max_pe = to_decimal(cpp_rates["max_pensionable_earnings"])
            p_exempt = to_decimal(cpp_rates["basic_exemption"])
            p_max_c = to_decimal(cpp_rates["max_employee_contribution"])

        p_earn = max(min(annual_gross, p_max_pe) - p_exempt, Decimal("0"))
        annual_p = min(round_currency(p_earn * p_rate), p_max_c)
        period_p = round_currency(annual_p / Decimal(str(periods)))

        # EI
        ei_rates = _get_ei_rates()
        ei_rk = "quebec_rate" if is_quebec else "rate"
        ei_mk = "quebec_max_employee_premium" if is_quebec else "max_employee_premium"
        ei_r = to_decimal(ei_rates[ei_rk]) / Decimal("100")
        ei_max_ins = to_decimal(ei_rates["max_insurable_earnings"])
        ei_max_p = to_decimal(ei_rates[ei_mk])
        employer_mult = to_decimal(ei_rates["employer_multiplier"])
        annual_ei = min(round_currency(min(annual_gross, ei_max_ins) * ei_r), ei_max_p)
        period_ei = round_currency(annual_ei / Decimal(str(periods)))
        employer_ei = round_currency(period_ei * employer_mult)

        # Federal tax
        fed_data = _get_federal_brackets()
        fed_bpa = to_decimal(fed_data["basic_personal_amount"])
        fed_gt, _, _ = _progressive_tax(annual_gross, fed_data["brackets"])
        fed_cr = round_currency(fed_bpa * Decimal("0.15"))
        annual_ft = max(round_currency(fed_gt - fed_cr), Decimal("0"))
        period_ft = round_currency(annual_ft / Decimal(str(periods)))

        # Provincial tax
        prov_all = _get_provincial_brackets()
        prov_d = prov_all.get(emp_province, prov_all.get("ON"))
        prov_bpa = to_decimal(prov_d["basic_personal_amount"])
        prov_gt, _, _ = _progressive_tax(annual_gross, prov_d["brackets"])
        prov_lr = to_decimal(prov_d["brackets"][0]["rate"]) / Decimal("100")
        prov_cr = round_currency(prov_bpa * prov_lr)
        net_prov = max(round_currency(prov_gt - prov_cr), Decimal("0"))
        # Surtax
        surtax_total = Decimal("0")
        if prov_d.get("surtax"):
            st1 = to_decimal(prov_d.get("surtax_threshold_1", "0"))
            sr1 = to_decimal(prov_d.get("surtax_rate_1", "0")) / Decimal("100")
            if net_prov > st1:
                surtax_total += round_currency((net_prov - st1) * sr1)
            if "surtax_threshold_2" in prov_d:
                st2 = to_decimal(prov_d["surtax_threshold_2"])
                sr2 = to_decimal(prov_d.get("surtax_rate_2", "0")) / Decimal("100")
                if net_prov > st2:
                    surtax_total += round_currency((net_prov - st2) * sr2)
        annual_pt = round_currency(net_prov + surtax_total)
        period_pt = round_currency(annual_pt / Decimal(str(periods)))

        total_ded = period_p + period_ei + period_ft + period_pt
        net_pay = round_currency(gross - total_ded)

        pension_label = "qpp" if is_quebec else "cpp"
        emp_list.append({
            "employee_id": emp["id"],
            "employee_name": emp.get("full_name", emp.get("first_name", "")),
            "province": emp_province,
            "gross": str(round_currency(gross)),
            "deductions": {
                pension_label: str(period_p),
                "ei": str(period_ei),
                "federal_tax": str(period_ft),
                "provincial_tax": str(period_pt),
            },
            "total_deductions": str(round_currency(total_ded)),
            "net_pay": str(net_pay),
        })

        totals["total_gross"] += gross
        totals["total_cpp"] += period_p
        totals["total_ei"] += period_ei
        totals["total_federal_tax"] += period_ft
        totals["total_provincial_tax"] += period_pt
        totals["total_deductions"] += total_ded
        totals["total_net_pay"] += net_pay
        totals["total_employer_cpp"] += period_p
        totals["total_employer_ei"] += employer_ei

    ok({
        "report": "Canada Payroll Summary",
        "period": f"{args.year}-{int(args.month):02d}",
        "company": company.get("name", ""),
        "employee_count": len(emp_list),
        "employees": emp_list,
        "totals": {k: str(round_currency(v)) for k, v in totals.items()},
    })


# ---------------------------------------------------------------------------
# Compliance — GST/HST Return, QST Return, T4, T4A, ROE, PD7A
# ---------------------------------------------------------------------------

def generate_gst_hst_return(conn, args):
    """Generate GST/HST return (Form GST34) for a reporting period."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)
    period_val = args.period or args.month
    year_val = args.year or args.tax_year
    if not period_val or not year_val:
        err("--period (or --month) and --year are required")

    company_id = company["id"]
    month = int(period_val)
    year = int(year_val)
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month + 1:02d}-01" if month < 12 else f"{year + 1}-01-01"

    # Sales (GST/HST collected)
    sales = conn.execute(
        """SELECT COALESCE(SUM(CAST(total_amount AS REAL)), 0) as revenue,
                  COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as tax_collected,
                  COUNT(*) as invoice_count
           FROM sales_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date < ?""",
        (company_id, start_date, end_date),
    ).fetchone()

    # Purchases (ITC)
    purchases = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as tax_paid,
                  COUNT(*) as invoice_count
           FROM purchase_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date < ?""",
        (company_id, start_date, end_date),
    ).fetchone()

    revenue = round_currency(to_decimal(str(sales["revenue"])))
    tax_collected = round_currency(to_decimal(str(sales["tax_collected"])))
    itc = round_currency(to_decimal(str(purchases["tax_paid"])))
    net_tax = round_currency(tax_collected - itc)

    # Get BN from regional_settings
    try:
        bn_row = conn.execute(
            "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'bn'",
            (company_id,),
        ).fetchone()
    except Exception:
        bn_row = None

    ok({
        "report": "GST/HST Return",
        "period": f"{year}-{month:02d}",
        "company": company.get("name", ""),
        "business_number": bn_row["value"] if bn_row else "Not configured",
        "revenue": str(revenue),
        "tax_collected": str(tax_collected),
        "itc_claimed": str(itc),
        "net_tax": str(net_tax),
        "sales_invoice_count": sales["invoice_count"],
        "purchase_invoice_count": purchases["invoice_count"],
        "action_required": "Remit" if net_tax > Decimal("0") else "Refund",
        "amount_due_or_refund": str(round_currency(abs(net_tax))),
    })


def generate_qst_return(conn, args):
    """Generate Quebec Sales Tax (QST) return for a reporting period."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)
    period_val = args.period or args.month
    year_val = args.year or args.tax_year
    if not period_val or not year_val:
        err("--period (or --month) and --year are required")

    province = _get_company_province(conn, company)
    if province != "QC":
        err("QST return is only for Quebec companies.",
             suggestion="Use generate-gst-hst-return for non-Quebec companies.")

    company_id = company["id"]
    month = int(period_val)
    year = int(year_val)
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month + 1:02d}-01" if month < 12 else f"{year + 1}-01-01"

    # Sales QST collected
    sales = conn.execute(
        """SELECT COALESCE(SUM(CAST(total_amount AS REAL)), 0) as revenue,
                  COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as tax_collected,
                  COUNT(*) as invoice_count
           FROM sales_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date < ?""",
        (company_id, start_date, end_date),
    ).fetchone()

    # Purchase QST ITR
    purchases = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as tax_paid,
                  COUNT(*) as invoice_count
           FROM purchase_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date < ?""",
        (company_id, start_date, end_date),
    ).fetchone()

    revenue = round_currency(to_decimal(str(sales["revenue"])))
    # Estimate QST portion (QST is 9.975 / 14.975 of total tax for QC)
    total_tax = to_decimal(str(sales["tax_collected"]))
    qst_fraction = Decimal("9.975") / Decimal("14.975")
    qst_collected = round_currency(total_tax * qst_fraction)

    purchase_tax = to_decimal(str(purchases["tax_paid"]))
    qst_itr = round_currency(purchase_tax * qst_fraction)
    net_qst = round_currency(qst_collected - qst_itr)

    ok({
        "report": "QST Return",
        "period": f"{year}-{month:02d}",
        "company": company.get("name", ""),
        "taxable_sales": str(revenue),
        "qst_collected": str(qst_collected),
        "qst_input_tax_refunds": str(qst_itr),
        "net_qst": str(net_qst),
        "sales_invoice_count": sales["invoice_count"],
        "purchase_invoice_count": purchases["invoice_count"],
        "action_required": "Remit" if net_qst > Decimal("0") else "Refund",
        "amount_due_or_refund": str(round_currency(abs(net_qst))),
        "note": "File with Revenu Quebec. QST is filed separately from GST.",
    })


def generate_t4(conn, args):
    """Generate T4 Statement of Remuneration Paid for an employee."""
    if not args.employee_id:
        err("--employee-id is required")
    year_val = args.tax_year or args.year
    if not year_val:
        err("--tax-year (or --year) is required")

    emp = conn.execute("SELECT * FROM employee WHERE id = ?", (args.employee_id,)).fetchone()
    if not emp:
        err(f"Employee not found: {args.employee_id}")
    emp_dict = row_to_dict(emp)

    year = int(year_val)
    emp_province = (emp_dict.get("province") or "ON").upper()
    is_quebec = emp_province == "QC"

    # Sum salary slips for the year
    slips = conn.execute(
        """SELECT COALESCE(SUM(CAST(gross_pay AS REAL)), 0) as total_gross,
                  COALESCE(SUM(CAST(total_deductions AS REAL)), 0) as total_deductions,
                  COUNT(*) as slip_count
           FROM salary_slip
           WHERE employee_id = ? AND status = 'submitted'
             AND period_start LIKE ?""",
        (args.employee_id, f"{year}-%"),
    ).fetchone()

    total_gross = round_currency(to_decimal(str(slips["total_gross"])))
    periods = max(slips["slip_count"], 1)

    # Estimate deductions based on gross
    annual_gross = total_gross
    cpp_rates = _get_cpp_rates() if not is_quebec else _get_qpp_rates()
    p_rate = to_decimal(cpp_rates["rate"]) / Decimal("100")
    p_max_pe = to_decimal(cpp_rates["max_pensionable_earnings"])
    p_exempt = to_decimal(cpp_rates["basic_exemption"])
    p_max_c = to_decimal(cpp_rates["max_employee_contribution"])
    p_earn = max(min(annual_gross, p_max_pe) - p_exempt, Decimal("0"))
    pension_deducted = min(round_currency(p_earn * p_rate), p_max_c)

    ei_rates = _get_ei_rates()
    ei_rk = "quebec_rate" if is_quebec else "rate"
    ei_mk = "quebec_max_employee_premium" if is_quebec else "max_employee_premium"
    ei_r = to_decimal(ei_rates[ei_rk]) / Decimal("100")
    ei_max_ins = to_decimal(ei_rates["max_insurable_earnings"])
    ei_max_p = to_decimal(ei_rates[ei_mk])
    ei_deducted = min(round_currency(min(annual_gross, ei_max_ins) * ei_r), ei_max_p)

    fed_data = _get_federal_brackets()
    fed_gt, _, _ = _progressive_tax(annual_gross, fed_data["brackets"])
    fed_cr = round_currency(to_decimal(fed_data["basic_personal_amount"]) * Decimal("0.15"))
    income_tax_deducted = max(round_currency(fed_gt - fed_cr), Decimal("0"))

    pension_label = "QPP" if is_quebec else "CPP"
    sin_raw = emp_dict.get("sin", "")
    sin_masked = sin_raw[:3] + "***" + sin_raw[6:] if len(sin_raw) == 9 else sin_raw
    t4 = {
        "form": "T4",
        "tax_year": year,
        "employee_name": emp_dict.get("full_name", emp_dict.get("first_name", "")),
        "sin_masked": sin_masked,
        "province_of_employment": emp_province,
        "box_14_employment_income": str(round_currency(annual_gross)),
        f"box_16_{pension_label.lower()}_contributions": str(pension_deducted),
        "box_18_ei_premiums": str(ei_deducted),
        "box_22_income_tax_deducted": str(income_tax_deducted),
        "box_24_ei_insurable_earnings": str(round_currency(min(annual_gross, ei_max_ins))),
        f"box_26_{pension_label.lower()}_pensionable_earnings": str(round_currency(min(annual_gross, p_max_pe))),
        "salary_slips_count": slips["slip_count"],
    }
    if is_quebec:
        t4["note"] = "Quebec employees also receive RL-1 from Revenu Quebec."

    ok(t4)


def generate_t4a(conn, args):
    """Generate T4A Statement of Pension, Retirement, Annuity, and Other Income."""
    if not args.recipient_name:
        err("--recipient-name is required")
    if not args.amount:
        err("--amount is required")
    if not args.year:
        err("--year is required (tax year)")

    try:
        amount = to_decimal(args.amount)
    except (InvalidOperation, ValueError):
        err(f"Invalid amount: {args.amount}")

    income_type = args.income_type or "other"
    box_map = {
        "pension": ("016", "Pension or superannuation"),
        "annuity": ("024", "Annuity payments"),
        "retiring_allowance": ("026", "Eligible retiring allowance"),
        "self_employed_commission": ("020", "Self-employed commissions"),
        "fees": ("048", "Fees for services"),
        "other": ("028", "Other income"),
    }
    box_num, box_desc = box_map.get(income_type, ("028", "Other income"))

    # Estimate withholding at 25% for lump sums
    withholding_rate = Decimal("25") if amount > Decimal("5000") else Decimal("15")
    tax_withheld = round_currency(amount * withholding_rate / Decimal("100"))

    ok({
        "report": "T4A — Statement of Pension, Retirement, Annuity, and Other Income",
        "tax_year": int(args.year),
        "recipient_name": args.recipient_name,
        f"box_{box_num}_{income_type}": str(round_currency(amount)),
        "box_description": box_desc,
        "box_022_income_tax_deducted": str(tax_withheld),
        "withholding_rate": f"{withholding_rate}%",
        "note": "T4A is for reporting non-employment income (pensions, fees, commissions, etc.)",
    })


def generate_roe(conn, args):
    """Generate Record of Employment (ROE) data."""
    if not args.employee_id:
        err("--employee-id is required")

    emp = conn.execute("SELECT * FROM employee WHERE id = ?", (args.employee_id,)).fetchone()
    if not emp:
        err(f"Employee not found: {args.employee_id}")
    emp_dict = row_to_dict(emp)

    reason_code = args.reason_code or "K"
    reason_map = {
        "A": "Shortage of work / End of contract",
        "B": "Strike or lockout",
        "C": "Return to school",
        "D": "Illness or injury",
        "E": "Quit",
        "F": "Maternity",
        "G": "Retirement",
        "H": "Work-sharing",
        "J": "Apprentice training",
        "K": "Other",
        "M": "Dismissal",
        "N": "Leave of absence",
        "P": "Parental",
        "Z": "Compassionate care",
    }
    reason_desc = reason_map.get(reason_code.upper(), "Unknown")

    # Get insurable earnings from salary slips (last 26 weeks)
    slips = conn.execute(
        """SELECT gross_pay, period_start
           FROM salary_slip
           WHERE employee_id = ? AND status = 'submitted'
           ORDER BY period_start DESC
           LIMIT 26""",
        (args.employee_id,),
    ).fetchall()

    insurable_earnings = []
    total_insurable = Decimal("0")
    total_hours = Decimal("0")
    for slip in slips:
        s = row_to_dict(slip)
        gp = to_decimal(str(s.get("gross_pay", "0")))
        insurable_earnings.append({
            "period": s.get("period_start", ""),
            "earnings": str(round_currency(gp)),
        })
        total_insurable += gp
        # Estimate hours (standard 40 hrs/week * ~4.33 weeks/month)
        total_hours += Decimal("173.33") if gp > Decimal("0") else Decimal("0")

    ok({
        "form": "ROE",
        "employee_name": emp_dict.get("full_name", emp_dict.get("first_name", "")),
        "employee_sin": emp_dict.get("sin", ""),
        "block_11_last_day_worked": emp_dict.get("date_of_leaving", ""),
        "block_15a_total_insurable_hours": str(round_currency(total_hours)),
        "block_15b_total_insurable_earnings": str(round_currency(total_insurable)),
        "block_16_reason_for_separation": f"{reason_code.upper()} - {reason_desc}",
        "block_15c_insurable_earnings_by_period": insurable_earnings[:14],
        "periods_reported": len(insurable_earnings),
        "note": "ROE must be filed electronically via ROE Web within 5 calendar days of the pay period end date.",
    })


def generate_pd7a(conn, args):
    """Generate PD7A Statement of Account for Current Source Deductions (monthly remittance)."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)
    if not args.month or not args.year:
        err("--month and --year are required")

    company_id = company["id"]
    month = int(args.month)
    year = int(args.year)
    period_str = f"{year}-{month:02d}"
    company_province = _get_company_province(conn, company)

    # Get all employees with salary slips for this period
    slips = conn.execute(
        """SELECT ss.employee_id, ss.gross_pay, e.province
           FROM salary_slip ss
           LEFT JOIN employee e ON e.id = ss.employee_id
           WHERE ss.company_id = ? AND ss.status = 'submitted'
             AND ss.period_start = ?""",
        (company_id, period_str),
    ).fetchall()

    total_cpp_employee = Decimal("0")
    total_cpp_employer = Decimal("0")
    total_ei_employee = Decimal("0")
    total_ei_employer = Decimal("0")
    total_income_tax = Decimal("0")
    employee_count = 0

    cpp_rates = _get_cpp_rates()
    ei_rates = _get_ei_rates()
    fed_data = _get_federal_brackets()
    periods = 12  # Monthly

    for slip_row in slips:
        slip = row_to_dict(slip_row)
        gross = to_decimal(str(slip.get("gross_pay", "0")))
        if gross <= Decimal("0"):
            continue
        employee_count += 1
        emp_province = (slip.get("province") or company_province or "ON").upper()
        is_quebec = emp_province == "QC"

        annual_gross = gross * Decimal(str(periods))

        # CPP/QPP
        if is_quebec:
            qpp_rates = _get_qpp_rates()
            p_r = to_decimal(qpp_rates["rate"]) / Decimal("100")
            p_max = to_decimal(qpp_rates["max_pensionable_earnings"])
            p_ex = to_decimal(qpp_rates["basic_exemption"])
            p_mc = to_decimal(qpp_rates["max_employee_contribution"])
        else:
            p_r = to_decimal(cpp_rates["rate"]) / Decimal("100")
            p_max = to_decimal(cpp_rates["max_pensionable_earnings"])
            p_ex = to_decimal(cpp_rates["basic_exemption"])
            p_mc = to_decimal(cpp_rates["max_employee_contribution"])

        p_earn = max(min(annual_gross, p_max) - p_ex, Decimal("0"))
        ann_p = min(round_currency(p_earn * p_r), p_mc)
        per_p = round_currency(ann_p / Decimal(str(periods)))
        total_cpp_employee += per_p
        total_cpp_employer += per_p

        # EI
        ei_rk = "quebec_rate" if is_quebec else "rate"
        ei_mk = "quebec_max_employee_premium" if is_quebec else "max_employee_premium"
        ei_r = to_decimal(ei_rates[ei_rk]) / Decimal("100")
        ei_mi = to_decimal(ei_rates["max_insurable_earnings"])
        ei_mp = to_decimal(ei_rates[ei_mk])
        emp_mult = to_decimal(ei_rates["employer_multiplier"])
        ann_ei = min(round_currency(min(annual_gross, ei_mi) * ei_r), ei_mp)
        per_ei = round_currency(ann_ei / Decimal(str(periods)))
        total_ei_employee += per_ei
        total_ei_employer += round_currency(per_ei * emp_mult)

        # Income tax (federal only for PD7A purposes)
        fed_gt, _, _ = _progressive_tax(annual_gross, fed_data["brackets"])
        fed_cr = round_currency(to_decimal(fed_data["basic_personal_amount"]) * Decimal("0.15"))
        ann_ft = max(round_currency(fed_gt - fed_cr), Decimal("0"))
        per_ft = round_currency(ann_ft / Decimal(str(periods)))
        total_income_tax += per_ft

    total_remittance = round_currency(
        total_cpp_employee + total_cpp_employer +
        total_ei_employee + total_ei_employer +
        total_income_tax
    )

    ok({
        "report": "PD7A — Statement of Account for Current Source Deductions",
        "period": period_str,
        "company": company.get("name", ""),
        "employee_count": employee_count,
        "line_1_cpp_employee": str(round_currency(total_cpp_employee)),
        "line_2_cpp_employer": str(round_currency(total_cpp_employer)),
        "line_3_ei_employee": str(round_currency(total_ei_employee)),
        "line_4_ei_employer": str(round_currency(total_ei_employer)),
        "line_5_income_tax": str(round_currency(total_income_tax)),
        "total_remittance": str(total_remittance),
        "due_date": f"15th of following month ({year}-{month + 1:02d}-15)" if month < 12 else f"{year + 1}-01-15",
        "note": "Remit to CRA by the 15th of the month following the deduction. Penalties apply for late remittance.",
    })


# ---------------------------------------------------------------------------
# Report actions
# ---------------------------------------------------------------------------

def ca_tax_summary(conn, args):
    """Dashboard: GST/HST collected, ITCs, net GST payable, payroll totals."""
    company = _get_company(conn, args.company_id)
    _check_ca_company(company)
    if not args.from_date or not args.to_date:
        err("--from-date and --to-date are required")

    company_id = company["id"]

    # GST/HST collected (sales)
    sales_tax = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total,
                  COALESCE(SUM(CAST(total_amount AS REAL)), 0) as revenue,
                  COUNT(*) as count
           FROM sales_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date <= ?""",
        (company_id, args.from_date, args.to_date),
    ).fetchone()

    # ITC (purchases)
    purchase_tax = conn.execute(
        """SELECT COALESCE(SUM(CAST(tax_amount AS REAL)), 0) as total,
                  COUNT(*) as count
           FROM purchase_invoice
           WHERE company_id = ? AND status = 'submitted'
             AND posting_date >= ? AND posting_date <= ?""",
        (company_id, args.from_date, args.to_date),
    ).fetchone()

    gst_collected = round_currency(to_decimal(str(sales_tax["total"])))
    gst_paid = round_currency(to_decimal(str(purchase_tax["total"])))
    net_gst = gst_collected - gst_paid

    # Payroll totals (salary slips in the period)
    payroll = conn.execute(
        """SELECT COALESCE(SUM(CAST(gross_pay AS REAL)), 0) as total_gross,
                  COALESCE(SUM(CAST(total_deductions AS REAL)), 0) as total_deductions,
                  COUNT(*) as slip_count
           FROM salary_slip
           WHERE company_id = ? AND status = 'submitted'
             AND period_start >= ? AND period_start <= ?""",
        (company_id, args.from_date, args.to_date),
    ).fetchone()

    total_payroll_gross = round_currency(to_decimal(str(payroll["total_gross"])))
    total_payroll_ded = round_currency(to_decimal(str(payroll["total_deductions"])))

    ok({
        "report": "Canada Tax Summary",
        "period": f"{args.from_date} to {args.to_date}",
        "company": company.get("name", ""),
        "gst_hst_collected": str(gst_collected),
        "itc_paid": str(gst_paid),
        "net_gst_hst_payable": str(round_currency(max(net_gst, Decimal("0")))),
        "sales_invoice_count": sales_tax["count"],
        "purchase_invoice_count": purchase_tax["count"],
        "total_gross_payroll": str(total_payroll_gross),
        "total_payroll_deductions": str(total_payroll_ded),
        "salary_slip_count": payroll["slip_count"],
        "revenue": str(round_currency(to_decimal(str(sales_tax["revenue"])))),
    })


def available_reports(conn, args):
    """List all Canada-specific reports available."""
    reports = [
        {"name": "GST/HST Return (GST34)", "action": "generate-gst-hst-return", "description": "Monthly/quarterly GST/HST return"},
        {"name": "QST Return", "action": "generate-qst-return", "description": "Quebec Sales Tax return (QC only)"},
        {"name": "T4", "action": "generate-t4", "description": "Statement of Remuneration Paid (annual per employee)"},
        {"name": "T4A", "action": "generate-t4a", "description": "Statement of non-employment income"},
        {"name": "ROE", "action": "generate-roe", "description": "Record of Employment (on separation)"},
        {"name": "PD7A", "action": "generate-pd7a", "description": "Monthly remittance statement (CPP + EI + tax)"},
        {"name": "Canada Tax Summary", "action": "ca-tax-summary", "description": "GST/HST + payroll dashboard"},
        {"name": "Payroll Summary", "action": "ca-payroll-summary", "description": "Per-employee deduction breakdown"},
        {"name": "Tax Rates", "action": "list-tax-rates", "description": "All provinces GST/HST/PST/QST rates"},
        {"name": "Input Tax Credits", "action": "compute-itc", "description": "ITC from purchase invoices"},
        {"name": "Sales Tax Calculator", "action": "compute-sales-tax", "description": "All-in-one province tax calculator"},
        {"name": "Payroll Deductions", "action": "compute-total-payroll-deductions", "description": "CPP/EI/tax all-in-one"},
    ]

    if args.company_id:
        company = _get_company(conn, args.company_id)
        ok({"company": company.get("name", ""), "reports": reports, "total": len(reports)})
    else:
        ok({"reports": reports, "total": len(reports)})


def status_action(conn, args):
    """Skill status: version, config, asset files."""
    result = {
        "skill": "erpclaw-region-ca",
        "version": "1.0.0",
        "description": "Canada Regional Compliance (GST/HST/PST/QST, CPP/EI, T4/ROE)",
    }

    if args.company_id:
        company = _get_company(conn, args.company_id)
        result["company"] = company.get("name", "")
        result["country"] = company.get("country", "")
        result["province"] = _get_company_province(conn, company)

        # Check GST/HST configuration
        try:
            bn_row = conn.execute(
                "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'bn'",
                (company["id"],),
            ).fetchone()
            result["bn_configured"] = bn_row is not None
            if bn_row:
                result["business_number"] = bn_row["value"]

            prov_row = conn.execute(
                "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'province'",
                (company["id"],),
            ).fetchone()
            if prov_row:
                result["configured_province"] = prov_row["value"]

            config_row = conn.execute(
                "SELECT value FROM regional_settings WHERE company_id = ? AND key = 'gst_hst_configured'",
                (company["id"],),
            ).fetchone()
            result["gst_hst_configured"] = config_row is not None
        except Exception:
            result["bn_configured"] = False
            result["gst_hst_configured"] = False

        # Count templates
        templates = conn.execute(
            "SELECT COUNT(*) as cnt FROM tax_template WHERE company_id = ? AND name LIKE 'Canada%'",
            (company["id"],),
        ).fetchone()
        result["ca_tax_templates"] = templates["cnt"]

        # Count accounts
        accounts = conn.execute(
            "SELECT COUNT(*) as cnt FROM account WHERE company_id = ? AND (name LIKE '%GST%' OR name LIKE '%HST%' OR name LIKE '%PST%' OR name LIKE '%QST%')",
            (company["id"],),
        ).fetchone()
        result["ca_tax_accounts"] = accounts["cnt"]

    # Asset files status
    asset_files = [
        "ca_gst_hst_rates.json", "ca_pst_rates.json", "ca_cpp_rates.json",
        "ca_qpp_rates.json", "ca_ei_rates.json", "ca_federal_tax_brackets.json",
        "ca_provincial_tax_brackets.json", "ca_coa_aspe.json",
    ]
    result["asset_files"] = {}
    for f in asset_files:
        path = os.path.join(ASSETS_DIR, f)
        result["asset_files"][f] = os.path.exists(path)

    ok(result)


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

ACTIONS = {
    # Validation (2)
    "validate-business-number": validate_business_number,
    "validate-sin": validate_sin,
    # Tax computation (7)
    "compute-gst": compute_gst,
    "compute-hst": compute_hst,
    "compute-pst": compute_pst,
    "compute-qst": compute_qst,
    "compute-sales-tax": compute_sales_tax,
    "list-tax-rates": list_tax_rates,
    "compute-itc": compute_itc,
    # Seed / Setup (4)
    "seed-ca-defaults": seed_ca_defaults,
    "setup-gst-hst": setup_gst_hst,
    "seed-ca-coa": seed_ca_coa,
    "seed-ca-payroll": seed_ca_payroll,
    # Payroll (8)
    "compute-cpp": compute_cpp,
    "compute-cpp2": compute_cpp2,
    "compute-qpp": compute_qpp,
    "compute-ei": compute_ei,
    "compute-federal-tax": compute_federal_tax,
    "compute-provincial-tax": compute_provincial_tax,
    "compute-total-payroll-deductions": compute_total_payroll_deductions,
    "ca-payroll-summary": ca_payroll_summary,
    # Compliance (6)
    "generate-gst-hst-return": generate_gst_hst_return,
    "generate-qst-return": generate_qst_return,
    "generate-t4": generate_t4,
    "generate-t4a": generate_t4a,
    "generate-roe": generate_roe,
    "generate-pd7a": generate_pd7a,
    # Reports (3)
    "ca-tax-summary": ca_tax_summary,
    "available-reports": available_reports,
    "status": status_action,
}


def main():
    parser = argparse.ArgumentParser(description="ERPClaw Canada Regional Skill")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # Company
    parser.add_argument("--company-id")

    # Tax
    parser.add_argument("--amount")
    parser.add_argument("--province")
    parser.add_argument("--search")
    parser.add_argument("--business-number")

    # Validation
    parser.add_argument("--bn")
    parser.add_argument("--sin")

    # Reports / periods
    parser.add_argument("--month")
    parser.add_argument("--year")
    parser.add_argument("--period")
    parser.add_argument("--tax-year")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")

    # Payroll
    parser.add_argument("--gross-salary")
    parser.add_argument("--annual-income")
    parser.add_argument("--annual-earnings")
    parser.add_argument("--pay-period")
    parser.add_argument("--pay-periods", default="12")
    parser.add_argument("--employee-id")

    # T4A
    parser.add_argument("--recipient-name")
    parser.add_argument("--income-type")

    # ROE
    parser.add_argument("--reason-code")

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
