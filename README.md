# ERPClaw — AI-Native ERP for OpenClaw

A complete ERP system built as an [OpenClaw](https://openclaw.org) skill. Full double-entry accounting, invoicing, inventory, purchasing, tax, billing, HR, payroll, and financial reporting — all in a single install. 413 actions across 14 domains.

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
- **Module system** — 43 additional modules (44 total including core) available via `install-module` from GitHub

## Quick Start

### Install via OpenClaw

```
clawhub install erpclaw
```

This installs the core ERP (413 actions) and initializes the database.

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

ERPClaw has 43 additional modules for specific industries and features:

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

ERPClaw organizes functionality into 14 domains — setup, general ledger, selling, buying, inventory, billing, tax, payments, journals, reports, HR, payroll, advanced accounting, and integrations. All domains share one local database with full referential integrity.

### Data Integrity

- Tamper-proof financial records — cancellations create auditable reverse entries
- Multi-step financial validation on every transaction
- Atomic writes ensure data consistency across all operations

## Database

Single local database with hundreds of tables across all modules. Built for data integrity — foreign key enforcement, concurrent read support, and a shared library for consistent behavior across all modules.

## Module Registry

The module registry (`scripts/module_registry.json`) tracks all 44 modules across 14 GitHub repositories. Use `install-module` to add any module:

```
"Install the manufacturing module"
"Add retail capabilities"
"I need dental practice management"
```

Modules install from `github.com/avansaber/*` repos via sparse checkout — only the requested module is downloaded, not the entire repo.

## Web Dashboard

Two web dashboard options are available:

### ERPClaw Web (Recommended)

[ERPClaw Web](https://github.com/avansaber/erpclaw-web) is a purpose-built dashboard for ERPClaw with live data tables, action execution, AI chat, and real-time WebSocket updates.

```bash
git clone https://github.com/avansaber/erpclaw-web.git
cd erpclaw-web && npm install && pip install -r api/requirements.txt
```

See [erpclaw-web README](https://github.com/avansaber/erpclaw-web#readme) for setup and deployment.

### WebClaw (Universal)

[WebClaw](https://github.com/avansaber/webclaw) is a universal OpenClaw dashboard that works with any skill:

```
clawhub install webclaw
```

WebClaw reads ERPClaw's SKILL.md and automatically generates forms, data tables, charts, and dashboards — zero per-skill configuration needed.

## ERPClaw OS -- Self-Extending ERP

ERPClaw OS is a self-extending ERP platform where AI generates, validates, and deploys new industry modules autonomously.

### Financial Integrity Rules

Built-in financial integrity rules automatically enforce accounting standards on every generated module. Modules that violate any rule are automatically rejected -- no human review needed for compliance.

### How It Works

AI generates complete modules from business descriptions, validated before deployment. Describe your industry, and ERPClaw OS produces a fully functional module with database schema, business actions, documentation, and tests -- all passing automated validation before going live.

### Current Status

- **Three evolution phases complete** -- Learns from business descriptions, validates against integrity rules, continuously improves.
- **Thousands of automated tests** across multiple validation layers.
- **Proof-of-concept modules generated and validated** -- indistinguishable from hand-written modules in architecture and test coverage.

## Links

- **Website**: [erpclaw.ai](https://www.erpclaw.ai)
- **ERPClaw Web**: [erpclaw-web](https://github.com/avansaber/erpclaw-web) — purpose-built web dashboard
- **WebClaw**: [webclaw](https://github.com/avansaber/webclaw) — universal OpenClaw dashboard
- **OpenClaw**: [openclaw.org](https://openclaw.org)
- **All modules**: [github.com/avansaber](https://github.com/avansaber)

## License

MIT License — Copyright (c) 2026 AvanSaber

See [LICENSE.txt](LICENSE.txt) for details.
