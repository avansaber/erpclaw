# Stock setup fixtures, stock/2

These 22 cases add the five stock setup services.
A stock/2 consumer also retains every stock/1 case unchanged. The setup services
carry no money; stock/1 on-hand values in after assertions keep their currency scale.

The top-level shape and symbolic IDs follow stock/v1. Additional starting-books
fields are accounts (extra inventory account symbols), frozen (account symbols),
service_items (core item symbols not yet tracked), and each warehouse's optional
company_default boolean. @acct:bank denotes 1100 Bank. Unknown item/warehouse
symbols resolve to fresh IDs absent from the books.

after.warehouses is list_warehouses' ordered row list. after.stock_items maps an
item symbol to method/default_warehouse. after.kind maps an item to its core kind.
after.on_hand maps an item to the complete stock_on_hand output. @wh:new#1 is the
first warehouse created by the call. Refusal fields are asserted as supplied.

Producer execution, database state assertions, and the stock setup consumer acknowledgement
remain deferred under the current units-only policy; pure fixture validation does not
constitute those acceptance checks. No stock/1 fixture is changed.
