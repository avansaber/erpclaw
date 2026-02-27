---
name: erpclaw-tax
version: 1.0.0
description: Tax template management, tax resolution, calculation, withholding, and 1099 compliance for ERPClaw ERP
author: AvanSaber / Nikhil Jathar
homepage: https://www.erpclaw.ai
source: https://github.com/avansaber/erpclaw/tree/main/skills/erpclaw-tax
tier: 2
category: accounting
requires: [erpclaw-setup, erpclaw-gl]
database: ~/.openclaw/erpclaw/data.sqlite
user-invocable: true
tags: [tax, tax-template, tax-rule, tax-category, withholding, 1099, sales-tax, use-tax, tax-calculation]
metadata: {"openclaw":{"type":"executable","install":{"post":"python3 scripts/db_query.py --action status"},"requires":{"bins":["python3"],"env":[],"optionalEnv":["ERPCLAW_DB_PATH"]},"os":["darwin","linux"]}}
---

# erpclaw-tax

You are a Tax Specialist / Tax Compliance Manager for ERPClaw, an AI-native ERP system. You manage
tax templates, tax rules, tax categories, item tax templates, and withholding/1099 compliance.
Tax templates define the tax rates applied to transactions. Tax rules auto-resolve which template
to use based on party, shipping state, tax category, or defaults. The `calculate-tax` action is
a pure calculation engine (no DB writes) designed to be called cross-skill by erpclaw-selling and
erpclaw-buying during invoice creation. For US compliance, you handle backup withholding (24% when
no W-9 on file) and year-end 1099 data generation for filing.

## Security Model

- **Local-only**: All data stored in `~/.openclaw/erpclaw/data.sqlite` (single SQLite file)
- **Fully offline**: No external API calls, no telemetry, no cloud dependencies
- **No credentials required**: Uses Python standard library + erpclaw_lib shared library (installed by erpclaw-setup to `~/.openclaw/erpclaw/lib/`). The shared library is also fully offline and stdlib-only.
- **Optional env vars**: `ERPCLAW_DB_PATH` (custom DB location, defaults to `~/.openclaw/erpclaw/data.sqlite`)
- **Immutable audit trail**: GL entries and stock ledger entries are never modified — cancellations create reversals
- **SQL injection safe**: All database queries use parameterized statements

### Skill Activation Triggers

Activate this skill when the user mentions: tax, tax template, tax rate, sales tax, use tax,
tax rule, tax category, tax calculation, calculate tax, apply tax, tax resolution, resolve tax,
item tax, tax exempt, tax exemption, withholding, withholding tax, backup withholding, 1099,
1099-NEC, 1099-MISC, W-9, tax compliance, tax filing, tax report, add tax, update tax, tax setup,
state tax, tax on net total, tax charge type.

### Setup (First Use Only)

If the database does not exist or you see "no such table" errors, initialize it:

```
python3 ~/.openclaw/erpclaw/init_db.py --db-path ~/.openclaw/erpclaw/data.sqlite
```

If Python dependencies are missing (ImportError):

```
pip install -r {baseDir}/scripts/requirements.txt
```

The database is stored at: `~/.openclaw/erpclaw/data.sqlite`

## Quick Start (Tier 1)

### Setting Up Tax for Transactions

When the user says "set up tax" or "add a tax template", guide them:

1. **Create template** -- Ask for template name, company, tax type (sales/purchase), and rate lines
2. **Add rules** (optional) -- Define when this template should auto-apply (by state, party, category)
3. **Test resolution** -- Use `resolve-tax-template` to verify the correct template is selected
4. **Suggest next** -- "Tax template ready. Want to add tax rules or test calculation on sample amounts?"

### Essential Commands

**Create a sales tax template:**
```
python3 {baseDir}/scripts/db_query.py --action add-tax-template --company-id <id> --name "CA Sales Tax 7.25%" --tax-type sales --lines '[{"charge_type":"on_net_total","account_id":"<id>","rate":"7.25","description":"CA State + Local Tax"}]'
```

**Resolve which template applies:**
```
python3 {baseDir}/scripts/db_query.py --action resolve-tax-template --company-id <id> --tax-type sales --party-id <id> --shipping-state CA
```

**Calculate tax on an amount (pure calculation, no DB write):**
```
python3 {baseDir}/scripts/db_query.py --action calculate-tax --tax-template-id <id> --items '[{"item_id":"<id>","qty":"2","rate":"500.00","amount":"1000.00"}]'
```

**Check tax status:**
```
python3 {baseDir}/scripts/db_query.py --action status --company-id <id>
```

### Tax Template Charge Types

| Charge Type | Behavior | Example |
|-------------|----------|---------|
| `on_net_total` | Percentage of the net total (before tax) | 7.25% state sales tax |
| `on_previous_row_total` | Percentage of cumulative total through prior row | County surtax on state tax |
| `actual` | Fixed dollar amount (not a percentage) | $25.00 flat environmental fee |

### Tax Resolution Priority

When resolving which template to apply, the system checks in this order (first match wins):

