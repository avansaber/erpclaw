# A sales order delivered in two parts, then invoiced once

Provenance: worked out from scratch under `CONVENTIONS.md`.

## Starting books

A fresh company, USD, perpetual inventory on. Item Bearing 6205 (unit Nos, **FIFO**), Main
Warehouse (inventory account Raw Materials). Customer Oscorp Industries.

| Date | Document | Effect |
|---|---|---|
| 2026-05-01 | receipt | 60 @ 6.25 = 375.00 in (Raw Materials Dr / Stock Received Not Billed Cr) |
| 2026-05-04 | receipt | 50 @ 6.70 = 335.00 in (Raw Materials Dr / Stock Received Not Billed Cr) |
| 2026-05-10 | SO-A, submitted | Oscorp, 80 Bearing 6205 @ 9.95 = 796.00, delivery 2026-05-15. No ledger or stock rows |

Stock now: 110 on hand, layers 60 @ 6.25, 50 @ 6.70, value 710.00.

## Requests, in order, each approved

| Step | Date | Request |
|---|---|---|
| 1 | 2026-05-12 | "Ship 50 of Oscorp's bearings on SO-A today." |
| 2 | 2026-05-15 | "Ship the rest of SO-A." |
| 3 | 2026-05-15 | "Invoice Oscorp for SO-A and submit." |

## Expected books

| Step | Document | Lines | Ledger | Stock |
|---|---|---|---|---|
| 1 | delivery against SO-A | 50 Bearing 6205 | COGS - Materials Dr / Raw Materials Cr 312.50 | -50, -312.50 (50 @ 6.25); after: 60, 397.50, layers 10 @ 6.25, 50 @ 6.70 |
| 2 | delivery against SO-A | 30 Bearing 6205 (the 80 ordered less the 50 delivered) | COGS - Materials Dr / Raw Materials Cr 196.50 | -30, -196.50 (10 @ 6.25 + 20 @ 6.70); after: 30, 201.00, layer 30 @ 6.70 |
| 3 | invoice against SO-A and both deliveries, stock not moved again | 80 Bearing 6205 @ 9.95 = 796.00 | Trade Receivables Dr (party Oscorp) / Sales Revenue Cr 796.00 | none |

SO-A after each step:

| After step | Delivered | Invoiced | Status |
|---|---|---|---|
| 1 | 50 of 80 (62.5%) | 0 | partly delivered |
| 2 | 80 of 80 | 0 | delivered, to invoice |
| 3 | 80 of 80 | 80 of 80, 796.00 | completed |

Invoice after step 3: grand total 796.00, outstanding 796.00, status submitted.

## Balances at the end

| Account | Debit | Credit |
|---|---|---|
| Raw Materials (inventory) | 201.00 | |
| Trade Receivables (Oscorp) | 796.00 | |
| COGS - Materials | 509.00 | |
| Stock Received Not Billed | | 710.00 |
| Sales Revenue | | 796.00 |
| **Total** | **1506.00** | **1506.00** |

## Must not happen

- Step 2 shipping anything but 30.
- Step 3 moving stock or posting cost of goods a second time.
