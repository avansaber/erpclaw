# ERPClaw

AI-native ERP built as a modular skill suite for [OpenClaw](https://openclaw.com). 26 skills, 609 actions, single shared SQLite database. US-focused for v1, with regional modules for Canada, UK, EU, and India.

**Author:** Nikhil Jathar / [AvanSaber Inc.](https://www.avansaber.com)
**Website:** [erpclaw.ai](https://www.erpclaw.ai)
**Web UI:** [Webclaw](https://github.com/avansaber/webclaw) (separate repo)
**License:** MIT

## Architecture

```
┌────────────────────────────────────────────────┐
│              OpenClaw Gateway                   │
│         (Telegram / CLI / API)                  │
├────────┬──────────┬──────────┬────────┬────────┤
│  GL    │ Selling  │ Buying   │   HR   │  ...   │
│  Tax   │ Invoices │ Receipts │Payroll │  26    │
│Reports │ Delivery │ PO/PR    │Projects│ skills │
├────────┴──────────┴──────────┴────────┴────────┤
│           Shared Library (erpclaw_lib)          │
│  GL posting · stock posting · tax calc · RBAC  │
├────────────────────────────────────────────────┤
│        Single SQLite Database (WAL mode)       │
│    ~/.openclaw/erpclaw/data.sqlite             │
│    183 tables · 498 indexes · FK enforced      │
└────────────────────────────────────────────────┘
```

- **Single database:** All skills read/write the same SQLite file. WAL mode, FK enforcement, `busy_timeout = 5000`.
- **Financial precision:** All money stored as TEXT, computed with Python `Decimal`. No floats.
- **Immutable GL:** `gl_entry` and `stock_ledger_entry` have no `updated_at`. Cancel = reverse (insert mirror entries).
- **Draft-Submit lifecycle:** `add-*`/`create-*` produces a draft. `submit-*` validates and writes GL/SLE in a single transaction.
- **Inter-skill isolation:** Each skill owns its tables (write). Any skill can read any table.

## Skills

| Skill | Description |
|-------|-------------|
| **erpclaw** | Meta-package: install checker, onboarding, seed demo data |
| **erpclaw-setup** | Company setup, users, RBAC, shared library, chart of accounts |
| **erpclaw-gl** | General Ledger, chart of accounts, period closing |
| **erpclaw-journals** | Journal entries with draft-submit-cancel lifecycle |
| **erpclaw-tax** | Tax templates, rules, calculation, withholding, 1099 |
| **erpclaw-reports** | Trial balance, P&L, balance sheet, cash flow, aging, budgets |
| **erpclaw-inventory** | Items, warehouses, stock entries, batches, serial numbers, pricing |
| **erpclaw-selling** | Customers, quotations, sales orders, delivery notes, invoices, credit notes |
| **erpclaw-buying** | Suppliers, purchase orders, receipts, invoices, debit notes, landed costs |
| **erpclaw-manufacturing** | BOMs, work orders, job cards, MRP, subcontracting |
| **erpclaw-hr** | Employees, departments, leave, attendance, expenses |
| **erpclaw-payroll** | Salary structures, payroll processing, FICA, W-2 |
| **erpclaw-projects** | Projects, tasks, milestones, timesheets |
| **erpclaw-assets** | Fixed assets, depreciation, disposal |
| **erpclaw-quality** | Inspections, non-conformance, quality goals |
| **erpclaw-crm** | Leads, opportunities, campaigns, activities |
| **erpclaw-support** | Issues, SLAs, warranty, maintenance |
| **erpclaw-billing** | Usage-based billing, meters, rate plans, prepaid credits |
| **erpclaw-payments** | Payment entries, allocation, bank reconciliation |
| **erpclaw-ai-engine** | Anomaly detection, forecasting, scoring, business rules |
| **erpclaw-analytics** | KPIs, ratios, trends, dashboards |
| **erpclaw-integrations** | Plaid bank sync, Stripe payments, S3 backups |
| **erpclaw-region-ca** | Canada: GST/HST/PST, CPP/EI, T4, Canadian CoA |
| **erpclaw-region-in** | India: GST, e-invoicing, GSTR-1/3B, TDS, Ind-AS CoA |
| **erpclaw-region-uk** | UK: VAT, PAYE, NI, CIS, FRS 102 CoA |
| **erpclaw-region-eu** | EU: VAT (27 states), reverse charge, SAF-T, e-invoicing |

## Quick Start

### Install from ClawHub

```bash
# Install the meta-package (checks dependencies, seeds shared library)
clawhub install erpclaw

# Install individual skills
clawhub install erpclaw-setup
clawhub install erpclaw-gl
clawhub install erpclaw-selling
# ... etc.
```

### Install from Source

```bash
git clone https://github.com/avansaber/erpclaw.git
cd erpclaw

# Copy a skill to OpenClaw's skill directory
cp -r skills/erpclaw-setup ~/clawd/skills/erpclaw-setup

# Initialize the database
python3 skills/erpclaw-setup/scripts/init_db.py
```

### Web Dashboard

For a browser-based UI, install [Webclaw](https://github.com/avansaber/webclaw) separately:

```bash
clawhub install webclaw
```

Webclaw provides forms, tables, charts, and AI chat for every installed ERPClaw skill with zero per-skill configuration.

## Repo Structure

```
erpclaw/
├── init_db.py              # Master schema DDL (183 tables)
├── skills/
│   ├── erpclaw/             # Meta-package
│   ├── erpclaw-setup/       # Foundation (includes shared lib)
│   ├── erpclaw-gl/          # General Ledger
│   ├── ...                  # 23 more skills
│   └── erpclaw-region-eu/   # EU compliance
└── tests/
    └── integration/         # Cross-skill integration tests
```

Each skill follows the same structure:
```
erpclaw-{name}/
├── SKILL.md          # Skill definition (YAML frontmatter, max 300 lines)
├── scripts/
│   └── db_query.py   # All actions (routed via --action flag)
├── tests/
│   └── test_*.py     # pytest tests
├── assets/           # Seed data (JSON)
└── references/       # Domain documentation
```

## Technical Details

- **Python 3.10+** (match Ubuntu 24.04 server)
- **SQLite** with WAL mode, foreign keys ON, parameterized queries only
- **Action naming:** kebab-case (`add-customer`, `submit-sales-order`, `cancel-invoice`)
- **Output:** JSON to stdout (OpenClaw reads this)
- **Shared library:** `~/.openclaw/erpclaw/lib/erpclaw_lib/` — GL posting, stock posting, tax calculation, naming, RBAC, crypto, CSV import

## Links

- [erpclaw.ai](https://www.erpclaw.ai) — Product website
- [avansaber.com](https://www.avansaber.com) — Company website
- [nikhilj.com](https://www.nikhilj.com) — Author
- [Webclaw](https://github.com/avansaber/webclaw) — Web dashboard (separate repo)
- [ClawHub](https://clawhub.ai) — Skill marketplace

## License

MIT License. Copyright 2026 AvanSaber Inc. See [LICENSE.txt](LICENSE.txt).
