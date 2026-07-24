"""Stock Ledger Entry (SLE) engine for ERPClaw.

All stock_ledger_entry writes go through this module. This is the ONLY code
that writes to the stock_ledger_entry table. Critical design invariant.

Key functions:
- validate_stock_entries(): Validate SLE entries before insertion
- insert_sle_entries(): Validate + insert + compute running balance
- reverse_sle_entries(): Cancel by creating negative-qty mirror entries
- get_stock_balance(): Current qty & value for item/warehouse
- get_valuation_rate(): Current moving-average or FIFO valuation rate
- create_perpetual_inventory_gl(): Generate GL entries for stock movements

Valuation methods:
- moving_average: New rate = (old_value + incoming_value) / (old_qty + incoming_qty)
- fifo: First-In First-Out. Each incoming creates a layer in stock_fifo_layer;
        each outgoing consumes layers oldest-first by (posting_date, created_at).

NEVER commit inside these functions — the caller owns the transaction.

NOTE: This file is in SAFETY_EXCLUDED_FILES for DGM, but it is modified by
the developer directly. All FIFO logic is clearly commented.
"""
import uuid
import sqlite3
from decimal import Decimal
from typing import Optional

from erpclaw_lib.decimal_utils import to_decimal, round_currency
from erpclaw_lib.db import db_error_types
from erpclaw_lib.query import latest_insert_order


# ---------------------------------------------------------------------------
# FIFO Layer Management
# ---------------------------------------------------------------------------

def _get_item_valuation_method(conn: sqlite3.Connection, item_id: str) -> str:
    """Return the valuation method for an item ('moving_average' or 'fifo').

    Falls back to 'moving_average' if item not found or column missing.
    Dialect-agnostic: the except classes come from db_error_types() (SQLite + PG).
    """
    _missing_table, _db_error = db_error_types()
    try:
        row = conn.execute(
            "SELECT valuation_method FROM item WHERE id = ?", (item_id,)
        ).fetchone()
        if row and row["valuation_method"]:
            return row["valuation_method"]
    except (KeyError,) + _missing_table:
        pass  # item/column/table absent on minimal installs; use the default
    except _db_error:
        pass  # any other DB read issue → fall back to the default method
    return "moving_average"


