# ERPClaw — AI-Native ERP for OpenClaw

A complete ERP system built as an [OpenClaw](https://openclaw.org) skill. Full double-entry accounting, invoicing, inventory, purchasing, tax, billing, HR, payroll, and financial reporting — all in a single install. 365+ actions across 14 domains.

## Features

- **Double-entry GL** — US GAAP chart of accounts, immutable journal entries, multi-company support
- **Sales** — customers, sales orders, delivery notes, sales invoices, credit notes, payment tracking
- **Buying** — suppliers, purchase orders, purchase invoices, goods received notes
- **Inventory** — items, warehouses, stock entries, serial/batch tracking, reorder levels
- **Billing** — usage-based billing, recurring invoices, subscription management
- **Tax** — tax templates, multi-rate support, tax returns
- **Payments** — payment entries, bank reconciliation, multi-currency
- **HR** — employees, departments, designations, leave management, attendance, expenses
- **Payroll** — salary structures, FICA, federal/state income tax, W-2 generation, garnishments
- **Advanced Accounting** — ASC 606 revenue recognition, ASC 842 lease accounting, intercompany transactions, consolidation
- **Reports** — trial balance, P&L, balance sheet, cash flow, AR/AP aging, inventory valuation
- **Module system** — 44 additional modules available via `install-module` from GitHub

## Quick Start

### Install via OpenClaw

```
clawhub install erpclaw
```

This installs the core ERP (365+ actions) and initializes the database.

### First Steps

Once installed, just talk to your AI assistant naturally:

```
"I'm opening a retail store called Sunrise Goods in Portland, Oregon. Set me up."
```

The bot will:
1. Create your company with US GAAP chart of accounts (94 accounts)
2. Set up fiscal year, tax rates, and cost center
3. Suggest relevant modules for your industry

### Adding Modules

ERPClaw has 44 additional modules for specific industries and features:

```
"I need manufacturing capabilities"
→ Installs erpclaw-ops (Manufacturing, Projects, Assets, Quality, Support)

"I need CRM"
→ Installs erpclaw-growth (CRM, Analytics, AI Engine)

"Set me up for healthcare"
→ Installs HealthClaw (140+ actions for clinical practice management)
```

Available modules:
- **Addon modules** (16): CRM, Manufacturing, Projects, Assets, Quality, Fleet, POS, Logistics, and more
- **Healthcare** (5): Core clinical + Dental, Veterinary, Mental Health, Home Health
- **Education** (8): Core SIS + Financial Aid, K-12, Scheduling, LMS, State Reporting, Higher Ed, SPED
- **Property** (2): Residential + Commercial property management
- **Industry verticals** (8): Retail, Construction, Agriculture, Automotive, Food, Hospitality, Legal, Nonprofit
- **Regional** (4): Canada, UK, India, EU (tax rules, COA templates, compliance)

## Architecture

```
OpenClaw Bot → erpclaw/scripts/db_query.py --action {action} --args
                         │
                         ├── erpclaw-setup     → Company, COA, fiscal year, database init
                         ├── erpclaw-gl        → Chart of accounts, journal entries
                         ├── erpclaw-selling   → Customers, sales orders, invoices
                         ├── erpclaw-buying    → Suppliers, purchase orders
                         ├── erpclaw-inventory → Items, warehouses, stock
                         ├── erpclaw-billing   → Recurring invoices, subscriptions
                         ├── erpclaw-tax       → Tax templates, calculations
                         ├── erpclaw-payments  → Payment entries, reconciliation
                         ├── erpclaw-journals  → Manual journal entries
                         ├── erpclaw-reports   → Financial statements
                         ├── erpclaw-hr        → Employees, leave, attendance
                         ├── erpclaw-payroll   → Salary, tax withholding, W-2
                         └── erpclaw-accounting-adv → ASC 606/842, intercompany
                         │
                         ▼
              SQLite (~/.openclaw/erpclaw/data.sqlite)
              WAL mode, FK enforcement, parameterized queries
```

### Data Integrity

- All financial amounts stored as TEXT (Python `Decimal`) — never float
- IDs are UUID4 (TEXT)
- GL entries are immutable — cancellation creates reverse entries
- All cross-table writes in single SQLite transactions
- 12-step GL validation on every posting

## Database

Single SQLite database at `~/.openclaw/erpclaw/data.sqlite`:

- **685+ tables** across all modules
- WAL mode for concurrent reads
- Foreign key enforcement ON
- `PRAGMA busy_timeout = 5000`
- Shared library at `~/.openclaw/erpclaw/lib/erpclaw_lib/`

## Module Registry

The module registry (`scripts/module_registry.json`) tracks all 45 modules across 14 GitHub repositories. Use `install-module` to add any module:

```
"Install the manufacturing module"
"Add retail capabilities"
"I need dental practice management"
```

Modules install from `github.com/avansaber/*` repos via sparse checkout — only the requested module is downloaded, not the entire repo.

## Web Dashboard

Install [WebClaw](https://github.com/avansaber/webclaw) for a browser-based UI:

```
clawhub install webclaw
```

WebClaw reads ERPClaw's SKILL.md and automatically generates forms, data tables, charts, and dashboards — zero per-skill configuration needed.

## Links

- **Website**: [erpclaw.ai](https://www.erpclaw.ai)
- **Web Dashboard**: [WebClaw](https://github.com/avansaber/webclaw)
- **OpenClaw**: [openclaw.org](https://openclaw.org)
- **All modules**: [github.com/avansaber](https://github.com/avansaber)

## License

MIT License — Copyright (c) 2026 AvanSaber

See [LICENSE.txt](LICENSE.txt) for details.
