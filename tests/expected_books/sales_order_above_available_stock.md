# A sales order for more than the stock on hand

Provenance: adapted from the ERPClaw v2 demo books (Stark Manufacturing Inc.), with one request
from its write pilot. Worked out from scratch under `CONVENTIONS.md`.

## Starting books

Test clock: 2026-10-26.

| Item | Unit | Method | Selling rate |
|---|---|---|---|
| Circuit Board | Nos | moving average | 15.00 |

| Warehouse | Circuit Board on hand | Value |
|---|---|---|
| Raw Materials Store | 300 (200 opening on 2026-01-01, 100 received 2026-01-12, both at 15.00) | 4500.00 |
| every other warehouse | 0 | 0.00 |

No open sales order, reservation or draft holds any Circuit Board. Customer Wayne Industries exists
(one match for "Wayne").

## Request

"Wayne wants 500 Circuit Board tomorrow; book the order."

The card states the shortfall before approval: 300 available, 200 short. The owner still decides;
on approval the order is submitted for the full 500.

## Expected books

New sales order, customer Wayne Industries, order date 2026-10-26, delivery date 2026-10-27,
status submitted (to deliver).

| Line | Item | Qty | Rate | Amount |
|---|---|---|---|---|
| 1 | Circuit Board | 500 | 15.00 | 7500.00 |

Totals: net 7500.00, tax 0.00, grand total 7500.00. Delivered 0, invoiced 0.

Ledger: no rows. Stock ledger: no rows. Stock reservations: none.

## Variant b: selling rate above cost

Same books and request, except that Circuit Board's selling rate is 22.00 (its cost stays 15.00).
The order line is 500 @ 22.00 = 11000.00, and the totals are net 11000.00, tax 0.00, grand total
11000.00. The card shows the same shortfall (300 available, 200 short). There are still no ledger,
stock or reservation rows.

## Must not happen

- The order quantity saved as anything but 500 (50 is the known wrong answer).
- A refusal because stock is short, or an order cut down to 300 without the owner asking.
- Any stock movement or reservation.
