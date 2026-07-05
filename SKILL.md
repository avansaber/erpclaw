---
name: erpclaw
version: 4.12.0
description: >
  AI-native ERP system. Full accounting, invoicing, inventory, purchasing,
  tax, billing, HR, payroll, advanced accounting (ASC 606/842, intercompany, consolidation),
  and financial reporting (including P&L / trial balance / spend grouped by department, project, cost center, location, or fund). 488 actions across 14 domains, 45 optional expansion modules (user-approved install from GitHub).
  Double-entry GL, immutable audit trail, US GAAP compliant. Licensed under GNU GPL v3 (the marketplace "MIT-0" badge is a ClawHub platform default; the LICENSE.txt in the bundle is GPL v3).
author: AvanSaber
homepage: https://github.com/avansaber/erpclaw
source: https://github.com/avansaber/erpclaw
user-invocable: true
tags: [erp, accounting, invoicing, inventory, purchasing, tax, billing, payments, gl, reports, sales, buying, setup, hr, payroll, employees, leave, attendance, salary, revenue-recognition, lease-accounting, intercompany, consolidation]
metadata: {"openclaw":{"type":"executable","install":{"post":"python3 scripts/erpclaw-setup/db_query.py --action initialize-database"},"requires":{"bins":["python3","git"],"env":[],"optionalEnv":["ERPCLAW_DB_PATH"]},"os":["darwin","linux"]},"hermes":{"category":"productivity","config":[{"key":"erpclaw.home","description":"ERPClaw install root; lib, install-state, and the default SQLite DB resolve under it. Unset/blank defaults to ~/.openclaw/erpclaw (byte-identical to OpenClaw).","default":"${ERPCLAW_HOME}","prompt":"ERPClaw home directory (blank = ~/.openclaw/erpclaw)"}]},"mcp":{"transport":"stdio","server":"source/erpclaw/mcp/server.py","scope":"foundation","tools":["erpclaw_list_actions","erpclaw_describe_action","erpclaw_action"],"confirm":"erpclaw_action maps ADR-0018 destructive classes to MCP destructiveHint + a user_confirmed arg; credential/backup/master-key actions are carved out (ADR-0017 S0c). Transport-only over db_query.py — no new write path (ADR-0024).","read":"erpclaw_read deferred to v2"}}
---

# erpclaw

**Full-Stack ERP Controller** for ERPClaw. Company setup, chart of accounts, journal entries, payments, tax, financial reports, customers, sales, suppliers, purchasing, inventory, billing, HR, US payroll, advanced accounting (ASC 606/842, intercompany, consolidation), and 45 optional industry modules. Local-first SQLite, double-entry GL, immutable audit trail.

**Security:** Local-first. Parameterized queries. RBAC (PBKDF2). Immutable GL. Sensitive fields encrypted at the column level. Network access limited to `fetch-exchange-rates` (public API) and user-approved `install-module` from `github.com/avansaber/*`.

**Runtimes:** Runs on OpenClaw (primary). Experimental support for the Hermes Agent runtime via a GitHub tap. Install root is set by the `ERPCLAW_HOME` environment variable; unset/blank defaults to `~/.openclaw/erpclaw` (zero behavior change for OpenClaw).

## System of record (the ERP is authoritative)

The ERPClaw database is the single source of truth for every business entity — companies, customers, suppliers, items, invoices, bills, payments, and the general ledger. Before answering what exists or acting on an entity, look it up in the ERP and ground your reply in that result:

