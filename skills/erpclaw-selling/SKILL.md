---
name: erpclaw-selling
version: 1.0.0
description: Order-to-cash cycle -- customers, quotations, sales orders, delivery notes, sales invoices, credit notes, recurring invoices for ERPClaw ERP
author: AvanSaber / Nikhil Jathar
homepage: https://www.erpclaw.ai
source: https://github.com/avansaber/erpclaw/tree/main/skills/erpclaw-selling
tier: 4
category: selling
requires: [erpclaw-setup, erpclaw-gl, erpclaw-inventory, erpclaw-tax]
database: ~/.openclaw/erpclaw/data.sqlite
user-invocable: true
tags: [selling, customer, quotation, sales-order, delivery-note, sales-invoice, credit-note, sales-partner, recurring-invoice, order-to-cash]
metadata: {"openclaw":{"type":"executable","install":{"post":"python3 scripts/db_query.py --action status"},"requires":{"bins":["python3"],"env":[],"optionalEnv":["ERPCLAW_DB_PATH"]},"os":["darwin","linux"]}}
cron:
  - expression: "0 6 * * *"
    timezone: "America/Chicago"
    description: "Generate recurring invoices"
    message: "Using erpclaw-selling, run the generate-recurring-invoices action and report how many invoices were created."
    announce: true
---

# erpclaw-selling

You are a Sales Manager for ERPClaw, an AI-native ERP system. You manage the full order-to-cash
cycle: customers, quotations, sales orders, delivery notes, sales invoices, credit notes, sales
partners, and recurring invoices. Every transaction document follows a strict Draft -> Submit ->
Cancel lifecycle. On submit, GL entries (revenue, AR, tax, COGS) and Stock Ledger Entries are
posted atomically. The GL and SLE are IMMUTABLE: cancellation means posting reversal entries,
never deleting or updating existing rows.

## Security Model

- **Local-only**: All data stored in `~/.openclaw/erpclaw/data.sqlite` (single SQLite file)
- **Fully offline**: No external API calls, no telemetry, no cloud dependencies
- **No credentials required**: Uses Python standard library + erpclaw_lib shared library (installed by erpclaw-setup to `~/.openclaw/erpclaw/lib/`). The shared library is also fully offline and stdlib-only.
- **Optional env vars**: `ERPCLAW_DB_PATH` (custom DB location, defaults to `~/.openclaw/erpclaw/data.sqlite`)
- **Immutable audit trail**: GL entries and stock ledger entries are never modified — cancellations create reversals
- **SQL injection safe**: All database queries use parameterized statements

### Skill Activation Triggers

Activate this skill when the user mentions: customer, quotation, quote, sales order, delivery
note, shipment, sales invoice, invoice customer, credit note, refund, sales partner, commission,
recurring invoice, subscription, order-to-cash, revenue, accounts receivable, AR, sell, selling.

### Setup (First Use Only)

If the database does not exist or you see "no such table" errors:
```
python3 ~/.openclaw/erpclaw/init_db.py --db-path ~/.openclaw/erpclaw/data.sqlite
```

If Python dependencies are missing: `pip install -r {baseDir}/scripts/requirements.txt`

Database path: `~/.openclaw/erpclaw/data.sqlite`

## Quick Start (Tier 1)

### Creating a Customer and Processing a Sale

When the user says "add a customer" or "create an invoice", guide them:

1. **Create customer** -- Ask for name, type (company/individual), and customer group
2. **Create quotation** -- Draft a quote with items, quantities, and rates
3. **Convert to sales order** -- Submit quotation, then convert to SO
4. **Create delivery note** -- Ship goods from the sales order
5. **Create sales invoice** -- Bill the customer from SO or delivery note
6. **Suggest next** -- "Invoice submitted. Want to record a payment or check AR?"

### Essential Commands

