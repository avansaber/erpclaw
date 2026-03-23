---
name: erpclaw
version: 3.3.0
description: >
  AI-native ERP system with self-extending OS. Full accounting, invoicing, inventory, purchasing,
  tax, billing, HR, payroll, advanced accounting (ASC 606/842, intercompany, consolidation),
  and financial reporting. 467 actions across 14 domains, 43 expansion modules.
  Constitutional guardrails, adversarial audit, schema migration. Double-entry GL, immutable audit trail, US GAAP.
author: AvanSaber
homepage: https://github.com/avansaber/erpclaw
source: https://github.com/avansaber/erpclaw
user-invocable: true
tags: [erp, accounting, invoicing, inventory, purchasing, tax, billing, payments, gl, reports, sales, buying, setup, hr, payroll, employees, leave, attendance, salary, revenue-recognition, lease-accounting, intercompany, consolidation, stripe, payment-gateway]
metadata: {"openclaw":{"type":"executable","install":{"post":"python3 scripts/erpclaw-setup/db_query.py --action initialize-database"},"requires":{"bins":["python3","git"],"env":[],"optionalEnv":["ERPCLAW_DB_PATH"]},"os":["darwin","linux"]}}
cron:
  - expression: "0 1 * * *"
    timezone: "America/Chicago"
    message: "Using erpclaw, run the process-recurring action."
    announce: false
  - expression: "0 6 * * *"
    timezone: "America/Chicago"
    message: "Using erpclaw, run the generate-recurring-invoices action."
    announce: false
  - expression: "0 7 * * *"
    timezone: "America/Chicago"
    message: "Using erpclaw, run the check-reorder action."
    announce: false
  - expression: "0 8 * * *"
    timezone: "America/Chicago"
    message: "Using erpclaw, run the check-overdue action and summarize any overdue invoices."
    announce: false
---

# erpclaw

**Full-Stack ERP Controller** for ERPClaw. Handles company setup, chart of accounts, journal entries,
payments, tax, financial reports, customers, sales, suppliers, purchasing, inventory, billing,
HR (employees, leave, attendance, expenses), US payroll (FICA, W-2, garnishments), advanced accounting
(ASC 606/842, intercompany, consolidation), and 43 industry modules. Single local SQLite DB, double-entry GL, immutable audit trail.

**Security:** Local-first (`~/.openclaw/erpclaw/data.sqlite`). Parameterized queries. RBAC (PBKDF2). Immutable GL. Network only for `fetch-exchange-rates` (public API) and `install-module` (GitHub `avansaber/*`).

### Skill Activation Triggers

Activate when user mentions: ERP, accounting, invoice, sales order, purchase order, customer, supplier, inventory, payment, GL, trial balance, P&L, balance sheet, tax, billing, modules, install module, onboard, CRM, manufacturing, healthcare, education, retail, employee, HR, payroll, salary, leave, attendance, expense claim, W-2, garnishment, Stripe, payment gateway, integration, connect, sync.

### Auto-Detection

When a user describes their business: detect type (e.g., "dental practice" -> dental), confirm with user, call `setup-company --industry <type>`, then `list-all-actions`. Industry values: retail, restaurant, healthcare, dental, veterinary, construction, manufacturing, legal, agriculture, hospitality, property, school, university, nonprofit, automotive, therapy, home-health, consulting, distribution, saas, stripe. When a user asks about a service or integration not currently installed, search the module registry for matching modules and suggest installation.

### Setup
```
python3 {baseDir}/scripts/erpclaw-setup/db_query.py --action initialize-database
python3 {baseDir}/scripts/db_query.py --action seed-defaults --company-id <id>
python3 {baseDir}/scripts/db_query.py --action setup-chart-of-accounts --company-id <id> --template us_gaap
```

## All 467 Actions

### Setup & Admin (44)
| Action | Description |
|--------|-------------|
| `initialize-database` / `setup-company` / `update-company` / `get-company` / `list-companies` | DB init & company CRUD |
| `add-currency` / `list-currencies` / `add-exchange-rate` / `get-exchange-rate` / `list-exchange-rates` / `fetch-exchange-rates` | Currency & FX |
| `add-payment-terms` / `list-payment-terms` / `add-uom` / `list-uoms` / `add-uom-conversion` | Terms & UoMs |
| `seed-defaults` / `seed-demo-data` / `check-installation` / `install-guide` / `setup-web-dashboard` / `tutorial` / `onboarding-step` / `status` | Seeding & utilities |
| `add-user` / `update-user` / `get-user` / `list-users` / `set-password` | User management |
| `add-role` / `list-roles` / `assign-role` / `revoke-role` / `seed-permissions` | RBAC & security |
| `link-telegram-user` / `unlink-telegram-user` / `check-telegram-permission` | Telegram integration |
| `backup-database` / `list-backups` / `verify-backup` / `restore-database` / `cleanup-backups` | DB backup/restore |
| `get-audit-log` / `get-schema-version` / `update-regional-settings` / `onboard` | System admin |

