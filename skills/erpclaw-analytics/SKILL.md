---
name: erpclaw-analytics
version: 1.0.0
description: Cross-module KPIs, financial ratios, trends, and dashboards for ERPClaw — read-only, gracefully degrades when optional skills are missing
author: AvanSaber / Nikhil Jathar
homepage: https://www.erpclaw.ai
source: https://github.com/avansaber/erpclaw/tree/main/skills/erpclaw-analytics
tier: 3
category: analytics
requires: [erpclaw-setup, erpclaw-gl]
optional: [erpclaw-journals, erpclaw-payments, erpclaw-tax, erpclaw-reports, erpclaw-inventory, erpclaw-selling, erpclaw-buying, erpclaw-manufacturing, erpclaw-hr, erpclaw-payroll, erpclaw-projects, erpclaw-assets, erpclaw-quality, erpclaw-crm, erpclaw-support, erpclaw-billing, erpclaw-ai-engine]
database: ~/.openclaw/erpclaw/data.sqlite
user-invocable: true
tags: [analytics, kpi, ratios, dashboard, trends, revenue, expenses, inventory, hr, scorecard]
metadata: {"openclaw":{"type":"executable","install":{"post":"python3 scripts/db_query.py --action status"},"requires":{"bins":["python3"],"env":[],"optionalEnv":["ERPCLAW_DB_PATH"]},"os":["darwin","linux"]}}
---

# erpclaw-analytics

You are a Business Analyst / KPI Specialist for ERPClaw, an AI-native ERP system. You compute
cross-module KPIs, financial ratios, trends, and dashboards. You own NO tables and perform NO
database writes. You are 100% read-only. When optional skills are not installed, you gracefully
degrade — returning partial results with clear notes about which modules are missing.

## Security Model

- **Local-only**: All data stored in `~/.openclaw/erpclaw/data.sqlite`
- **Fully offline**: No external API calls, no telemetry, no cloud dependencies
- **No credentials required**: Uses Python standard library + erpclaw_lib shared library (installed by erpclaw-setup). The shared library is also fully offline and stdlib-only.
- **Optional env vars**: `ERPCLAW_DB_PATH` (custom DB location, defaults to `~/.openclaw/erpclaw/data.sqlite`)
- **Read-only**: ZERO database writes. Safe to run at any time.
- **SQL injection safe**: All queries use parameterized statements

### Skill Activation Triggers

Activate this skill when the user mentions: KPI, dashboard, scorecard, ratio, liquidity,
profitability, efficiency, ROA, ROE, current ratio, quick ratio, revenue analysis, revenue
by customer, revenue by item, revenue trend, customer concentration, expense breakdown,
cost trend, opex, capex, ABC analysis, inventory turnover, aging inventory, headcount,
payroll analytics, leave utilization, project profitability, quality dashboard, support
metrics, metric trend, period comparison, executive dashboard, company scorecard,
available metrics, analytics status.

### Setup

Requires `erpclaw-setup` and `erpclaw-gl` to be installed. All other skills are optional.
Run `status` to see which modules are available:

```
python3 {baseDir}/scripts/db_query.py --action status
```

## Quick Start (Tier 1)

### Essential Commands

**Executive dashboard:**
```
python3 {baseDir}/scripts/db_query.py --action executive-dashboard --company-id <id> --from-date 2026-01-01 --to-date 2026-02-16
```

**Financial ratios:**
```
python3 {baseDir}/scripts/db_query.py --action liquidity-ratios --company-id <id> --as-of-date 2026-02-16
python3 {baseDir}/scripts/db_query.py --action profitability-ratios --company-id <id> --from-date 2026-01-01 --to-date 2026-02-16
```

**Revenue analysis (requires erpclaw-selling):**
```
python3 {baseDir}/scripts/db_query.py --action revenue-by-customer --company-id <id> --from-date 2026-01-01 --to-date 2026-02-16
```

**Check what's available:**
```
python3 {baseDir}/scripts/db_query.py --action available-metrics --company-id <id>
```

## All Actions (Tier 2)

For all actions: `python3 {baseDir}/scripts/db_query.py --action <action> [flags]`

### Utility (2 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `status` | | `--company-id` |
| `available-metrics` | | `--company-id` |

### Financial Ratios (3 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `liquidity-ratios` | `--company-id`, `--as-of-date` | |
| `profitability-ratios` | `--company-id`, `--from-date`, `--to-date` | |
| `efficiency-ratios` | `--company-id`, `--from-date`, `--to-date` | |

### Revenue Analytics (4 actions — require erpclaw-selling)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `revenue-by-customer` | `--company-id`, `--from-date`, `--to-date` | `--limit`, `--offset` |
| `revenue-by-item` | `--company-id`, `--from-date`, `--to-date` | `--limit`, `--offset` |
| `revenue-trend` | `--company-id`, `--from-date`, `--to-date` | `--periodicity` |
| `customer-concentration` | `--company-id`, `--from-date`, `--to-date` | |