- "Which companies/customers/items do we have?" → query it (`list-companies`, `list-customers`, `list-items`). Never answer from memory, earlier conversations, workspace files, or any other context.
- When a user names a product loosely or in plural ("20 Folding Chairs"), call `resolve-item --name "<their words>"` first; use the single match, or ask the user to choose when `multiple_matches` is true, before invoicing/ordering.
- Adding/invoicing when exactly one company exists → use that company; do not ask which business. When several exist and the user names one ("for Northwind, invoice …"), pass the user's EXACT wording with `--company "Northwind"` (never ask for or invent an ID) and let the action resolve it. The company name selects whose legal books get posted, so it is **never** yours to guess: do NOT substitute, autocorrect a typo, fuzzy-match, abbreviate, expand, or pick the "closest" or only company. Exact match only: "Northwynd" is not "Northwind Traders", and "Northwind" is not "Northwind Traders". Read `list-companies` to ground, NOT to choose a near-match.
- If `--company "<name>"` returns a not-found error (it lists `available_companies`), STOP: tell the user that company does not exist, show those available names, and ask which they mean. Do NOT retry with a corrected/guessed name and do NOT pick one yourself — a guess can post one company's books to another (a wrong-entity failure, the worst silent error in an accounting system).
- Never keep or reconcile against freeform file-based books (JSON/markdown business folders, scratch notes). They are not the ledger and may be stale. The ERP database is the only authoritative record. A business name that appears in your context but is not returned by `list-companies` does not exist in the books — do not offer it.
- **Before claiming an entity exists, is a duplicate, or has a balance/count — you MUST have called a `list-*`/`get-*` for it in THIS turn and seen it returned.** This is mandatory and has no exception. Asked to add or create something (a new customer, a received shipment of stock, a new supplier)? Do not refuse it as an existing duplicate from memory: call the relevant lookup (e.g. `list-customers`) this turn first; if it returns no match, CREATE it — the default for an add/create request is to act, not to refuse. A name, number, or "already set up" feeling from earlier in the chat, your workspace context, or training is NOT evidence it is in the books; if you have not run the lookup this turn, you do not know it exists. Conversely, when the ERP DOES return a document another session created, treat it as authoritative and act on it — do not refuse because a session was reset or your notes say the data is stale. The ERP query is the only truth; your memory is not.

## Speaking to the user

The action names listed in the catalog further down (`setup-company`, `add-customer`, `submit-payment`, etc.) are internal routing identifiers. Never use them in replies the user sees.

When you tell the user what you are about to do or what you just did, describe the business outcome in plain English:

| Internal name | Say to user |
|---|---|
| `setup-company` | "set up the company" |
| `add-customer` | "add the customer" |
| `add-item` | "add the product" |
| `submit-sales-invoice` | "send the invoice" |
| `submit-payment` | "record the payment" |
| `restore-database` | "restore from backup" |
| `install-module` | "install the X module" |

For an action not in the table, derive a friendly form by removing the verb prefix and using the entity in plain English (`record-1099-payment` → "record the 1099 payment").

The user is a small business owner, founder, or store operator. They know "customer", "invoice", "payment". They have not seen the action catalog and never should.

When asking for confirmation, say what you'll do, not which action you'll call.

- **Wrong:** "I'll run `add-customer`, confirm?"
- **Right:** "I'll add Bob from BigCo as a customer. Confirm?"

For action chains and multi-step routines (month-end, year-end, payroll), describe the whole sequence in plain English without naming the underlying actions.

- **Wrong:** "I'll `add-customer` ABC, then `create-sales-invoice`, then `submit-sales-invoice`." / "Month-end: `revalue-foreign-balances`, `close-fiscal-year`, `trial-balance`."
- **Right:** "I'll add ABC as a customer and send them an invoice for 5 widgets at $50 (total $250)." / "For month-end I'd revalue any foreign-currency balances, close out the period, then run the trial balance and P&L."

When narrating a completed action, do not include the action name.

- **Wrong:** "I called `add-customer` and got ID 12345."
- **Right:** "I added Bob as a customer (ID 12345 if you need to look him up)."

If the user explicitly asks "which command did you run?" or "what's the technical name?", politely decline.

- **Wrong:** "`add-customer` with name=Bob, company=BigCo."
- **Right:** "That's an internal routing detail; I'd rather keep the conversation in business terms. I added Bob from BigCo as a customer, if that's what you wanted to confirm."

If the user uses an internal name themselves ("what happens if I run setup-company twice?"), gently translate in your reply ("setting up a company twice would be rejected, since names are unique") without echoing the name or correcting the user.

### Accounting and ledger internals
The same rule applies to the bookkeeping behind an action: erpclaw returns the technical record (double-entry GL legs, ledger fields, status flags, internal IDs), but you confirm the business outcome, translate every internal label, and keep the mechanics out of the reply. Never describe the double-entry posting, the debit/credit legs, the account names, "no stock movement", or "stock ledger entry". Say what changed in business terms: "I recorded the bill, it's in your books and shows as owed to Gotham Steel."
- Translate internal labels, don't echo them: draft means "saved but not sent yet"; submitted or gl posted means "recorded in your books"; outstanding means "still owed"; valuation rate means "cost"; posting date means "date"; naming series and the gl or sle entry counts should be omitted entirely.
- Show an internal ID only as a trailing reference, never as the headline.
  - **Wrong:** "Posted. status: submitted. Posting date: 2026-06-07. gl entries created: 2 (debit Inventory, credit Accounts Payable). 3f2a-..." **Right:** "Done, I recorded that bill in your books; you still owe Gotham Steel $600 (reference 3f2a if you need to look it up)."

