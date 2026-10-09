# Buying commitment worksheet

`add-commitment-worksheet --company-id C --worksheet-json '<JSON>'` saves a
calculation in the creation audit. `get-commitment-worksheet --company-id C
--worksheet-id W` reads the original snapshot without recalculation.

Review and supply the budget, actual expenditure, fund and award references.
These inputs are not looked up in a fund ledger or certified as complete.
Requisition estimates use reviewed unit rates. Purchase orders use their stored
net lines and tax, in company currency at exchange rate one. A supplied fund or
award tag on an order must match the worksheet. Untagged documents use the
operator's reviewed allocation. The fiscal year must belong to the company;
order dates and requisition creation dates must be inside that year.

Example structure, replacing all IDs with existing documents:

```json
{
  "fund_reference": "GENERAL",
  "award_reference": "AWARD-2026",
  "fiscal_year_id": "existing-year-id",
  "budget_amount": "1000.00",
  "actual_amount": "100.00",
  "requisitions": [{
    "material_request_id": "existing-request-id",
    "rates": [{"material_request_item_id": "existing-request-line-id", "unit_rate": "10.01"}]
  }],
  "purchase_orders": [{
    "purchase_order_id": "existing-order-id",
    "requisition_links": [{
      "purchase_order_item_id": "existing-order-line-id",
      "material_request_item_id": "existing-request-line-id"
    }]
  }]
}
```

Only submitted purchase requisitions and non-draft orders are accepted.
Every selected requisition line needs a rate. Each order must explicitly state
its requisition links, using an empty array if unrelated. Linked lines must
match item and UOM, and the linked order quantities must equal each request
line's recorded ordered quantity. This refuses an incomplete link set rather
than counting the same purchase twice. Include only orders allocated wholly to
the selected fund and award; split allocation is not supported.

Pre-encumbrance equals each request's unordered quantity times its reviewed
rate. Remaining order commitments equal net lines plus allocated tax, reduced
by the greater of submitted receipt quantity or non-return submitted invoice
quantity for that line. Receipt and invoice relief is not added together.
The effective receipt or invoice UOM must match the order line; a missing UOM
uses the item's stock UOM. Mixed-unit quantities refuse rather than being added.
Draft and cancelled receipts/invoices do not relieve orders. Closed or cancelled
orders contribute zero commitment. Tax is allocated proportionally to net lines
in a deterministic line order, with the rounding remainder on the last line.
An exhausted budget is reported with a negative available balance; it does not
refuse, reserve funds or prevent an order from being submitted.

This is a current-document calculation snapshot. Review actual expenditure to
include relieved commitments as appropriate. It is neither a historical
as-of report nor a legal compliance determination. New calls create independent
snapshots, not cumulative reservations. It posts no GL, stock or payment entry.
Automatic reservation and relief, replacement of the GL budget check, verified
fund/award budget allocation, year-end carry-forward and lapse remain unimplemented.
