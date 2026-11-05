# A credit note for part of a partly paid invoice, after the item's cost has changed

Provenance: worked out from scratch under `CONVENTIONS.md`.

## Starting books

Test clock: 2026-04-08. A fresh company, USD, perpetual inventory on. Item Circuit Board (unit Nos,
**moving average**), Main Warehouse (inventory account Raw Materials). Customer Wayne Industries.

| Date | Document | Effect |
|---|---|---|
| 2026-03-31 | receipt | 40 Circuit Board @ 15.00 = 600.00 in (Raw Materials Dr / Stock Received Not Billed Cr 600.00) |
| 2026-04-01 | INV-A, submitted, stock moved | Wayne, 12 Circuit Board @ 24.99 = 299.88. Trade Receivables Dr / Sales Revenue Cr 299.88; COGS Dr / Raw Materials Cr 180.00 (12 @ 15.00) |
| 2026-04-03 | payment received | 100.00 from Wayne into Operating Checking, allocated to INV-A (bank Dr / Trade Receivables Cr 100.00) |
| 2026-04-06 | receipt | 20 Circuit Board @ 18.30 = 366.00 in (Raw Materials Dr / Stock Received Not Billed Cr 366.00) |

Stock now: 48 on hand, value 786.00 (current rate 16.375). INV-A: outstanding 199.88, status partly
paid.

## Request

"Wayne sent back 5 of the 12 circuit boards on INV-A. Make a credit note."

## Expected books

New credit note against INV-A, customer Wayne Industries, posting date 2026-04-08, status submitted,
stock moved by the credit note.

| Line | Item | Qty | Rate | Amount | Warehouse |
|---|---|---|---|---|---|
| 1 | Circuit Board | -5 | 24.99 | -124.95 | Main Warehouse |

Grand total -124.95, applied to INV-A.

Ledger (one document, balanced):

| Account (role) | Party | Debit | Credit |
|---|---|---|---|
| Sales Revenue (sales income) | | 124.95 | |
| Trade Receivables (receivable) | Wayne Industries | | 124.95 |
| Raw Materials (inventory) | | 75.00 | |
| COGS - Materials (cost of goods sold) | | | 75.00 |

Stock ledger (returned at the cost the goods left at, 180.00 / 12 = 15.00, not the current 16.375):

| Item | Warehouse | Qty change | Value change | Qty after | Value after |
|---|---|---|---|---|---|
| Circuit Board | Main Warehouse | 5 | 75.00 | 53 | 861.00 |

INV-A: outstanding 299.88 - 100.00 - 124.95 = 74.93, status partly paid. The payment and its
allocation are unchanged.

Known wrong figure: valuing the return at the current rate (5 x 16.375) gives 81.88.

## Must not happen

- A refund row: the credit (124.95) is less than the outstanding amount (199.88), so it reduces the
  invoice and nothing is paid out.
