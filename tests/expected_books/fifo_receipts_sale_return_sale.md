# FIFO: three receipts at different costs, a sale, a return, a second sale

Provenance: worked out from scratch under `CONVENTIONS.md`. The same moves are valued by moving
average in `moving_average_receipts_sale_return_sale.md`.

## Starting books

A fresh company, USD, calendar fiscal year 2026, perpetual inventory on, negative stock refused.
Item Copper Wire (unit Meter, **FIFO**), no stock anywhere. Customer Acme Corporation. Warehouse
Main Warehouse (inventory account Raw Materials). No tax.

## Requests, in order, each approved

| Step | Date | Request |
|---|---|---|
| 1 | 2026-03-02 | "We received 100 m of copper wire at 10.00." |
| 2 | 2026-03-05 | "We received 50 m of copper wire at 13.00." |
| 3 | 2026-03-09 | "We received 30 m of copper wire at 11.50." |
| 4 | 2026-03-12 | "Invoice Acme for 120 m of copper wire at 20.00 and submit." (stock moves with the invoice) |
| 5 | 2026-03-15 | "Acme sent back 10 m from that invoice; credit note." |
| 6 | 2026-03-20 | "Invoice Acme for 45 m of copper wire at 20.00 and submit." |

## Expected books, step by step

| Step | Stock qty change | Stock value change | Layers after (oldest first) | Qty after | Value after |
|---|---|---|---|---|---|
| 1 | 100 | 1000.00 | 100 @ 10.00 | 100 | 1000.00 |
| 2 | 50 | 650.00 | 100 @ 10.00, 50 @ 13.00 | 150 | 1650.00 |
| 3 | 30 | 345.00 | 100 @ 10.00, 50 @ 13.00, 30 @ 11.50 | 180 | 1995.00 |
| 4 | -120 | -1260.00 (100 @ 10.00 + 20 @ 13.00) | 30 @ 13.00, 30 @ 11.50 | 60 | 735.00 |
| 5 | 10 | 105.00 (at 1260.00 / 120 = 10.50) | 30 @ 13.00, 30 @ 11.50, 10 @ 10.50 | 70 | 840.00 |
| 6 | -45 | -562.50 (30 @ 13.00 + 15 @ 11.50) | 15 @ 11.50, 10 @ 10.50 | 25 | 277.50 |

Ledger, one balanced document per step:

| Step | Debit | Credit | Amount |
|---|---|---|---|
| 1 | Raw Materials (inventory) | Stock Received Not Billed | 1000.00 |
| 2 | Raw Materials (inventory) | Stock Received Not Billed | 650.00 |
| 3 | Raw Materials (inventory) | Stock Received Not Billed | 345.00 |
| 4 | Trade Receivables (party Acme) | Sales Revenue | 2400.00 |
| 4 | COGS - Materials | Raw Materials (inventory) | 1260.00 |
| 5 | Sales Revenue | Trade Receivables (party Acme) | 200.00 |
| 5 | Raw Materials (inventory) | COGS - Materials | 105.00 |
| 6 | Trade Receivables (party Acme) | Sales Revenue | 900.00 |
| 6 | COGS - Materials | Raw Materials (inventory) | 562.50 |

Documents: invoice of step 4 (120 @ 20.00 = 2400.00) is reduced by the step 5 credit note
(-10 @ 20.00 = -200.00), outstanding 2200.00, status submitted; invoice of step 6 (45 @ 20.00 =
900.00), outstanding 900.00.

## Balances at the end

| Account | Debit | Credit |
|---|---|---|
| Raw Materials (inventory) | 277.50 | |
| Trade Receivables (Acme) | 3100.00 | |
| COGS - Materials | 1717.50 | |
| Stock Received Not Billed | | 1995.00 |
| Sales Revenue | | 3100.00 |
| **Total** | **5095.00** | **5095.00** |

Inventory balance equals the stock value (277.50).
