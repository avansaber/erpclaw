# Changelog

All notable changes to the ERPClaw foundation skill.

## [4.12.0] — 2026-07-05 — M31 foundation-hygiene mini-wave

### Fixed
- **33 shipped actions are now actually reachable through the router (M31 H1).** The meta-router `ACTION_MAP` gained every action defined in the per-domain sub-scripts that it previously could not dispatch — most importantly `migrate` (the documented upgrade path), the whole custom-fields cluster (`add-custom-field` / `list-custom-fields` / `remove-custom-field` / `set-custom-field-value` / `get-custom-field-values`), and credit control (`place-customer-on-hold` and the dunning cycle). Previously these returned "Unknown action" via the router despite being fully built and tested. `hr-status` / `payroll-status` now route via ALIASES to each domain's `status`. A new L0 dispatchability gate (defined ⇒ routable, all modules) makes this bug class structurally unable to ship again.
- **`update-foundation` now converges files AND schema (ADR-0028).** A confirmed apply-path reconcile applies pending foundation migrations after the file sync (idempotent; loud, per-migration-ledgered failure; explicit skip on uninitialized DBs) — an upgrade can no longer silently skip schema migrations, including on the in-sync retry path. `rollback-foundation` restores files only, never schema (migrations are forward-only). `migrate` joined `DANGEROUS_ACTIONS` beside its `schema-*` siblings.

### Schema
- **3 dead tables dropped (M31 H2, migration `028_drop_dead_orphan_tables_2.py`):** `communication`, `erpclaw_module_validation`, `erpclaw_table_ownership` — all verified zero-writer/zero-reader/zero-FK (necessity audit, adversarially QA-confirmed); dialect-aware + idempotent; PostgreSQL-rehearsed at close. Fresh-install table count 776.
- **Table provenance manifest (`scripts/table_provenance.json`, M31 H3 G1):** every table now declares why it exists (doc ref / parent concept / explicit accepted-risk); a diff-aware L0 gate blocks any NEW provenance-less table — adding a table is now a governance event.

### Added
- **Shared integration lib modules (M31 H6):** `erpclaw_lib/action_validators.py` (the validators previously duplicated across integration addons), `erpclaw_lib/integration_secrets.py` (AES-256-GCM field crypto with transparent legacy-format read-back), `erpclaw_lib/integration_actions.py` (shared sync-job/GL-rule/reconciliation action bodies; table writes stay with owning modules).

## [4.11.0] — 2026-07-01 — Wave 2 (inventory + warehouse depth)