### Skill Activation Triggers

Activate when user mentions: ERP, accounting, invoice, sales order, purchase order, customer, supplier, inventory, payment, GL, trial balance, P&L, balance sheet, P&L / spend / revenue by department or project or cost center or location or fund, tax, billing, modules, install module, onboard, CRM, manufacturing, healthcare, education, retail, employee, HR, payroll, salary, leave, attendance, expense claim, W-2, garnishment, integration. **"By department / project / cost center / location / fund" reporting is a first-class capability (accounting dimensions), never hand-rolled:** tag entries at booking time with `--dimension-key/--dimension-value`, then report with `profit-and-loss --group-by <dimension>` (P&L by that dimension) or `multi-dim-trial-balance --group-by <dimension>` (whole trial balance) — NOT a `cost_center` column or raw SQL — see the Journal Entries and Financial Reports rows.

### Auto-Detection

When a user describes their business: detect type (e.g., "dental practice" → dental), **ask the user to confirm** before proceeding, then set the company up with that industry. (Internal routing only: invoke `setup-company` with `--industry <type>`. Never name the action to the user.) Industry values: retail, restaurant, healthcare, dental, veterinary, construction, manufacturing, legal, agriculture, hospitality, property, school, university, nonprofit, automotive, therapy, home-health, consulting, distribution, saas. When a user asks about a service or integration not currently installed, search the module registry and **suggest** installation (never auto-install without user approval).

### Setup
```
python3 {baseDir}/scripts/erpclaw-setup/db_query.py --action initialize-database
python3 {baseDir}/scripts/db_query.py --action seed-defaults --company-id <id>
python3 {baseDir}/scripts/db_query.py --action setup-chart-of-accounts --company-id <id> --template us_gaap
```

## Runtime gate

High-impact actions require the `--user-confirmed` flag on every invocation; the foundation router rejects unflagged calls with a structured JSON error. Read-only actions (`list`, `get`, reports) run without the flag.

**The flag confirms consent the user already gave — it is not a request to pause.** When the user has clearly asked for an action ("send the invoice", "record the payment", "post that entry"), pass `--user-confirmed` in that same call and act. Do NOT draft the steps and then ask "want me to submit?" — that re-asks for a yes you already have, and nothing is recorded. This is the default for every routine, reversible action: `submit-*`, `add-*`, `create-*`, `approve-*`.

Re-confirm a second time ONLY for the small destructive set, where a mistake is hard or impossible to undo: closing the fiscal year (`close-fiscal-year`), restoring from backup (`restore-database`), installing a module (`install-module`), reconciling foundation files (`rollback-foundation`), and generating a bank-payment file (`generate-nacha-file`). For these, state plainly what will happen and get an explicit yes before passing the flag.

## All 488 Actions

