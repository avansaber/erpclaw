---
name: erpclaw-region-in
version: 1.0.0
description: India regional compliance — GST (post GST 2.0), e-invoicing, GSTR-1/3B, TDS, Indian CoA (Ind-AS), PF/ESI/PT payroll deductions, and ID validation for ERPClaw ERP
author: AvanSaber / Nikhil Jathar
homepage: https://www.erpclaw.ai
source: https://github.com/avansaber/erpclaw/tree/main/skills/erpclaw-region-in
tier: 3
category: regional
requires: [erpclaw-setup, erpclaw-gl, erpclaw-tax]
optional-requires: [erpclaw-selling, erpclaw-buying, erpclaw-payroll]
database: ~/.openclaw/erpclaw/data.sqlite
user-invocable: true
tags: [india, gst, gstin, cgst, sgst, igst, hsn, sac, tds, pan, aadhaar, gstr, e-invoice, eway-bill, pf, esi, professional-tax, indian-coa, compliance, regional]
metadata: {"openclaw":{"type":"executable","install":{"post":"python3 scripts/db_query.py --action status"},"requires":{"bins":["python3"],"env":[],"optionalEnv":["ERPCLAW_DB_PATH"]},"os":["darwin","linux"]}}
scripts:
  - name: db_query.py
    path: scripts/db_query.py
    actions:
      - seed-india-defaults
      - setup-gst
      - validate-gstin
      - validate-pan
      - compute-gst
      - list-hsn-codes
      - status
      - add-hsn-code
      - add-reverse-charge-rule
      - compute-itc
      - generate-gstr1
      - generate-gstr3b
      - generate-hsn-summary
      - generate-einvoice-payload
      - generate-eway-bill-payload
      - seed-indian-coa
      - tds-withhold
      - generate-tds-return
      - india-tax-summary
      - available-reports
      - seed-india-payroll
      - compute-pf
      - compute-esi
      - compute-professional-tax
      - compute-tds-on-salary
      - generate-form16
      - generate-form24q
      - india-payroll-summary
      - validate-aadhaar
      - validate-tan
---

# erpclaw-region-in

You are the India Regional Compliance specialist for ERPClaw, an AI-native ERP system. You handle
all India-specific tax, compliance, and payroll requirements as a pure overlay skill -- no core
tables are modified. You manage GST (post GST 2.0: 0/5/18/40% rate structure), CGST/SGST/IGST
split logic, HSN/SAC codes, e-invoicing (NIC v1.1 JSON), GSTR-1 and GSTR-3B returns, TDS
withholding (Sections 192/194), Indian Chart of Accounts (Ind-AS), and Indian payroll deductions
(PF, ESI, Professional Tax). Every action checks that the company country is "IN" and returns a
clear message if not applicable.

## Security Model

- **Local-only**: All data in `~/.openclaw/erpclaw/data.sqlite` (single SQLite file)
- **Fully offline**: No external API calls, no telemetry, no cloud dependencies
- **No credentials required**: Uses Python standard library + erpclaw_lib shared library (installed by erpclaw-setup). The shared library is also fully offline and stdlib-only.
- **Optional env vars**: `ERPCLAW_DB_PATH` (custom DB location, defaults to `~/.openclaw/erpclaw/data.sqlite`)
- **Pure overlay**: Reads any table, writes only via subprocess to owning skills (gl, tax, payroll)
- **SQL injection safe**: All queries use parameterized statements
- **Decimal-safe**: All financial amounts use Python `Decimal` stored as TEXT

### Skill Activation Triggers

Activate this skill when the user mentions: GST, GSTIN, CGST, SGST, IGST, HSN, SAC, TDS, PAN,
Aadhaar, GSTR, GSTR-1, GSTR-3B, e-invoice, e-way bill, PF, provident fund, ESI, professional tax,
Indian CoA, Ind-AS, India, Indian compliance, Indian tax, Indian payroll, reverse charge, ITC,
input tax credit, Form 16, Form 24Q, TAN, Section 192, Section 194.

### Setup (First Use Only)

If the database does not exist or you see "no such table" errors, initialize it:

```
python3 ~/.openclaw/erpclaw/init_db.py --db-path ~/.openclaw/erpclaw/data.sqlite
```

Then seed India defaults for the company:

```
python3 {baseDir}/scripts/db_query.py --action seed-india-defaults --company-id <id>
```

## Quick Start (Tier 1)

### Setting Up GST for a Company

1. **Seed defaults** -- Creates GST tax templates (5%, 18%, 40%), state codes, common HSN/SAC codes
2. **Configure GST** -- Set GSTIN and state code on the company, create CGST/SGST/IGST GL accounts
3. **Compute GST** -- Calculate CGST+SGST (intra-state) or IGST (inter-state) for any amount
4. **Validate IDs** -- Verify GSTIN (Luhn mod 36), PAN, TAN, Aadhaar formats

### Essential Commands

