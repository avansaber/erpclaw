# Stock contract fixtures, version stock/1

Each JSON file here is one case of the stock contract: the books it starts from, the movements to apply first, one
service call, and what that call must return. The producer (the stock plugin) must give exactly the `output` or the
`refusal`; a consumer (any plugin calling the stock services) must accept it. Both test against these same files, so
a change on either side shows up as a failing fixture, not as a surprise in production.

The figures come from the hand-worked answer keys in `tests/expected_books/`, under the rules in
`tests/expected_books/CONVENTIONS.md`, and were checked by a separate Decimal-only calculation.

## Format

| Field | Meaning |
|---|---|
| `contract` | always `stock/1` |
| `service` | the service called: `stock_on_hand`, `choose_warehouse`, `preview_issue`, `issue`, `preview_return`, `return_in`, `preview_reverse`, `reverse_movements`, or the user-facing `receive_stock` |
| `name` | the file's name, unique |
| `books` | the starting books: `currency`, optional `default_warehouse`, `warehouses` (each with `name` and inventory `account`), `items` (each with `method`, `fifo` or `moving_average`) |
| `before` | movements applied first, in order: `receive` (item, warehouse, qty, rate, date, voucher), `issue` (item, warehouse, qty, date, voucher, optional line), `return` (`against` an earlier step's `as`, qty, date, voucher) |
| `input` | the service's input |
| `output` | the service's response, exactly; or, instead, `refusal`: the refusal `code` and the fields it names |
| `after` | the books after the call, which the producer test checks and a consumer test ignores: the stock `entries` a write service added (each with its symbolic `id`), the `head` of the item and warehouse where given, or `{"unchanged": true}` after a refusal or a preview |
| `note` | why the case exists, and the answer key it comes from |

- **Symbols, not ids.** `@item:…`, `@wh:…`, `@acct:…`, `@v:…` (vouchers), `@head:…` and `@entry:…` stand for rows
  the test creates; the loader maps each symbol to the real id. `@acct:cogs` is the company's cost of goods sold
  account and `@acct:srnb` its stock received not billed account.
- **Every quantity, rate and amount is a string** holding an exact decimal. The only JSON numbers are line ordinals
  (`"line": 1`), which are whole numbers; the loader refuses any other JSON number, and any number with a fraction or
  an exponent.
- **New entries have symbols too.** A write service's response names the entries it added as
  `@entry:<fixture name>#<n>`, in the order it wrote them; the producer test binds each symbol to the id the service
  returned and checks that entry against `after`.
- **Ledger lines** are one per account: cost of goods sold first, then the inventory accounts, then (for a receipt)
  stock received not billed; a zero amount gives no line.
- **A question** (`warehouse_ambiguous`) lists its options by warehouse name, never by id. Its question id is set at
  run time and is not part of the fixture.
- **A reversal's `contribution`** is the net amount, per stock account, that the reversal posting must carry. The
  stock service writes only the stock rows; the ledger side is the reversal of the original posting.

## Changing a fixture

Version `stock/1` is frozen. A change that breaks a consumer is a new version, served beside this one until every
consumer has moved; this folder is then kept until the old version is removed.