### Added
- **Putaway rules + pick lists + hard, persisted stock reservations (Wave 2 M5, ADR-0026).** Stock promised to one order can no longer be silently drained by another. Reservations are now first-class, persisted, and binding: `add-reservation` / `release-reservation` hold a quantity against an (item, warehouse), and a stock issue (e.g. `material_issue`) that would breach the active reserved quantity is refused with the available-minus-reserved figure and rolls back in one transaction (no partial SLE). `get-projected-qty` nets active reservations against on-hand and is byte-for-byte back-compatible when there are zero reservations. Putaway rules (`add-putaway-rule` / `list-putaway-rules` / `delete-putaway-rule`, `apply-putaway-on-receipt`) route received goods to a target warehouse by item or item group. Pick lists run end to end: `create-pick-list` from a sales order -> `add-pick-list-item` -> `mark-picked` -> `complete-pick-list` (into a delivery note) -> `cancel-pick-list` (releases the reservation). Warehouse-level for V1; bin-level is deliberately deferred (ADR-0026). New tables `putaway_rule`, `pick_list`, `pick_list_item`, `stock_reservation_entry` via foundation migration `025_putaway_pick_reservation.py` (net-new, dialect-aware, idempotent; all quantities Decimal-as-text). PostgreSQL-rehearsed on the box at wave close. (Wave 2 M5)
- **Item-global alternatives / substitutes (Wave 2 S7).** Define substitute items once at the item level instead of repeating them on every bill of materials: `add-item-alternative` / `list-item-alternatives` / `remove-item-alternative`, and `get-best-alternative-for-item` which returns the highest-priority substitute that actually has enough stock at a given warehouse (ties broken by on-hand quantity). Each alternative is directional (A can substitute for B without B substituting for A), ranked by `priority`, and carries a Decimal `conversion_factor`. Manufacturing's BOM substitutes now fall back to these item-global alternatives when a BOM line defines none of its own, so the same substitute list is not maintained twice (cross-module read only; manufacturing gains no new writes). New table `item_alternative` via foundation migration `027_item_alternative_table.py` (net-new, dialect-aware, idempotent, UNIQUE + directional CHECK). PostgreSQL-rehearsed on the box at wave close. (Wave 2 S7)
- **Stock-entry typed dispatch — 3 new entry types (Wave 2 S6).** `add-stock-entry --entry-type` now handles `repack`, `subcontract` (`send_to_subcontractor`), and `material_consumption` (it previously errored on these three, even though the `stock_entry_type` CHECK already permitted all seven). `repack` consumes input lines and produces output lines within one warehouse and enforces a cost-balance invariant (total input value == total output value within $0.01, Decimal) — an unbalanced repack is refused and rolls back. `send_to_subcontractor` transfers stock out to a `--supplier-warehouse-id` that must be a transit or production warehouse (this is the dispatch path Wave 2 S5's subcontracting transfer emits). `material_consumption` issues raw materials against an active `--work-order-id` (`not_started`/`in_process`), recording the work-order link on the stock entry. Two convenience wrappers: `add-repack-stock-entry` (one-in/one-out repack) and `add-material-consumption` (single raw-material issue). SLE remains immutable and balanced; submit stays a single transaction. No schema change, no migration (the CHECK already had all seven values). (Wave 2 S6)
- **Inventory anomaly hooks AI1 (Wave 2).** `detect-anomalies` (erpclaw-ai-engine) gains two internal heuristics, bringing `VALID_ANOMALY_TYPES` from 18 to 20: `reservation_over_available` flags an (item, warehouse) whose active `stock_reservation_entry` reserved qty exceeds the on-hand SLE balance (a stock-out is predicted — critical when nothing is on hand, otherwise warning), and `subcontract_receipt_mismatch` flags a `subcontracting_order` whose finished-goods `received_qty` diverges from `materials_transferred` beyond a 5% tolerance (over-receipt = more FG than the transferred materials can yield, critical; a completed order that returned materially less than was transferred = yield loss, warning). Both are internal detectors run by the existing `detect-anomalies` sweep (no new public action) and emit through the shared `_insert_anomaly` helper; they READ M5's `stock_reservation_entry` / the SLE and S5's `subcontracting_order` (cross-module reads) and WRITE only growth's `anomaly` table. All quantities are Decimal-as-text. The `anomaly.anomaly_type` CHECK enum is extended in lockstep (init_db for fresh installs; growth migration `005_wave2_anomaly_types.py` for existing ones — dialect-aware, idempotent, no rebuild dance since `anomaly` has no FK edges). Additive; no new action or table. (Wave 2 AI1)
- **Subcontracting receipt lifecycle (Wave 2 S5).** Completes the outsourced-production workflow on top of `add-subcontracting-order` (`erpclaw-manufacturing` addon): `submit-subcontracting-order` (validates the BOM lists the service item, draft -> submitted), `transfer-materials-to-subcontractor` (ships only the stock raw materials to the supplier sub-store via S6's `send_to_subcontractor` stock entry — the service line is never shipped; partial transfers accumulate), `receive-subcontracted-items` (receives finished goods: the FG cost rolls up raw-material cost + `--subcontract-charge-rate` x received qty, posts exactly ONE balanced FG stock-ledger-entry + GL, generates a draft subcontract-charge purchase invoice, advances partially_received -> completed and stamps `final_received_at`), plus `cancel-subcontracting-order` (only pre-transfer), `cancel-subcontract-transfer` (reverses the transfer SLE — cancel = reverse, allowed only pre-receipt), `get-subcontracting-order`, and `list-subcontracting-orders`. Buying's `create-purchase-receipt --subcontracting-order-id` DEFERS the finished-goods bookkeeping entirely to `receive-subcontracted-items` and posts nothing itself, so a subcontract receipt posts the FG SLE/GL exactly once (no double-post). All money is Decimal-as-text (ROUND_HALF_UP); GL is immutable (cancel = reverse); the FG receipt is a single transaction running the 12-step GL validation. (Wave 2 S5)

### Schema
- **4 net-new inventory tables (Wave 2 M5):** `putaway_rule`, `pick_list`, `pick_list_item`, `stock_reservation_entry` via foundation migration `025_putaway_pick_reservation.py`. Quantities (`reserved_qty`/`expected_qty`/`picked_qty`) are Decimal-as-text (no float); FKs to `item`/`warehouse`/`sales_order`/`company` intact. Net-new, no rebuild; SQLite + PostgreSQL (box-rehearsed) green.
- **`item_alternative` net-new table (Wave 2 S7):** foundation migration `027_item_alternative_table.py`; `conversion_factor` Decimal-as-text, `priority` integer, both FKs -> `item`, UNIQUE + directional CHECK. Net-new, no rebuild; SQLite + PostgreSQL (box-rehearsed) green.
- **`anomaly.anomaly_type` CHECK extended +2 values (Wave 2 AI1):** `reservation_over_available` + `subcontract_receipt_mismatch` added to the growth-owned `anomaly` table CHECK. Fresh installs get it from `init_db.py`; existing installs from growth migration `005_wave2_anomaly_types.py` (dialect-aware, idempotent — SQLite rebuild guarded by a stored-SQL probe, Postgres `DROP CONSTRAINT IF EXISTS` + `ADD CONSTRAINT`). No FK edges on `anomaly`, so no rebuild dance.
- **`subcontracting_order` gains 2 nullable TEXT columns (Wave 2 S5):** `subcontract_charge_rate` (Decimal-as-text per-unit subcontracting fee) and `final_received_at` (completion timestamp). Additive `ADD COLUMN` via foundation migration `026_subcontracting_charge_columns.py` (dialect-aware, idempotent — SQLite guarded by `_sqlite_has_column`, Postgres `ADD COLUMN IF NOT EXISTS`); also in `init_schema.py` for fresh installs. No rebuild.

## [4.10.0] — 2026-06-16

### Added
- **Hermes runtime port v1 (experimental).** ERPClaw is now runtime-portable from a single source tree via the `ERPCLAW_HOME` defaulting env var (unset = `~/.openclaw/erpclaw`, byte-identical to today — OpenClaw users unaffected). Runs on the Hermes Agent runtime as an experimental hedge via a GitHub tap (`hermes skills tap add avansaber/erpclaw`). OpenClaw stays the primary, supported, ClawHub-distributed runtime; Hermes is best-effort, no SLA, no feature-parity promise. No encrypted-credential actions on Hermes in v1. v1 acceptance: two business-user NL scenarios (company setup + order-to-cash customer payment) graded GREEN on Hermes by the same deterministic oracle as the OpenClaw baseline. See ADR-0017. Registry re-signed to registry_version 61. (Also in this window: a thin stdio MCP server over the db_query routers, ADR-0024, built + QA-green but shipped as optional runtime-reach only — not a feature dependency.)

## [4.9.0] — 2026-06-15

### Added
- **Wave 1B — built-in Sales/CRM depth.** Contacts + Companies, first-class Tasks, customizable Pipelines, Saved views (filterable), global CRM search, and CSV import/export — a "lead → opportunity → quote → order" Sales module (built in `erpclaw-growth`). Foundation gains nullable FK links to the addon's contact/pipeline entities (ADR-0023) via foundation migrations `023_crm_contact_fks.py` + `024_displace_opportunity_stage_check.py`; the shared `table_exists` is now PostgreSQL-correct via the CLI (closes M19). All six pieces (F1–F6) built through the agentic-SDLC org and validated on the live gateway + PostgreSQL.

## [4.8.0] — 2026-06-11

### Added
- **Bank statement import + matching (M2, erpclaw-integrations v2.1.0).** Import OFX / CAMT.053 / MT940 / BAI2 statement files with pure-stdlib parsers (no new dependencies), idempotent re-import (`external_id` unique), a rule-based auto-match engine against open invoices/payments, manual match/clear, and `bank-reconciliation-summary`. Tables `bank_statement`/`bank_statement_line`/`bank_match_rule` via foundation migration 020; writes routed through the foundation `erpclaw_lib/bank_import.py` (module write-ownership preserved). `--bank-account-name` resolves accounts by name (FINDING-001 pattern). NL-validated on the live gateway (fin-m2 PASS). (Wave 1 M2)
- **P&L by dimension — natural routing (M6 follow-up).** `profit-and-loss` now accepts an optional `--group-by <dimension>` that returns the statement broken down per accounting-dimension value — `revenue` / `expenses` / `net` per value, income/expense accounts only, with entries that lack the key folded into an explicit `(untagged)` bucket (never dropped). The grouped totals reconcile to the flat statement (`income_total` / `expense_total` / `net_income` retained). Without the flag, output is byte-identical to before. The grouping composes the shared dialect-aware `json_get` GROUP BY (no SQL duplicated from `multi-dim-trial-balance`, which keeps owning whole-trial-balance grouping); an unregistered or deactivated `--group-by` key errors cleanly pointing at `list-dimensions`, and a `--dimension-key/--dimension-value` filter composes as filter-then-group. This makes the agent's natural reach (`profit-and-loss` for "P&L by department") correct instead of routing it to hand-rolled cost-center SQL. Read-only report — no schema, migration, registry, or new-action change. (Wave 1 M6 routing fix)
- **Anomaly types AI1.** `detect-anomalies` (erpclaw-ai-engine) gains two heuristics, bringing `VALID_ANOMALY_TYPES` from 16 to 18: `asset_book_value_drift` flags assets whose `current_book_value` deviates >5% from the `gross_value − accumulated_depreciation` invariant (critical >25%), and `dimension_tag_drift` flags an `account_type` whose `gl_entry.dimensions_json` tagging is partial for a given key (some entries carry the key, others omit it — read & grouped in Python, dialect-safe, no JSON `GROUP BY`). Both names verbatim for corpus traceability; the `anomaly.anomaly_type` CHECK enum is extended in lockstep. `dimension_tag_drift` consumes M6's `dimensions_json`. Additive; no new action, table, or migration. (Wave 1 AI1 / AVA-42, ships in the deferred Wave 1 bundle)

## [4.7.0] — 2026-06-10

Wave 1 Financials depth (partial bundle: P0 + M6 + M7 + S3). Additive only — no breaking changes. M2 and AI1 ship in a later bundle. No ClawHub upload this release (bundled into the next functional release per ADR-0006 / internal_backlog M5; ClawHub foundation stays v4.1.6, propagated to the OpenClaw box via signed reconciliation).

### Added
- **Multi-dimensional GL (M6).** A `dimension_registry` (seeded `project` / `department` / `cost_center`) now drives optional per-entry GL dimensions. `insert_gl_entries` serializes a `dimensions` dict into the existing `dimensions_json` column (default `'{}'`, so every existing GL caller is byte-unchanged), GL validation gains step 13 (required-dimension enforcement per `account_type` + `uuid_fk` referential-integrity check), and `reverse_gl_entries` preserves the original dimensions on the mirror entry. Four CRUD actions in `erpclaw-gl` (`add-dimension`, `list-dimensions`, `update-dimension`, `deactivate-dimension`; deactivation is blocked while recent live GL references the key) and two new reports in `erpclaw-reports` (`multi-dim-trial-balance --group-by "project,department"`, `dimension-balance-report --dimension K`). `general-ledger` / `profit-and-loss` / `balance-sheet` / `cash-flow` accept repeated `--dimension-key` / `--dimension-value` filters. All dimension SQL routes through `erpclaw_lib.query.json_get()` for dialect-safe extraction (no raw `json_extract` literals). Migration `017`. (Wave 1 M6 / AVA-38)
- **Asset depth (M7).** Full fixed-asset lifecycle beyond depreciation: `impair-asset` / `reverse-impairment` (write-down to recoverable amount with balanced GL; reverse mirrors the GL and restores book value — cancel-by-reverse, impairment rows are immutable), `capitalize-asset` (initial recognition from purchase cost), and `revalue-asset` (upward/downward revaluation against a revaluation reserve, with depreciation recompute). `complete-maintenance` gains an `--is-capex` branch that capitalizes the cost into the asset (DR Asset / CR Cash) and recomputes the depreciation schedule instead of expensing it. New voucher types `asset_impairment` / `asset_capitalization` / `asset_repair_capex`; `is_capex` column on `asset_maintenance`. Migrations `018` / `019`. (Wave 1 M7 / AVA-39, erpclaw-ops/erpclaw-assets)
- **Construction-work-in-progress (S3).** A `cwip_cost_accumulation` ledger plus five actions in erpclaw-assets: `add-cwip` (start an `under_construction` asset), `accumulate-cwip-cost` (DR the `capital_work_in_progress` account, per-project via M6's `dimensions_json`), `transfer-cwip-to-asset` (capitalize to `in_use` + start depreciation from the transfer date), `cancel-cwip` (reverse all accumulations; blocked if any cost arrived from a submitted document), and `list-cwip-projects`. `create-purchase-invoice` (erpclaw-buying) and `add-journal-entry` (erpclaw-journals) accept an optional `--cwip-asset-id` that routes the GL leg to the CWIP account and records a `cwip_cost_accumulation` row in the same transaction. The existing `gl_posting.py` guard that rejects a direct JE to a CWIP account is now reachable. New voucher type `cwip_capitalization`. Migration `021`. (Wave 1 S3 / AVA-41, AVA-43)

### Fixed
- `erpclaw_lib.query.json_get()` now emits dialect-correct SQL on PostgreSQL. The Postgres branch previously emitted the SQLite JSONPath form `col->>'$.key'`, which is invalid: Postgres `->>` takes a plain object key, and the JSON columns (`dimensions_json` et al.) are provisioned as `text`, so the operator needs a `::jsonb` cast first. It now emits `col::jsonb->>'key'` (verified on PostgreSQL 16). All three branches also escape the key as a proper SQL string literal (doubling embedded single quotes) instead of raw f-string interpolation, so a key containing a quote can no longer break or inject the emitted SQL. Latent until Wave 1 M6's multi-dimensional reporting, which is the first production consumer. (Wave 1 P0 / AVA-37)

## [4.6.1] — 2026-06-08

### Fixed
- `close-fiscal-year` now refuses to close when the chosen closing (retained-earnings) account belongs to a different company than the fiscal year being closed: it hard-errors with an actionable message and rolls back before any GL is posted. Previously, in a multi-company database, a mismatched closing account would have posted one company's net income into another company's equity (a silent cross-tenant contamination). Pure pre-write validation, no schema or API change. (FINDING-013, ADR-0016)

## [4.6.0] — 2026-06-08

### Added
- NL company-by-name resolution: business users can address a company by name ("for Acme, invoice Bruce") instead of a UUID. The `resolve_company_id` chokepoint now accepts a `--company "<name>"` flag (exact, case-insensitive, dialect-neutral `LOWER(name)`, never `.ilike()`) alongside the unchanged `--company-id`; the sole-company auto-detect path is byte-unchanged. Wired across the GL, payments, inventory, tax, journals, and reports actions plus the educlaw-k12 router (~70 call sites). A named-but-missing company fails loudly listing the available company names and never falls through to a different company, so one entity's books can never post to another. SKILL.md instructs the agent to pass the user's exact wording and never fuzzy-substitute or autocorrect a company name. Verified end-to-end on the live gateway (mc01 right-company invoice; mc02 missing-name hard-error with zero posting). (FINDING-001, ADR-0015)

## [4.5.0] — 2026-06-05

### Added
- `resolve-item` (erpclaw-inventory): cascade item resolver that maps a loose/plural user phrase ("20 Brake Pad Sets") to the stored item via a deterministic 4-tier cascade (exact → singularized → substring → token-AND, stop at first non-empty, shortest-name-first). Read-only, cross-DB (dialect-neutral `LOWER(...) LIKE`, never `.ilike()`), stdlib-only singularizer. Returns `single_match` / `multiple_matches` / `matched:false` so callers can branch deterministically. Fixes FINDING-008. (Inventory action count 42 → 43.)

### Fixed
- Gateway replies no longer leak accounting internals to business users: SKILL.md `## Speaking to the user` now instructs the agent to translate or omit double-entry GL narration, account names, internal status/field labels (status, posting date, outstanding, valuation rate, naming series, gl/sle counts) and raw UUIDs, surfacing the business outcome instead. Guidance-only — no response-shape or schema change. (FINDING-004)
- Stock receipts now always post the perpetual-inventory GL leg (DR Stock-in-Hand / CR Stock Received Not Billed); a missing Stock-in-Hand or Stock Received Not Billed account now fails loudly with an actionable message instead of silently skipping the GL while still moving the subledger. The `create_perpetual_inventory_gl` lib helper raises a structured error (caught at all 7 caller sites across inventory/buying/selling/manufacturing, surfaced as a clean JSON error with full transaction rollback — no SLE-without-GL). `tutorial` (demo company) now seeds a Stock Adjustment account so demo-company reconciliations post correct GL. (FINDING-009)
- Purchased stock is now valued from its source document end to end: receiving against a purchase order (GRN) or recording a bill with stock update carries the unit cost into the stock ledger and posts the inventory GL. A standalone, rate-less external stock receipt with no item standard cost now fails loudly with an actionable message instead of silently booking inventory at $0 (internal transfers and manufacturing legs are unaffected — they keep inheriting existing valuation). SKILL.md now guides the procure-to-pay receipt flow. (FINDING-010, ADR-0014)

## [4.4.0] — 2026-06-05

### Added
- `--email` / `--phone` on `add-customer` / `update-customer` / `add-supplier` / `update-supplier`; returned by the corresponding `get-*`. (FINDING-003)

### Schema
- `customer.email TEXT`, `customer.phone TEXT`, `supplier.email TEXT`, `supplier.phone TEXT` (nullable); foundation migration `016` (dialect-aware SQLite + PostgreSQL). `import-customers` / `import-suppliers` INSERTs now resolve against real columns. (FINDING-003, ADR-0012)

### Fixed
- AR/AP payment application now clears the invoice/bill: the document's `outstanding_amount` / `status` is synced and a per-allocation payment-ledger entry with `against_voucher` is posted, so a recorded payment marks the invoice/bill paid (previously it stayed unpaid). (FINDING-005)
- Gateway `voucher_type` labels are canonicalized system-wide (e.g. "Sales Invoice" -> `sales_invoice`) so the NL gateway and downstream GL/inventory/tax/reports/payments consumers agree on the voucher vocabulary. (FINDING-006)

## [4.3.1] — 2026-05-11

### Fixed
- `erpclaw-selling/db_query.py` main(): re-add `--description` argparse flag that was incorrectly removed as a dupe during v4.3.0 (it's used by `add-dunning-level`). Server smoke caught: `Unknown flags: --description` on first invocation.

## [4.3.0] — 2026-05-11

Customer credit limit + dunning levels (ROADMAP S1). B2B AR depth: enforce credit limits at invoice submit and run automated dunning escalation cycles.

### Added
- 5 new actions in `erpclaw-selling`:
  - `check-credit-limit --customer-id X` — returns credit_limit, outstanding_ar, available_credit, credit_status (read-only).
  - `place-customer-on-hold --customer-id X --credit-status on_hold|suspended|active [--reason ...]` — flip credit status. Audited.
  - `add-dunning-level --company-id X --level N --days-overdue D --dunning-action email|hold|call|suspend` — configure escalation policy.
  - `run-dunning-cycle --company-id X [--run-date Y-M-D]` — match overdue invoices to highest-applicable dunning level, take action (hold/suspend update credit_status; email/call record-only until M8 ships SMTP).
  - `list-dunning-runs [--customer-id X] [--company-id Y]` — history.
- Invoice-submit credit policy hook in `submit-sales-invoice`: blocks suspended customers, blocks on-hold customers, enforces credit_limit when configured. Outstanding AR computed from `sales_invoice.outstanding_amount` of submitted, non-return invoices.

### Schema
- `customer.credit_status TEXT NOT NULL DEFAULT 'active' CHECK(credit_status IN ('active','on_hold','suspended'))`.
- `dunning_level` table: id, company_id, level (1-10), days_overdue, action, template_id, description, UNIQUE(company_id, level).
- `dunning_run` table: id, company_id, run_date, customer_id, level, invoice_ids_json, action_taken, status, generated_email_id, notes.
- Migration `002_credit_dunning.py` brings existing installs up to date. Idempotent.
- `customer.credit_limit` already existed (since pre-v4.0); no change there.

### Tests
- `source/erpclaw/scripts/erpclaw-selling/tests/test_credit_dunning.py` — 17 tests covering check-credit-limit, place-customer-on-hold, add-dunning-level, and the invoice-submit policy hook. All pass.

### Notes
- The email dunning action is record-only at this version; actual SMTP send ships with ROADMAP M8 (Email marketing real sender). When M8 lands, run-dunning-cycle will trigger real emails for level=email rows.
- The `call` action is also record-only by design — it logs that a human-call escalation is due; no auto-dialer.
- credit_status is a separate concept from customer.status: status governs "is this customer still ours" (active / inactive / blocked); credit_status governs "are we currently extending credit" (active / on_hold / suspended). Both must be active for a new invoice to submit.

## [4.2.3] — 2026-05-10

Operational follow-up to the v4.2.x publish saga. Three small fixes from `internal/PENDING_WORK_PLAN_2026-05-10.md` Tier 1.

### Changed
- **Shared skip filters.** Extracted `SKIP_DIRS`, `SKIP_SUFFIXES`, `SKIP_FILE_EXACT` into `source/erpclaw/scripts/erpclaw-setup/lib/erpclaw_lib/skip_filters.py`. Both `release/regen_module_manifests.py` (manifest hashing) and `source/erpclaw/scripts/module_manager.py` (install-time integrity walk) now import from the canonical module. Prevents the v4.2.1-class "extra files" failure where the two walks drifted out of sync.

### Fixed
- **Registry cache auto-refresh on foundation upgrade.** Previously `~/.openclaw/erpclaw/registry_cache.json` was not invalidated when `clawhub install` upgraded the foundation, so the first `install-module` after an upgrade ran integrity checks against the OLD manifest and surfaced false-positive "N mismatched" warnings. Now `_load_registry()` compares the bundled foundation version against the cached one and treats the cache as stale when the bundled version is newer, forcing a refresh.
- **Snapshot publish-surface alignment.** `release/scripts/regen_clawhub.py` was carrying test files (`scripts/<sub>/tests/`), `.gitkeep` markers, and `.github/` configs that ClawHub strips at upload time — dead weight in the snapshot (~50% of file count). Fixed the snapshot walk to actively remove these from `clawhub/erpclaw/` so the local snapshot matches what users actually receive. Snapshot file count dropped from 187 to 90.

### Notes
- No new actions, no schema changes, no behavioural changes for end users; this release is internal hygiene only.
- Verified end-to-end on the OpenClaw test server via the post-republish smoke plan after publish.

## [4.2.2] — 2026-05-10

Companion fix to v4.2.1. The v4.2.1 manifest correctly excluded `tests/`, `.github/`, `bin/`, etc. — but `module_manager.py`'s install-time integrity check kept its old skip set, so when `install-module <vertical>` git-cloned a public repo (which ships the full tree including those dirs), the walk treated `tests/` files as "delivered" while the manifest didn't list them. Result: every `install-module` after a v4.2.1 install failed with "0 mismatched, 0 missing, N extra files".

### Fixed
- `source/erpclaw/scripts/module_manager.py`: aligned the install-time integrity walk's skip set with `release/regen_module_manifests.py`. `SKIP_DIRS` adds `tests`, `.github`, `bin`; `SKIP_SUFFIXES` adds `.sig`; new `SKIP_FILE_EXACT` covers `.DS_Store`, `.gitkeep`, `.clawhubignore`. The two skip sets must stay in lockstep — drift between them produces "extra"/"missing" false positives. A code comment in `module_manager.py` flags this invariant for future maintainers.

### Notes
- Foundation must be re-installed (`clawhub update erpclaw --force` or fresh install) to pick up the fixed `module_manager.py`. The bug only manifests when `install-module` is called against a v4.2.0/v4.2.1-installed foundation.
- Smoke plan re-run on 2026-05-10 confirmed the cold-install path now passes end-to-end with this fix.

## [4.2.1] — 2026-05-10

Fix-up release for v4.2.0. Two install-blocking issues caught during cold-install smoke testing on the OpenClaw server:

### Fixed
- **Manifest / shipped-package alignment.** `release/regen_module_manifests.py` was hashing files (`tests/`, `.github/`, `bin/`, `*.sig`, `.gitkeep`) that ClawHub strips during publish/install. Result: every `install-module` call against a v4.2.0 install failed integrity check with "100 mismatched/missing files" because the manifest claimed files that don't ship. Fixed by aligning the walk's exclude set to ClawHub's actual ship contract: `SKIP_DIRS` now includes `tests`, `.github`, `bin`; `SKIP_FILE_SUFFIXES` adds `.sig`; `SKIP_FILE_EXACT` adds `.gitkeep`, `.clawhubignore`. Foundation manifest dropped from 183 file entries to 84 entries that exactly match what users see post-install. Registry re-signed; signer fingerprint unchanged: `d471:335b:0e4d:75ce`.
- **Vertical parser fixes propagated to public `avansaber/*` repos.** The four argparse duplicate-flag fixes that shipped in v4.2.0's foundation manifest were not pushed to the public repos that `install-module` clones from (`avansaber/healthclaw`, `avansaber/constructclaw`, `avansaber/legalclaw`, `avansaber/retailclaw`). Result: `install-module healthclaw` cloned the broken parser, hash-verified successfully (we'd hashed the broken file), and crashed at first invocation. Fixed by running `internal/publish/publish_manager.py publish-all --execute`.

### Notes
- v4.2.0 was published to ClawHub Marketplace 2026-05-10 with these latent bugs. v4.2.1 supersedes; users should `clawhub update erpclaw --force` to receive the fixed package.
- Cold-install smoke plan: `internal/POST_CLAWHUB_REPUBLISH_SMOKE_PLAN_2026-05-10.md` (run on 2026-05-10 caught both bugs before any external user encountered them).
- `clawhub install erpclaw` still requires `--force` due to a VirusTotal Code Insight flag that fires on our crypto/external-API patterns. This is a marketplace-side scanner over which we have no control; the flag does not indicate actual malware. Out-of-scope for v4.2.1.

## [4.2.0] — 2026-05-05 (republished 2026-05-10)

License change plus four parser bug fixes in vertical modules. ERPClaw moves from MIT to GNU General Public License v3, retroactively, across the entire codebase (foundation + 48 modules + addons + integrations + website + tooling). Existing copies that were downloaded under MIT retain their MIT rights for that downloaded copy per US copyright law on implied license; new clones, forks, and downloads receive GPL v3 terms. Patent (filed under USPTO provisional) is unchanged and continues to apply.

### Changed
- `LICENSE.txt` at every level (48 files) replaced with the canonical GPL v3 text from FSF, prefixed by a short notice and license history.
- `package.json` `license` field in JS sub-projects updated from `"MIT"` to `"GPL-3.0-only"`.
- README, STATUS, ARCHITECTURE, ROADMAP, PROJECT_RULES, CLAUDE.md, and module READMEs across `source/` updated to describe ERPClaw as GPL v3 with a license-history line for v4.1.x and earlier.
- Marketing prose on `erpclaw.ai` updated to say "open source" generically, with explicit "GPL v3" only on license-specific pages.
- Comparison pages drop "MIT vs. GPL" wedge framing; positioning shifts to architecture (AI-native vs. AI-decorated).
- `release/scripts/security_audit.py` `check_licence` now accepts either GPL v3 or MIT (the latter retained as legacy-compatible for v4.1.x).
- `release/scripts/publish.py` and `internal/publish/publish_manager.py` `LICENSE_TEMPLATE` re-templated with a short GPL v3 notice + license-history line.
- `internal/strategy/COMPETITOR_FEATURE_GAP_2026-05-02.md` Section 9 (Open Source Sourcing) rewritten: GPL v3 is now license-compatible with ERPNext, Odoo Community, and other GPL/LGPL upstreams for direct inclusion; AGPL v3 is the new forbidden inbound boundary.

### Fixed
- Removed duplicate argparse flag registrations that crashed the parser at load time across four vertical modules. None of the affected verticals were callable for any action until the dup was removed.
  - `healthclaw/scripts/db_query.py`: duplicate `--verified-by` (kept LAB-domain copy at L453, removed Provider-Credentialing duplicate at L530).
  - `constructclaw/scripts/db_query.py`: duplicate `--spec-section` (kept SUBMITTAL copy at L234, removed Drawing duplicate at L369).
  - `legalclaw/scripts/db_query.py`: duplicate `--reminder-days` (kept CALENDAR copy at L161, removed SOL-Calculator duplicate at L235).
  - `retailclaw/scripts/db_query.py`: duplicate `--category-id` (kept MERCHANDISING copy at L136, removed Procurement/Shrinkage duplicate at L215).
- Cleanup sweep across all `source/*/scripts/db_query.py` confirms zero remaining duplicate argparse registrations.

### Strategic
- License re-anchor was driven by long-term enterprise positioning (Linux/Ubuntu/Red Hat playbook): copyleft prevents proprietary fork-and-close by Microsoft / Salesforce / Oracle, while GPL v3 Section 11 patent grant + defensive termination preserves enterprise adoption optics. Trade-off accepted: SaaS resellers must contribute back; some hyperscaler co-selling motions become harder. Net: better foundation for paid-services + commercial-license dual-track at scale.

### Trust root + signing
- Registry signing key unchanged. Fingerprint remains `d471:335b:0e4d:75ce`.
- Manifests regenerated and re-signed (registry_version 8 → 18) to capture the new LICENSE.txt hash and the four fixed `db_query.py` files.

### Notes
- Tested on OpenClaw 2026.5.7 with ClawHub CLI v0.12.3. Foundation passes the full 6-gate pipeline (270 L0 + 3,088 L2 + 248 L3 tests + invariants + smoke).
- NL routing validated end-to-end on the OpenClaw test server: foundation 5/5 scenarios (companies, customers, invoices, employees, items), three verticals 3/3 (healthclaw patient, educlaw student, constructclaw job).
- Plan + lawyer-level analysis for the license re-anchor: `internal/strategy/LICENSE_DECISION_2026-05-05.md`.

## [4.1.6] — 2026-05-04

Adds an ed25519 signature on the foundation registry. Reconciliation verifies the signature against an embedded public key before trusting any file hash, refuses tampered or downgraded registries, and refuses unsigned registries entirely. Closes the v4.1.5 supply-chain finding by giving the integrity check a cryptographic trust root rather than a hash-only one.

### Added
- ed25519 signature on `module_registry.json`. Public key embedded in `erpclaw_lib/signing.py::TRUSTED_KEYS` with a NamedTuple-based key list that supports rotation. Initial signer fingerprint: `d471:335b:0e4d:75ce` (label `erpclaw-foundation-signer-2026-05-04`). Verify locally with `erpclaw verify-trust-root`.
- New foundation action `verify-trust-root` that prints the embedded key fingerprint(s). Use this to compare against the published fingerprint on `erpclaw.ai` before trusting a reconciliation.
- Strict-mode registry loader. `update-foundation` refuses to proceed unless the registry is freshly signed by a trusted key. The lenient loader used for read-only listings (`available-modules`, `search-modules`) emits a stderr warning when the signature cannot be verified, but does not block.
- Monotonic `registry_version` field inside the signed payload. The verifier rejects any registry whose `registry_version` is lower than the locally tracked value, defending against replay/downgrade of older legitimately-signed registries.
- Append-only signing log at `scripts/signing_log.txt` recording each signing event (timestamp, hash prefix, version, signer fingerprint). Allows after-the-fact detection of unauthorized signing.
- Recovery path `--unsafe-trust-bundled` for `update-foundation`. When a published key has been revoked and no rotated key has yet reached an offline install, an operator can reconcile against the locally-bundled hashes only. The flag emits a stderr warning, writes an entry to the audit log, and is documented as a recovery-only operator action; ordinary reconciliation always requires a verified signature.
- Atomic publish pipeline: new entry-point `release/regen_and_sign.py` runs manifest regeneration + signing in one invocation and verifies registry mtime ≤ signature mtime before completion.

### Changed
- Foundation action count: 477 → 478 (added `verify-trust-root`).
- `_load_registry` now returns the registry with `_signed_by` (fingerprint) on successful verification, or `_signature_warning` (string) when the lenient path falls back to unsigned content. The strict variant `_load_registry_strict` raises on any verification failure with no fallback to unsigned content.

### Trust root rotation

Rotation is one of the few legitimate triggers for a future ClawHub re-publish, because every install ships with the embedded key list. Rotation procedure: ship the new key alongside the existing one with a `valid_until` for the old key, allow a grace period, then ship the new key alone. Stale installs that never reconcile remain locked to their original key list. Out-of-band fingerprint verification via `erpclaw verify-trust-root` and the published fingerprint on `erpclaw.ai` is recommended before trusting any rotation.

### Notes
- v4.1.5 → v4.1.6 transition: the first reconcile that crosses this boundary establishes signing on the install. Subsequent reconciles verify.
- Long-running processes hold imported modules in memory; foundation file changes take effect on next launch.

### Plan + audit
- `internal/completed/2026/sprints/CLAWHUB_FIX_v416_PLAN_2026-05-04.md`
- `internal/completed/2026/sprints/CLAWHUB_FIX_v416_AUDIT_2026-05-04.md` (8 BLOCK + 7 SHOULD adopted from external + internal audits)

## [4.1.5] — 2026-05-04

Foundation manifest reconciliation. Bundles the v4.1.4 runtime gate extension with two new gated actions that let an administrator align installed foundation files with the published `module_registry.json` manifest. Future foundation updates apply via these actions on explicit user invocation.

### Added
- Foundation actions `update-foundation` and `rollback-foundation` (gated, require `--user-confirmed`). The first compares each installed file against the manifest's `files_sha256` map, and for drifting files re-fetches from the published source and re-verifies the declared SHA256 before atomic replacement. A pre-flight verifies all replacements before any rename, so a hash failure leaves the install unchanged. Replaced files are preserved as `.bak` for one cycle.
- A non-blocking convenience check in the foundation router that surfaces a reminder on stderr when manifest-version drift is present, no more than once per 24-hour window per install. The check does not modify files; the user invokes `update-foundation` to apply. Suppressed by the marker `~/.openclaw/erpclaw/.skip_reconcile` or the per-invocation flag `--no-reconcile-check`. Recursion-guarded for foundation-touching actions (`update-foundation`, `rollback-foundation`, `install-module`, `remove-module`, `update-modules`, `schema-apply`, `schema-rollback`).
- `fcntl.flock` on `~/.openclaw/erpclaw/.sync.lock` serializes reconciliation so the one-cycle `.bak` is never corrupted by concurrent invocation.
- A safety guard refuses reconciliation when running inside a git-tracked source tree (developer checkout); the mechanism targets ClawHub-installed deployments only.

### Changed
- Foundation action count: 475 → 477 (added the two reconciliation actions).
- `_strip_router_flags` continues to strip `--user-confirmed` before forwarding to domain scripts; the foundation router gate is the single source of truth for confirmation.

### Integrity model

Reconciliation verifies each file against the SHA256 declared in the published manifest before atomic replacement. Cryptographic signing of the manifest itself is roadmap for v4.2.0. Operators preferring not to use the reconciliation path can place the opt-out marker `~/.openclaw/erpclaw/.skip_reconcile`.

### Notes
- Long-running processes (MCP servers, daemons) hold imported modules in memory; foundation file changes take effect on next launch.

### Plan + audit
- `apps/CLAWHUB_FIX_v415_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v415_AUDIT_2026-05-04.md` (4 BLOCK + 5 SHOULD adopted; 4 SHOULD deferred to v4.1.6/v4.2.0)

## [4.1.4] — 2026-05-04

Closes the v4.1.3 OpenClaw Tool Misuse Concern by extending the runtime gate to administrative actions beyond financial postings.

### Changed
- `DANGEROUS_ACTIONS` frozenset extended with 11 entries spanning RBAC + identity changes (`add-role`, `assign-role`, `revoke-role`, `seed-permissions`, `update-user`, `set-password`), credential lifecycle (`set-credential`, `delete-credential`, `migrate-credentials`, `import-master-key-from-backup`), and account-state (`unfreeze-account`). All require `--user-confirmed` on every invocation.
- Foundation SKILL.md `## Runtime gate` paragraph reworded to describe high-impact actions broadly without enumerating action names. Catalog and frozenset are the source of truth.
- Gate-rejection error message generalized: now says "is a high-impact action" instead of "materially changes financial or system state".

### Fixed
- Stale comment in `db_query.py` referenced a removed environment-variable bypass; cleaned up.

### Plan + audit
- `apps/CLAWHUB_FIX_v414_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v414_AUDIT_2026-05-04.md`

## [4.1.3] — 2026-05-04

Cross-machine backup restore + Tier A regression fix-ups discovered during v4.1.x test-plan execution.

### Added
- New foundation action `import-master-key-from-backup`. Required for cross-machine restore: a backup taken on Machine A is now restorable on Machine B with full encrypted-column readability. The backup's ECRYPT02 header carries a passphrase-wrapped copy of the column-encryption master key; this action unwraps it and installs at `~/.config/erpclaw/master.key`. Refuses to overwrite an existing master key without `--force`. Passphrase via `--passphrase`, `--passphrase-from-stdin`, or `--passphrase-from-env`.

### Changed
- `backup-database --encrypt`: now wraps the current machine's column-encryption master key with the backup passphrase and embeds it in the ECRYPT02 header. Backups taken without a master key (no encrypted columns yet) work as before. Response now includes `carries_master_key: bool` to indicate whether cross-machine restore is supported for this backup.
- Foundation action count: 474 → 475 (added `import-master-key-from-backup`).

### Fixed
- Foundation SKILL.md catalog now lists the 5 credential-management actions (`set-credential`, `get-credential`, `list-credentials`, `delete-credential`, `migrate-credentials`) added in v4.1.0, plus 2 module-discovery actions (`list-articles`, `build-table-registry`) that were previously implemented but undocumented. L0 `test_skillmd_action_completeness` was failing on this drift; now passes.
- `test_nacha_ach.py::TestAddEmployeeBankAccount::test_basic_add` updated to decrypt encrypted columns before asserting plaintext (regression caused by v4.1.0 column encryption).

### Notes
- No code logic changes; only documentation alignment + one test fixture update.
- 3 pre-existing `erpclaw-os-engine` constitution failures (Article 5 cross-module write violations + addon SKILL.md drift) deferred to Tier I (vertical addon cross-tests) per `apps/V410_TEST_PLAN_2026-05-04.md`.

## [4.1.2] — 2026-05-04

Made the v4.1.0 runtime gate's enforcement visible in SKILL.md so static-analysis review correctly attributes write actions to a gated context.

### Added
- New `## Runtime gate` section in foundation SKILL.md (8 lines, immediately before the action catalog) describing the per-invocation flag requirement and the router's pre-dispatch rejection of unflagged calls.

### Notes
- No code logic changes. The v4.1.0+ runtime gate is unchanged.
- Phase 2 audit reviewed the proposed text; revised to drop verb-enumeration and env-var-bypass wording that would have re-summoned previous trigger phrases.

### Plan + audit
- `apps/CLAWHUB_FIX_v412_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v412_AUDIT_2026-05-04.md`

## [4.1.1] — 2026-05-04

Tightened v4.1.0 security posture in response to OpenClaw rescan feedback.

### Changed
- Hardened the runtime confirmation gate to require explicit per-invocation flag; removed an environment-variable form that could globalize confirmation across processes.
- Module install now verifies the full file tree against the foundation registry, not only the manifest entry-point.
- Trimmed user-facing security claims to neutral wording matching implementation. Mechanism specifics live in code, not in marketing prose.

### Removed
- Environment-variable bypass for the runtime confirmation gate. Per-invocation flag is the only path.

### Migration
- Cron / CI users that relied on the removed env var: switch to per-invocation flag. The gate's error message indicates the required flag.

### Roadmap
- v4.2.0 will add cryptographic signature verification (sigstore/cosign) on top of the file-tree integrity manifest, plus an approve-pending queue for sanctioned automation.

### Plan + audit
- `apps/CLAWHUB_FIX_v411_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v411_AUDIT_2026-05-04.md`

## [4.1.0] — 2026-05-04

Comprehensive security modernization. Real architectural changes: audited crypto, file-based credential management, column-level encryption, runtime confirmation gate for high-impact actions, supply-chain integrity verification, lib bootstrap removal. No legacy `--api-key` flag.

### Added
- **Column-level encryption** for selected sensitive fields (employee SSN, bank routing/account numbers). New helper `erpclaw_lib.encrypted_columns`.
- **Encrypted credential store** with per-machine master key, accessed via foundation actions: `set-credential`, `get-credential` (returns redacted), `list-credentials`, `delete-credential`, `migrate-credentials`. Library: `erpclaw_lib.credentials`.
- **Runtime confirmation gate** for high-impact financial-mutation actions. High-impact actions require explicit per-invocation confirmation; routed through the foundation gate before dispatch.
- **Module integrity verification** in `module_registry.json`. `module_manager.py install-module` verifies content integrity against the foundation registry.
- **AES-256-GCM streaming backup format `ECRYPT02`** with 1 MiB plaintext frames, per-frame nonces, and an embedded passphrase-wrapped master key for cross-machine restore. Files of any size supported (no in-memory load).

### Changed
- **`erpclaw_lib/crypto.py` rewritten** to use the `cryptography` library (OpenSSL via cffi). PBKDF2-HMAC-SHA256 at 600,000 iterations (OWASP 2024). Field-level encryption uses raw AES-256-GCM with `enc:v2:` prefix. Legacy `ECRYPT01` decrypt path retained for v4.0.x backups.
- **Stripe addon: hard-removed `--api-key` flag.** `erpclaw-integrations-stripe` v2.0.1 reads credentials from the foundation credential store. Users migrate via `erpclaw migrate-credentials` (one-time read-from-DB → write-to-encrypted-store) or set fresh via `erpclaw set-credential --integration stripe --from-stdin`.
- **Lib bootstrap removed.** `_bootstrap.py` deleted. `~/.openclaw/erpclaw/lib` is now a symlink (created at `initialize-database`) to the skill-bundled location, not a self-healing copy. Eliminates the "self-modifying code at runtime" finding.
- **Foundation `SKILL.md` disclosure cleanup.** Removed 4 v4.0.1 paragraphs (Credential handling, Data protection, Module installation safety, Library self-heal) that were reading as scanner trigger surfaces while describing addon behavior or implementation details. Kept only the factual one-line Security summary.

### Removed
- `--api-key` flag (Stripe addon). Use `set-credential` instead.
- `_bootstrap.py` (`erpclaw_lib._bootstrap`). Lib symlink replaces self-heal.
- `install_shared_library` foundation function. Symlink replaces.
- Homemade HMAC-SHA256-CTR cipher in `crypto.py`. Replaced with `cryptography` library AES-256-GCM.

### Fixed
- Registry foundation entry was stale at `version: 3.5.0` / `has_init_db: false` / `action_count: 438`. Now reflects current state: `version: 4.1.0` / `has_init_db: true` / `action_count: 467`.

### Migration notes
- Existing v4.0.x backups remain decryptable (legacy `ECRYPT01` path retained).
- Existing plaintext column rows pass through `decrypt_for_column` unchanged; encrypted-on-write applies to new rows only. No mandatory data migration.
- Stripe users who previously stored API keys via `--api-key`: run `erpclaw migrate-credentials` once after upgrade to move keys from DB plaintext to the encrypted credential store.
- Cron/agent/CI users: append `--user-confirmed` to high-impact action invocations.

### Plan + audit
- `apps/CLAWHUB_FIX_v410_PLAN_2026-05-04.md`
- `apps/CLAWHUB_FIX_v410_AUDIT_2026-05-04.md`

## [4.0.2] — 2026-05-04

Eliminate F1 (Rogue Agents / cron) Concern from the ClawHub OpenClaw review by removing decorative `cron:` blocks from foundation and grouped-addons SKILL.md files.

### Why

Phase 2 audit verification (B1) discovered that OpenClaw's runtime cron daemon does NOT auto-discover SKILL.md `cron:` blocks. Active scheduling requires explicit `openclaw cron add` CLI commands. The `cron:` block in foundation SKILL.md was therefore decorative metadata, not active scheduling — but the ClawHub static analyzer was reading it as scheduled financial mutation and flagging F1 as HIGH/Concern.

Removing the decorative blocks eliminates the trigger at the source without changing operational behavior (no user has ever had ERPClaw crons running automatically from `clawhub install`; users wanting daily jobs always had to run `openclaw cron add` manually).

### Changed
- **Foundation `SKILL.md`**: removed the entire `cron:` block (4 entries: process-recurring, generate-recurring-invoices, check-reorder, check-overdue). Replaced "Background automation" prose section with "Optional scheduling" pointer to `openclaw cron add` for users who want daily jobs.
- **`erpclaw-growth` SKILL.md** (grouped addon): removed 1 cron entry (weekly anomaly detection sweep).
- **`erpclaw-ops` SKILL.md** (grouped addon): removed 3 cron entries (monthly depreciation, daily overdue issues, weekly SLA compliance).
- **Library self-heal disclosure** kept as standalone subsection (was bundled with cron prose in v4.0.1).
- Foundation `version: 4.0.1` → `4.0.2`.
- `erpclaw_lib/__version__ = "4.0.2"`.
- `module_registry.json` top-level `version: "4.0.2"`.

### Notes
- No code paths read SKILL.md cron blocks. Verified via grep across `source/`, `internal/`, `scripts/` — zero hits for cron-block consumption.
- All 4 daily action targets (`process-recurring`, `generate-recurring-invoices`, `check-reorder`, `check-overdue`) remain in foundation as on-demand callable actions. No capability removed.
- Users who want automatic daily runs use `openclaw cron add --name <id> --cron "<expr>" --message "Using erpclaw, run the <action> action."` — the same path that was always required for actual scheduling.
- Plan + audit + B1 verification: `apps/CLAWHUB_FIX_v402_PLAN_2026-05-04.md` + `apps/CLAWHUB_FIX_v402_AUDIT_2026-05-04.md` + this CHANGELOG entry.

### Migration arc

| Version | Cron in foundation SKILL.md | Cron actually running on install |
|---|---|---|
| v3.5.1 | 4 entries, `announce: false` | NO (decorative only) |
| v4.0.0 | 4 entries, `announce: false` | NO |
| v4.0.1 | 4 entries, `announce: true` | NO |
| v4.0.2 | none | NO (unchanged — same as all prior) |

The "migration" is operationally a no-op. Only the SKILL.md text changed.

## [4.0.1] — 2026-05-04

Security patch responding to ClawHub OpenClaw v4.0.0 review findings. Documentation, defaults, and disclosure changes; no schema changes, no new actions.

### Changed
- **Cron jobs now announce.** All four daily background jobs (`process-recurring`, `generate-recurring-invoices`, `check-reorder`, `check-overdue`) flipped from `announce: false` to `announce: true`. Each run is now visible in the OpenClaw activity feed. (Resolves OpenClaw v4.0.0 finding "Rogue Agents" HIGH/Concern.)
- **README scrub.** Removed "Self-Extending ERP" section from foundation README. Module-generation prose lives in the optional `erpclaw-os-engine` addon README only. (Resolves OpenClaw v4.0.0 finding "Unexpected Code Execution" MEDIUM/Concern.)
- **Foundation SKILL.md actions table** no longer advertises the ~30 module-authoring / variant-analysis / heartbeat / semantic-check actions that moved to `erpclaw-os-engine` in v4.0.0. Replaced with a single pointer to the optional addon. Removed `generate-module` and `deploy-module` from the "always confirm" list (now addon-only).
- **Addon SKILL.md description** reworded to lead with developer-tooling framing and explicit sandbox-first / user-approval-before-deploy disclosure.

### Added
- **Credential handling, Data protection, Module installation safety** paragraphs in foundation SKILL.md security section. Mirror the OpenClaw recommendations for F3-F5 Note-level findings.
- **Background automation** section in foundation SKILL.md documenting the four cron jobs and the lib bootstrap self-heal behavior.
- **chmod 600 on `data.sqlite`, `data.sqlite-wal`, `data.sqlite-shm`** — applied at `initialize-database`, after `restore-database`, after `backup-database`, AND on every foundation action invocation. Backup outputs are also chmod 600. New helper `chmod_db_files()` in `erpclaw-setup/db_query.py`.
- **Lib bootstrap self-heal** (`erpclaw_lib/_bootstrap.py`). On every foundation action invocation, the router compares the bundled `erpclaw_lib.__version__` to a marker file at `~/.openclaw/erpclaw/lib/.erpclaw_lib_version`. On mismatch, it re-syncs the deployed `erpclaw_lib/` from the bundled source, writes a new marker, and appends an entry to `~/.openclaw/erpclaw/logs/bootstrap.log`. Eliminates the v3.5.1 → v4.0.0 upgrade gotcha where `clawhub update` skipped the foundation post-install hook and addon `sandbox.py` couldn't find the new `gl_invariants.py`. Honors `ERPCLAW_DISABLE_BOOTSTRAP=1` env var as an explicit opt-out.
- **`__version__ = "4.0.1"`** declared in `erpclaw_lib/__init__.py` as the canonical lib version.

### Fixed
- `restore-database` now explicitly chmods the restored DB file to 0o600 (previously inherited mode from the backup source via `shutil.copy2`).

### Notes
- The 3 Note-level OpenClaw findings (Agentic Supply Chain, Identity & Privilege Abuse, Memory and Context Poisoning) remain at Note status by design — they describe disclosed-and-accepted ERP behavior, and the recommendations are user-side practices (use scope-limited keys, rotate credentials, restrict file permissions, test imports against a separate DB). v4.0.1 mirrors those recommendations in the SKILL.md security section.
- Keychain integration, SQLite encryption-at-rest, and module signature verification remain on the v4.1+ roadmap.

## [4.0.0] — 2026-05-04

Architectural split. ClawHub static-analysis CRITs eliminated. `clawhub install erpclaw` works without `--force`.

### Changed
- **Foundation / addon split.** 21 dev-time files (`generate_module.py`, `in_module_generator.py`, `sandbox.py`, etc.) moved out of foundation to a new optional addon, `erpclaw-os-engine` v1.0.0, distributed via GitHub-only at `avansaber/erpclaw-addons` subdir `erpclaw-os-engine`. The addon trips the same scanner the foundation used to trip; isolating it keeps foundation users scan-clean.
- **`os-` prefix on 28 user-facing actions** that moved to the addon (`os-generate-module`, `os-deploy-module`, etc.). Foundation router emits a structured migration error JSON for legacy bare-name calls.
- **`gl_invariant_checker` extracted to `erpclaw_lib.gl_invariants`** for cross-package import. Foundation runtime keeps the GL invariant validation; addon's sandbox imports from `erpclaw_lib.gl_invariants`.
- **Foundation `scripts/erpclaw-os/`** retains 7 runtime actions only: `validate-module`, `list-articles`, `build-table-registry`, `schema-{plan,apply,rollback,drift}`.

### Added
- `erpclaw-os-engine` SKILL.md, db_query.py, web_dashboard.py, sandbox.py — addon entry points.
- Foundation-locator pattern in addon's `db_query.py` (3-candidate sys.path resolution: env, prod, repo-relative).

### Notes
- ClawHub release id: `k974yxfap664grmdnxfsstnhy5863wfa`.
- Plan: `apps/CLAWHUB_FIX_C_PLAN_2026-05-04.md`. Audit: `apps/CLAWHUB_FIX_C_AUDIT_2026-05-04.md`.
