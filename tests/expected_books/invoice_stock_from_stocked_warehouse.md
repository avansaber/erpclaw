# Invoice a stocked item kept outside the default warehouse

Provenance: adapted from the ERPClaw v2 demo books (Stark Manufacturing Inc.), with one request
from its write pilot. Figures worked out from scratch under `CONVENTIONS.md`.

## Starting books

Test clock: 2026-10-26. Company Stark Manufacturing Inc., USD, perpetual inventory on.

| Item | Unit | Method | Selling rate |
|---|---|---|---|
| Copper Wire | Meter | moving average | 28.00 |

| Warehouse | Copper Wire on hand | Value |
|---|---|---|
| Stark Manufacturing Inc. - Main Warehouse (the company's default) | 0 | 0.00 |
| Raw Materials Store | 200 | 5600.00 (rate 28.00) |
| Finished Goods Store | 0 | 0.00 |

Customer Acme Corporation exists (one match for "Acme"). Other records in the demo books (an
Initech draft invoice for 2 m of Copper Wire among them) must stay as they are.

## Request

"Invoice Acme for 5 Copper Wire at the usual price and submit."

The owner asked for a submitted invoice, so after the one approval card no second confirmation is
asked. The card names Raw Materials Store as the source (the only warehouse holding the stock) and
shows no internal ids.

## Expected books

New sales invoice, customer Acme Corporation, posting date 2026-10-26, status submitted, stock
moved by the invoice.

| Line | Item | Qty | Rate | Amount | Warehouse |
|---|---|---|---|---|---|
| 1 | Copper Wire | 5 | 28.00 | 140.00 | Raw Materials Store |

Totals: net 140.00, tax 0.00, grand total 140.00, outstanding 140.00.

Ledger (one document, balanced):

| Account (role) | Party | Debit | Credit |
|---|---|---|---|
| Trade Receivables (receivable) | Acme Corporation | 140.00 | |
| Sales Revenue (sales income) | | | 140.00 |
| COGS - Materials (cost of goods sold) | | 140.00 | |
| Raw Materials (inventory, Raw Materials Store) | | | 140.00 |

Stock ledger:

| Item | Warehouse | Qty change | Value change | Qty after | Value after |
|---|---|---|---|---|---|
| Copper Wire | Raw Materials Store | -5 | -140.00 | 195 | 5460.00 |

## Variant b: selling rate above cost

Same books and request, except that Copper Wire's selling rate is 35.00 per metre (its cost stays
28.00). This variant tells a price from a cost.

| Line | Item | Qty | Rate | Amount | Warehouse |
|---|---|---|---|---|---|
| 1 | Copper Wire | 5 | 35.00 | 175.00 | Raw Materials Store |

Totals: net 175.00, tax 0.00, grand total 175.00, outstanding 175.00.

| Account (role) | Party | Debit | Credit |
|---|---|---|---|
| Trade Receivables (receivable) | Acme Corporation | 175.00 | |
| Sales Revenue (sales income) | | | 175.00 |
| COGS - Materials (cost of goods sold) | | 140.00 | |
| Raw Materials (inventory, Raw Materials Store) | | | 140.00 |

Stock ledger as in the main case: -5, -140.00, leaving 195 worth 5460.00.

## Must not happen

- No stock transfer, no stock entry, no movement in any other warehouse.
- No refusal for lack of stock in the default warehouse.
- The Initech draft is not submitted or changed.
