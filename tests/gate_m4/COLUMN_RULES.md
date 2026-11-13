# Column comparison rules

The JSON `rules` block classifies storage columns. This inventory specifies the effective comparison for each column. Unmentioned values within JSON and every non-generated scalar are literal. NULL is always literal. Every table, including every empty table, is compared.

Bindings cover every snapshot and event in one trial. UUID and hex bindings are bijective: equal references stay equal and distinct values cannot alias. JSON is parsed structurally; only UUIDs and hex tokens are bound. All JSON keys, cardinalities, other strings and figures are literal. The zero chain origin is literal. A timestamp already bound in the seed must stay unchanged wherever referenced; newly created timestamps must be in the trial window. Expiry timestamps have a 600-second upper allowance. Business dates are literal.

Fixtures use **typed first-appearance placeholders**: `<uuid:1>`, `<hex32:1>`, `<hex64:1>`, and so on. Each type has its own counter starting at 1. Traverse the seed (`before`), then each step's owner, events and books, then `after`; within each object use stored field order and within each array use stored row order. Counters span the whole trial, including embedded JSON and event strings. Equal concrete values reuse a placeholder; distinct values receive distinct placeholders. The loader rejects raw generated values, incorrect types and numbering gaps. Actual captures must contain concrete values of the named shape; both directions of the binding are checked, including across different snapshots and events.

`<hex64:4:prefix16>` denotes the first 16 characters of the already named `<hex64:4>` artifact digest. It is a reference to that full digest, not an independent binding. Implementation versions retain their literal version and `+`, followed by this prefix reference. `<chain-origin>` denotes exactly 64 zeroes: a fixed literal constant, never a generated binding. Its symbolic spelling keeps long hex strings out of fixtures. Timestamps retain the existing window and repeated-reference rules and are stored as captured. All other values remain literal.

Fixture metadata stores the exact allowlisted public-key texts in `public_keys`, rather than digest strings. Qualification compares those texts to the current public files before grading, preserving detection of any key change. Revision hashes remain report metadata only; reports belong in disposable temporary directories.

The storage classification `hex64` is a capacity, not a claim that a request token has 64 characters. Both request-token columns in this capture contain **32** hex characters; `<hex32:N>` enforces their width, along with continuity. Hashes and digests use `<hex64:N>`. `audit_log.detail` is JSON despite lacking the `_json` suffix; its UUID references are bound and every other value is literal.

These fixture replay rules do not authorize control writes. The independent S1 check reads the build's `CONVENTIONS.md` and rejects every fence change unless the exact approval-fence bullet is present. With that bullet, S1 checks one row for the trial conversation after each turn and non-decreasing generation, rather than comparing generation to a key figure. The live token remains opaque, with shape and bijective references checked by the fixture rules and its approval reference checked by S1. No other row may change on a question turn, and no other conversation's fence may change.