### General Ledger (26)
| Action | Description |
|--------|-------------|
| `setup-chart-of-accounts` / `add-account` / `update-account` / `get-account` / `list-accounts` | Account CRUD |
| `freeze-account` / `unfreeze-account` / `get-account-balance` / `check-gl-integrity` | Account management |
| `post-gl-entries` / `reverse-gl-entries` / `list-gl-entries` | GL posting |
| `add-fiscal-year` / `list-fiscal-years` / `validate-period-close` / `close-fiscal-year` / `reopen-fiscal-year` | Fiscal year |
| `add-cost-center` / `list-cost-centers` / `add-budget` / `list-budgets` | Cost centers & budgets |
| `seed-naming-series` / `next-series` / `revalue-foreign-balances` | Naming & FX revaluation |
| `import-chart-of-accounts` / `import-opening-balances` | CSV import |

### Journal Entries (16)
| Action | Description |
|--------|-------------|
| `add-journal-entry` / `update-journal-entry` / `get-journal-entry` / `list-journal-entries` | JE CRUD |
| `submit-journal-entry` / `cancel-journal-entry` / `amend-journal-entry` / `delete-journal-entry` / `duplicate-journal-entry` | JE lifecycle |
| `create-intercompany-je` | Intercompany JE |
| `add-recurring-template` / `update-recurring-template` / `list-recurring-templates` / `get-recurring-template` / `process-recurring` / `delete-recurring-template` | Recurring JEs |

### Payments (13)
| Action | Description |
|--------|-------------|
| `add-payment` / `update-payment` / `get-payment` / `list-payments` / `submit-payment` / `cancel-payment` / `delete-payment` | Payment CRUD & lifecycle |
| `create-payment-ledger-entry` / `get-outstanding` / `get-unallocated-payments` / `allocate-payment` / `reconcile-payments` / `bank-reconciliation` | Reconciliation |

### Tax (17)
| Action | Description |
|--------|-------------|
| `add-tax-template` / `update-tax-template` / `get-tax-template` / `list-tax-templates` / `delete-tax-template` | Tax template CRUD |
| `resolve-tax-template` / `calculate-tax` / `add-tax-category` / `list-tax-categories` / `add-tax-rule` / `list-tax-rules` | Tax rules |
| `add-item-tax-template` / `add-tax-withholding-category` / `get-withholding-details` | Withholding |
| `record-withholding-entry` / `record-1099-payment` / `generate-1099-data` | 1099 reporting |

### Financial Reports (20)
| Action | Description |
|--------|-------------|
| `trial-balance` / `profit-and-loss` / `balance-sheet` / `cash-flow` / `general-ledger` / `party-ledger` | Core statements |
| `ar-aging` / `ap-aging` / `budget-vs-actual` (alias: `budget-variance`) | Aging & budget |
| `tax-summary` / `payment-summary` / `gl-summary` / `comparative-pl` / `check-overdue` | Summaries |
| `add-elimination-rule` / `list-elimination-rules` / `run-elimination` / `list-elimination-entries` | Intercompany |

### Selling (48)
| Action | Description |
|--------|-------------|
| `add-customer` / `update-customer` / `get-customer` / `list-customers` / `import-customers` | Customer CRUD |
| `add-quotation` / `update-quotation` / `get-quotation` / `list-quotations` / `submit-quotation` / `convert-quotation-to-so` | Quotations |
| `add-sales-order` / `update-sales-order` / `get-sales-order` / `list-sales-orders` / `submit-sales-order` / `cancel-sales-order` / `amend-sales-order` / `close-sales-order` | Sales orders |
| `add-blanket-order` / `get-blanket-order` / `list-blanket-orders` / `submit-blanket-order` / `create-so-from-blanket` | Blanket orders |
| `create-delivery-note` / `get-delivery-note` / `list-delivery-notes` / `submit-delivery-note` / `cancel-delivery-note` / `add-packing-slip` / `get-packing-slip` / `list-packing-slips` | Delivery & packing |
| `create-sales-invoice` / `update-sales-invoice` / `get-sales-invoice` / `list-sales-invoices` / `submit-sales-invoice` / `cancel-sales-invoice` | Invoicing |
| `create-credit-note` / `list-credit-notes` / `update-invoice-outstanding` | Credit notes |
| `add-sales-partner` / `list-sales-partners` | Sales partners |
| `add-recurring-template` / `update-recurring-template` / `list-recurring-templates` / `generate-recurring-invoices` | Recurring invoices |
| `add-intercompany-account-map` / `list-intercompany-account-maps` / `create-intercompany-invoice` / `list-intercompany-invoices` / `cancel-intercompany-invoice` | Intercompany |