### Setup & Admin (66)
| Action | Description |
|--------|-------------|
| `initialize-database` / `setup-company` / `update-company` / `get-company` / `list-companies` | DB init & company CRUD |
| `migrate` | Run pending schema migrations (`migrations/NNN_*.py`) in order, recording each in the `erpclaw_schema_migration` ledger. Idempotent + dialect-aware; `--dry-run` lists pending without applying. Run on install and on upgrades. |
| `add-currency` / `list-currencies` / `add-exchange-rate` / `get-exchange-rate` / `list-exchange-rates` / `fetch-exchange-rates` | Currency & FX |
| `add-payment-terms` / `list-payment-terms` / `add-uom` / `list-uoms` / `add-uom-conversion` | Terms & UoMs |
| `seed-defaults` / `seed-demo-data` / `check-installation` / `install-guide` / `setup-web-dashboard` / `tutorial` / `onboarding-step` / `status` | Seeding & utilities |
| `add-account-type` / `list-account-types` / `deactivate-account-type` / `add-voucher-type` / `list-voucher-types` / `deactivate-voucher-type` / `validate-registry-completeness` | Type/status registry admin (M0): register/inspect/soft-disable the account_type / voucher_type values that replaced hardcoded CHECK constraints |
| `add-custom-field` / `list-custom-fields` / `remove-custom-field` / `set-custom-field-value` / `get-custom-field-values` | Custom fields (M1): define user-defined fields on any core table and store/read their values. Write-side actions in selling/buying/inventory accept `--custom-fields '{"name":"value"}'` and return them on get |
| `set-advance-account` | Advances (S2): configure a company's B1-style advance sub-account (`--type customer`→liability, `--type supplier`→asset); submit-payment then routes the unallocated advance leg there |
| `add-user` / `update-user` / `get-user` / `list-users` / `set-password` | User management |
| `add-role` / `list-roles` / `assign-role` / `revoke-role` / `seed-permissions` | RBAC & security |
| `link-telegram-user` / `unlink-telegram-user` / `check-telegram-permission` | Telegram integration |
| `backup-database` / `list-backups` / `verify-backup` / `restore-database` / `cleanup-backups` | DB backup/restore |
| `set-credential` / `get-credential` / `list-credentials` / `delete-credential` / `migrate-credentials` | Encrypted credential management |
| `import-master-key-from-backup` | Cross-machine restore: install master key from a backup taken on another machine |
| `get-audit-log` / `get-schema-version` / `update-regional-settings` / `onboard` | System admin |

### General Ledger (30)
| Action | Description |
|--------|-------------|
| `setup-chart-of-accounts` / `add-account` / `update-account` / `get-account` / `list-accounts` | Account CRUD |
| `freeze-account` / `unfreeze-account` / `get-account-balance` / `check-gl-integrity` | Account management |
| `post-gl-entries` / `reverse-gl-entries` / `list-gl-entries` | GL posting |
| `add-fiscal-year` / `list-fiscal-years` / `validate-period-close` / `close-fiscal-year` / `reopen-fiscal-year` | Fiscal year |
| `add-cost-center` / `list-cost-centers` / `add-budget` / `list-budgets` | Cost centers & budgets |
| `add-dimension` / `list-dimensions` / `update-dimension` / `deactivate-dimension` | Accounting dimensions (M6): register/inspect/update/retire the dimension keys (department, project, location, fund, etc.) that drive `gl_entry.dimensions_json` (enforced as GL validation step 13). **To make a posting reportable "by <dimension>", tag it at entry time with `--dimension-key/--dimension-value` on the journal/invoice/payment actions** — then report with `multi-dim-trial-balance` / `dimension-balance-report` (see Financial Reports). `deactivate-dimension` is blocked while recent live GL still references the key |
| `seed-naming-series` / `next-series` / `revalue-foreign-balances` | Naming & FX revaluation |
| `import-chart-of-accounts` / `import-opening-balances` | CSV import |

### Journal Entries (16)
| Action | Description |
|--------|-------------|
| `add-journal-entry` / `update-journal-entry` / `get-journal-entry` / `list-journal-entries` | JE CRUD. **When the user attributes an entry to a department / project / location / fund / cost center (e.g. "office supplies for Engineering", "travel on the Apollo project"), tag it with `--dimension-key department --dimension-value Engineering` — do NOT add a "cost center" line/column or hand-roll the attribution; the dimension tag is what makes it reportable later with `multi-dim-trial-balance`.** `add-journal-entry --cwip-asset-id <A>` tags the JE; submit records its CWIP debit leg as a cost accumulation against the asset (S3) |
| `submit-journal-entry` / `cancel-journal-entry` / `amend-journal-entry` / `delete-journal-entry` / `duplicate-journal-entry` | JE lifecycle |
| `create-intercompany-je` | Intercompany JE |
| `add-recurring-template` / `update-recurring-template` / `list-recurring-templates` / `get-recurring-template` / `process-recurring` / `delete-recurring-template` | Recurring JEs |

### Payments (15)
| Action | Description |
|--------|-------------|
| `add-payment` / `update-payment` / `get-payment` / `list-payments` / `submit-payment` / `cancel-payment` / `delete-payment` | Payment CRUD & lifecycle |
| `create-payment-ledger-entry` / `get-outstanding` / `get-unallocated-payments` / `allocate-payment` / `reconcile-payments` / `bank-reconciliation` | Reconciliation |
| `list-open-advances` / `apply-advance-to-invoice` | Advances (S2): legacy-erp-vocabulary aliases for `get-unallocated-payments` / `allocate-payment` (same semantics) |

