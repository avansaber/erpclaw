# Expected books: how to read these keys

Each file in this folder is one case: the starting books, the request in plain words, and the books
the engine must leave behind. The figures were worked out by hand from the rules below, with no
engine involved, and checked by a separate Decimal-only calculation.

## What a key compares

- **Business rows**, exactly: documents and their lines (invoices, credit notes, orders, deliveries,
  payments and their allocations), ledger postings and their entries, stock ledger rows, valuation
  layers or balances, stock reservations, and the masters (customers, items, warehouses, accounts).
  A test passes only if these match the key exactly. A business row the key does not list must not
  be added, and an existing one the key does not change must stay as it is.
- **Control rows**, by shared expectations rather than per-key figures. Every request the owner
  approves leaves one consumed approval, one committed operation outcome and the audit rows of that
  write. Each posting advances the company's posting chain by one, and each new numbered document
  advances its number series by one. Nothing else in the control tables changes.
- **A request the engine answers with a question** (an unclear request, a missing choice) makes no
  card and leaves no row in any table, business or control.
- **A card that is not carried out**, whatever the approval service records for it (declined,
  expired, superseded by a fresh card because the books changed under it, invalidated), adds no
  business row, no operation outcome and no audit row. Its approval rows are not set by these keys:
  they are as the engine's own card tests specify (a superseded card, for example, is replaced by
  a fresh pending card).
- Rows the assistant keeps in its own conversation store are outside the books.

## Money and quantities

- Every figure is an exact decimal string. Money has two decimal places (USD). Quantities are exact
  and are never rounded to money precision.
- A line amount is `qty x rate`, rounded half-up to 0.01.
- Ledger debits equal credits exactly for every document. No tolerance, no rounding residue.

## Accounts

Keys name accounts by their role and name in the starting books' chart. The test maps each role to
the company's account, so a renumbered chart does not change the key.

| Role | Account in the starting books | Normal balance |
|---|---|---|
| Receivable | Trade Receivables (1121), with the customer as party | debit |
| Sales income | Sales Revenue (4110) | credit |
| Cost of goods sold | COGS - Materials (5110) | debit |
| Inventory, per warehouse | Raw Materials (1131) for Raw Materials Store and Main Warehouse; Finished Goods (1133) for Finished Goods Store | debit |
| Stock received not billed | Stock Received Not Billed, a current liability | credit |
| Bank | Operating Checking Account (1112) | debit |

Perpetual inventory is on: every stock movement posts its value to the ledger in the same document.
Negative stock is refused. No tax template is set in any case, so no tax line is expected, and the
card must say no tax was calculated (not that none is owed).

## Stock valuation

Each item's method is stated in its case.

- **Moving average.** The item carries a quantity and a value. A receipt adds `qty x rate` to the
  value. An issue takes out `round(value x qty_out / qty_on_hand, 0.01)`, and the value left is the
  value before minus that figure, so the remaining value never drifts. The rate (`value / qty`) is
  shown for reading only; tests check quantity and value. Rounding the rate to 0.01 first and
  multiplying is wrong (the moving-average sequence shows the difference).
- **FIFO.** Stock is a queue of layers, oldest first, each with a quantity and a value (its rate is
  `value / qty`). A receipt adds a layer worth `qty x rate` at the end of the queue (merged into the
  last layer if the rate is the same). An issue takes from the oldest layers first. A whole layer
  gives its whole value; part of a layer gives `round(layer value x qty taken / layer qty, 0.01)`
  and the rest of its value stays with the layer.
- **Sales returns** (a credit note that brings stock back) come in at the cost the goods left at,
  allocated cumulatively against the original issue. With the issue's quantity `Q` and value `V`,
  and `R` the quantity returned against it so far, this return included, the return's cost is
  `round(V x R / Q, 0.01)` less the cost of the earlier returns against the same issue. A single
  return costs `round(V x qty / Q, 0.01)`. When everything has come back, the returns add up to `V`
  exactly, so no residue stays in cost of goods sold. Under FIFO the returned stock is a new layer
  at the end of the queue, worth the return's cost. This reverses the cost of goods sold the
  original sale recognised, whatever the current cost is.

## Documents

- Document numbers are not part of a key, except that a new document takes the next number in its
  series and a key refers to existing documents by number.
- Statuses are checked by meaning: draft, submitted (open, nothing paid), partly paid, paid,
  credited (settled by a credit note), cancelled; for orders: to deliver, partly delivered,
  delivered, completed.
- "Today" is the test clock's date, stated in each case.