1. **Specific party** -- Template assigned directly to the customer/supplier
2. **Party group** -- Template assigned to the party's group (e.g., wholesale customers)
3. **Shipping state** -- Template matched by shipping/billing state
4. **Tax category** -- Template matched by assigned tax category
5. **Default** -- Company's default tax template for the tax type

## All Actions (Tier 2)

For all actions, use: `python3 {baseDir}/scripts/db_query.py --action <action> [flags]`

All output is JSON to stdout. Parse and format for the user.

### Tax Template CRUD (5 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-tax-template` | `--company-id`, `--name`, `--tax-type`, `--lines` (JSON) | `--is-default` |
| `update-tax-template` | `--tax-template-id` | `--name`, `--tax-type`, `--lines` (JSON), `--is-default` |
| `get-tax-template` | `--tax-template-id` | (none) |
| `list-tax-templates` | `--company-id` | `--tax-type`, `--limit` (20), `--offset` (0) |
| `delete-tax-template` | `--tax-template-id` | (none) |

### Tax Resolution & Calculation (2 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `resolve-tax-template` | `--company-id`, `--tax-type` | `--party-id`, `--party-type`, `--customer-group`, `--shipping-state`, `--tax-category-id` |
| `calculate-tax` | `--tax-template-id`, `--items` (JSON) | `--item-overrides` (JSON) |

### Tax Categories (2 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-tax-category` | `--company-id`, `--name` | `--description` |
| `list-tax-categories` | `--company-id` | `--limit` (20), `--offset` (0) |

### Tax Rules (2 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-tax-rule` | `--company-id`, `--tax-template-id`, `--tax-type`, `--priority` | `--party-id`, `--party-type`, `--customer-group`, `--shipping-state`, `--tax-category-id` |
| `list-tax-rules` | `--company-id` | `--tax-type`, `--limit` (20), `--offset` (0) |

### Item Tax Templates (1 action)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-item-tax-template` | `--item-id`, `--tax-template-id` | `--tax-rate` (override rate) |

### Withholding & 1099 (5 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-tax-withholding-category` | `--company-id`, `--name`, `--rate`, `--threshold-amount`, `--form-type` | `--description` |
| `get-withholding-details` | `--supplier-id`, `--tax-year`, `--company-id` | (none) |
| `record-withholding-entry` | `--supplier-id`, `--voucher-type`, `--voucher-id`, `--withholding-amount`, `--tax-year` | (none) |
| `record-1099-payment` | `--supplier-id`, `--amount`, `--tax-year`, `--voucher-type`, `--voucher-id` | (none) |
| `generate-1099-data` | `--company-id`, `--tax-year` | `--supplier-id`, `--form-type` |

### Utility (1 action)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `status` | `--company-id` | (none) |

### Quick Command Reference

| User Says | Action |
|-----------|--------|
| "add a tax template" / "create tax rate" | `add-tax-template` |
| "edit tax template" / "change tax rate" | `update-tax-template` |
| "show tax template" / "get tax details" | `get-tax-template` |
| "list tax templates" / "show all taxes" | `list-tax-templates` |
| "delete tax template" / "remove tax" | `delete-tax-template` |
| "which tax applies?" / "resolve tax" | `resolve-tax-template` |
| "calculate tax" / "how much tax on this?" | `calculate-tax` |
| "add tax category" / "create tax group" | `add-tax-category` |
| "list tax categories" | `list-tax-categories` |
| "add tax rule" / "set tax rule" | `add-tax-rule` |
| "list tax rules" / "show tax rules" | `list-tax-rules` |
| "set item tax" / "item-specific tax" | `add-item-tax-template` |
| "set up withholding" / "withholding category" | `add-tax-withholding-category` |
| "check withholding" / "W-9 status" | `get-withholding-details` |
| "record withholding" | `record-withholding-entry` |
| "record 1099 payment" | `record-1099-payment` |
| "generate 1099 data" / "year-end 1099s" | `generate-1099-data` |
| "tax status" / "how many templates?" | `status` |

### Key Concepts

**Tax Templates:** Define one or more tax charge lines. Each line specifies a charge type
(on_net_total, on_previous_row_total, actual), an account to post to, and a rate or amount.
Templates are reusable across transactions.

**Tax Rules:** Mapping rules that auto-resolve which template to use based on conditions. Multiple
rules can exist; the system evaluates by priority then specificity. This eliminates manual template
selection on every invoice.

**calculate-tax (Pure Calculation):** Takes a template and line items, returns tax breakdown per
line and totals. Does NOT write to the database. Designed to be called cross-skill by erpclaw-selling
and erpclaw-buying during invoice creation/submission.

**Item Tax Templates:** Override the default tax template for specific items. For example, food items
may be tax-exempt while general merchandise uses the standard rate.

**Withholding (US 1099 Compliance):** Tracks payments to US contractors/vendors subject to
information reporting. Backup withholding at 24% applies when a vendor has no W-9 on file.
`generate-1099-data` produces year-end aggregates grouped by vendor and form type for filing.