def _insert_fifo_layer(
    conn: sqlite3.Connection,
    item_id: str,
    warehouse_id: str,
    posting_date: str,
    qty: Decimal,
    rate: Decimal,
    voucher_type: str,
    voucher_id: str,
) -> str:
    """Insert a new FIFO layer for an incoming stock transaction.

    Each purchase receipt, stock entry (receive), or other incoming movement
    creates a layer. The remaining_qty starts equal to qty and is decremented
    as outgoing transactions consume from this layer.

    Returns the layer ID.
    """
    layer_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO stock_fifo_layer (
            id, item_id, warehouse_id, posting_date,
            qty, rate, remaining_qty,
            source_voucher_type, source_voucher_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(CURRENT_TIMESTAMP AS TEXT))
        """,
        (
            layer_id, item_id, warehouse_id, posting_date,
            str(round_currency(qty)),
            str(round_currency(rate)),
            str(round_currency(qty)),
            voucher_type, voucher_id,
        ),
    )
    return layer_id


def _consume_fifo_layers(
    conn: sqlite3.Connection,
    item_id: str,
    warehouse_id: str,
    qty_to_consume: Decimal,
) -> tuple[Decimal, list[dict]]:
    """Consume FIFO layers oldest-first for an outgoing stock transaction.

    Deducts from remaining_qty of existing layers, starting with the oldest
    (ORDER BY posting_date ASC, created_at ASC).

    Args:
        conn: SQLite connection (within a transaction).
        item_id: The item being issued.
        warehouse_id: The warehouse being issued from.
        qty_to_consume: Positive quantity to consume.

    Returns:
        (weighted_average_rate, consumed_layers) where consumed_layers is a
        list of {"layer_id", "qty_consumed", "rate"} dicts.

    Raises:
        ValueError if there are insufficient FIFO layers to fulfil the request.
    """
    layers = conn.execute(
        """
        SELECT id, qty, rate, remaining_qty
        FROM stock_fifo_layer
        WHERE item_id = ? AND warehouse_id = ?
          AND CAST(remaining_qty AS REAL) > 0
        ORDER BY posting_date ASC, created_at ASC
        """,
        (item_id, warehouse_id),
    ).fetchall()

    remaining = qty_to_consume
    total_value = Decimal("0")
    consumed = []

    for layer in layers:
        if remaining <= 0:
            break

        layer_remaining = to_decimal(layer["remaining_qty"])
        layer_rate = to_decimal(layer["rate"])

        consume_from_this = min(remaining, layer_remaining)
        total_value += round_currency(consume_from_this * layer_rate)
        remaining -= consume_from_this

        new_remaining = layer_remaining - consume_from_this
        conn.execute(
            "UPDATE stock_fifo_layer SET remaining_qty = ? WHERE id = ?",
            (str(round_currency(new_remaining)), layer["id"]),
        )

        consumed.append({
            "layer_id": layer["id"],
            "qty_consumed": consume_from_this,
            "rate": layer_rate,
        })

    if remaining > Decimal("0.0001"):
        # Allow tiny rounding remainder (< 0.01 of a unit) to avoid
        # false failures from floating-point edge cases.
        raise ValueError(
            f"Insufficient FIFO layers for item {item_id} in warehouse "
            f"{warehouse_id}. Needed {qty_to_consume}, "
            f"unfulfilled: {remaining}"
        )

    if qty_to_consume > 0:
        weighted_rate = round_currency(total_value / qty_to_consume)
    else:
        weighted_rate = Decimal("0")

    return weighted_rate, consumed


def _reverse_fifo_layers(
    conn: sqlite3.Connection,
    voucher_type: str,
    voucher_id: str,
) -> None:
    """Reverse FIFO layers created by a voucher (for cancellation).

    For incoming vouchers: sets remaining_qty to 0 on layers created by voucher.
    This is called during reverse_sle_entries for FIFO items.
    """
    conn.execute(
        """
        UPDATE stock_fifo_layer
        SET remaining_qty = '0'
        WHERE source_voucher_type = ? AND source_voucher_id = ?
        """,
        (voucher_type, voucher_id),
    )


def get_fifo_valuation_rate(
    conn: sqlite3.Connection,
    item_id: str,
    warehouse_id: str,
    qty: Decimal,
) -> Decimal:
    """Calculate the FIFO valuation rate for consuming a given qty.

    This is a READ-ONLY query — it does not modify layers. Used for
    previewing the cost of an outgoing transaction before committing.

    Args:
        conn: SQLite connection.
        item_id: Item to query.
        warehouse_id: Warehouse to query.
        qty: Positive quantity to price.

    Returns:
        Weighted average rate from the oldest layers that would be consumed.
    """
    if qty <= 0:
        return Decimal("0")

    layers = conn.execute(
        """
        SELECT rate, remaining_qty
        FROM stock_fifo_layer
        WHERE item_id = ? AND warehouse_id = ?
          AND CAST(remaining_qty AS REAL) > 0
        ORDER BY posting_date ASC, created_at ASC
        """,
        (item_id, warehouse_id),
    ).fetchall()

    remaining = qty
    total_value = Decimal("0")

    for layer in layers:
        if remaining <= 0:
            break
        layer_remaining = to_decimal(layer["remaining_qty"])
        layer_rate = to_decimal(layer["rate"])
        consume = min(remaining, layer_remaining)
        total_value += round_currency(consume * layer_rate)
        remaining -= consume

    if qty > 0:
        return round_currency(total_value / qty)
    return Decimal("0")


# ---------------------------------------------------------------------------
# Valuation Repricing (shared by revalue-stock and landed-cost-voucher)
# ---------------------------------------------------------------------------

def reprice_stock_valuation(
    conn: sqlite3.Connection,
    item_id: str,
    warehouse_id: str,
    *,
    voucher_type: str,
    voucher_id: str,
    posting_date: str,
    value_delta: Optional[str] = None,
    new_rate: Optional[str] = None,
    fiscal_year: Optional[str] = None,
    posting_time: Optional[str] = None,
    fifo_source_voucher_ids: Optional[list[str]] = None,
    fifo_source_voucher_type: str = "purchase_receipt",
) -> dict:
    """Reprice an item's stock valuation without changing quantity.

    This is the ONE shared idiom behind both ``revalue-stock`` and the
    ``add-landed-cost-voucher`` valuation half (D1 / R1, ADR-0030). It:

    1. Inserts a single zero-qty (``actual_qty='0'``) stock_ledger_entry row
       carrying the new valuation_rate/stock_value and a
       ``stock_value_difference`` equal to the value change — the same
       revaluation idiom ``revalue-stock`` has always used. This is the SLE
       side of the stock-account GL ≡ SLE valuation reconciliation (INV-24):
       the GL debit to the Stock-in-Hand account is mirrored here as the SLE
       value delta.
    2. For FIFO items, updates ``stock_fifo_layer.rate`` so future issues
       consume at the repriced cost (moving-average items keep no layers, so
       step 2 is a no-op for them — behaviour there is unchanged):
         - **landed cost** (``value_delta`` + ``fifo_source_voucher_ids``):
           bump the layers sourced from the given voucher(s) by the per-unit
           charge (``value_delta`` spread over their received qty).
         - **revaluation** (``new_rate``, no source vouchers): set every open
           layer for the item/warehouse to the new rate, so the layer-based
           value matches the new SLE stock_value.

    Exactly one of ``value_delta`` (signed change to stock value) or
    ``new_rate`` (target valuation rate) must be supplied. The reversal of a
    landed-cost voucher calls this with a NEGATED ``value_delta`` (and the same
    ``fifo_source_voucher_ids``), which restores both the SLE value and the
    layer rates — cancel = reverse, never edit.

    NEVER commits — the caller owns the transaction. All money is TEXT Decimal
    rounded ROUND_HALF_UP via round_currency().

    Returns:
        {"sle_id", "value_delta", "new_rate", "new_stock_value",
         "qty_after", "fifo_layers_updated"}.

    Raises:
        ValueError if neither / both of value_delta and new_rate are given.
    """
    if (value_delta is None) == (new_rate is None):
        raise ValueError(
            "reprice_stock_valuation requires exactly one of value_delta or new_rate"
        )

    balance = get_stock_balance(conn, item_id, warehouse_id)
    current_qty = to_decimal(balance["qty"])
    current_value = to_decimal(balance["stock_value"])
    current_rate = to_decimal(balance["valuation_rate"])

    if new_rate is not None:
        new_rate_d = round_currency(to_decimal(new_rate))
        new_value = round_currency(current_qty * new_rate_d)
        delta = round_currency(new_value - current_value)
    else:
        delta = round_currency(to_decimal(value_delta))
        new_value = round_currency(current_value + delta)
        # A value change over unchanged qty gives the new per-unit rate; with no
        # qty on hand the rate is left at the prior rate (the value delta still
        # rides on stock_value_difference so INV-24 stays balanced).
        new_rate_d = round_currency(new_value / current_qty) if current_qty > 0 else current_rate

    sle_id = str(uuid.uuid4())
    # Zero-qty valuation row — mirrors the revalue-stock idiom exactly:
    # actual_qty='0', qty_after unchanged, is_cancelled=0 (an active revaluation,
    # not a cancelled/audit row). Kept as raw SQL for the mixed literal pattern.
    conn.execute(
        """
        INSERT INTO stock_ledger_entry (
            id, posting_date, posting_time, item_id, warehouse_id,
            actual_qty, qty_after_transaction, valuation_rate,
            stock_value, stock_value_difference,
            voucher_type, voucher_id, batch_id, serial_number,
            incoming_rate, is_cancelled, fiscal_year, created_at
        ) VALUES (?, ?, ?, ?, ?, '0', ?, ?, ?, ?, ?, ?, NULL, NULL, '0', 0, ?,
                  CAST(CURRENT_TIMESTAMP AS TEXT))
        """,
        (
            sle_id, posting_date, posting_time, item_id, warehouse_id,
            str(round_currency(current_qty)),
            str(new_rate_d),
            str(new_value),
            str(delta),
            voucher_type, voucher_id,
            fiscal_year,
        ),
    )

    fifo_layers_updated = 0
    if _get_item_valuation_method(conn, item_id) == "fifo":
        if fifo_source_voucher_ids:
            # Landed-cost mode: bump only the layers sourced from the receipt(s)
            # for this item/warehouse by the per-unit share of the value delta.
            placeholders = ",".join("?" for _ in fifo_source_voucher_ids)
            layers = conn.execute(
                f"""
                SELECT id, qty, rate FROM stock_fifo_layer
                WHERE item_id = ? AND warehouse_id = ?
                  AND source_voucher_type = ?
                  AND source_voucher_id IN ({placeholders})
                """,
                (item_id, warehouse_id, fifo_source_voucher_type,
                 *fifo_source_voucher_ids),
            ).fetchall()
            total_layer_qty = sum((to_decimal(lyr["qty"]) for lyr in layers), Decimal("0"))
            if total_layer_qty > 0:
                per_unit = round_currency(delta / total_layer_qty)
                for lyr in layers:
                    new_layer_rate = round_currency(to_decimal(lyr["rate"]) + per_unit)
                    conn.execute(
                        "UPDATE stock_fifo_layer SET rate = ? WHERE id = ?",
                        (str(new_layer_rate), lyr["id"]),
                    )
                    fifo_layers_updated += 1
        elif new_rate is not None:
            # Revaluation mode: every open layer takes the new uniform rate, so
            # the sum of (remaining_qty * rate) tracks the new SLE stock_value.
            cur = conn.execute(
                """
                UPDATE stock_fifo_layer SET rate = ?
                WHERE item_id = ? AND warehouse_id = ?
                  AND CAST(remaining_qty AS REAL) > 0
                """,
                (str(new_rate_d), item_id, warehouse_id),
            )
            fifo_layers_updated = cur.rowcount if cur.rowcount is not None else 0

    return {
        "sle_id": sle_id,
        "value_delta": str(delta),
        "new_rate": str(new_rate_d),
        "new_stock_value": str(new_value),
        "qty_after": str(round_currency(current_qty)),
        "fifo_layers_updated": fifo_layers_updated,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_stock_entries(
    conn: sqlite3.Connection,
    entries: list[dict],
    posting_date: str,
    company_id: str,
) -> list[str]:
    """Validate stock entries before insertion.

    Checks:
    1. Each entry has item_id, warehouse_id, actual_qty
    2. Item exists and is a stock item
    3. Warehouse exists, is not a group, belongs to company
    4. For outgoing (actual_qty < 0): sufficient stock exists
    5. Batch validation (if item has_batch)
    6. Serial number validation (if item has_serial)

    Returns:
        List of warning messages (empty if no warnings).

    Raises:
        ValueError on validation failure.
    """
    warnings = []

    for i, entry in enumerate(entries):
        item_id = entry.get("item_id")
        warehouse_id = entry.get("warehouse_id")
        actual_qty = to_decimal(entry.get("actual_qty", "0"))

        # 1. Required fields
        if not item_id:
            raise ValueError(f"SLE entry {i}: item_id is required")
        if not warehouse_id:
            raise ValueError(f"SLE entry {i}: warehouse_id is required")
        if actual_qty == 0:
            raise ValueError(f"SLE entry {i}: actual_qty cannot be zero")

        # 2. Item exists and is stock item
        item = conn.execute(
            "SELECT * FROM item WHERE id = ?", (item_id,)
        ).fetchone()
        if item is None:
            raise ValueError(f"SLE entry {i}: item {item_id} does not exist")
        if item["is_stock_item"] == 0:
            raise ValueError(
                f"SLE entry {i}: item '{item['item_name']}' is not a stock item "
                f"(item_type={item['item_type']})"
            )
        if item["status"] == "disabled":
            raise ValueError(
                f"SLE entry {i}: item '{item['item_name']}' is disabled"
            )

        # 3. Warehouse exists and belongs to company
        wh = conn.execute(
            "SELECT * FROM warehouse WHERE id = ?", (warehouse_id,)
        ).fetchone()
        if wh is None:
            raise ValueError(f"SLE entry {i}: warehouse {warehouse_id} does not exist")
        if wh["is_group"]:
            raise ValueError(
                f"SLE entry {i}: warehouse '{wh['name']}' is a group — "
                f"cannot post stock to group warehouses"
            )
        if wh["company_id"] != company_id:
            raise ValueError(
                f"SLE entry {i}: warehouse '{wh['name']}' belongs to "
                f"company {wh['company_id']}, expected {company_id}"
            )

        # 4. Sufficient stock for outgoing entries
        if actual_qty < 0:
            balance = get_stock_balance(conn, item_id, warehouse_id)
            current_qty = to_decimal(balance["qty"])
            if current_qty + actual_qty < 0:
                raise ValueError(
                    f"SLE entry {i}: insufficient stock for item "
                    f"'{item['item_name']}' in warehouse '{wh['name']}'. "
                    f"Current qty: {current_qty}, requested: {actual_qty}"
                )

        # 5. Batch validation
        batch_id = entry.get("batch_id")
        if item["has_batch"] and actual_qty > 0 and not batch_id:
            warnings.append(
                f"SLE entry {i}: item '{item['item_name']}' has batch tracking "
                f"enabled but no batch_id provided"
            )
        if batch_id:
            batch = conn.execute(
                "SELECT * FROM batch WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise ValueError(f"SLE entry {i}: batch {batch_id} does not exist")
            if batch["item_id"] != item_id:
                raise ValueError(
                    f"SLE entry {i}: batch {batch_id} belongs to item "
                    f"{batch['item_id']}, not {item_id}"
                )
            # Check expiry for outgoing
            if actual_qty < 0 and batch.get("expiry_date"):
                if batch["expiry_date"] < posting_date:
                    warnings.append(
                        f"SLE entry {i}: batch '{batch['batch_name']}' expired on "
                        f"{batch['expiry_date']}"
                    )

        # 6. Serial number validation
        serial_no = entry.get("serial_number")
        if item["has_serial"] and actual_qty != 0 and not serial_no:
            warnings.append(
                f"SLE entry {i}: item '{item['item_name']}' has serial tracking "
                f"enabled but no serial_number provided"
            )

    return warnings


# ---------------------------------------------------------------------------
# Insert SLE Entries
# ---------------------------------------------------------------------------

def insert_sle_entries(
    conn: sqlite3.Connection,
    entries: list[dict],
    voucher_type: str,
    voucher_id: str,
    posting_date: str,
    company_id: str,
) -> list[str]:
    """Insert stock ledger entries within the caller's transaction.

    MUST be called inside an existing transaction. Does NOT commit.

    Steps:
    1. Idempotency check — reject if SLE already exist for this voucher
    2. Run validation
    3. For each entry: compute running balance, valuation rate, insert SLE
       - If item.valuation_method == 'fifo': use FIFO layer logic
       - Otherwise: use moving average (default)

    Returns:
        List of generated SLE IDs.

    Raises:
        ValueError on validation failure or if entries already exist.
    """
    # 1. Idempotency check
    existing = conn.execute(
        "SELECT COUNT(*) as cnt FROM stock_ledger_entry "
        "WHERE voucher_type = ? AND voucher_id = ? AND is_cancelled = 0",
        (voucher_type, voucher_id),
    ).fetchone()
    if existing["cnt"] > 0:
        raise ValueError(
            f"SLE entries already exist for voucher ({voucher_type}, {voucher_id})"
        )

    # 1b. voucher_type validity from voucher_type_registry (M0 phase 3a): the
    # hardcoded CHECK on stock_ledger_entry.voucher_type was dropped, so the type
    # is registry-sourced and enforced here on every SLE write (same membership
    # the CHECK enforced; now extensible at runtime).
    if not conn.execute(
        "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? AND target_table = 'stock_ledger_entry' AND is_active = 1",
        (voucher_type,),
    ).fetchone():
        raise ValueError(
            f"voucher_type '{voucher_type}' is not a registered, active type for stock_ledger_entry. "
            f"Register it (add-voucher-type) or run seed-registry-defaults."
        )

    # 2. Validate
    _warnings = validate_stock_entries(conn, entries, posting_date, company_id)

    # 3. Insert with running balance computation
    generated_ids = []
    for entry in entries:
        sle_id = str(uuid.uuid4())
        generated_ids.append(sle_id)

        item_id = entry["item_id"]
        warehouse_id = entry["warehouse_id"]
        actual_qty = to_decimal(entry.get("actual_qty", "0"))
        incoming_rate = to_decimal(entry.get("incoming_rate", "0"))
        # FINDING-010 / ADR-0014: true external receipts (purchase invoice stock
        # leg, GRN, standalone material_receipt) opt in via require_rate=True so a
        # rate-less, no-standard_rate incoming movement refuses loudly instead of
        # silently booking $0. Internal moves (transfer-in, manufacture FG leg,
        # issue, reconciliation) leave it False and keep inheriting valuation.
        require_rate = bool(entry.get("require_rate", False))

        # Determine valuation method for this item
        valuation_method = _get_item_valuation_method(conn, item_id)

        # Get current balance for this item/warehouse
        current = get_stock_balance(conn, item_id, warehouse_id)
        current_qty = to_decimal(current["qty"])
        current_value = to_decimal(current["stock_value"])

        # --- Branch: FIFO vs Moving Average ---
        if valuation_method == "fifo":
            valuation_rate, incoming_rate, new_qty, new_value = _compute_fifo_sle(
                conn, item_id, warehouse_id, posting_date,
                actual_qty, incoming_rate,
                current_qty, current_value,
                voucher_type, voucher_id,
                require_rate,
            )
        else:
            # Moving average (original logic)
            valuation_rate, incoming_rate, new_qty, new_value = _compute_moving_avg_sle(
                conn, item_id,
                actual_qty, incoming_rate,
                current_qty, current_value,
                require_rate,
            )

        new_stock_value = round_currency(new_value)
        stock_value_diff = round_currency(new_stock_value - current_value)
        qty_after = round_currency(new_qty)

        conn.execute(
            """
            INSERT INTO stock_ledger_entry (
                id, posting_date, posting_time, item_id, warehouse_id,
                actual_qty, qty_after_transaction, valuation_rate,
                stock_value, stock_value_difference,
                voucher_type, voucher_id, batch_id, serial_number,
                incoming_rate, is_cancelled, fiscal_year, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, CAST(CURRENT_TIMESTAMP AS TEXT))
            """,
            (
                sle_id,
                posting_date,
                entry.get("posting_time"),
                item_id,
                warehouse_id,
                str(round_currency(actual_qty)),
                str(qty_after),
                str(round_currency(valuation_rate)),
                str(new_stock_value),
                str(stock_value_diff),
                voucher_type,
                voucher_id,
                entry.get("batch_id"),
                entry.get("serial_number"),
                str(round_currency(incoming_rate)),
                entry.get("fiscal_year"),
            ),
        )

    return generated_ids


def _compute_moving_avg_sle(
    conn: sqlite3.Connection,
    item_id: str,
    actual_qty: Decimal,
    incoming_rate: Decimal,
    current_qty: Decimal,
    current_value: Decimal,
    require_rate: bool = False,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Compute valuation using moving average method.

    Returns: (valuation_rate, incoming_rate, new_qty, new_value)
    """
    if actual_qty > 0:
        # Incoming: use provided rate or fall back to item standard_rate
        if incoming_rate <= 0:
            item_row = conn.execute(
                "SELECT standard_rate FROM item WHERE id = ?", (item_id,)
            ).fetchone()
            incoming_rate = to_decimal(item_row["standard_rate"]) if item_row else Decimal("0")
        # FINDING-010 / ADR-0014: a true external receipt (require_rate=True) must
        # carry a positive cost; refuse rather than silently book inventory at $0.
        if require_rate and incoming_rate <= 0:
            raise ValueError(
                f"Cannot value incoming stock for item {item_id}: no rate was provided "
                f"and the item has no standard_rate to fall back on. A purchase receipt "
                f"must carry a positive cost. Receive the stock against its purchase order "
                f"or bill (which carries the rate), or set the item's standard cost first "
                f"(add-item / update-item --standard-rate), or restate the rate on the receipt."
            )
        incoming_value = round_currency(actual_qty * incoming_rate)
        new_qty = current_qty + actual_qty
        new_value = current_value + incoming_value
        if new_qty > 0:
            valuation_rate = round_currency(new_value / new_qty)
        else:
            valuation_rate = incoming_rate
    else:
        # Outgoing: use current valuation rate
        if current_qty > 0:
            valuation_rate = round_currency(current_value / current_qty)
        else:
            valuation_rate = Decimal("0")
        outgoing_value = round_currency(abs(actual_qty) * valuation_rate)
        new_qty = current_qty + actual_qty  # actual_qty is negative
        new_value = current_value - outgoing_value
        incoming_rate = Decimal("0")  # Not an incoming entry

    return valuation_rate, incoming_rate, new_qty, new_value