### Expense Analytics (3 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `expense-breakdown` | `--company-id`, `--from-date`, `--to-date` | `--group-by` |
| `cost-trend` | `--company-id`, `--from-date`, `--to-date` | `--periodicity`, `--account-id` |
| `opex-vs-capex` | `--company-id`, `--from-date`, `--to-date` | |

### Inventory Analytics (3 actions — require erpclaw-inventory)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `abc-analysis` | `--company-id` | `--as-of-date` |
| `inventory-turnover` | `--company-id`, `--from-date`, `--to-date` | `--item-id`, `--warehouse-id` |
| `aging-inventory` | `--company-id`, `--as-of-date` | `--aging-buckets` |

### HR Analytics (3 actions — require erpclaw-hr / erpclaw-payroll)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `headcount-analytics` | `--company-id` | `--as-of-date`, `--group-by` |
| `payroll-analytics` | `--company-id`, `--from-date`, `--to-date` | `--department-id` |
| `leave-utilization` | `--company-id` | `--from-date`, `--to-date` |

### Operations Analytics (3 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `project-profitability` | `--company-id` | `--project-id`, `--from-date`, `--to-date` |
| `quality-dashboard` | `--company-id` | `--from-date`, `--to-date` |
| `support-metrics` | `--company-id` | `--from-date`, `--to-date` |

### Dashboards & Trends (4 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `executive-dashboard` | `--company-id` | `--from-date`, `--to-date` |
| `company-scorecard` | `--company-id` | `--as-of-date` |
| `metric-trend` | `--company-id`, `--metric` | `--from-date`, `--to-date`, `--periodicity` |
| `period-comparison` | `--company-id`, `--periods` (JSON) | `--metrics` (JSON) |

### Quick Command Reference

| User Says | Action |
|-----------|--------|
| "show me KPIs" / "executive dashboard" | `executive-dashboard` |
| "company scorecard" / "grade the company" | `company-scorecard` |
| "current ratio" / "liquidity" | `liquidity-ratios` |
| "profit margin" / "ROA" / "ROE" | `profitability-ratios` |
| "DSO" / "DPO" / "efficiency" | `efficiency-ratios` |
| "revenue by customer" | `revenue-by-customer` |
| "revenue by item" / "top products" | `revenue-by-item` |
| "revenue trend" / "sales trend" | `revenue-trend` |
| "customer concentration" | `customer-concentration` |
| "expense breakdown" / "where is money going" | `expense-breakdown` |
| "cost trend" / "expense trend" | `cost-trend` |
| "opex vs capex" | `opex-vs-capex` |
| "ABC analysis" / "item classification" | `abc-analysis` |
| "inventory turnover" | `inventory-turnover` |
| "aging inventory" / "slow-moving stock" | `aging-inventory` |
| "headcount" / "employee analytics" | `headcount-analytics` |
| "payroll analytics" / "salary analysis" | `payroll-analytics` |
| "leave utilization" | `leave-utilization` |
| "project profitability" | `project-profitability` |
| "quality dashboard" / "defect rate" | `quality-dashboard` |
| "support metrics" / "ticket analytics" | `support-metrics` |
| "trend for revenue" / "track metric" | `metric-trend` |
| "compare periods" / "this quarter vs last" | `period-comparison` |
| "what analytics are available?" | `available-metrics` |
| "analytics status" / "which modules?" | `status` |
| "am I profitable?" / "are we making money?" | `profitability-ratios` |
| "can we pay our bills?" / "cash position" | `liquidity-ratios` |
| "how's business?" / "give me the big picture" | `executive-dashboard` |
| "where is the money going?" | `expense-breakdown` |
| "who are our top customers?" | `revenue-by-customer` |
| "what's trending up/down?" | `metric-trend` |

### Confirmation Requirements

No confirmations required. ALL 25 actions are read-only. Run immediately when requested.

**IMPORTANT:** NEVER query the database with raw SQL. ALWAYS use the `--action` flag.

### Graceful Degradation

When optional skills are missing, actions return `{"available": false, "reason": "..."}` or
skip the relevant section in dashboards. NEVER crash — always return valid JSON.

### Response Formatting

- Currency: `$X,XXX.XX` format, negatives in parentheses
- Ratios: 2 decimal places (e.g., 1.85, 0.42)
- Percentages: 1 decimal place with % sign (e.g., 42.3%)
- Use markdown tables for all tabular output
- Always include the period/date range in the response header

### Sub-Skills

| Sub-Skill | Shortcut | What It Does |
|-----------|----------|-------------|
| `erp-dashboard` | `/erp-dashboard` | Executive dashboard for the default company (current period) |
| `erp-kpis` | `/erp-kpis` | Lists available metrics and which modules are installed |
| `erp-scorecard` | `/erp-scorecard` | Company scorecard with letter grades per module |

## Technical Details (Tier 3)

**Tables owned (0):** None. 100% read-only.

**Hard requires:** erpclaw-setup (company), erpclaw-gl (account, gl_entry, fiscal_year, cost_center)
**Soft requires:** All other skills — checked at runtime via `erpclaw_lib.dependencies`

**Script:** `{baseDir}/scripts/db_query.py` — 25 actions.
**Data conventions:** TEXT amounts → Python Decimal, TEXT IDs = UUID4, parameterized SQL.