### Tax (17)
| Action | Description |
|--------|-------------|
| `add-tax-template` / `update-tax-template` / `get-tax-template` / `list-tax-templates` / `delete-tax-template` | Tax template CRUD |
| `resolve-tax-template` / `calculate-tax` / `add-tax-category` / `list-tax-categories` / `add-tax-rule` / `list-tax-rules` | Tax rules |
| `add-item-tax-template` / `add-tax-withholding-category` / `get-withholding-details` | Withholding |
| `record-withholding-entry` / `record-1099-payment` / `generate-1099-data` | 1099 reporting |

### Financial Reports (21)
| Action | Description |
|--------|-------------|
| `trial-balance` / `profit-and-loss` / `balance-sheet` / `cash-flow` / `general-ledger` / `party-ledger` / `multi-dim-trial-balance` / `dimension-balance-report` | Core statements. **For a "P&L by DEPARTMENT / project / cost center / location / fund / any dimension" ask, call `profit-and-loss --group-by <dimension>` — it returns revenue / expenses / net per dimension value (with an explicit `(untagged)` bucket), so you NEVER hand-roll the split or write SQL over `cost_center`.** Example: "show me this month's P&L broken down by department" → `profit-and-loss --group-by department --from-date <start> --to-date <end>`. For grouping the WHOLE trial balance (all account types, not just income/expense) use `multi-dim-trial-balance --group-by "project,department"`; `dimension-balance-report --dimension K` gives one dimension's balances. `--group-by` takes ONE dimension on `profit-and-loss`; group by an UNREGISTERED dimension errors (run `list-dimensions`). Without `--group-by`, `profit-and-loss` returns ONE company-wide statement. All four headline statements + `general-ledger` also accept repeated `--dimension-key/--dimension-value` filters to scope (and `profit-and-loss` filters THEN groups) |
| `ar-aging` / `ap-aging` / `budget-vs-actual` (alias: `budget-variance`) | Aging & budget |
| `tax-summary` / `payment-summary` / `gl-summary` / `comparative-pl` / `check-overdue` | Summaries |
| `add-elimination-rule` / `list-elimination-rules` / `run-elimination` / `list-elimination-entries` | Intercompany |

### Selling (57)
| Action | Description |
|--------|-------------|
| `add-customer` / `update-customer` / `get-customer` / `list-customers` / `import-customers` | Customer CRUD (add/update accept `--email` / `--phone` — dedicated structured columns) |
| `add-quotation` / `update-quotation` / `get-quotation` / `list-quotations` / `submit-quotation` / `convert-quotation-to-so` | Quotations |
| `add-sales-order` / `update-sales-order` / `get-sales-order` / `list-sales-orders` / `submit-sales-order` / `cancel-sales-order` / `amend-sales-order` / `close-sales-order` | Sales orders |
| `add-blanket-order` / `get-blanket-order` / `list-blanket-orders` / `submit-blanket-order` / `create-so-from-blanket` | Blanket orders |
| `create-delivery-note` / `get-delivery-note` / `list-delivery-notes` / `submit-delivery-note` / `cancel-delivery-note` / `add-packing-slip` / `get-packing-slip` / `list-packing-slips` | Delivery & packing |
| `create-sales-invoice` / `update-sales-invoice` / `get-sales-invoice` / `list-sales-invoices` / `submit-sales-invoice` / `cancel-sales-invoice` | Invoicing |
| `create-credit-note` / `list-credit-notes` / `update-invoice-outstanding` | Credit notes |
| `add-sales-partner` / `list-sales-partners` | Sales partners |
| `add-recurring-template` / `update-recurring-template` / `list-recurring-templates` / `generate-recurring-invoices` | Recurring invoices |
| `add-intercompany-account-map` / `list-intercompany-account-maps` / `create-intercompany-invoice` / `list-intercompany-invoices` / `cancel-intercompany-invoice` | Intercompany |
| `check-credit-limit` / `place-customer-on-hold` | Credit control: compute available credit (limit minus outstanding AR); place customer on hold / suspend / restore active |
| `add-dunning-level` / `run-dunning-cycle` / `list-dunning-runs` | Dunning: configure escalation levels (at N days overdue → email / call / hold / suspend); run a cycle that matches overdue invoices to their highest applicable level and applies the configured action — `email` levels enqueue a dunning email via the erpclaw-alerts send-email action and record the outbox id on `dunning_run.generated_email_id` (missing customer email or template skips-with-note, never failing the cycle); view run history |