def _compute_fifo_sle(
    conn: sqlite3.Connection,
    item_id: str,
    warehouse_id: str,
    posting_date: str,
    actual_qty: Decimal,
    incoming_rate: Decimal,
    current_qty: Decimal,
    current_value: Decimal,
    voucher_type: str,
    voucher_id: str,
    require_rate: bool = False,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Compute valuation using FIFO method.

    For incoming: creates a new FIFO layer and computes weighted average across
    all remaining layers for the SLE valuation_rate.

    For outgoing: consumes oldest layers first. The valuation_rate on the SLE
    reflects the weighted average cost of the consumed layers (i.e., the true
    FIFO cost of goods issued).

    Returns: (valuation_rate, incoming_rate, new_qty, new_value)
    """
    if actual_qty > 0:
        # --- FIFO INCOMING ---
        # Use provided rate or fall back to item standard_rate
        if incoming_rate <= 0:
            item_row = conn.execute(
                "SELECT standard_rate FROM item WHERE id = ?", (item_id,)
            ).fetchone()
            incoming_rate = to_decimal(item_row["standard_rate"]) if item_row else Decimal("0")

        # FINDING-010 / ADR-0014: a true external receipt (require_rate=True) must
        # carry a positive cost; refuse before any zero-cost FIFO layer is created.
        if require_rate and incoming_rate <= 0:
            raise ValueError(
                f"Cannot value incoming stock for item {item_id}: no rate was provided "
                f"and the item has no standard_rate to fall back on. A purchase receipt "
                f"must carry a positive cost. Receive the stock against its purchase order "
                f"or bill (which carries the rate), or set the item's standard cost first "
                f"(add-item / update-item --standard-rate), or restate the rate on the receipt."
            )

        # Create a new FIFO layer for this incoming stock
        _insert_fifo_layer(
            conn, item_id, warehouse_id, posting_date,
            actual_qty, incoming_rate, voucher_type, voucher_id,
        )

        incoming_value = round_currency(actual_qty * incoming_rate)
        new_qty = current_qty + actual_qty
        new_value = current_value + incoming_value

        # SLE valuation_rate = total value / total qty (weighted average of all layers)
        if new_qty > 0:
            valuation_rate = round_currency(new_value / new_qty)
        else:
            valuation_rate = incoming_rate

    else:
        # --- FIFO OUTGOING ---
        # Consume layers oldest-first; get the weighted cost of consumed layers
        abs_qty = abs(actual_qty)
        fifo_rate, _consumed = _consume_fifo_layers(
            conn, item_id, warehouse_id, abs_qty,
        )

        # The valuation_rate on the SLE = FIFO cost of goods issued
        valuation_rate = fifo_rate
        outgoing_value = round_currency(abs_qty * fifo_rate)
        new_qty = current_qty + actual_qty  # actual_qty is negative
        new_value = current_value - outgoing_value
        incoming_rate = Decimal("0")  # Not an incoming entry

    return valuation_rate, incoming_rate, new_qty, new_value


# ---------------------------------------------------------------------------
# Reverse SLE Entries
# ---------------------------------------------------------------------------

def reverse_sle_entries(
    conn: sqlite3.Connection,
    voucher_type: str,
    voucher_id: str,
    posting_date: str,
) -> list[str]:
    """Create reversing SLE entries for cancellation.

    Finds all SLE rows for the voucher where is_cancelled=0.
    Creates mirror entries with negated actual_qty.
    Sets is_cancelled=1 on originals.

    For FIFO items with incoming vouchers, also zeros out the FIFO layers
    created by this voucher.

    Does NOT commit — caller manages transaction.

    Returns:
        List of new reversal SLE IDs.

    Raises:
        ValueError if no entries found.
    """
    originals = conn.execute(
        "SELECT * FROM stock_ledger_entry "
        "WHERE voucher_type = ? AND voucher_id = ? AND is_cancelled = 0",
        (voucher_type, voucher_id),
    ).fetchall()

    if not originals:
        raise ValueError(
            f"No active SLE entries found for voucher ({voucher_type}, {voucher_id})"
        )

    # Reverse FIFO layers created by this voucher (for incoming cancellations).
    # This is safe to call even for moving_average items — it's a no-op if no
    # layers exist for this voucher.
    _reverse_fifo_layers(conn, voucher_type, voucher_id)

    reversal_ids = []

    for orig in originals:
        reversal_id = str(uuid.uuid4())
        reversal_ids.append(reversal_id)

        item_id = orig["item_id"]
        warehouse_id = orig["warehouse_id"]
        orig_qty = to_decimal(orig["actual_qty"])
        reversal_qty = -orig_qty

        # Get current balance to compute new running totals
        current = get_stock_balance(conn, item_id, warehouse_id)
        current_qty = to_decimal(current["qty"])
        current_value = to_decimal(current["stock_value"])

        # Compute reversal impact
        orig_value_diff = to_decimal(orig["stock_value_difference"])
        new_value = current_value - orig_value_diff
        new_qty = current_qty + reversal_qty

        if new_qty > 0:
            valuation_rate = round_currency(new_value / new_qty)
        elif new_qty == 0:
            valuation_rate = Decimal("0")
        else:
            valuation_rate = to_decimal(orig["valuation_rate"])

        new_stock_value = round_currency(new_value)
        stock_value_diff = round_currency(-orig_value_diff)

        # Insert reversal entry (also marked as cancelled — it's an audit record)
        conn.execute(
            """
            INSERT INTO stock_ledger_entry (
                id, posting_date, posting_time, item_id, warehouse_id,
                actual_qty, qty_after_transaction, valuation_rate,
                stock_value, stock_value_difference,
                voucher_type, voucher_id, batch_id, serial_number,
                incoming_rate, is_cancelled, fiscal_year, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, CAST(CURRENT_TIMESTAMP AS TEXT))
            """,
            (
                reversal_id,
                posting_date,
                None,
                item_id,
                warehouse_id,
                str(round_currency(reversal_qty)),
                str(round_currency(new_qty)),
                str(round_currency(valuation_rate)),
                str(new_stock_value),
                str(stock_value_diff),
                voucher_type,
                voucher_id,
                orig["batch_id"],
                orig["serial_number"],
                "0",
                orig["fiscal_year"],
            ),
        )

        # Mark original as cancelled
        conn.execute(
            "UPDATE stock_ledger_entry SET is_cancelled = 1 WHERE id = ?",
            (orig["id"],),
        )

    return reversal_ids


# ---------------------------------------------------------------------------
# Query: Stock Balance
# ---------------------------------------------------------------------------

def get_stock_balance(
    conn: sqlite3.Connection,
    item_id: str,
    warehouse_id: str,
    as_of_date: Optional[str] = None,
) -> dict:
    """Get current stock balance for item in warehouse.

    Computes from SLE entries (sum of actual_qty where is_cancelled=0).

    Args:
        conn: SQLite connection.
        item_id: Item to query.
        warehouse_id: Warehouse to query.
        as_of_date: Optional cutoff date. None = all time.

    Returns:
        {"qty": str, "valuation_rate": str, "stock_value": str}
    """
    if as_of_date:
        row = conn.execute(
            f"""
            SELECT
                COALESCE(decimal_sum(actual_qty), '0') as total_qty,
                COALESCE(
                    (SELECT stock_value FROM stock_ledger_entry
                     WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0
                       AND posting_date <= ?
                     ORDER BY {latest_insert_order()} LIMIT 1),
                    '0'
                ) as last_stock_value,
                COALESCE(
                    (SELECT valuation_rate FROM stock_ledger_entry
                     WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0
                       AND posting_date <= ?
                     ORDER BY {latest_insert_order()} LIMIT 1),
                    '0'
                ) as last_valuation_rate
            FROM stock_ledger_entry
            WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0
              AND posting_date <= ?
            """,
            (
                item_id, warehouse_id, as_of_date,
                item_id, warehouse_id, as_of_date,
                item_id, warehouse_id, as_of_date,
            ),
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT
                COALESCE(decimal_sum(actual_qty), '0') as total_qty,
                COALESCE(
                    (SELECT stock_value FROM stock_ledger_entry
                     WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0
                     ORDER BY {latest_insert_order()} LIMIT 1),
                    '0'
                ) as last_stock_value,
                COALESCE(
                    (SELECT valuation_rate FROM stock_ledger_entry
                     WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0
                     ORDER BY {latest_insert_order()} LIMIT 1),
                    '0'
                ) as last_valuation_rate
            FROM stock_ledger_entry
            WHERE item_id = ? AND warehouse_id = ? AND is_cancelled = 0
            """,
            (
                item_id, warehouse_id,
                item_id, warehouse_id,
                item_id, warehouse_id,
            ),
        ).fetchone()

    qty = to_decimal(str(row["total_qty"]))
    stock_value = to_decimal(str(row["last_stock_value"]))
    valuation_rate = to_decimal(str(row["last_valuation_rate"]))

    # Recalculate stock_value from qty * rate if qty-based is more accurate
    if qty > 0 and valuation_rate > 0:
        stock_value = round_currency(qty * valuation_rate)

    return {
        "qty": str(round_currency(qty)),
        "valuation_rate": str(round_currency(valuation_rate)),
        "stock_value": str(round_currency(stock_value)),
    }