| Column | Effective rule |
|---|---|
| `books_identity.id` | Literal |
| `books_identity.books_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `currency.code` | Literal |
| `currency.name` | Literal |
| `currency.minor_units` | Literal |
| `erpclaw_books.mark` | Literal |
| `plugin_install.name` | Literal |
| `plugin_install.version` | Literal |
| `plugin_install.kind` | Literal |
| `plugin_install.installed_at` | Seed/trial window; repeated timestamp values must remain equal |
| `plugin_install.manifest_digest` | 64-hex shape + bijective binding; zero chain origin literal |
| `plugin_install.artifact_digest` | 64-hex shape + bijective binding; zero chain origin literal |
| `plugin_install.after_core_revision` | Literal |
| `plugin_install.status` | Literal |
| `principal.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `principal.name` | Literal |
| `principal.kind` | Literal |
| `principal.disabled` | Literal |
| `principal.grants_version` | Literal |
| `principal.token_sha256` | 64-hex shape + bijective binding; zero chain origin literal |
| `principal.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `company.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `company.name` | Literal |
| `company.functional_currency` | Literal |
| `company.fiscal_year_start_month` | Literal |
| `company.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `company.date_order` | Literal |
| `company.status` | Literal |
| `principal_session.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `principal_session.secret_sha256` | 64-hex shape + bijective binding; zero chain origin literal |
| `principal_session.principal_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `principal_session.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `principal_session.last_seen_at` | Seed/trial window; repeated timestamp values must remain equal |
| `principal_session.idle_expires_at` | Seed/trial window; repeated timestamp values must remain equal |
| `principal_session.expires_at` | Seed/trial window with 600-second expiry allowance; repeated values equal |
| `principal_session.ended_at` | Seed/trial window; repeated timestamp values must remain equal |
| `principal_session.ended_why` | Literal |
| `principal_session.user_agent` | Literal |
| `principal_sign_in.code_sha256` | 64-hex shape + bijective binding; zero chain origin literal |
| `principal_sign_in.principal_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `principal_sign_in.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `principal_sign_in.expires_at` | Seed/trial window with 600-second expiry allowance; repeated values equal |
| `principal_sign_in.used_at` | Seed/trial window; repeated timestamp values must remain equal |
| `principal_sign_in.created_by` | Literal |
| `principal_sign_in.run_id` | Literal |
| `account.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `account.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `account.number` | Literal |
| `account.name` | Literal |
| `account.root_type` | Literal |
| `account.account_type` | Literal |
| `account.is_group` | Literal |
| `account.parent_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `account.frozen` | Literal |
| `account.normal_side` | Literal |
| `approval.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `approval.principal_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `approval.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `approval.conversation_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `approval.contract` | Literal |
| `approval.contract_version` | Literal |
| `approval.policy_version` | Literal |
| `approval.impl_version` | Literal version; 16-hex artifact suffix must equal the bound plugin artifact digest prefix |
| `approval.args_json` | Structural JSON; bound UUID/hex references; all other content literal |
| `approval.read_set_json` | Structural JSON; bound UUID/hex references; all other content literal |
| `approval.card_json` | Structural JSON; bound UUID/hex references; all other content literal |
| `approval.nonce` | 32-hex shape + bijective binding |
| `approval.digest` | 64-hex shape + bijective binding; zero chain origin literal |
| `approval.status` | Literal |
| `approval.operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `approval.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `approval.expires_at` | Seed/trial window with 600-second expiry allowance; repeated values equal |
| `approval.decided_at` | Seed/trial window; repeated timestamp values must remain equal |
| `approval.approved_by` | UUID shape + bijective reference binding (NULL stays NULL) |
| `approval.approver_grants_version` | Literal |
| `approval.decision_json` | Structural JSON; bound UUID/hex references; all other content literal |
| `approval.request_card_token` | 32-hex shape + bijective binding |
| `audit_log.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `audit_log.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `audit_log.at` | Seed/trial window; repeated timestamp values must remain equal |
| `audit_log.actor` | Literal |
| `audit_log.action` | Literal |
| `audit_log.document_type` | Literal |
| `audit_log.document_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `audit_log.approval_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `audit_log.detail` | Structural JSON; bound UUID/hex references; all other content literal |
| `conversation_fence.conversation_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `conversation_fence.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `conversation_fence.generation` | Literal |
| `conversation_fence.live_token` | 32-hex shape + bijective binding |
| `conversation_fence.updated_at` | Seed/trial window; repeated timestamp values must remain equal |
| `dimension_set.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `dimension_set.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `dimension_set.content` | Literal |
| `dimension_set.digest` | 64-hex shape + bijective binding; zero chain origin literal |
| `dimension_set.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `external_ref.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `external_ref.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `external_ref.external_system` | Literal |
| `external_ref.object_type` | Literal |
| `external_ref.local_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `external_ref.external_id` | Literal |
| `external_ref.last_synced_at` | Seed/trial window; repeated timestamp values must remain equal |
| `file_object.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `file_object.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `file_object.sha256` | Literal |
| `file_object.size` | Literal |
| `file_object.media_type` | Literal |
| `file_object.voucher_type` | Literal |
| `file_object.voucher_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `file_object.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `file_object.retention_until` | Literal |
| `file_object.render_inputs` | Literal |
| `fiscal_year.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `fiscal_year.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `fiscal_year.label` | Literal |
| `fiscal_year.start_date` | Literal |
| `fiscal_year.end_date` | Literal |
| `fiscal_year.status` | Literal |
| `item.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `item.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `item.name` | Literal |
| `item.unit` | Literal |
| `item.standard_rate` | Literal |
| `item.kind` | Literal |
| `number_series.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `number_series.series` | Literal |
| `number_series.fiscal_year` | Literal |
| `number_series.next_value` | Literal |
| `outbox.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `outbox.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `outbox.operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `outbox.slot` | Literal |
| `outbox.recipient` | Literal |
| `outbox.subject` | Literal |
| `outbox.body` | Literal |
| `outbox.status` | Literal |
| `outbox.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `outbox.delivered_at` | Seed/trial window; repeated timestamp values must remain equal |
| `outbox.detail` | Literal |
| `outbox.message_id` | Literal |
| `outbox.from_address` | Literal |
| `outbox.from_name` | Literal |
| `outbox.reply_to` | Literal |
| `outbox.current_attempt_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `outbox.next_try_at` | Seed/trial window; repeated timestamp values must remain equal |
| `outbox.tries` | Literal |
| `outbox.first_failed_at` | Seed/trial window; repeated timestamp values must remain equal |
| `outbox.accepted_at` | Seed/trial window; repeated timestamp values must remain equal |
| `party.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `party.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `party.kind` | Literal |
| `party.name` | Literal |
| `party.email` | Literal |
| `party.city` | Literal |
| `party.city_key` | Literal |
| `posting.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `posting.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `posting.seq` | Literal |
| `posting.posting_date` | Literal |
| `posting.voucher_type` | Literal |
| `posting.voucher_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `posting.memo` | Literal |
| `posting.reverses_posting_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `posting.operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `posting.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `posting.created_by` | Literal |
| `posting.prev_hash` | 64-hex shape + bijective binding; zero chain origin literal |
| `posting.hash` | 64-hex shape + bijective binding; zero chain origin literal |
| `posting.hash_format` | Literal |
| `posting.ledger` | Literal |
| `posting_chain_head.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `posting_chain_head.last_seq` | Literal |
| `posting_chain_head.last_hash` | 64-hex shape + bijective binding; zero chain origin literal |
| `principal_grant.principal_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `principal_grant.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `principal_grant.permission` | Literal |
| `stock_voucher.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_voucher.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_voucher.kind` | Literal |
| `stock_voucher.number` | Literal |
| `stock_voucher.posting_date` | Literal |
| `stock_voucher.reason` | Literal |
| `stock_voucher.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `sync_payload.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sync_payload.digest` | 64-hex shape + bijective binding; zero chain origin literal |
| `sync_payload.body` | Literal |
| `system_of_record.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `system_of_record.area` | Literal |
| `system_of_record.mode` | Literal |
| `system_of_record.external_system` | Literal |
| `system_of_record.set_at` | Seed/trial window; repeated timestamp values must remain equal |
| `system_of_record.set_by` | Literal |
| `account_map.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `account_map.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `account_map.external_system` | Literal |
| `account_map.account_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `account_map.external_code` | Literal |
| `fiscal_period.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `fiscal_period.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `fiscal_period.fiscal_year_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `fiscal_period.period_no` | Literal |
| `fiscal_period.start_date` | Literal |
| `fiscal_period.end_date` | Literal |
| `fiscal_period.status` | Literal |
| `fiscal_year_close.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `fiscal_year_close.fiscal_year_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `fiscal_year_close.closing_posting_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `fiscal_year_close.closed_by` | Literal |
| `fiscal_year_close.closed_at` | Seed/trial window; repeated timestamp values must remain equal |
| `ledger_entry.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `ledger_entry.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `ledger_entry.posting_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `ledger_entry.line_no` | Literal |
| `ledger_entry.account_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `ledger_entry.party_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `ledger_entry.debit` | Literal |
| `ledger_entry.credit` | Literal |
| `ledger_entry.currency` | Literal |
| `ledger_entry.rate` | Literal |
| `ledger_entry.functional_currency` | Literal |
| `ledger_entry.functional_debit` | Literal |
| `ledger_entry.functional_credit` | Literal |
| `ledger_entry.kind` | Literal |
| `ledger_entry.dimension_set_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `operation_outcome.key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `operation_outcome.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `operation_outcome.approval_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `operation_outcome.card_digest` | 64-hex shape + bijective binding; zero chain origin literal |
| `operation_outcome.status` | Literal |
| `operation_outcome.result_json` | Structural JSON; bound UUID/hex references; all other content literal |
| `operation_outcome.committed_at` | Seed/trial window; repeated timestamp values must remain equal |
| `outbox_attachment.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `outbox_attachment.outbox_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `outbox_attachment.name` | Literal |
| `outbox_attachment.media_type` | Literal |
| `outbox_attachment.sha256` | Literal |
| `outbox_attachment.file_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `outbox_attempt.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `outbox_attempt.outbox_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `outbox_attempt.started_at` | Seed/trial window; repeated timestamp values must remain equal |
| `outbox_attempt.lease_until` | Seed/trial window; repeated timestamp values must remain equal |
| `outbox_attempt.data_at` | Seed/trial window; repeated timestamp values must remain equal |
| `outbox_attempt.finished_at` | Seed/trial window; repeated timestamp values must remain equal |
| `outbox_attempt.code` | Literal |
| `outbox_attempt.result` | Literal |
| `outbox_attempt.text` | Literal |
| `sales_invoice.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice.number` | Literal |
| `sales_invoice.customer_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice.posting_date` | Literal |
| `sales_invoice.due_date` | Literal |
| `sales_invoice.currency` | Literal |
| `sales_invoice.total` | Literal |
| `sales_invoice.outstanding` | Literal |
| `sales_invoice.status` | Literal |
| `sales_invoice.operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice.submit_operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice.cancel_operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `sales_order.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_order.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_order.number` | Literal |
| `sales_order.customer_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_order.posting_date` | Literal |
| `sales_order.delivery_date` | Literal |
| `sales_order.currency` | Literal |
| `sales_order.total` | Literal |
| `sales_order.status` | Literal |
| `sales_order.operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_order.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `sales_payment.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_payment.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_payment.number` | Literal |
| `sales_payment.customer_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_payment.posting_date` | Literal |
| `sales_payment.currency` | Literal |
| `sales_payment.amount` | Literal |
| `sales_payment.unallocated` | Literal |
| `sales_payment.method` | Literal |
| `sales_payment.reference` | Literal |
| `sales_payment.status` | Literal |
| `sales_payment.operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_payment.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `sales_payment.cancel_operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_goods_received.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_goods_received.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_goods_received.voucher_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_goods_received.supplier_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_goods_received.settlement` | Literal |
| `stock_goods_received.account_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_goods_received.number` | Literal |
| `stock_warehouse.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_warehouse.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_warehouse.name` | Literal |
| `stock_warehouse.account_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_warehouse.is_default` | Literal |
| `stock_warehouse.position` | Literal |
| `stock_warehouse.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `supplier_payable.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payable.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payable.supplier_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payable.source_type` | Literal |
| `supplier_payable.source_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payable.source_number` | Literal |
| `supplier_payable.posting_date` | Literal |
| `supplier_payable.currency` | Literal |
| `supplier_payable.amount` | Literal |
| `supplier_payable.outstanding` | Literal |
| `supplier_payable.status` | Literal |
| `supplier_payable.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `supplier_payment.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment.number` | Literal |
| `supplier_payment.supplier_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment.posting_date` | Literal |
| `supplier_payment.currency` | Literal |
| `supplier_payment.amount` | Literal |
| `supplier_payment.unallocated` | Literal |
| `supplier_payment.account_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment.status` | Literal |
| `supplier_payment.operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment.reverse_operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `sync_journal.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sync_journal.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sync_journal.external_system` | Literal |
| `sync_journal.direction` | Literal |
| `sync_journal.object_type` | Literal |
| `sync_journal.local_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sync_journal.external_id` | Literal |
| `sync_journal.operation_key` | Literal |
| `sync_journal.record_identity` | Literal |
| `sync_journal.payload_digest` | 64-hex shape + bijective binding; zero chain origin literal |
| `sync_journal.status` | Literal |
| `sync_journal.attempt_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sync_journal.at` | Seed/trial window; repeated timestamp values must remain equal |
| `sales_credit_note.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note.number` | Literal |
| `sales_credit_note.invoice_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note.customer_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note.posting_date` | Literal |
| `sales_credit_note.currency` | Literal |
| `sales_credit_note.total` | Literal |
| `sales_credit_note.operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `sales_delivery.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery.number` | Literal |
| `sales_delivery.order_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery.posting_date` | Literal |
| `sales_delivery.operation_key` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `sales_invoice_line.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_line.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_line.invoice_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_line.currency` | Literal |
| `sales_invoice_line.item_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_line.quantity` | Literal |
| `sales_invoice_line.rate` | Literal |
| `sales_invoice_line.amount` | Literal |
| `sales_invoice_line.line_no` | Literal |
| `sales_invoice_line.named_warehouse_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_line.item_kind` | Literal |
| `sales_order_line.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_order_line.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_order_line.order_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_order_line.currency` | Literal |
| `sales_order_line.line_no` | Literal |
| `sales_order_line.item_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_order_line.quantity` | Literal |
| `sales_order_line.rate` | Literal |
| `sales_order_line.amount` | Literal |
| `sales_payment_allocation.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_payment_allocation.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_payment_allocation.payment_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_payment_allocation.invoice_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_payment_allocation.currency` | Literal |
| `sales_payment_allocation.amount` | Literal |
| `stock_head.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_head.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_head.item_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_head.warehouse_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_head.currency` | Literal |
| `stock_head.last_seq` | Literal |
| `stock_head.qty` | Literal |
| `stock_head.value` | Literal |
| `stock_head.last_date` | Literal |
| `stock_item.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_item.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_item.item_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_item.method` | Literal |
| `stock_item.default_warehouse_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_item.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `stock_ledger_entry.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_ledger_entry.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_ledger_entry.item_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_ledger_entry.warehouse_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_ledger_entry.currency` | Literal |
| `stock_ledger_entry.posting_date` | Literal |
| `stock_ledger_entry.seq` | Literal |
| `stock_ledger_entry.direction` | Literal |
| `stock_ledger_entry.qty` | Literal |
| `stock_ledger_entry.value` | Literal |
| `stock_ledger_entry.qty_after` | Literal |
| `stock_ledger_entry.value_after` | Literal |
| `stock_ledger_entry.voucher_type` | Literal |
| `stock_ledger_entry.voucher_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_ledger_entry.line_no` | Literal |
| `stock_ledger_entry.against_entry_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_ledger_entry.reverses_entry_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_ledger_entry.created_at` | Seed/trial window; repeated timestamp values must remain equal |
| `stock_voucher_line.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_voucher_line.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_voucher_line.voucher_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_voucher_line.line_no` | Literal |
| `stock_voucher_line.item_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_voucher_line.warehouse_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_voucher_line.currency` | Literal |
| `stock_voucher_line.qty_moved` | Literal |
| `stock_voucher_line.counted_qty` | Literal |
| `stock_voucher_line.books_qty` | Literal |
| `stock_voucher_line.rate` | Literal |
| `stock_voucher_line.direction` | Literal |
| `stock_voucher_line.value` | Literal |
| `supplier_payment_allocation.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment_allocation.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment_allocation.payment_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment_allocation.payable_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `supplier_payment_allocation.currency` | Literal |
| `supplier_payment_allocation.amount` | Literal |
| `sales_credit_note_line.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_line.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_line.credit_note_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_line.currency` | Literal |
| `sales_credit_note_line.line_no` | Literal |
| `sales_credit_note_line.invoice_line_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_line.item_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_line.quantity` | Literal |
| `sales_credit_note_line.rate` | Literal |
| `sales_credit_note_line.amount` | Literal |
| `sales_delivery_line.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery_line.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery_line.delivery_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery_line.order_line_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery_line.currency` | Literal |
| `sales_delivery_line.warehouse_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery_line.quantity` | Literal |
| `sales_delivery_line.cost` | Literal |
| `sales_delivery_line.stock_entry_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_delivery_line.stock_line` | Literal |
| `sales_invoice_issue.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_issue.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_issue.invoice_line_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_issue.currency` | Literal |
| `sales_invoice_issue.warehouse_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_issue.quantity` | Literal |
| `sales_invoice_issue.cost` | Literal |
| `sales_invoice_issue.stock_entry_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_issue.stock_line` | Literal |
| `stock_layer.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer.entry_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer.item_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer.warehouse_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer.currency` | Literal |
| `stock_layer.qty` | Literal |
| `stock_layer.value` | Literal |
| `stock_layer.group_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_return.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_return.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_return.credit_note_line_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_return.currency` | Literal |
| `sales_credit_note_return.original_voucher_type` | Literal |
| `sales_credit_note_return.original_voucher_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_return.original_line` | Literal |
| `sales_credit_note_return.warehouse_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_credit_note_return.quantity` | Literal |
| `sales_credit_note_return.cost` | Literal |
| `sales_credit_note_return.stock_entry_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_delivery.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_delivery.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_delivery.invoice_line_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_delivery.delivery_line_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `sales_invoice_delivery.quantity` | Literal |
| `stock_layer_take.id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer_take.company_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer_take.entry_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer_take.group_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer_take.layer_id` | UUID shape + bijective reference binding (NULL stays NULL) |
| `stock_layer_take.currency` | Literal |
| `stock_layer_take.qty` | Literal |
| `stock_layer_take.value` | Literal |
| `stock_layer_take.kind` | Literal |
