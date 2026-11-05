# A customer pays its one open invoice by bank transfer

Provenance: adapted from the ERPClaw v2 demo books (Stark Manufacturing Inc.), with one request
from its write pilot. Worked out from scratch under `CONVENTIONS.md`.

## Starting books

Test clock: 2026-10-26. The company's default bank account is Operating Checking Account (1112);
Savings Account (1113) also exists.

| Invoice | Customer | Posted | Grand total | Outstanding | Status |
|---|---|---|---|---|---|
| INV-2026-00004 | Pied Piper Inc | 2026-01-20 | 2235.00 | 2235.00 | submitted |

INV-2026-00004 is Pied Piper's only invoice with anything outstanding. Its ledger at posting: Trade
Receivables Dr 2235.00 (party Pied Piper Inc), Sales Revenue Cr 2235.00, plus its cost of goods.

## Request

"Pied Piper paid its open invoice today by bank transfer."

## Expected books

New payment received, party Pied Piper Inc, date 2026-10-26, mode bank transfer, paid 2235.00 into
Operating Checking Account, allocated 2235.00 to INV-2026-00004, unallocated 0.00.

| Account (role) | Party | Debit | Credit |
|---|---|---|---|
| Operating Checking Account (bank) | | 2235.00 | |
| Trade Receivables (receivable) | Pied Piper Inc | | 2235.00 |

INV-2026-00004: outstanding 0.00, status paid. Its own ledger rows are unchanged.

Stock ledger: no rows.

## Variant: no default bank account

Same books, but the company has no default bank account. Expected: empty book diff, and the engine
asks which bank account received the money (Operating Checking Account or Savings Account).