### Buying (43)
| Action | Description |
|--------|-------------|
| `add-supplier` / `update-supplier` / `get-supplier` / `list-suppliers` / `import-suppliers` | Supplier CRUD (add/update accept `--email` / `--phone` — dedicated structured columns) |
| `add-material-request` / `submit-material-request` / `list-material-requests` | Material requests |
| `add-rfq` / `submit-rfq` / `list-rfqs` / `add-supplier-quotation` / `list-supplier-quotations` / `compare-supplier-quotations` | RFQs & quotes |
| `add-purchase-order` / `update-purchase-order` / `get-purchase-order` / `list-purchase-orders` / `submit-purchase-order` / `cancel-purchase-order` / `close-purchase-order` | Purchase orders |
| `add-blanket-po` / `get-blanket-po` / `list-blanket-pos` / `submit-blanket-po` / `create-po-from-blanket` / `create-po-from-so` / `create-drop-ship-order` | Blanket POs & drop ship |
| `create-purchase-receipt` / `get-purchase-receipt` / `list-purchase-receipts` / `submit-purchase-receipt` / `cancel-purchase-receipt` | Receipts |
| `create-purchase-invoice` / `update-purchase-invoice` / `get-purchase-invoice` / `list-purchase-invoices` / `submit-purchase-invoice` / `cancel-purchase-invoice` | Purchase invoices. `create-purchase-invoice --cwip-asset-id <A>` (standalone cost bill) routes the expense GL to the asset's CWIP account + records a cost accumulation on submit (S3) |
| `create-debit-note` / `add-landed-cost-voucher` / `update-receipt-tolerance` / `update-three-way-match-policy` | Adjustments |

**Receiving purchased stock — flow:** to bring purchased goods into inventory, receive them against their source document so valuation carries automatically. Canonical flow: `submit-purchase-order` (confirms the order + rate) → `create-purchase-receipt --purchase-order-id <PO>` then `submit-purchase-receipt` (this values the stock at the PO rate and posts inventory GL) → `create-purchase-invoice` + `submit-purchase-invoice` for the bill (leave stock update off — the receipt already moved it) → pay. Do NOT use a standalone `add-stock-entry --type material_receipt` to receive purchased goods unless you restate the unit cost; a rate-less receipt cannot be valued and will be refused.