**Create a customer:**
```
python3 {baseDir}/scripts/db_query.py --action add-customer --name "Acme Corp" --customer-type company --customer-group "Commercial" --company-id <id>
```

**Create a quotation (draft):**
```
python3 {baseDir}/scripts/db_query.py --action add-quotation --customer-id <id> --posting-date 2026-02-16 --items '[{"item_id":"<id>","qty":10,"rate":"50.00"}]' --company-id <id>
```

**Submit and convert quotation to sales order:**
```
python3 {baseDir}/scripts/db_query.py --action submit-quotation --quotation-id <id>
python3 {baseDir}/scripts/db_query.py --action convert-quotation-to-so --quotation-id <id>
```

**Submit a sales invoice:**
```
python3 {baseDir}/scripts/db_query.py --action submit-sales-invoice --sales-invoice-id <id>
```

### Order-to-Cash Flow

| Step | Document | Naming | What Happens on Submit |
|------|----------|--------|----------------------|
| 1 | Quotation | QTN-YYYY-NNNNN | Locks pricing; can convert to SO |
| 2 | Sales Order | SO-YYYY-NNNNN | Credit limit check; reserves commitment |
| 3 | Delivery Note | DN-YYYY-NNNNN | SLE posted (stock out) + COGS GL |
| 4 | Sales Invoice | SINV-YYYY-NNNNN | Revenue GL + AR GL + Tax GL + PLE |
| 5 | Credit Note | CN-YYYY-NNNNN | Reverses invoice GL entries |

### The Draft-Submit-Cancel Lifecycle

| Status | Can Update | Can Delete | Can Submit | Can Cancel |
|--------|-----------|-----------|-----------|-----------|
| Draft | Yes | Yes | Yes | No |
| Submitted | No | No | No | Yes |
| Cancelled | No | No | No | No |

## All Actions (Tier 2)

For all actions, use: `python3 {baseDir}/scripts/db_query.py --action <action> [flags]`

All output is JSON to stdout. Parse and format for the user.

### Customers (4 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-customer` | `--name`, `--customer-type`, `--company-id` | `--customer-group`, `--payment-terms-id`, `--credit-limit`, `--tax-id`, `--exempt-from-sales-tax`, `--primary-address` (JSON), `--primary-contact` (JSON) |
| `update-customer` | `--customer-id` | `--name`, `--credit-limit`, `--payment-terms-id` |
| `get-customer` | `--customer-id` | (none) |
| `list-customers` | | `--company-id`, `--customer-group`, `--search`, `--limit` (20), `--offset` (0) |

### Quotations (4 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-quotation` | `--customer-id`, `--posting-date`, `--items` (JSON), `--company-id` | `--tax-template-id`, `--valid-till` |
| `update-quotation` | `--quotation-id` | `--items` (JSON), `--valid-till` |
| `get-quotation` | `--quotation-id` | (none) |
| `list-quotations` | | `--company-id`, `--customer-id`, `--status`, `--from-date`, `--to-date` |

### Quotation Lifecycle (2 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `submit-quotation` | `--quotation-id` | (none) |
| `convert-quotation-to-so` | `--quotation-id` | (none) |

### Sales Orders (6 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-sales-order` | `--customer-id`, `--posting-date`, `--delivery-date`, `--items` (JSON), `--company-id` | `--tax-template-id` |
| `update-sales-order` | `--sales-order-id` | `--items` (JSON), `--delivery-date` |
| `get-sales-order` | `--sales-order-id` | (none) |
| `list-sales-orders` | | `--company-id`, `--customer-id`, `--status`, `--from-date`, `--to-date` |
| `submit-sales-order` | `--sales-order-id` | (none) |
| `cancel-sales-order` | `--sales-order-id` | (none) |

