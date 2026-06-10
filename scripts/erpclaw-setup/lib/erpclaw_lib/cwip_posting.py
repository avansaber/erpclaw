"""CWIP (construction-in-progress) accumulation — shared cross-module write.

`cwip_cost_accumulation` is owned by erpclaw-assets, but the S3 invoice/JE hooks
(AVA-43) must record an accumulation row IN THE SAME TRANSACTION as a purchase
invoice / journal entry submit, which live in erpclaw-buying / erpclaw-journals.
The sanctioned cross-module path (subprocess INVOKE) is a *separate* transaction
and so cannot satisfy the single-transaction requirement.

This module is the agreed resolution: the accumulation write extracted into
shared `erpclaw_lib` infra — exactly as `insert_gl_entries` lives in
`erpclaw_lib.gl_posting` and is called in-transaction by every module. The lib
is sanctioned infrastructure, so ownership is preserved (erpclaw-assets keeps the
action surface; buying/journals call this helper only via the lib).

NEVER commit inside these functions — the caller owns the transaction.

Two posting shapes share `record_cwip_accumulation`:
  - erpclaw-assets `accumulate-cwip-cost`: posts its OWN GL (DR CWIP / CR source,
    voucher_type='cwip_capitalization', voucher_id=accum_id), then records the row.
  - buying/journals hooks: the *document's* submit posts GL (routed to the CWIP
    account); the accumulation row links to that GL leg (no extra GL posted).
"""
import sqlite3
import uuid
from decimal import Decimal

from erpclaw_lib.decimal_utils import to_decimal, round_currency

CWIP_ACCOUNT_TYPE = "capital_work_in_progress"


def get_under_construction_asset(conn: sqlite3.Connection, asset_id: str) -> dict:
    """Fetch an asset row and assert it is under construction.

    Raises ValueError if the asset is missing or not `under_construction` — the
    shared guard for every CWIP accumulation path (standalone + hooks)."""
    row = conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise ValueError(f"CWIP asset {asset_id} not found")
    asset = dict(row)
    if asset["status"] != "under_construction":
        raise ValueError(
            f"CWIP accumulation requires an under_construction asset; "
            f"'{asset.get('naming_series') or asset_id}' is '{asset['status']}'. "
            f"Start one with add-cwip.")
    return asset


def cwip_account_for_asset(conn: sqlite3.Connection, asset_id: str):
    """The single capital_work_in_progress account this asset has accumulated to,
    read from the DR leg each submitted accumulation row points at via gl_entry_id.
    Returns the account id or None (no accumulations yet). Raises ValueError if
    accumulations span more than one CWIP account (unsupported — would split the
    transfer CR leg).

    Derives from the accumulation row's gl_entry_id rather than a fixed
    voucher_type, so it sees costs accumulated via the standalone
    accumulate-cwip-cost path (voucher_type='cwip_capitalization') AND via the
    AVA-43 invoice/JE hooks (voucher_type='purchase_invoice' / 'journal_entry')
    uniformly — every accumulation stamps gl_entry_id with its DR CWIP leg."""
    rows = conn.execute(
        "SELECT g.account_id AS account_id, g.debit AS debit "
        "FROM cwip_cost_accumulation a JOIN gl_entry g ON g.id = a.gl_entry_id "
        "WHERE a.asset_id = ? AND a.status = 'submitted' AND g.is_cancelled = 0",
        (asset_id,)).fetchall()
    accts = {r["account_id"] for r in rows if to_decimal(r["debit"]) > 0}
    if len(accts) > 1:
        raise ValueError("Asset has accumulations across multiple CWIP accounts; unsupported.")
    return next(iter(accts)) if accts else None


def resolve_cwip_account(conn: sqlite3.Connection, asset_id: str, company_id: str) -> str:
    """The CWIP account to debit for a new accumulation against `asset_id`.

    Prefers the account the asset has already accumulated to (one CWIP account per
    asset). Falls back to the company's single non-group capital_work_in_progress
    account. Raises ValueError if none exists or the choice is ambiguous (multiple
    company CWIP accounts and no prior accumulation) — the caller should then route
    the cost explicitly (e.g. a journal entry that debits the intended account)."""
    prior = cwip_account_for_asset(conn, asset_id)
    if prior:
        return prior
    rows = conn.execute(
        "SELECT id FROM account WHERE account_type = ? AND company_id = ? AND is_group = 0",
        (CWIP_ACCOUNT_TYPE, company_id)).fetchall()
    if not rows:
        raise ValueError(
            f"No {CWIP_ACCOUNT_TYPE} account for company {company_id}; "
            f"create one before routing a document to CWIP.")
    if len(rows) > 1:
        raise ValueError(
            f"Company {company_id} has multiple {CWIP_ACCOUNT_TYPE} accounts and the "
            f"asset has no prior accumulation to disambiguate; accumulate once via "
            f"accumulate-cwip-cost (or a journal entry that debits the intended "
            f"CWIP account) first.")
    return rows[0]["id"]