### Buying (40)
| Action | Description |
|--------|-------------|
| `add-supplier` / `update-supplier` / `get-supplier` / `list-suppliers` / `import-suppliers` | Supplier CRUD |
| `add-material-request` / `submit-material-request` / `list-material-requests` | Material requests |
| `add-rfq` / `submit-rfq` / `list-rfqs` / `add-supplier-quotation` / `list-supplier-quotations` / `compare-supplier-quotations` | RFQs & quotes |
| `add-purchase-order` / `update-purchase-order` / `get-purchase-order` / `list-purchase-orders` / `submit-purchase-order` / `cancel-purchase-order` / `close-purchase-order` | Purchase orders |
| `add-blanket-po` / `get-blanket-po` / `list-blanket-pos` / `submit-blanket-po` / `create-po-from-blanket` / `create-po-from-so` / `create-drop-ship-order` | Blanket POs & drop ship |
| `create-purchase-receipt` / `get-purchase-receipt` / `list-purchase-receipts` / `submit-purchase-receipt` / `cancel-purchase-receipt` | Receipts |
| `create-purchase-invoice` / `update-purchase-invoice` / `get-purchase-invoice` / `list-purchase-invoices` / `submit-purchase-invoice` / `cancel-purchase-invoice` | Purchase invoices |
| `create-debit-note` / `add-landed-cost-voucher` / `update-receipt-tolerance` / `update-three-way-match-policy` | Adjustments |

### Inventory (42)
| Action | Description |
|--------|-------------|
| `add-item` / `update-item` / `get-item` / `list-items` / `import-items` / `add-item-group` / `list-item-groups` | Item master |
| `add-item-attribute` / `create-item-variant` / `generate-item-variants` / `list-item-variants` | Item variants |
| `add-item-supplier` / `list-item-suppliers` / `set-item-purchase-uom` | Item suppliers |
| `add-warehouse` / `update-warehouse` / `list-warehouses` | Warehouses |
| `add-stock-entry` / `get-stock-entry` / `list-stock-entries` / `submit-stock-entry` / `cancel-stock-entry` | Stock entries |
| `create-stock-ledger-entries` / `reverse-stock-ledger-entries` | Stock ledger |
| `get-stock-balance` / `stock-balance` / `stock-balance-report` / `stock-ledger-report` / `get-projected-qty` | Stock reports |
| `add-batch` / `list-batches` / `add-serial-number` / `list-serial-numbers` | Batch & serial |
| `add-price-list` / `add-item-price` / `get-item-price` / `add-pricing-rule` | Pricing |
| `add-stock-reconciliation` / `submit-stock-reconciliation` | Reconciliation |
| `revalue-stock` / `list-stock-revaluations` / `get-stock-revaluation` / `cancel-stock-revaluation` / `check-reorder` | Revaluation & reorder |

### Billing & Metering (23)
| Action | Description |
|--------|-------------|
| `add-meter` / `update-meter` / `get-meter` / `list-meters` / `add-meter-reading` / `list-meter-readings` | Meters |
| `add-usage-event` / `add-usage-events-batch` | Usage tracking |
| `add-rate-plan` / `update-rate-plan` / `get-rate-plan` / `list-rate-plans` / `rate-consumption` | Rate plans |
| `create-billing-period` / `run-billing` / `generate-invoices` / `get-billing-period` / `list-billing-periods` | Billing cycles |
| `add-billing-adjustment` / `add-prepaid-credit` / `get-prepaid-balance` | Adjustments & prepaid |
| `add-recurring-bill-template` / `list-recurring-bill-templates` / `generate-recurring-bills` | Recurring bills |

### Advanced Accounting (45)
| Action | Description |
|--------|-------------|
| `add-revenue-contract` / `update-revenue-contract` / `get-revenue-contract` / `list-revenue-contracts` | Revenue contracts |
| `add-performance-obligation` / `list-performance-obligations` / `satisfy-performance-obligation` | ASC 606 |
| `add-variable-consideration` / `list-variable-considerations` / `modify-contract` | Variable consideration |
| `calculate-revenue-schedule` / `generate-revenue-entries` / `revenue-waterfall-report` / `revenue-recognition-summary` | Revenue recognition |
| `recognize-schedule-entry` / `update-performance-obligation` / `update-schedule-amounts` | Revenue schedule management |
| `add-lease` / `update-lease` / `get-lease` / `list-leases` / `classify-lease` | ASC 842 leases |
| `calculate-rou-asset` / `calculate-lease-liability` / `generate-amortization-schedule` / `record-lease-payment` | Lease calculations |
| `lease-maturity-report` / `lease-disclosure-report` / `lease-summary` | Lease reports |
| `add-ic-transaction` / `update-ic-transaction` / `get-ic-transaction` / `list-ic-transactions` | Intercompany |
| `approve-ic-transaction` / `post-ic-transaction` / `add-transfer-price-rule` / `list-transfer-price-rules` | IC approvals |
| `ic-reconciliation-report` / `ic-elimination-report` | IC reports |
| `add-consolidation-group` / `list-consolidation-groups` / `add-group-entity` / `add-currency-translation` | Consolidation setup |
| `run-consolidation` / `generate-elimination-entries` / `consolidation-trial-balance-report` / `consolidation-summary` | Consolidation |
| `standards-compliance-dashboard` | ASC 606/842 compliance |