# ---------------------------------------------------------------------------
# Query: Valuation Rate
# ---------------------------------------------------------------------------

def get_valuation_rate(
    conn: sqlite3.Connection,
    item_id: str,
    warehouse_id: Optional[str] = None,
) -> Decimal:
    """Get current valuation rate for an item.

    Uses moving average: total stock value / total qty across warehouses
    (or for a specific warehouse if provided).

    Args:
        conn: SQLite connection.
        item_id: Item to query.
        warehouse_id: Optional specific warehouse. None = all warehouses.

    Returns:
        Decimal valuation rate.
    """
    if warehouse_id:
        balance = get_stock_balance(conn, item_id, warehouse_id)
        rate = to_decimal(balance["valuation_rate"])
        if rate > 0:
            return rate
    else:
        # Aggregate across all warehouses
        row = conn.execute(
            """
            SELECT
                COALESCE(decimal_sum(actual_qty), '0') as total_qty,
                COALESCE(decimal_sum(stock_value_difference), '0') as total_value
            FROM stock_ledger_entry
            WHERE item_id = ? AND is_cancelled = 0
            """,
            (item_id,),
        ).fetchone()
        total_qty = to_decimal(str(row["total_qty"]))
        total_value = to_decimal(str(row["total_value"]))
        if total_qty > 0:
            return round_currency(total_value / total_qty)

    # Fallback to item standard rate
    item = conn.execute(
        "SELECT standard_rate FROM item WHERE id = ?", (item_id,)
    ).fetchone()
    if item:
        return to_decimal(item["standard_rate"])
    return Decimal("0")