def cwip_debit_legs(conn: sqlite3.Connection, gl_entries: list[dict]) -> list[tuple]:
    """Return [(index, account_id, Decimal debit)] for every gl_entry whose account
    is a capital_work_in_progress account debited > 0. Used by the journal-entry
    hook to locate the user-supplied CWIP leg(s) the accumulation attaches to."""
    legs = []
    for i, e in enumerate(gl_entries):
        debit = to_decimal(e.get("debit", "0"))
        if debit <= 0:
            continue
        row = conn.execute(
            "SELECT account_type FROM account WHERE id = ?", (e["account_id"],)).fetchone()
        if row and row["account_type"] == CWIP_ACCOUNT_TYPE:
            legs.append((i, e["account_id"], debit))
    return legs


def record_cwip_accumulation(conn: sqlite3.Connection, asset: dict, amount,
                             *, source_voucher_type: str, source_voucher_id: str,
                             gl_entry_id: str, accumulated_at: str,
                             notes: str = None, accum_id: str = None) -> str:
    """Insert one immutable cwip_cost_accumulation row and bump the asset's
    gross_value + current_book_value. Caller owns the transaction and has already
    posted the GL that `gl_entry_id` points at. Mutates `asset` in place so a
    caller that accumulates then reads the carrying amount sees updated figures.

    Returns the accumulation id (generated when `accum_id` is None)."""
    amount = round_currency(to_decimal(amount))
    accum_id = accum_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO cwip_cost_accumulation "
        "(id, asset_id, source_voucher_type, source_voucher_id, accumulated_amount, "
        " gl_entry_id, accumulated_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (accum_id, asset["id"], source_voucher_type, source_voucher_id,
         str(amount), gl_entry_id, accumulated_at, notes))

    new_gross = round_currency(to_decimal(asset["gross_value"]) + amount)
    new_book = round_currency(to_decimal(asset["current_book_value"]) + amount)
    conn.execute(
        "UPDATE asset SET gross_value = ?, current_book_value = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (str(new_gross), str(new_book), asset["id"]))
    asset["gross_value"] = str(new_gross)
    asset["current_book_value"] = str(new_book)
    return accum_id


def reverse_cwip_accumulations(conn: sqlite3.Connection, source_voucher_type: str,
                              source_voucher_id: str) -> int:
    """Unwind the accumulation rows a now-cancelled document created: mark each
    'reversed' and decrement its asset's gross_value + current_book_value. The
    document's own GL reversal (reverse_gl_entries) already mirrors the CWIP debit
    leg, so this only repairs the accumulation row + asset carrying amount. Caller
    owns the transaction (no commit). Returns the number of accumulations reversed.

    Used by cancel-purchase-invoice / cancel-journal-entry (AVA-43). The normal
    case is a pre-transfer asset; cancelling a source document after the asset has
    been capitalised (transfer-cwip-to-asset) is an accounting event outside S3
    scope — the carrying-value decrement here assumes the cost is still in CWIP."""
    rows = conn.execute(
        "SELECT id, asset_id, accumulated_amount FROM cwip_cost_accumulation "
        "WHERE source_voucher_type = ? AND source_voucher_id = ? AND status = 'submitted'",
        (source_voucher_type, source_voucher_id)).fetchall()
    for r in rows:
        amount = round_currency(to_decimal(r["accumulated_amount"]))
        conn.execute(
            "UPDATE cwip_cost_accumulation SET status = 'reversed' WHERE id = ?", (r["id"],))
        asset = conn.execute(
            "SELECT gross_value, current_book_value FROM asset WHERE id = ?",
            (r["asset_id"],)).fetchone()
        if asset:
            new_gross = round_currency(to_decimal(asset["gross_value"]) - amount)
            new_book = round_currency(to_decimal(asset["current_book_value"]) - amount)
            conn.execute(
                "UPDATE asset SET gross_value = ?, current_book_value = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (str(new_gross), str(new_book), r["asset_id"]))
    return len(rows)
