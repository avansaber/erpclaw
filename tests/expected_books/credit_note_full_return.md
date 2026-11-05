# A credit note for everything on an unpaid invoice, with the stock coming back

Provenance: adapted from the ERPClaw v2 demo books (Stark Manufacturing Inc.), with one request
from its write pilot. The demo books posted finished goods to the Raw Materials account; here the
Finished Goods Store posts to Finished Goods (1133). Worked out from scratch under `CONVENTIONS.md`.

## Starting books

Test clock: 2026-10-26.

| Item | Unit | Method |
|---|---|---|
| Motor Housing Unit | Nos | moving average |

Stock ledger for Motor Housing Unit, Finished Goods Store:

| Date | Document | Qty change | Value change | Qty after | Value after |
|---|---|---|---|---|---|
| 2026-01-01 | opening stock | 15 | 3150.00 | 15 | 3150.00 |
| 2026-01-20 | INV-2026-00005 | -8 | -1680.00 | 7 | 1470.00 |

| Invoice | Customer | Posted | Lines | Grand total | Outstanding | Status |
|---|---|---|---|---|---|---|
| INV-2026-00005 | Hooli Technologies | 2026-01-20 | 8 Motor Housing Unit @ 349.00 = 2792.00 | 2792.00 | 2792.00 | submitted (nothing paid) |

INV-2026-00005 is Hooli's latest (and only) invoice. Its ledger: Trade Receivables Dr 2792.00 (party
Hooli Technologies), Sales Revenue Cr 2792.00, COGS - Materials Dr 1680.00, Finished Goods Cr 1680.00.

## Request

"Hooli returns everything on its last invoice; make the credit note."

The card states the invoice's real status (submitted, 2792.00 outstanding, nothing paid).

## Expected books

New credit note against INV-2026-00005, customer Hooli Technologies, posting date 2026-10-26,
status submitted, stock moved by the credit note.

| Line | Item | Qty | Rate | Amount | Warehouse |
|---|---|---|---|---|---|
| 1 | Motor Housing Unit | -8 | 349.00 | -2792.00 | Finished Goods Store |

Grand total -2792.00. Applied in full to INV-2026-00005.

Ledger (one document, balanced):

| Account (role) | Party | Debit | Credit |
|---|---|---|---|
| Sales Revenue (sales income) | | 2792.00 | |
| Trade Receivables (receivable) | Hooli Technologies | | 2792.00 |
| Finished Goods (inventory, Finished Goods Store) | | 1680.00 | |
| COGS - Materials (cost of goods sold) | | | 1680.00 |

Stock ledger (returned at the cost the goods left at: 1680.00 / 8 = 210.00):

| Item | Warehouse | Qty change | Value change | Qty after | Value after |
|---|---|---|---|---|---|
| Motor Housing Unit | Finished Goods Store | 8 | 1680.00 | 15 | 3150.00 |

INV-2026-00005: outstanding 0.00, status credited. Its own ledger and stock rows are unchanged.

## Variant: returns posted to a sales returns account

Same books and request, except that the company posts sales returns to Sales Returns & Allowances
(4130), a contra-income account with a debit normal balance. Only the income leg changes:

| Account (role) | Party | Debit | Credit |
|---|---|---|---|
| Sales Returns & Allowances (contra income) | | 2792.00 | |
| Trade Receivables (receivable) | Hooli Technologies | | 2792.00 |
| Finished Goods (inventory, Finished Goods Store) | | 1680.00 | |
| COGS - Materials (cost of goods sold) | | | 1680.00 |

Sales Revenue gets no new row. Net sales (Sales Revenue less Sales Returns & Allowances) fall by
2792.00, as in the main case. The stock ledger and the invoice's outstanding amount and status are
as in the main case.

## Must not happen

- A refusal claiming the invoice is paid.
- A refund or payment row: nothing was paid, so nothing is refunded.