### HR & Payroll (58)
| Action | Description |
|--------|-------------|
| `add-employee` / `update-employee` / `get-employee` / `list-employees` / `record-lifecycle-event` | Employee CRUD |
| `add-employee-bank-account` / `list-employee-bank-accounts` / `add-employee-document` / `get-employee-document` / `list-employee-documents` / `check-expiring-documents` | Employee details |
| `add-department` / `list-departments` / `add-designation` / `list-designations` | Org structure |
| `add-leave-type` / `list-leave-types` / `add-leave-allocation` / `get-leave-balance` | Leave config |
| `add-leave-application` / `approve-leave` / `reject-leave` / `list-leave-applications` | Leave requests |
| `mark-attendance` / `bulk-mark-attendance` / `list-attendance` / `add-holiday-list` | Attendance |
| `add-shift-type` / `update-shift-type` / `list-shift-types` / `assign-shift` / `list-shift-assignments` | Shift management |
| `add-regularization-rule` / `apply-attendance-regularization` | Attendance regularization |
| `add-expense-claim` / `submit-expense-claim` / `approve-expense-claim` / `reject-expense-claim` / `update-expense-claim-status` / `list-expense-claims` | Expenses |
| `add-salary-component` / `list-salary-components` / `add-salary-structure` / `get-salary-structure` / `list-salary-structures` | Salary config |
| `add-salary-assignment` / `list-salary-assignments` / `add-income-tax-slab` / `add-state-tax-slab` / `update-employee-state-config` | Payroll config |
| `update-fica-config` / `update-futa-suta-config` / `add-overtime-policy` / `calculate-overtime` / `calculate-retro-pay` | Tax & overtime |
| `create-payroll-run` / `generate-salary-slips` / `get-salary-slip` / `list-salary-slips` / `submit-payroll-run` / `cancel-payroll-run` | Payroll processing |
| `generate-w2-data` / `generate-nacha-file` / `add-garnishment` / `update-garnishment` / `get-garnishment` / `list-garnishments` | W-2, NACHA, garnishments |
| `get-amendment-history` | Amendment tracking |

### Module Management & OS (41)
| Action | Description |
|--------|-------------|
| `install-module` / `remove-module` / `update-modules` / `list-modules` / `available-modules` / `search-modules` / `module-status` | Module catalog |
| `rebuild-action-cache` / `list-all-actions` / `list-profiles` / `onboard` / `list-industries` | Actions & profiles |
| `validate-module` / `generate-module` / `configure-module` / `deploy-module` | OS: Module lifecycle |
| `build-table-registry` / `list-articles` / `install-suite` / `classify-operation` / `run-audit` / `compliance-weather-status` | OS: Constitution & audit |
| `schema-plan` / `schema-apply` / `schema-rollback` / `schema-drift` / `deploy-audit-log` | OS: Schema migration |
| `semantic-check` / `semantic-rules-list` | OS: Semantic correctness |
| `log-improvement` / `list-improvements` / `review-improvement` | OS: Self-improvement |
| `dgm-run-variant` / `dgm-list-variants` / `dgm-select-best` | OS: DGM variant engine |
| `heartbeat-analyze` / `heartbeat-report` / `heartbeat-suggest` / `detect-gaps` / `suggest-modules` | OS: Gap detection |
| `regenerate-skill-md` | OS: Regenerate SKILL.md |

**Confirm before running:** `setup-company`, `onboard`, `install-module`, `remove-module`, `update-modules`, `submit-*`, `cancel-*`, `approve-*`, `reject-*`, `run-elimination`, `run-consolidation`, `restore-database`, `close-fiscal-year`, `initialize-database --force`.

## Technical Details (Tier 3)
Router: `scripts/db_query.py` -> 14 core domains + erpclaw-os. Modules from GitHub to `~/.openclaw/erpclaw/modules/`. Single SQLite DB (WAL). 188 core tables (688 with modules). Money=TEXT(Decimal), IDs=TEXT(UUID4), GL immutable. Python 3.10+.