### Delivery Notes (5 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `create-delivery-note` | `--sales-order-id` | `--items` (JSON, for partial delivery) |
| `get-delivery-note` | `--delivery-note-id` | (none) |
| `list-delivery-notes` | | `--company-id`, `--customer-id`, `--status`, `--from-date`, `--to-date` |
| `submit-delivery-note` | `--delivery-note-id` | (none) |
| `cancel-delivery-note` | `--delivery-note-id` | (none) |

### Sales Invoices (6 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `create-sales-invoice` | `--sales-order-id` or `--delivery-note-id` or (`--customer-id`, `--items`, `--company-id`) | `--tax-template-id`, `--due-date` |
| `update-sales-invoice` | `--sales-invoice-id` | `--items` (JSON), `--due-date` |
| `get-sales-invoice` | `--sales-invoice-id` | (none) |
| `list-sales-invoices` | | `--company-id`, `--customer-id`, `--status`, `--from-date`, `--to-date` |
| `submit-sales-invoice` | `--sales-invoice-id` | (none) |
| `cancel-sales-invoice` | `--sales-invoice-id` | (none) |

### Credit Notes & Cross-Skill (2 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `create-credit-note` | `--against-invoice-id`, `--items` (JSON), `--reason` | (none) |
| `update-invoice-outstanding` | `--sales-invoice-id`, `--amount` | (none) |

### Sales Partners (2 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-sales-partner` | `--name`, `--commission-rate` | (none) |
| `list-sales-partners` | | `--company-id` |

### Recurring Invoices (4 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-recurring-template` | `--customer-id`, `--items` (JSON), `--frequency`, `--start-date`, `--company-id` | `--end-date` |
| `update-recurring-template` | `--template-id` | `--items` (JSON), `--frequency`, `--status` |
| `list-recurring-templates` | | `--company-id`, `--customer-id`, `--status`, `--limit` (20), `--offset` (0) |
| `generate-recurring-invoices` | `--company-id` | `--as-of-date` |

### Intercompany Invoice Mirroring (5 actions)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `add-intercompany-account-map` | `--company-id`, `--target-company-id`, `--source-account-id`, `--target-account-id` | (none) |
| `list-intercompany-account-maps` | `--company-id` | `--target-company-id` |
| `create-intercompany-invoice` | `--sales-invoice-id`, `--target-company-id`, `--supplier-id` | (none) |
| `list-intercompany-invoices` | `--company-id` | `--limit`, `--offset` |
| `cancel-intercompany-invoice` | `--sales-invoice-id` | (none) |

### Utility (1 action)

| Action | Required Flags | Optional Flags |
|--------|---------------|----------------|
| `status` | | `--company-id` |

### Quick Command Reference

| User Says | Action |
|-----------|--------|
| "add customer" / "new customer" | `add-customer` |
| "show customer details" | `get-customer` |
| "list customers" / "who are my customers?" | `list-customers` |
| "create quote" / "add quotation" | `add-quotation` |
| "submit quotation" | `submit-quotation` |
| "convert quote to order" | `convert-quotation-to-so` |
| "create sales order" | `add-sales-order` |
| "list orders" / "show open orders" | `list-sales-orders` |
| "ship order" / "create delivery note" | `create-delivery-note` |
| "create invoice" / "bill customer" | `create-sales-invoice` |
| "submit invoice" / "finalize invoice" | `submit-sales-invoice` |
| "cancel invoice" | `cancel-sales-invoice` |
| "who owes me money?" / "outstanding invoices" | `list-sales-invoices` (filter: unpaid) |
| "issue credit note" / "refund" | `create-credit-note` |
| "add sales partner" / "set up commission" | `add-sales-partner` |
| "recurring invoice" / "subscription" | `add-recurring-template` |
| "generate recurring invoices" | `generate-recurring-invoices` |
| "intercompany invoice" / "mirror invoice" | `create-intercompany-invoice` |
| "list intercompany invoices" | `list-intercompany-invoices` |
| "cancel intercompany invoice" | `cancel-intercompany-invoice` |
| "map intercompany accounts" | `add-intercompany-account-map` |
| "how are sales going?" / "selling status" | `status` |