# ---------------------------------------------------------------------------
# Perpetual Inventory GL Entries
# ---------------------------------------------------------------------------

def create_perpetual_inventory_gl(
    conn: sqlite3.Connection,
    sle_entries: list[dict],
    voucher_type: str,
    voucher_id: str,
    posting_date: str,
    company_id: str,
    expense_account_id: Optional[str] = None,
    cost_center_id: Optional[str] = None,
) -> list[dict]:
    """Generate GL entries for perpetual inventory from SLE entries.

    For each SLE entry, creates balanced GL entries:
    - Receipt (qty > 0): DR warehouse account (Stock-in-Hand), CR contra account
    - Issue (qty < 0): DR contra account, CR warehouse account (Stock-in-Hand)

    The contra account is determined by context:
    - Stock entry receipt: Stock Received Not Billed or Opening Stock account
    - Stock entry issue: COGS / expense account
    - Delivery note: COGS account
    - Purchase receipt: Stock Received Not Billed
    - Stock reconciliation: Stock Adjustment account

    Does NOT insert GL entries — returns the list for the caller to pass
    to gl_posting.insert_gl_entries().

    Args:
        conn: SQLite connection.
        sle_entries: List of SLE entry dicts (must have item_id, warehouse_id,
                     actual_qty, stock_value_difference).
        voucher_type: The voucher type.
        voucher_id: The voucher ID.
        posting_date: Posting date.
        company_id: Company ID.
        expense_account_id: Override contra account (e.g., COGS for delivery).
        cost_center_id: Cost center for P&L accounts.

    Returns:
        List of GL entry dicts ready for gl_posting.insert_gl_entries().
    """
    gl_entries = []

    for entry in sle_entries:
        warehouse_id = entry["warehouse_id"]
        value_diff = to_decimal(entry.get("stock_value_difference", "0"))

        if value_diff == 0:
            continue

        # Get warehouse's linked GL account (Stock-in-Hand)
        wh = conn.execute(
            "SELECT account_id, name FROM warehouse WHERE id = ?",
            (warehouse_id,)
        ).fetchone()
        warehouse_account_id = wh["account_id"] if wh and wh["account_id"] else None

        if not warehouse_account_id:
            # Fallback: find a stock-in-hand account for the company
            stock_acct = conn.execute(
                "SELECT id FROM account WHERE account_type = 'stock' "
                "AND company_id = ? AND is_group = 0 LIMIT 1",
                (company_id,),
            ).fetchone()
            warehouse_account_id = stock_acct["id"] if stock_acct else None

        if not warehouse_account_id:
            raise ValueError(
                f"No Stock-in-Hand account for company {company_id} "
                f"(warehouse {warehouse_id} is not linked to an account and no "
                f"account_type='stock' account exists). Run setup-company, link the "
                f"warehouse to your Inventory account, or add-account "
                f"--account-type stock --root-type asset."
            )

        # Determine contra account
        contra_account_id = expense_account_id
        if not contra_account_id:
            # Default: look for Stock Received Not Billed or COGS
            if to_decimal(entry.get("actual_qty", "0")) > 0:
                contra = conn.execute(
                    "SELECT id FROM account WHERE account_type = 'stock_received_not_billed' "
                    "AND company_id = ? AND is_group = 0 LIMIT 1",
                    (company_id,),
                ).fetchone()
            else:
                contra = conn.execute(
                    "SELECT id FROM account WHERE account_type = 'cost_of_goods_sold' "
                    "AND company_id = ? AND is_group = 0 LIMIT 1",
                    (company_id,),
                ).fetchone()
            contra_account_id = contra["id"] if contra else None

        if not contra_account_id:
            is_receipt = to_decimal(entry.get("actual_qty", "0")) > 0
            needed = "stock_received_not_billed" if is_receipt else "cost_of_goods_sold"
            label = "Stock Received Not Billed" if is_receipt else "Cost of Goods Sold"
            raise ValueError(
                f"No '{label}' account for company {company_id}. A stock "
                f"{'receipt' if is_receipt else 'issue'} cannot post balanced GL without it. "
                f"Run setup-company or add-account --account-type {needed} "
                f"--root-type {'liability' if is_receipt else 'expense'}."
            )

        abs_value = abs(value_diff)

        if value_diff > 0:
            # Incoming stock: DR Stock-in-Hand, CR Contra
            gl_entries.append({
                "account_id": warehouse_account_id,
                "debit": str(round_currency(abs_value)),
                "credit": "0",
            })
            gl_entries.append({
                "account_id": contra_account_id,
                "debit": "0",
                "credit": str(round_currency(abs_value)),
                "cost_center_id": cost_center_id,
            })
        else:
            # Outgoing stock: DR Contra, CR Stock-in-Hand
            gl_entries.append({
                "account_id": contra_account_id,
                "debit": str(round_currency(abs_value)),
                "credit": "0",
                "cost_center_id": cost_center_id,
            })
            gl_entries.append({
                "account_id": warehouse_account_id,
                "debit": "0",
                "credit": str(round_currency(abs_value)),
            })

    return gl_entries
