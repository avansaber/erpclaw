# An invoice request that names nothing known

Provenance: adapted from the ERPClaw v2 demo books (Stark Manufacturing Inc.), with one request
from its write pilot. Worked out from scratch under `CONVENTIONS.md`.

## Starting books

Test clock: 2026-10-26. The demo books as in `invoice_stock_from_stocked_warehouse.md`. No customer,
item, invoice or order is named or numbered "ABC" (customers: Acme Corporation, Wayne Industries,
Oscorp Industries, Pied Piper Inc, Hooli Technologies, Stark Retail Outlets, Parker Electronics,
Xavier Laboratories, LexCorp, Daily Planet Media, Initech).

## Request

"invoice ABC"

## Expected books

Empty book diff: no row added or changed in any table. No approval card is made.

The reply is a question: there is no customer or invoice called ABC, and the engine asks who should
be invoiced (or which invoice was meant). It does not claim to have found one.