### Inventory (65)
| Action | Description |
|--------|-------------|
| `add-item` / `update-item` / `get-item` / `list-items` / `resolve-item` / `import-items` / `add-item-group` / `list-item-groups` | Item master (`resolve-item`: resolve a loose/plural user phrase like "20 Brake Pad Sets" to the stored item) |
| `add-item-attribute` / `create-item-variant` / `generate-item-variants` / `list-item-variants` | Item variants |
| `add-item-supplier` / `list-item-suppliers` / `set-item-purchase-uom` | Item suppliers |
| `add-warehouse` / `update-warehouse` / `list-warehouses` | Warehouses |
| `add-stock-entry` / `add-repack-stock-entry` / `add-material-consumption` / `get-stock-entry` / `list-stock-entries` / `submit-stock-entry` / `cancel-stock-entry` | Stock entries. `--entry-type` accepts receive / issue / transfer / manufacture / repack / subcontract / consume. A `material_receipt` requires a stated rate or the item's standard cost (non-purchase adjustments only; to receive purchased goods, use the Buying procure-to-pay flow). `repack` consumes input lines and produces output lines in one warehouse with input value == output value (cost-balanced within $0.01); `add-repack-stock-entry --warehouse W --from-item-id I1 --from-qty Q1 --to-item-id I2 --to-qty Q2 [--standard-rate R]` is the one-in/one-out shortcut. `subcontract` (`send_to_subcontractor`) transfers stock out to a `--supplier-warehouse-id` (a transit/production warehouse). `consume` (`material_consumption`) issues raw material against an active `--work-order-id`; `add-material-consumption --warehouse W --work-order-id WO --item-id I --qty Q` is the shortcut |
| `create-stock-ledger-entries` / `reverse-stock-ledger-entries` | Stock ledger |
| `get-stock-balance` / `stock-balance` / `stock-balance-report` / `stock-ledger-report` / `get-projected-qty` | Stock reports. `get-projected-qty`'s `reserved_qty` reads persisted active reservations (M5); falls back to open sales-order lines when none exist |
| `add-putaway-rule` / `list-putaway-rules` / `update-putaway-rule` / `delete-putaway-rule` / `apply-putaway-on-receipt` | Putaway (M5, warehouse-level). Route received stock to a target warehouse by item or item-group match (`--match-item I` beats `--match-item-group G`, then `--priority` ASC). `delete-putaway-rule` soft-disables. `apply-putaway-on-receipt --stock-entry SE` computes the deterministic routing for a `material_receipt` |
| `create-pick-list` / `add-pick-list-item` / `submit-pick-list` / `mark-picked` / `complete-pick-list` / `cancel-pick-list` | Pick lists (M5). `create-pick-list --from-sales-order SO` drafts a pick from open SO lines; `submit-pick-list` reserves the qty (hard); `mark-picked --pick-list P --item I --picked-qty Q` records actuals (full pick → `picked`); `complete-pick-list` consumes the reservations and generates a delivery note; `cancel-pick-list` releases them |
| `add-reservation` / `release-reservation` / `list-reservations` | Hard stock reservations (M5, ADR-0026). `add-reservation --voucher-type T --item I --warehouse W --qty Q` holds stock and is refused if it would exceed available (`actual − active reserved`); a `material_issue` that would breach active reservations is blocked. `release-reservation --id I` frees it |
| `add-item-alternative` / `list-item-alternatives` / `get-best-alternative-for-item` / `remove-item-alternative` | Item-global substitutes (S7, directional). `add-item-alternative --item I --alternative A [--priority P --conversion-factor C --notes "..."]` (lower priority = preferred; self-ref rejected; pair (a,b) unique but (b,a) is a distinct valid row). `get-best-alternative-for-item --item I [--required-qty Q --warehouse W]` returns the highest-priority active alternative with enough stock at W (ties by available qty); no match is a clean empty result. `remove-item-alternative --id I` soft-disables. Manufacturing BOM substitutes inherit from these when a BOM line has none of its own |
| `add-batch` / `list-batches` / `add-serial-number` / `list-serial-numbers` / `add-price-list` / `add-item-price` / `get-item-price` / `add-pricing-rule` | Batch & serial; pricing |
| `add-stock-reconciliation` / `submit-stock-reconciliation` / `revalue-stock` / `list-stock-revaluations` / `get-stock-revaluation` / `cancel-stock-revaluation` / `check-reorder` | Reconciliation, revaluation & reorder |

### Billing & Metering (24)
| Action | Description |
|--------|-------------|
| `add-meter` / `update-meter` / `get-meter` / `list-meters` / `add-meter-reading` / `list-meter-readings` | Meters |
| `add-usage-event` / `add-usage-events-batch` | Usage tracking |
| `add-rate-plan` / `update-rate-plan` / `get-rate-plan` / `list-rate-plans` / `rate-consumption` | Rate plans |
| `create-billing-period` / `run-billing` / `generate-invoices` / `get-billing-period` / `list-billing-periods` | Billing cycles |
| `add-billing-adjustment` / `add-prepaid-credit` / `get-prepaid-balance` | Adjustments & prepaid |
| `add-recurring-bill-template` / `list-recurring-bill-templates` / `generate-recurring-bills` | Recurring bills |

### Advanced Accounting (48)
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

### HR & Payroll (68)
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

### Module Management & Schema (22)
| Action | Description |
|--------|-------------|
| `install-module` / `remove-module` / `update-modules` / `list-modules` / `available-modules` / `search-modules` / `module-status` | Module catalog (install/remove require user approval) |
| `rebuild-action-cache` / `list-all-actions` / `list-profiles` / `onboard` | Actions & profiles |
| `validate-module` / `list-articles` / `build-table-registry` | Constitution + module discovery (read-only) |
| `schema-plan` / `schema-apply` / `schema-rollback` / `schema-drift` | Schema migration (apply/rollback require user approval) |
| `regenerate-skill-md` | Regenerate SKILL.md |
| `update-foundation` / `rollback-foundation` / `verify-trust-root` | Reconcile installed foundation files with the published manifest; reconcile actions require user approval |