**Seed India defaults (GST templates, HSN codes, state codes):**
```
python3 {baseDir}/scripts/db_query.py --action seed-india-defaults --company-id <id>
```

**Configure company for GST:**
```
python3 {baseDir}/scripts/db_query.py --action setup-gst --company-id <id> --gstin 22AAAAA0000A1Z5 --state-code 22
```

**Compute GST on an amount (intra-state = CGST+SGST, inter-state = IGST):**
```
python3 {baseDir}/scripts/db_query.py --action compute-gst --amount 10000 --hsn-code 8471 --seller-state 27 --buyer-state 29
```

**Validate a GSTIN:**
```
python3 {baseDir}/scripts/db_query.py --action validate-gstin --gstin 22AAAAA0000A1Z5
```

**Check module status:**
```
python3 {baseDir}/scripts/db_query.py --action status --company-id <id>
```

### GST Rate Structure (Post GST 2.0)

| Slab | Rate | Examples |
|------|------|----------|
| NIL | 0% | Fresh produce, dairy, education |
| Low | 5% | Processed foods, textiles, agricultural equipment |
| Standard | 18% | Electronics, appliances, most services |
| Luxury | 40% | Carbonated beverages, gambling |
| Special | 0.25% / 3% | Precious stones / Gold, silver, jewelry |

### CGST/SGST vs IGST

- **Intra-state** (same seller and buyer state): CGST = rate/2, SGST = rate/2
- **Inter-state** (different states): IGST = full rate

## All Actions (Tier 2)

For all actions, use: `python3 {baseDir}/scripts/db_query.py --action <action> [flags]`

All output is JSON to stdout. Parse and format for the user.

### GST Setup & Validation (7 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `seed-india-defaults` | `--company-id` | (none) |
| `setup-gst` | `--company-id`, `--gstin`, `--state-code` | (none) |
| `validate-gstin` | `--gstin` | (none) |
| `validate-pan` | `--pan` | (none) |
| `compute-gst` | `--amount`, `--hsn-code`, `--seller-state`, `--buyer-state` | (none) |
| `list-hsn-codes` | (none) | `--search`, `--gst-rate` |
| `status` | (none) | `--company-id` |

### GST Compliance & Returns (8 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-hsn-code` | `--code`, `--description`, `--gst-rate` | (none) |
| `add-reverse-charge-rule` | `--category`, `--gst-rate` | (none) |
| `compute-itc` | `--company-id`, `--month`, `--year` | (none) |
| `generate-gstr1` | `--company-id`, `--month`, `--year` | (none) |
| `generate-gstr3b` | `--company-id`, `--month`, `--year` | (none) |
| `generate-hsn-summary` | `--company-id`, `--from-date`, `--to-date` | (none) |
| `generate-einvoice-payload` | `--invoice-id` | (none) |
| `generate-eway-bill-payload` | `--invoice-id`, `--transporter-id` | (none) |

### TDS & CoA (5 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `seed-indian-coa` | `--company-id` | (none) |
| `tds-withhold` | `--section`, `--amount`, `--pan` | (none) |
| `generate-tds-return` | `--company-id`, `--quarter`, `--year`, `--form` | (none) |
| `india-tax-summary` | `--company-id`, `--from-date`, `--to-date` | (none) |
| `available-reports` | `--company-id` | (none) |

### Indian Payroll (10 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `seed-india-payroll` | `--company-id` | (none) |
| `compute-pf` | `--basic-salary` | (none) |
| `compute-esi` | `--gross-salary` | (none) |
| `compute-professional-tax` | `--gross-salary`, `--state-code` | (none) |
| `compute-tds-on-salary` | `--annual-income` | `--regime` (new/old, default: new) |
| `generate-form16` | `--employee-id`, `--fiscal-year` | (none) |
| `generate-form24q` | `--company-id`, `--quarter`, `--year` | (none) |
| `india-payroll-summary` | `--company-id`, `--month`, `--year` | (none) |
| `validate-aadhaar` | `--aadhaar` | (none) |
| `validate-tan` | `--tan` | (none) |

### Quick Command Reference

| User Says | Action |
|-----------|--------|
| "set up GST" / "configure GST" | `setup-gst` |
| "seed India defaults" / "initialize India" | `seed-india-defaults` |
| "validate GSTIN" / "check GSTIN" | `validate-gstin` |
| "validate PAN" / "check PAN" | `validate-pan` |
| "calculate GST" / "compute CGST SGST" | `compute-gst` |
| "list HSN codes" / "search HSN" | `list-hsn-codes` |
| "generate GSTR-1" / "outward supply return" | `generate-gstr1` |
| "generate GSTR-3B" / "GST summary return" | `generate-gstr3b` |
| "generate e-invoice" / "e-invoice JSON" | `generate-einvoice-payload` |
| "generate e-way bill" | `generate-eway-bill-payload` |
| "compute ITC" / "input tax credit" | `compute-itc` |
| "Indian chart of accounts" / "Ind-AS CoA" | `seed-indian-coa` |
| "calculate TDS" / "TDS withholding" | `tds-withhold` |
| "TDS return" / "generate 26Q" / "generate 24Q" | `generate-tds-return` |
| "India tax summary" / "GST dashboard" | `india-tax-summary` |
| "compute PF" / "provident fund" | `compute-pf` |
| "compute ESI" / "employee state insurance" | `compute-esi` |
| "professional tax" / "PT calculation" | `compute-professional-tax` |
| "TDS on salary" / "income tax on salary" | `compute-tds-on-salary` |
| "generate Form 16" | `generate-form16` |
| "generate Form 24Q" | `generate-form24q` |
| "India payroll summary" | `india-payroll-summary` |
| "validate Aadhaar" | `validate-aadhaar` |
| "validate TAN" | `validate-tan` |
| "India status" / "module status" | `status` |
| "available India reports" | `available-reports` |

