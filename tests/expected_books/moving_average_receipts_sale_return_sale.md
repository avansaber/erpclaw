# Moving average: three receipts at different costs, a sale, a return, a second sale

Provenance: worked out from scratch under `CONVENTIONS.md`. The same moves are valued by FIFO in
`fifo_receipts_sale_return_sale.md`.

## Starting books

As in the FIFO case, except that Copper Wire is valued by **moving average**.

## Requests, in order, each approved

The same six requests and dates as `fifo_receipts_sale_return_sale.md`.

## Expected books, step by step

| Step | Stock qty change | Stock value change | Qty after | Value after | Rate after (reading only) |
|---|---|---|---|---|---|
| 1 | 100 | 1000.00 | 100 | 1000.00 | 10.00 |
| 2 | 50 | 650.00 | 150 | 1650.00 | 11.00 |
| 3 | 30 | 345.00 | 180 | 1995.00 | 11.083333… |
| 4 | -120 | -1330.00 (1995.00 x 120 / 180) | 60 | 665.00 | 11.083333… |
| 5 | 10 | 110.83 (1330.00 x 10 / 120, rounded) | 70 | 775.83 | 11.083285… |
| 6 | -45 | -498.75 (775.83 x 45 / 70 = 498.7478…, rounded) | 25 | 277.08 | 11.0832 |

Known wrong figure: step 4 at a rate first rounded to 11.08 gives 1329.60.

Ledger, one balanced document per step:

| Step | Debit | Credit | Amount |
|---|---|---|---|
| 1 | Raw Materials (inventory) | Stock Received Not Billed | 1000.00 |
| 2 | Raw Materials (inventory) | Stock Received Not Billed | 650.00 |
| 3 | Raw Materials (inventory) | Stock Received Not Billed | 345.00 |
| 4 | Trade Receivables (party Acme) | Sales Revenue | 2400.00 |
| 4 | COGS - Materials | Raw Materials (inventory) | 1330.00 |
| 5 | Sales Revenue | Trade Receivables (party Acme) | 200.00 |
| 5 | Raw Materials (inventory) | COGS - Materials | 110.83 |
| 6 | Trade Receivables (party Acme) | Sales Revenue | 900.00 |
| 6 | COGS - Materials | Raw Materials (inventory) | 498.75 |

Documents: as in the FIFO case (step 4 invoice outstanding 2200.00 after the credit note; step 6
invoice outstanding 900.00).

## Variant: the whole sale comes back in three returns

Same books through step 4 (60 on hand, value 665.00; the step 4 invoice is 120 @ 20.00 = 2400.00,
issued at 1330.00). Then, instead of steps 5 and 6, Acme sends back the whole 120 in three parts,
each with its own credit note against the step 4 invoice. Return cost is allocated cumulatively
(`CONVENTIONS.md`): `round(1330.00 x returned so far / 120)` less the earlier returns.

| Return | Date | Qty | Credit | Cost back | Worked | Qty after | Value after | Invoice outstanding |
|---|---|---|---|---|---|---|---|---|
| a | 2026-03-15 | 40 | 800.00 | 443.33 | round(1330.00 x 40 / 120) = 443.33 | 100 | 1108.33 | 1600.00 |
| b | 2026-03-16 | 40 | 800.00 | 443.34 | round(1330.00 x 80 / 120) = 886.67, less 443.33 | 140 | 1551.67 | 800.00 |
| c | 2026-03-17 | 40 | 800.00 | 443.33 | 1330.00, less 886.67 | 180 | 1995.00 | 0.00 |

Each credit note posts Sales Revenue Dr / Trade Receivables (party Acme) Cr 800.00, and Raw
Materials Dr / COGS - Materials Cr at its cost back. After return c the step 4 invoice is credited
(outstanding 0.00), the three returns total 1330.00 exactly, the sale's cost of goods sold nets to
0.00, and the stock is back to 180 worth 1995.00, as after step 3.

Known wrong figure: rounding each return on its own (3 x 443.33 = 1329.99) leaves 0.01 in cost of
goods sold.

## Balances at the end

| Account | Debit | Credit |
|---|---|---|
| Raw Materials (inventory) | 277.08 | |
| Trade Receivables (Acme) | 3100.00 | |
| COGS - Materials | 1717.92 | |
| Stock Received Not Billed | | 1995.00 |
| Sales Revenue | | 3100.00 |
| **Total** | **5095.00** | **5095.00** |

Inventory balance equals the stock value (277.08).