> **Foundation reconciliation.** Reconciliation verifies an ed25519 signature on the registry against an embedded public key before trusting any file hash. Two user-invoked actions keep an installed foundation aligned with the published manifest in `module_registry.json`. `update-foundation --user-confirmed` compares each installed file's SHA256 against the signed manifest, and for any drift, replaces the file from the published source after re-verifying the declared hash; a pre-flight verifies all replacements before any rename, so a hash failure leaves the install unchanged. Each replaced file is preserved as `.bak` for one cycle, and `rollback-foundation --user-confirmed` reverts that cycle. A confirmed apply-path `update-foundation` also applies any pending schema migrations after the file reconcile (idempotent; loud, ledgered failure) — note that `rollback-foundation` restores files only, never schema (migrations are forward-only). `verify-trust-root` prints the embedded key fingerprint for out-of-band verification. A periodic convenience check, suppressed by the marker file `~/.openclaw/erpclaw/.skip_reconcile` or the per-invocation flag `--no-reconcile-check`, may surface a reminder when version drift is present; the user runs `update-foundation` to apply. The router never modifies installed code without an explicit gated invocation. Signature verification is mandatory on the reconciliation path; the only exception is a documented operator recovery path that records to the audit log.

> **Module authoring + variant analysis (developer tooling):** module generation, in-module feature injection, sandboxed test execution, deploy pipeline, variant analysis, gap detection, heartbeat analysis, semantic checks, and the OS-engine status command live in the optional `erpclaw-os-engine` addon (~30 actions, all `os-` prefixed). The addon is GitHub-only and not installed by default. Install via `module_manager.py --action install-module --module-name erpclaw-os-engine`. Foundation does not run module-generation or auto-deploy code paths.

**Confirmation follows the two-class protocol in `## Runtime gate`:** for the destructive set (`close-fiscal-year`, `restore-database`, `install-module`, `rollback-foundation`, `generate-nacha-file`, plus `initialize-database --force` and `remove-module`/`schema-rollback`) get a genuine second yes before acting; for routine reversible work (`submit-*` / `cancel-*` / `approve-*` / `reject-*`, `setup-company`, `onboard`, `run-consolidation`/`run-elimination`) pass `--user-confirmed` on a clear request without re-asking. Speak in business terms; the action names are routing-only and never spoken to the user.

## Optional scheduling (background email workers)

ERPClaw never installs cron jobs automatically. Two background workers are meant to run on a schedule once a business turns on email automation. Register them with the OpenClaw cron facility — the same `openclaw cron add` path used for any recurring ERPClaw job (confirm with the user first):

```bash
# Send queued emails — every 1 minute (erpclaw-alerts: process-email-queue)
openclaw cron add --name erpclaw-email-queue --cron "* * * * *" \
  --message "Using erpclaw, run the process-email-queue action."

# Advance drip-campaign sends — every 5 minutes (erpclaw-growth: process-drip-sends)
openclaw cron add --name erpclaw-drip-sends --cron "*/5 * * * *" \
  --message "Using erpclaw, run the process-drip-sends action."
```

`process-email-queue` (every 1 minute) drains the email outbox with exponential backoff; `process-drip-sends` (every 5 minutes) advances due drip enrollments. Both are idempotent, so re-running inside an interval will not double-send. Remove a schedule with `openclaw cron remove --name <name>`. SKILL.md `cron:` blocks are decorative and never auto-register — explicit `openclaw cron add` is the only active scheduling path (see CHANGELOG v4.1.0).

## Technical Details (Tier 3)
Router: `scripts/db_query.py` -> 14 core domains. Optional modules installed from GitHub (`avansaber/*`) to `~/.openclaw/erpclaw/modules/` (user-approved only). Single SQLite DB (WAL). 188 core tables (688 with modules). Money=TEXT(Decimal), IDs=TEXT(UUID4), GL immutable. Python 3.10+. All network activity limited to: (1) `fetch-exchange-rates`, the public exchange rate API; (2) `install-module`, git clone from `github.com/avansaber/*` only, requires user approval.