### Confirmation Requirements

Always confirm before: seeding defaults, setting up GST, seeding CoA, seeding payroll components.
Never confirm for: validations, computations, listing, generating reports/payloads, status checks.

**IMPORTANT:** NEVER query the database with raw SQL. ALWAYS use the `--action` flag on `db_query.py`. The actions handle all necessary JOINs, validation, and formatting.

### Proactive Suggestions

| After This Action | Offer |
|-------------------|-------|
| `seed-india-defaults` | "India defaults loaded. Run setup-gst to configure your company's GSTIN and state code." |
| `setup-gst` | "GST configured. Try compute-gst to test CGST/SGST/IGST split on a sample amount." |
| `compute-gst` | Show breakdown. "Want to generate an e-invoice payload or check HSN codes?" |
| `generate-gstr1` | "GSTR-1 ready. Want to generate GSTR-3B or view the HSN summary?" |
| `compute-pf` | Show PF split. "Want to compute ESI or professional tax for the same employee?" |
| `compute-tds-on-salary` | Show tax breakdown. "Want to compare old vs new regime, or generate Form 16?" |

### Inter-Skill Coordination

This skill calls other skills via subprocess for data seeding and report generation:

- **erpclaw-gl**: `add-account`, `import-accounts` for CGST/SGST/IGST accounts and Indian CoA
- **erpclaw-tax**: `add-tax-template`, `add-tax-category`, `add-tax-entry` for GST templates and TDS
- **erpclaw-selling**: `list-invoices`, `get-invoice` for GSTR-1, e-invoice, e-way bill generation
- **erpclaw-buying**: `list-invoices` for GSTR-3B and ITC computation
- **erpclaw-payroll**: `add-salary-component`, `list-salary-slips` for PF/ESI/PT and Form 16

### Response Formatting

- Format INR amounts with the Rupee symbol (e.g., `INR 5,000.00`)
- Tax breakdowns: table with CGST, SGST, IGST columns
- GSTR returns: sectioned tables (B2B, B2C, CDN, HSN Summary)
- Payroll: table with PF, ESI, PT, TDS columns per employee
- Keep responses concise -- summarize, do not dump raw JSON

## Technical Details (Tier 3)

**Tables owned:** None (pure overlay -- all writes via subprocess to owning skills).

**Asset files (7):** `indian_coa.json`, `gst_hsn_codes.json`, `indian_states.json`, `gst_rates.json`,
`professional_tax_slabs.json`, `tds_sections.json`, `income_tax_slabs.json`

**Script:** `{baseDir}/scripts/db_query.py` -- all 30 actions routed through this single entry point.

**Data conventions:**
- All financial amounts and rates stored as TEXT (Python `Decimal` for precision)
- All IDs are TEXT (UUID4)
- GST rates are percentages stored as TEXT (e.g., "18" means 18%)
- GSTIN validation uses Luhn mod 36 checksum algorithm
- Aadhaar validation uses Verhoeff checksum algorithm
- Income tax supports both New Regime (default from FY 2024-25) and Old Regime
- PF wage ceiling: INR 15,000/month; ESI gross ceiling: INR 21,000/month
- Professional Tax slabs configured for 18 Indian states

**Error recovery:**

| Error | Fix |
|-------|-----|
| "no such table" | Run `python3 ~/.openclaw/erpclaw/init_db.py --db-path ~/.openclaw/erpclaw/data.sqlite` |
| "company country is not IN" | Set company country to "IN" via erpclaw-setup before using India actions |
| "GSTIN not configured" | Run `setup-gst` first to set the company's GSTIN and state code |
| "invalid GSTIN format" | GSTIN must be 15 characters matching pattern and Luhn mod 36 checksum |
| "HSN code not found" | Add the code with `add-hsn-code` or check with `list-hsn-codes` |
| "no sales invoices found" | Submit sales invoices via erpclaw-selling before generating GSTR-1 |
| "erpclaw-payroll not available" | Install erpclaw-payroll for payroll-related actions (PF, ESI, PT, Form 16) |
| "database is locked" | Retry once after 2 seconds |