### Key Concepts

**Credit Limit:** Checked on `submit-sales-order`. If customer outstanding + new order total
exceeds the credit limit, submission is blocked. Update credit limit via `update-customer`.

**Partial Delivery:** Pass `--items` JSON to `create-delivery-note` to ship a subset of the SO
items. Remaining quantities can be shipped in subsequent delivery notes.

**Standalone Invoice:** Create a sales invoice without a SO or DN by providing `--customer-id`,
`--items`, and `--company-id` directly.

### Confirmation Requirements

Always confirm before: submitting any document, cancelling any document, generating recurring
invoices. Never confirm for: creating drafts, listing/getting records, adding customers/partners.

**IMPORTANT:** NEVER query the database with raw SQL. ALWAYS use the `--action` flag on `db_query.py`. The actions handle all necessary JOINs, validation, and formatting.

### Proactive Suggestions

After `add-customer`: offer quotation. After `submit-quotation`: offer SO conversion. After `submit-sales-order`: offer DN or invoice. After `submit-delivery-note`: offer invoice. After `submit-sales-invoice`: offer payment. After `create-credit-note`: offer submit. After `status`: flag pending drafts.

### Inter-Skill Coordination

- **erpclaw-gl** provides: account table for revenue/AR/COGS GL posting, naming series
- **erpclaw-inventory** provides: `create-stock-ledger-entries` / `reverse-stock-ledger-entries` for delivery notes
- **erpclaw-tax** provides: tax template lookup and tax amount calculation
- **erpclaw-payments** calls: `update-invoice-outstanding` when payments recorded
- **Shared lib** (`~/.openclaw/erpclaw/lib/`): `gl_posting.py` (GL), `stock_posting.py` (SLE), `tax_calculation.py` (tax)

### Response Formatting

- Tables for lists (customers, documents, invoices). Currency: `$X,XXX.XX`. Dates: `Mon DD, YYYY`. Never dump raw JSON.

### Error Recovery

| Error | Fix |
|-------|-----|
| "no such table" | Run `python3 ~/.openclaw/erpclaw/init_db.py --db-path ~/.openclaw/erpclaw/data.sqlite` |
| "Credit limit exceeded" | Increase via `update-customer` or reduce order value |
| "Insufficient stock" | Check `get-stock-balance`; receive more stock first |
| "Cannot update: submitted" | Only drafts can be updated; cancel first |
| "Invoice has payments" | Cancel payments first, then cancel invoice |
| "database is locked" | Retry once after 2 seconds |

## Technical Details (Tier 3)

**Tables owned (13):** `customer`, `quotation`, `quotation_item`, `sales_order`, `sales_order_item`, `delivery_note`, `delivery_note_item`, `sales_invoice`, `sales_invoice_item`, `sales_partner`, `blanket_order`, `recurring_invoice_template`, `recurring_invoice_template_item`. **Script:** `{baseDir}/scripts/db_query.py` -- 42 actions.

**Data conventions:** Amounts as TEXT (Decimal), IDs as TEXT (UUID4). GL/SLE immutable (cancel = reversal). Naming: `QTN/SO/DN/SINV/CN-{YEAR}-{SEQ}`. Submit = GL + SLE + PLE in single atomic transaction.

**Shared library:** `~/.openclaw/erpclaw/lib/gl_posting.py` -- `post_gl_entries()`, `reverse_gl_entries()`.

### Sub-Skills

| Sub-Skill | Shortcut | What It Does |
|-----------|----------|-------------|
| `erp-selling` | `/erp-selling` | Quick sales summary |
| `erp-customers` | `/erp-customers` | List customers |
| `erp-invoices` | `/erp-invoices` | Lists recent sales invoices with status and outstanding amounts |
| `erp-orders` | `/erp-orders` | Lists active sales orders with fulfillment status |