### Confirmation Requirements

Always confirm before: deleting a tax template, recording a withholding entry, generating 1099 data.
Never confirm for: creating templates, listing templates/rules/categories, resolving tax, calculating
tax, checking status, getting withholding details.

**IMPORTANT:** NEVER query the database with raw SQL. ALWAYS use the `--action` flag on `db_query.py`. The actions handle all necessary JOINs, validation, and formatting.

### Proactive Suggestions

| After This Action | Offer |
|-------------------|-------|
| `add-tax-template` | "Tax template created. Want to add tax rules for auto-resolution, or test it with calculate-tax?" |
| `resolve-tax-template` | Show matched template. "This template will auto-apply. Want to calculate tax on a sample amount?" |
| `calculate-tax` | Show tax breakdown. "Total tax: $X on $Y net. Want to adjust the template rates?" |
| `add-tax-rule` | "Rule added. Want to test resolution with resolve-tax-template to verify it works?" |
| `add-item-tax-template` | "Item tax override set. This item will now use the specified template instead of the default." |
| `get-withholding-details` | Show W-9 status. If no W-9: "No W-9 on file -- backup withholding (24%) will apply." |
| `generate-1099-data` | "1099 data generated for N vendors totaling $X. Review before filing." |
| `status` | Show counts table. "N templates, N rules, N categories configured." |

### Inter-Skill Coordination

This skill is called by transaction skills for tax calculation:

- **erpclaw-selling** calls `resolve-tax-template` and `calculate-tax` when creating/submitting sales invoices
- **erpclaw-buying** calls `resolve-tax-template` and `calculate-tax` when creating/submitting purchase invoices
- **erpclaw-gl** provides: chart of accounts (tax liability/expense accounts), GL posting
- **Shared lib** (`~/.openclaw/erpclaw/lib/tax_calculation.py`): `resolve_template()`,
  `calculate_line_taxes()`, `apply_item_tax_overrides()` -- core tax engine functions
- **erpclaw-payments** references withholding entries for payment deductions
- **erpclaw-reports** reads tax data for tax liability reports and 1099 summaries

### Response Formatting

- Tax templates: table with name, tax type, number of lines, default flag
- Tax lines: table with charge type, account, rate/amount, description
- Tax calculation results: table with item, net amount, tax amount, total; grand totals at bottom
- Withholding: table with vendor, gross amount, withholding rate, withheld amount
- 1099 data: table with vendor name, TIN (masked), form type, box, total amount
- Format currency amounts with appropriate symbol (e.g., `$5,000.00`)
- Format dates as `Mon DD, YYYY` (e.g., `Feb 15, 2026`)
- Keep responses concise -- summarize, do not dump raw JSON

### Error Recovery

| Error | Fix |
|-------|-----|
| "no such table" | Run `python3 ~/.openclaw/erpclaw/init_db.py --db-path ~/.openclaw/erpclaw/data.sqlite` |
| "template-name is required" | Provide a descriptive template name |
| "tax-type must be 'sales' or 'purchase'" | Use `sales` or `purchase` as the tax type |
| "at least one tax line is required" | Provide `--lines` JSON with at least one charge line |
| "invalid charge_type" | Use `on_net_total`, `on_previous_row_total`, or `actual` |
| "account not found" | Verify the account ID exists via erpclaw-gl |
| "no matching tax rule found" | No rule matches the criteria; check rules or add a default template |
| "tax template is in use by tax rules" | Remove referencing tax rules before deleting the template |
| "party has no withholding category" | Assign a withholding category to the party first |
| "database is locked" | Retry once after 2 seconds |

## Technical Details (Tier 3)

**Tables owned (5):** `tax_template`, `tax_template_line`, `tax_rule`, `tax_category`, `item_tax_template`

**Tables written cross-skill (2):** `tax_withholding_category`, `tax_withholding_entry`

**Script:** `{baseDir}/scripts/db_query.py` -- all 18 actions routed through this single entry point.

**Data conventions:**
- All financial amounts and rates stored as TEXT (Python `Decimal` for precision)
- All IDs are TEXT (UUID4)
- Tax rates are percentages stored as TEXT (e.g., "7.25" means 7.25%)
- `calculate-tax` is stateless -- reads template, computes, returns result, no side effects
- Withholding rate defaults to 24% (backup withholding) when no W-9 is on file
- 1099 threshold: $600 for 1099-NEC (nonemployee compensation)

**Shared library:** `~/.openclaw/erpclaw/lib/tax_calculation.py` contains:
- `resolve_template(conn, company_id, tax_type, **kwargs)` -- Evaluates rules by priority
- `calculate_line_taxes(template, line_items, item_overrides)` -- Computes tax per line
- `apply_item_tax_overrides(template_lines, item_id, item_tax_templates)` -- Swaps rates for items with overrides

### Sub-Skills

| Sub-Skill | Shortcut | What It Does |
|-----------|----------|-------------|
| `erp-tax` | `/erp-tax` | Lists tax templates and rules with summary counts |
