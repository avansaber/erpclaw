#!/usr/bin/env python3
"""ERPClaw Integrations Skill -- db_query.py

Unified external integrations: Plaid bank sync, Stripe payments, S3 cloud backups.
All external API calls are mocked -- no real network requests are made.

Usage: python3 db_query.py --action <action-name> [--flags ...]
Output: JSON to stdout, exit 0 on success, exit 1 on error.
"""
import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

# ---------------------------------------------------------------------------
# Shared library
# ---------------------------------------------------------------------------
try:
    sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
    from erpclaw_lib.db import get_connection, ensure_db_exists, DEFAULT_DB_PATH
    from erpclaw_lib.decimal_utils import to_decimal, round_currency
    from erpclaw_lib.validation import check_input_lengths
    from erpclaw_lib.response import ok, err, row_to_dict, rows_to_list
    from erpclaw_lib.audit import audit
    from erpclaw_lib.dependencies import check_required_tables
except ImportError:
    import json as _json
    print(_json.dumps({"status": "error", "error": "ERPClaw foundation not installed. Install erpclaw-setup first: clawhub install erpclaw-setup", "suggestion": "clawhub install erpclaw-setup"}))
    sys.exit(1)

REQUIRED_TABLES = ["company"]

SKILL_NAME = "erpclaw-integrations"


# ===========================================================================
#
#  PLAID — Bank Account Integration (mock)
#
# ===========================================================================

# ---------------------------------------------------------------------------
# Mock bank transaction data
# ---------------------------------------------------------------------------
MOCK_TRANSACTIONS = [
    {
        "name": "Starbucks Store #12345",
        "amount": "-4.85",
        "category": "Food and Drink",
        "merchant_name": "Starbucks",
    },
    {
        "name": "Amazon Prime Membership",
        "amount": "-14.99",
        "category": "Service",
        "merchant_name": "Amazon",
    },
    {
        "name": "Whole Foods Market #10234",
        "amount": "-87.32",
        "category": "Shops",
        "merchant_name": "Whole Foods",
    },
    {
        "name": "Comcast Internet Bill",
        "amount": "-125.00",
        "category": "Service",
        "merchant_name": "Comcast",
    },
    {
        "name": "Wire Transfer from Acme Corp",
        "amount": "2500.00",
        "category": "Transfer",
        "merchant_name": "Acme Corp",
    },
]


# ---------------------------------------------------------------------------
# Plaid helpers
# ---------------------------------------------------------------------------

def _plaid_get_config_or_err(conn, company_id: str) -> dict:
    """Fetch Plaid config for a company. Calls err() if not found."""
    row = conn.execute(
        "SELECT * FROM plaid_config WHERE company_id = ?", (company_id,)
    ).fetchone()
    if not row:
        err(f"Plaid config not found for company {company_id}",
            suggestion="Run 'configure-plaid' first to set up Plaid credentials.")
    return row_to_dict(row)


def _plaid_get_linked_account_or_err(conn, linked_account_id: str) -> dict:
    """Fetch a linked account by ID. Calls err() if not found."""
    row = conn.execute(
        "SELECT * FROM plaid_linked_account WHERE id = ?", (linked_account_id,)
    ).fetchone()
    if not row:
        err(f"Linked account {linked_account_id} not found",
            suggestion="Use 'list-transactions' or check the linked account ID.")
    return row_to_dict(row)


# ---------------------------------------------------------------------------
# Plaid actions
# ---------------------------------------------------------------------------

def configure_plaid(conn, args):
    """Save Plaid configuration (client_id, secret, environment) for a company.

    Each company can have exactly one Plaid config (UNIQUE constraint on company_id).
    """
    company_id = args.company_id
    if not company_id:
        err("--company-id is required")

    client_id = args.client_id
    if not client_id:
        err("--client-id is required")

    secret = args.secret
    if not secret:
        err("--secret is required")

    environment = args.environment or "sandbox"
    if environment not in ("sandbox", "development", "production"):
        err(f"Invalid environment '{environment}'. Must be: sandbox, development, production")

    # Validate company exists
    if not conn.execute("SELECT id FROM company WHERE id = ?", (company_id,)).fetchone():
        err(f"Company {company_id} not found")

    # Check for existing config
    existing = conn.execute(
        "SELECT id FROM plaid_config WHERE company_id = ?", (company_id,)
    ).fetchone()
    if existing:
        err(f"Config already exists for this company",
            suggestion="Each company can only have one Plaid configuration.")

    # Encrypt sensitive credentials before storing
    from erpclaw_lib.crypto import encrypt_field, derive_key
    from erpclaw_lib.db import DEFAULT_DB_PATH
    _fk = derive_key(os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH),
                      b"erpclaw-field-key")
    enc_client_id = encrypt_field(client_id, _fk)
    enc_secret = encrypt_field(secret, _fk)

    config_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO plaid_config (id, company_id, client_id, secret, environment)
           VALUES (?, ?, ?, ?, ?)""",
        (config_id, company_id, enc_client_id, enc_secret, environment),
    )

    audit(conn, SKILL_NAME, "configure-plaid", "plaid_config", config_id,
          new_values={"company_id": company_id, "environment": environment})
    conn.commit()

    ok({"config_id": config_id, "company_id": company_id,
        "environment": environment, "message": "Plaid configured successfully"})


def link_account(conn, args):
    """Mock-link a bank account via Plaid.

    Generates a fake access token and creates a plaid_linked_account record.
    In production, this would involve the Plaid Link flow and token exchange.
    """
    company_id = args.company_id
    if not company_id:
        err("--company-id is required")

    institution_name = args.institution_name
    if not institution_name:
        err("--institution-name is required")

    account_name = args.account_name
    if not account_name:
        err("--account-name is required")

    account_type = args.account_type
    if not account_type:
        err("--account-type is required")

    account_mask = args.account_mask
    if not account_mask:
        err("--account-mask is required")

    # Validate company exists
    if not conn.execute("SELECT id FROM company WHERE id = ?", (company_id,)).fetchone():
        err(f"Company {company_id} not found")

    # Validate Plaid config exists for this company
    _plaid_get_config_or_err(conn, company_id)

    # Validate ERP account if provided
    erp_account_id = args.erp_account_id
    if erp_account_id:
        if not conn.execute("SELECT id FROM account WHERE id = ?",
                            (erp_account_id,)).fetchone():
            err(f"ERP account {erp_account_id} not found")

    # Generate mock access token
    access_token = f"mock-access-{uuid.uuid4()}"

    linked_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO plaid_linked_account
           (id, company_id, access_token, institution_name, account_name,
            account_type, account_mask, erp_account_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (linked_id, company_id, access_token, institution_name,
         account_name, account_type, account_mask, erp_account_id),
    )

    audit(conn, SKILL_NAME, "link-account", "plaid_linked_account", linked_id,
          new_values={"institution_name": institution_name,
                      "account_name": account_name,
                      "account_type": account_type})
    conn.commit()

    ok({"linked_account_id": linked_id,
        "institution_name": institution_name,
        "account_name": account_name,
        "account_type": account_type,
        "account_mask": account_mask,
        "access_token": access_token,
        "message": "Bank account linked successfully (mock)"})


def sync_transactions(conn, args):
    """Mock-sync bank transactions from Plaid.

    Generates 5 sample transactions with realistic merchant names and amounts.
    Uses a deterministic seed based on the linked account ID so repeated syncs
    with the same date range produce the same transaction IDs (idempotent).
    """
    linked_account_id = args.linked_account_id
    if not linked_account_id:
        err("--linked-account-id is required")

    linked = _plaid_get_linked_account_or_err(conn, linked_account_id)

    # Determine date range for mock transactions
    to_date = args.to_date or datetime.now().strftime("%Y-%m-%d")
    from_date = args.from_date or (
        datetime.strptime(to_date, "%Y-%m-%d") - timedelta(days=30)
    ).strftime("%Y-%m-%d")

    # Generate mock transactions with deterministic IDs based on linked account
    # This ensures idempotency: same linked account + same data = same txn IDs
    base_date = datetime.strptime(from_date, "%Y-%m-%d")
    created_count = 0
    skipped_count = 0
    transactions = []

    for i, mock in enumerate(MOCK_TRANSACTIONS):
        # Deterministic transaction ID: based on linked account + index
        txn_id = f"mock-txn-{linked_account_id[:8]}-{i:04d}"
        txn_date = (base_date + timedelta(days=i * 5 + 1)).strftime("%Y-%m-%d")

        # Check if already synced (idempotent)
        existing = conn.execute(
            """SELECT id FROM plaid_transaction
               WHERE plaid_linked_account_id = ? AND plaid_transaction_id = ?""",
            (linked_account_id, txn_id),
        ).fetchone()

        if existing:
            skipped_count += 1
            continue

        row_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO plaid_transaction
               (id, plaid_linked_account_id, plaid_transaction_id, date,
                amount, name, category, merchant_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (row_id, linked_account_id, txn_id, txn_date,
             mock["amount"], mock["name"], mock["category"],
             mock["merchant_name"]),
        )
        created_count += 1
        transactions.append({
            "id": row_id,
            "plaid_transaction_id": txn_id,
            "date": txn_date,
            "amount": mock["amount"],
            "name": mock["name"],
            "merchant_name": mock["merchant_name"],
            "match_status": "unmatched",
        })

    # Update last_synced_at
    conn.execute(
        """UPDATE plaid_linked_account SET last_synced_at = datetime('now'),
           updated_at = datetime('now') WHERE id = ?""",
        (linked_account_id,),
    )

    audit(conn, SKILL_NAME, "sync-transactions", "plaid_linked_account",
          linked_account_id,
          new_values={"created": created_count, "skipped": skipped_count})
    conn.commit()

    ok({"linked_account_id": linked_account_id,
        "created": created_count,
        "skipped": skipped_count,
        "transactions": transactions,
        "message": f"Synced {created_count} new transactions ({skipped_count} already existed)"})


def match_transactions(conn, args):
    """Auto-match unmatched plaid_transactions to gl_entries.

    Matching criteria:
    - ABS(plaid_transaction.amount) == ABS(gl_entry.debit - gl_entry.credit)
    - gl_entry.posting_date within +/- 3 days of plaid_transaction.date
    - gl_entry.is_cancelled = 0
    - gl_entry.account_id matches the linked account's erp_account_id (if set)
    """
    linked_account_id = args.linked_account_id
    if not linked_account_id:
        err("--linked-account-id is required")

    linked = _plaid_get_linked_account_or_err(conn, linked_account_id)
    erp_account_id = linked.get("erp_account_id")

    # Get all unmatched transactions for this linked account
    unmatched = conn.execute(
        """SELECT id, plaid_transaction_id, date, amount, name
           FROM plaid_transaction
           WHERE plaid_linked_account_id = ? AND match_status = 'unmatched'
           ORDER BY date""",
        (linked_account_id,),
    ).fetchall()

    matched_count = 0
    unmatched_count = 0
    matches = []

    for txn in unmatched:
        txn_dict = row_to_dict(txn)
        txn_amount = abs(to_decimal(txn_dict["amount"]))
        txn_date = txn_dict["date"]

        # Calculate date window: +/- 3 days
        dt = datetime.strptime(txn_date, "%Y-%m-%d")
        date_from = (dt - timedelta(days=3)).strftime("%Y-%m-%d")
        date_to = (dt + timedelta(days=3)).strftime("%Y-%m-%d")

        # Build GL query
        if erp_account_id:
            gl_row = conn.execute(
                """SELECT id, posting_date, debit, credit
                   FROM gl_entry
                   WHERE account_id = ?
                     AND posting_date >= ? AND posting_date <= ?
                     AND is_cancelled = 0
                     AND id NOT IN (
                         SELECT matched_gl_entry_id FROM plaid_transaction
                         WHERE matched_gl_entry_id IS NOT NULL
                     )
                   ORDER BY posting_date
                   LIMIT 50""",
                (erp_account_id, date_from, date_to),
            ).fetchall()
        else:
            gl_row = conn.execute(
                """SELECT id, posting_date, debit, credit
                   FROM gl_entry
                   WHERE posting_date >= ? AND posting_date <= ?
                     AND is_cancelled = 0
                     AND id NOT IN (
                         SELECT matched_gl_entry_id FROM plaid_transaction
                         WHERE matched_gl_entry_id IS NOT NULL
                     )
                   ORDER BY posting_date
                   LIMIT 50""",
                (date_from, date_to),
            ).fetchall()

        # Find a GL entry with matching amount
        found_match = False
        for gl in gl_row:
            gl_dict = row_to_dict(gl)
            gl_debit = to_decimal(gl_dict["debit"] or "0")
            gl_credit = to_decimal(gl_dict["credit"] or "0")
            gl_net = abs(gl_debit - gl_credit)

            if gl_net == txn_amount:
                # Match found
                conn.execute(
                    """UPDATE plaid_transaction
                       SET matched_gl_entry_id = ?, match_status = 'auto_matched'
                       WHERE id = ?""",
                    (gl_dict["id"], txn_dict["id"]),
                )
                matched_count += 1
                matches.append({
                    "transaction_id": txn_dict["id"],
                    "transaction_name": txn_dict["name"],
                    "transaction_amount": txn_dict["amount"],
                    "gl_entry_id": gl_dict["id"],
                    "gl_posting_date": gl_dict["posting_date"],
                })
                found_match = True
                break

        if not found_match:
            unmatched_count += 1

    audit(conn, SKILL_NAME, "match-transactions", "plaid_linked_account",
          linked_account_id,
          new_values={"matched": matched_count, "unmatched": unmatched_count})
    conn.commit()

    ok({"linked_account_id": linked_account_id,
        "matched": matched_count,
        "unmatched": unmatched_count,
        "matches": matches,
        "message": f"Matched {matched_count} transactions, {unmatched_count} remain unmatched"})


def list_transactions(conn, args):
    """List synced transactions for a linked account with optional filters."""
    linked_account_id = args.linked_account_id
    if not linked_account_id:
        err("--linked-account-id is required")

    # Verify linked account exists
    _plaid_get_linked_account_or_err(conn, linked_account_id)

    conditions = ["pt.plaid_linked_account_id = ?"]
    params = [linked_account_id]

    if args.match_status:
        conditions.append("pt.match_status = ?")
        params.append(args.match_status)
    if args.from_date:
        conditions.append("pt.date >= ?")
        params.append(args.from_date)
    if args.to_date:
        conditions.append("pt.date <= ?")
        params.append(args.to_date)

    where = " AND ".join(conditions)

    count_row = conn.execute(
        f"SELECT COUNT(*) FROM plaid_transaction pt WHERE {where}", params
    ).fetchone()
    total_count = count_row[0]

    limit = int(args.limit) if args.limit else 20
    offset = int(args.offset) if args.offset else 0
    params.extend([limit, offset])

    rows = conn.execute(
        f"""SELECT pt.id, pt.plaid_transaction_id, pt.date, pt.amount,
               pt.name, pt.category, pt.merchant_name,
               pt.matched_gl_entry_id, pt.match_status
           FROM plaid_transaction pt
           WHERE {where}
           ORDER BY pt.date DESC
           LIMIT ? OFFSET ?""",
        params,
    ).fetchall()

    ok({"transactions": [row_to_dict(r) for r in rows],
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total_count})


def plaid_status(conn, args):
    """Show Plaid integration status: config count, linked accounts, transactions."""
    company_id = args.company_id
    if not company_id:
        row = conn.execute("SELECT id FROM company LIMIT 1").fetchone()
        if not row:
            err("No company found. Create one with erpclaw-setup first.",
                suggestion="Run 'tutorial' to create a demo company, or 'setup company' to create your own.")
        company_id = row["id"]

    # Config status
    config = conn.execute(
        "SELECT id, environment, status FROM plaid_config WHERE company_id = ?",
        (company_id,),
    ).fetchone()

    config_info = None
    if config:
        config_dict = row_to_dict(config)
        config_info = {
            "config_id": config_dict["id"],
            "environment": config_dict["environment"],
            "status": config_dict["status"],
        }

    # Linked accounts
    accounts = conn.execute(
        """SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active
           FROM plaid_linked_account WHERE company_id = ?""",
        (company_id,),
    ).fetchone()

    # Transaction counts by match status
    txn_counts = conn.execute(
        """SELECT pt.match_status, COUNT(*) AS cnt
           FROM plaid_transaction pt
           JOIN plaid_linked_account pla ON pt.plaid_linked_account_id = pla.id
           WHERE pla.company_id = ?
           GROUP BY pt.match_status""",
        (company_id,),
    ).fetchall()

    txn_summary = {}
    total_txns = 0
    for row in txn_counts:
        d = row_to_dict(row)
        txn_summary[d["match_status"]] = d["cnt"]
        total_txns += d["cnt"]

    return {
        "config": config_info,
        "linked_accounts": {
            "total": accounts["total"] or 0,
            "active": accounts["active"] or 0,
        },
        "transactions": {
            "total": total_txns,
            **txn_summary,
        },
    }


# ===========================================================================
#
#  STRIPE -- Payment Gateway Integration (mock)
#
# ===========================================================================

# ---------------------------------------------------------------------------
# Stripe helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _mock_stripe_id(prefix: str) -> str:
    """Generate a realistic-looking mock Stripe ID."""
    short = uuid.uuid4().hex[:24]
    return f"{prefix}_mock_{short}"


def _stripe_get_config(conn, company_id: str):
    """Fetch stripe config for a company. Returns dict or calls err()."""
    row = conn.execute(
        "SELECT * FROM stripe_config WHERE company_id = ? AND status = 'active'",
        (company_id,),
    ).fetchone()
    if not row:
        err(f"Stripe not configured for company {company_id}",
            suggestion="Use 'configure-stripe' action to set up Stripe credentials first.")
    return row_to_dict(row)


# ---------------------------------------------------------------------------
# Stripe actions
# ---------------------------------------------------------------------------

def configure_stripe(conn, args):
    """Save Stripe API credentials for a company."""
    if not args.company_id:
        err("--company-id is required")
    if not args.publishable_key:
        err("--publishable-key is required")
    if not args.secret_key:
        err("--secret-key is required")

    # Validate company exists
    company = conn.execute(
        "SELECT id, name FROM company WHERE id = ?", (args.company_id,)
    ).fetchone()
    if not company:
        err(f"Company not found: {args.company_id}")

    # Check for existing config
    existing = conn.execute(
        "SELECT id FROM stripe_config WHERE company_id = ?",
        (args.company_id,),
    ).fetchone()
    if existing:
        err(f"Stripe already configured for company {args.company_id}. "
            "Only one configuration per company is allowed.",
            suggestion="Update the existing config or delete it first.")

    mode = args.mode or "test"
    if mode not in ("test", "live"):
        err(f"Invalid mode: {mode}. Must be 'test' or 'live'")

    # Encrypt sensitive credentials before storing
    from erpclaw_lib.crypto import encrypt_field, derive_key
    from erpclaw_lib.db import DEFAULT_DB_PATH
    _fk = derive_key(os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH),
                      b"erpclaw-field-key")
    enc_publishable = encrypt_field(args.publishable_key, _fk)
    enc_secret = encrypt_field(args.secret_key, _fk)
    enc_webhook = encrypt_field(args.webhook_secret, _fk) if args.webhook_secret else None

    config_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stripe_config
           (id, company_id, publishable_key, secret_key, webhook_secret,
            mode, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (config_id, args.company_id, enc_publishable, enc_secret,
         enc_webhook, mode, _now(), _now()),
    )
    conn.commit()
    audit(conn, SKILL_NAME, "configure-stripe", "stripe_config", config_id,
          new_values={"company_id": args.company_id, "mode": mode})

    config = row_to_dict(conn.execute(
        "SELECT id, company_id, mode, status, created_at FROM stripe_config WHERE id = ?",
        (config_id,),
    ).fetchone())
    ok({"stripe_config": config,
        "message": f"Stripe configured for company {company['name']} in {mode} mode"})


def create_payment_intent(conn, args):
    """Create a mock Stripe payment intent linked to a sales invoice."""
    if not args.invoice_id:
        err("--invoice-id is required")
    if not args.amount:
        err("--amount is required")

    # Validate amount
    amount = to_decimal(args.amount)
    if amount <= Decimal("0"):
        err("Amount must be greater than zero")

    # Look up the sales invoice
    invoice = conn.execute(
        "SELECT id, customer_id, grand_total, currency, status "
        "FROM sales_invoice WHERE id = ?",
        (args.invoice_id,),
    ).fetchone()
    if not invoice:
        err(f"Sales invoice not found: {args.invoice_id}",
            suggestion="Use erpclaw-selling 'list-sales-invoices' to find valid invoice IDs.")

    # Resolve company from customer
    customer = conn.execute(
        "SELECT company_id FROM customer WHERE id = ?",
        (invoice["customer_id"],),
    ).fetchone()
    if not customer:
        err(f"Customer not found for invoice {args.invoice_id}")

    company_id = customer["company_id"]

    # Ensure Stripe is configured for this company
    _stripe_get_config(conn, company_id)

    currency = args.currency or invoice["currency"] or "USD"
    stripe_id = _mock_stripe_id("pi")
    metadata = args.metadata or None

    intent_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stripe_payment_intent
           (id, company_id, stripe_id, amount, currency, customer_id,
            sales_invoice_id, status, payment_entry_id, metadata,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'created', NULL, ?, ?, ?)""",
        (intent_id, company_id, stripe_id, str(round_currency(amount)),
         currency, invoice["customer_id"], args.invoice_id,
         metadata, _now(), _now()),
    )
    conn.commit()
    audit(conn, SKILL_NAME, "create-payment-intent",
          "stripe_payment_intent", intent_id,
          new_values={"stripe_id": stripe_id, "amount": str(amount),
                      "invoice_id": args.invoice_id})

    intent = row_to_dict(conn.execute(
        "SELECT * FROM stripe_payment_intent WHERE id = ?",
        (intent_id,),
    ).fetchone())
    ok({"payment_intent": intent})


def sync_payments(conn, args):
    """Check all pending payment intents and update status.

    In mock mode, all 'created' intents are moved to 'succeeded'.
    In a real integration, this would poll the Stripe API.
    """
    if not args.company_id:
        err("--company-id is required")

    # Verify config exists
    _stripe_get_config(conn, args.company_id)

    # Find all created/processing intents for this company
    pending = conn.execute(
        """SELECT * FROM stripe_payment_intent
           WHERE company_id = ? AND status IN ('created', 'processing')""",
        (args.company_id,),
    ).fetchall()

    synced = []
    for pi in pending:
        pi_dict = row_to_dict(pi)
        # Mock: all pending intents succeed
        conn.execute(
            """UPDATE stripe_payment_intent
               SET status = 'succeeded', updated_at = ?
               WHERE id = ?""",
            (_now(), pi_dict["id"]),
        )
        synced.append({
            "id": pi_dict["id"],
            "stripe_id": pi_dict["stripe_id"],
            "amount": pi_dict["amount"],
            "previous_status": pi_dict["status"],
            "new_status": "succeeded",
            "sales_invoice_id": pi_dict["sales_invoice_id"],
        })

    conn.commit()

    if synced:
        audit(conn, SKILL_NAME, "sync-payments",
              "stripe_payment_intent", ",".join(s["id"] for s in synced),
              new_values={"synced_count": len(synced)})

    ok({
        "synced_count": len(synced),
        "synced": synced,
        "message": f"Synced {len(synced)} payment(s) for company {args.company_id}",
    })


def handle_webhook(conn, args):
    """Process a mock Stripe webhook event.

    For payment_intent.succeeded events, updates the payment intent status
    and records the webhook event. Duplicate events are safely ignored
    (idempotent via stripe_event_id UNIQUE constraint).
    """
    if not args.event_id:
        err("--event-id is required (Stripe event ID, e.g. evt_mock_xxx)")
    if not args.event_type:
        err("--event-type is required (e.g. payment_intent.succeeded)")
    if not args.payload:
        err("--payload is required (JSON string)")

    # Validate payload is valid JSON
    try:
        payload_data = json.loads(args.payload)
    except (json.JSONDecodeError, TypeError):
        err("--payload must be valid JSON")

    # Check for duplicate event (idempotency)
    existing = conn.execute(
        "SELECT id, processed FROM stripe_webhook_event WHERE stripe_event_id = ?",
        (args.event_id,),
    ).fetchone()
    if existing:
        evt = row_to_dict(conn.execute(
            "SELECT * FROM stripe_webhook_event WHERE id = ?",
            (existing["id"],),
        ).fetchone())
        ok({"webhook_event": evt, "deduplicated": True,
            "message": "Duplicate webhook event -- already processed"})

    # Insert webhook event record
    webhook_id = str(uuid.uuid4())
    error_message = None
    processed = 0
    processed_at = None

    # Process based on event type
    if args.event_type == "payment_intent.succeeded":
        stripe_pi_id = payload_data.get("payment_intent_id") or payload_data.get("stripe_id")
        if stripe_pi_id:
            pi = conn.execute(
                "SELECT id, status FROM stripe_payment_intent WHERE stripe_id = ?",
                (stripe_pi_id,),
            ).fetchone()
            if pi:
                conn.execute(
                    """UPDATE stripe_payment_intent
                       SET status = 'succeeded', updated_at = ?
                       WHERE id = ?""",
                    (_now(), pi["id"]),
                )
                processed = 1
                processed_at = _now()
            else:
                error_message = f"Payment intent not found for stripe_id: {stripe_pi_id}"
        else:
            error_message = "No payment_intent_id or stripe_id in payload"

    elif args.event_type == "payment_intent.payment_failed":
        stripe_pi_id = payload_data.get("payment_intent_id") or payload_data.get("stripe_id")
        if stripe_pi_id:
            pi = conn.execute(
                "SELECT id FROM stripe_payment_intent WHERE stripe_id = ?",
                (stripe_pi_id,),
            ).fetchone()
            if pi:
                conn.execute(
                    """UPDATE stripe_payment_intent
                       SET status = 'failed', updated_at = ?
                       WHERE id = ?""",
                    (_now(), pi["id"]),
                )
                processed = 1
                processed_at = _now()
            else:
                error_message = f"Payment intent not found for stripe_id: {stripe_pi_id}"
        else:
            error_message = "No payment_intent_id or stripe_id in payload"

    else:
        # Unknown event type -- store but mark as unprocessed
        error_message = f"Unhandled event type: {args.event_type}"

    conn.execute(
        """INSERT INTO stripe_webhook_event
           (id, stripe_event_id, event_type, payload, processed,
            processed_at, error_message, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (webhook_id, args.event_id, args.event_type, args.payload,
         processed, processed_at, error_message, _now()),
    )
    conn.commit()
    audit(conn, SKILL_NAME, "handle-webhook",
          "stripe_webhook_event", webhook_id,
          new_values={"event_type": args.event_type,
                      "stripe_event_id": args.event_id,
                      "processed": processed})

    evt = row_to_dict(conn.execute(
        "SELECT * FROM stripe_webhook_event WHERE id = ?",
        (webhook_id,),
    ).fetchone())
    ok({"webhook_event": evt, "deduplicated": False})


def list_stripe_payments(conn, args):
    """List payment intents with optional filters."""
    where, params = [], []

    if args.company_id:
        where.append("spi.company_id = ?")
        params.append(args.company_id)
    if args.status:
        if args.status not in ("created", "processing", "succeeded", "failed", "cancelled"):
            err(f"Invalid status: {args.status}. "
                "Must be one of: created, processing, succeeded, failed, cancelled")
        where.append("spi.status = ?")
        params.append(args.status)
    if args.invoice_id:
        where.append("spi.sales_invoice_id = ?")
        params.append(args.invoice_id)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    limit = int(args.limit or 20)
    offset = int(args.offset or 0)

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM stripe_payment_intent spi {where_clause}",
        params,
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""SELECT spi.*, c.name AS customer_name
            FROM stripe_payment_intent spi
            LEFT JOIN customer c ON spi.customer_id = c.id
            {where_clause}
            ORDER BY spi.created_at DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()

    ok({
        "payment_intents": [dict(r) for r in rows],
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total_count,
    })


def stripe_status(conn, args):
    """Stripe integration summary."""
    where_pi = ""
    params_pi = []

    if args.company_id:
        company = conn.execute(
            "SELECT id FROM company WHERE id = ?", (args.company_id,)
        ).fetchone()
        if not company:
            err(f"Company not found: {args.company_id}")
        where_pi = "WHERE company_id = ?"
        params_pi = [args.company_id]

    # Config count
    if args.company_id:
        config_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM stripe_config WHERE company_id = ?",
            (args.company_id,),
        ).fetchone()["cnt"]
    else:
        config_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM stripe_config"
        ).fetchone()["cnt"]

    # Payment intent counts by status
    pi_q = f"""SELECT status, COUNT(*) AS cnt
               FROM stripe_payment_intent {where_pi}
               GROUP BY status"""
    pi_counts = {}
    for row in conn.execute(pi_q, params_pi).fetchall():
        pi_counts[row["status"]] = row["cnt"]

    pi_total = sum(pi_counts.values())

    # Webhook event counts
    wh_total = conn.execute(
        "SELECT COUNT(*) AS cnt FROM stripe_webhook_event"
    ).fetchone()["cnt"]
    wh_processed = conn.execute(
        "SELECT COUNT(*) AS cnt FROM stripe_webhook_event WHERE processed = 1"
    ).fetchone()["cnt"]
    wh_unprocessed = wh_total - wh_processed

    return {
        "configured_companies": config_count,
        "payment_intents": pi_counts,
        "payment_intents_total": pi_total,
        "webhook_events_total": wh_total,
        "webhook_events_processed": wh_processed,
        "webhook_events_unprocessed": wh_unprocessed,
    }


# ===========================================================================
#
#  S3 -- Cloud Backup (mock)
#
# ===========================================================================

# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _s3_get_db_path_for_conn(conn):
    """Resolve the file path of the database associated with a connection."""
    row = conn.execute("PRAGMA database_list").fetchone()
    if row:
        return row[2] or DEFAULT_DB_PATH
    return DEFAULT_DB_PATH


def _s3_compute_file_checksum(file_path):
    """Compute SHA-256 checksum of a file.  Returns hex digest string."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def _s3_get_file_size(file_path):
    """Return file size in bytes."""
    return os.path.getsize(file_path)


def _s3_generate_s3_key(prefix):
    """Generate a mock S3 object key with a timestamp."""
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    prefix = prefix or "erpclaw-backups/"
    if not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{timestamp}.sqlite"


def _s3_validate_company_exists(conn, company_id):
    """Validate that a company exists, or error."""
    row = conn.execute(
        "SELECT id, name FROM company WHERE id = ?", (company_id,)
    ).fetchone()
    if not row:
        err(f"Company not found: {company_id}")
    return row


def _s3_get_config(conn, company_id):
    """Fetch the active S3 config for a company, or None."""
    return conn.execute(
        "SELECT * FROM s3_config WHERE company_id = ? AND status = 'active'",
        (company_id,),
    ).fetchone()


def _s3_validate_backup_exists(conn, backup_id):
    """Fetch a backup record by ID, or error if not found."""
    row = conn.execute(
        "SELECT * FROM s3_backup_record WHERE id = ?", (backup_id,)
    ).fetchone()
    if not row:
        err(f"Backup record not found: {backup_id}")
    return row


# ---------------------------------------------------------------------------
# S3 actions
# ---------------------------------------------------------------------------

def configure_s3(conn, args):
    """Save S3 configuration for a company.

    Required: --company-id, --bucket-name, --access-key-id, --secret-access-key
    Optional: --region (us-east-1), --prefix (erpclaw-backups/)
    """
    if not args.company_id:
        err("--company-id is required")
    if not args.bucket_name:
        err("--bucket-name is required")
    if not args.access_key_id:
        err("--access-key-id is required")
    if not args.secret_access_key:
        err("--secret-access-key is required")

    _s3_validate_company_exists(conn, args.company_id)

    # Check for existing config
    existing = conn.execute(
        "SELECT id FROM s3_config WHERE company_id = ?", (args.company_id,)
    ).fetchone()
    if existing:
        err("S3 configuration already exists for this company. "
            "Delete the existing config before reconfiguring.")

    region = args.region or "us-east-1"
    prefix = args.prefix or "erpclaw-backups/"

    # Encrypt sensitive credentials before storing
    from erpclaw_lib.crypto import encrypt_field, derive_key
    from erpclaw_lib.db import DEFAULT_DB_PATH
    _fk = derive_key(os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH),
                      b"erpclaw-field-key")
    enc_access_key = encrypt_field(args.access_key_id, _fk)
    enc_secret_key = encrypt_field(args.secret_access_key, _fk)

    config_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO s3_config
           (id, company_id, bucket_name, region, access_key_id,
            secret_access_key, prefix, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active')""",
        (config_id, args.company_id, args.bucket_name, region,
         enc_access_key, enc_secret_key, prefix),
    )

    audit(conn, SKILL_NAME, "configure-s3", "s3_config", config_id,
          new_values={"bucket_name": args.bucket_name, "region": region},
          description=f"Configured S3 backup for bucket '{args.bucket_name}'")
    conn.commit()

    ok({
        "config": {
            "id": config_id,
            "company_id": args.company_id,
            "bucket_name": args.bucket_name,
            "region": region,
            "prefix": prefix,
            "status": "active",
        },
        "message": f"S3 backup configured for bucket '{args.bucket_name}' in {region}",
    })


def upload_backup(conn, args):
    """Mock-upload a database backup to S3.

    Required: --company-id
    Optional: --encrypted (0), --backup-type (full)

    Reads the actual database file to compute real file size and SHA-256
    checksum, then generates a mock S3 key and inserts a completed record.
    """
    if not args.company_id:
        err("--company-id is required")

    _s3_validate_company_exists(conn, args.company_id)

    config = _s3_get_config(conn, args.company_id)
    if not config:
        err("No S3 configuration found for this company. Run 'configure-s3' first.")

    # Resolve the database file path
    db_file = _s3_get_db_path_for_conn(conn)
    if not os.path.exists(db_file):
        err(f"Database file not found: {db_file}")

    # Compute real file metrics
    file_size = _s3_get_file_size(db_file)
    checksum = _s3_compute_file_checksum(db_file)

    # Generate mock S3 key
    s3_key = _s3_generate_s3_key(config["prefix"])

    encrypted = int(args.encrypted or 0)
    backup_type = args.backup_type or "full"
    if backup_type not in ("full", "incremental"):
        err("--backup-type must be 'full' or 'incremental'")

    record_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO s3_backup_record
           (id, company_id, s3_key, file_size_bytes, backup_type,
            encrypted, checksum, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'completed')""",
        (record_id, args.company_id, s3_key, file_size,
         backup_type, encrypted, checksum),
    )

    audit(conn, SKILL_NAME, "upload-backup", "s3_backup_record", record_id,
          new_values={"s3_key": s3_key, "file_size_bytes": file_size,
                      "checksum": checksum[:12]},
          description=f"Mock-uploaded backup to {s3_key}")
    conn.commit()

    ok({
        "backup": {
            "id": record_id,
            "company_id": args.company_id,
            "s3_key": s3_key,
            "bucket_name": config["bucket_name"],
            "file_size_bytes": file_size,
            "backup_type": backup_type,
            "encrypted": encrypted,
            "checksum": checksum,
            "status": "completed",
        },
        "message": f"Backup uploaded to s3://{config['bucket_name']}/{s3_key} "
                   f"({file_size:,} bytes, SHA-256: {checksum[:12]}...)",
    })


def list_remote_backups(conn, args):
    """List backup records for a company.

    Required: --company-id
    Optional: --status, --limit (20), --offset (0)
    """
    if not args.company_id:
        err("--company-id is required")

    _s3_validate_company_exists(conn, args.company_id)

    query = "SELECT * FROM s3_backup_record WHERE company_id = ?"
    params = [args.company_id]

    if args.status:
        if args.status not in ("uploading", "completed", "failed", "deleted"):
            err("--status must be one of: uploading, completed, failed, deleted")
        query += " AND status = ?"
        params.append(args.status)
    else:
        # By default, exclude deleted backups
        query += " AND status != 'deleted'"

    # Count
    count_query = query.replace("SELECT *", "SELECT COUNT(*) AS cnt", 1)
    total = conn.execute(count_query, params).fetchone()["cnt"]

    # Paginate
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    limit = int(args.limit or 20)
    offset = int(args.offset or 0)
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    backups = rows_to_list(rows)

    ok({
        "backups": backups,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    })


def restore_from_s3(conn, args):
    """Mock-restore a database backup from S3.

    Required: --backup-id

    Does not actually download anything.  Marks the record as "restore
    requested" and returns a mock restore command the user would run.
    """
    if not args.backup_id:
        err("--backup-id is required")

    backup = _s3_validate_backup_exists(conn, args.backup_id)

    if backup["status"] == "deleted":
        err("Cannot restore a deleted backup")
    if backup["status"] != "completed":
        err(f"Backup is not in 'completed' status (current: {backup['status']})")

    # Fetch the S3 config for context
    config = _s3_get_config(conn, backup["company_id"])
    bucket_name = config["bucket_name"] if config else "unknown-bucket"

    # Generate a mock restore command
    restore_path = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")
    restore_cmd = (
        f"aws s3 cp s3://{bucket_name}/{backup['s3_key']} {restore_path} "
        f"&& echo 'Restore complete. Verify with: sqlite3 {restore_path} "
        f"\".tables\"'"
    )

    audit(conn, SKILL_NAME, "restore-from-s3", "s3_backup_record",
          args.backup_id,
          description=f"Restore requested for {backup['s3_key']}")
    conn.commit()

    ok({
        "backup_id": args.backup_id,
        "s3_key": backup["s3_key"],
        "bucket_name": bucket_name,
        "file_size_bytes": backup["file_size_bytes"],
        "checksum": backup["checksum"],
        "restore_command": restore_cmd,
        "message": (
            f"Restore prepared for s3://{bucket_name}/{backup['s3_key']}. "
            f"Run the restore_command to replace the current database. "
            f"Verify checksum: {backup['checksum'][:12]}..."
        ),
    })


def delete_remote_backup(conn, args):
    """Soft-delete a remote backup record.

    Required: --backup-id

    Sets the record status to 'deleted'.  It will no longer appear in
    default list-remote-backups results.
    """
    if not args.backup_id:
        err("--backup-id is required")

    backup = _s3_validate_backup_exists(conn, args.backup_id)

    if backup["status"] == "deleted":
        err("Backup is already deleted")

    old_status = backup["status"]
    conn.execute(
        "UPDATE s3_backup_record SET status = 'deleted' WHERE id = ?",
        (args.backup_id,),
    )

    audit(conn, SKILL_NAME, "delete-remote-backup", "s3_backup_record",
          args.backup_id,
          old_values={"status": old_status},
          new_values={"status": "deleted"},
          description=f"Soft-deleted backup {backup['s3_key']}")
    conn.commit()

    ok({
        "backup_id": args.backup_id,
        "s3_key": backup["s3_key"],
        "old_status": old_status,
        "new_status": "deleted",
        "message": f"Backup {backup['s3_key']} marked as deleted",
    })


def s3_status(conn, args):
    """Return S3 backup status summary."""
    config_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM s3_config WHERE status = 'active'"
    ).fetchone()["cnt"]

    backup_rows = conn.execute(
        """SELECT status, COUNT(*) AS cnt
           FROM s3_backup_record
           GROUP BY status"""
    ).fetchall()
    backups_by_status = {r["status"]: r["cnt"] for r in backup_rows}
    total_backups = sum(backups_by_status.values())

    # Total size of completed backups
    size_row = conn.execute(
        """SELECT COALESCE(SUM(file_size_bytes), 0) AS total_bytes
           FROM s3_backup_record WHERE status = 'completed'"""
    ).fetchone()
    total_size_bytes = size_row["total_bytes"]

    return {
        "configured_companies": config_count,
        "total_backups": total_backups,
        "backups_by_status": backups_by_status,
        "total_size_bytes": total_size_bytes,
    }


# ===========================================================================
#
#  UNIFIED STATUS -- all 3 integrations
#
# ===========================================================================

def status(conn, args):
    """Unified status for all 3 integrations: Plaid, Stripe, S3."""
    result = {}

    # Plaid status
    try:
        result["plaid"] = plaid_status(conn, args)
    except SystemExit:
        raise
    except Exception:
        result["plaid"] = {"error": "Could not fetch Plaid status"}

    # Stripe status
    try:
        result["stripe"] = stripe_status(conn, args)
    except SystemExit:
        raise
    except Exception:
        result["stripe"] = {"error": "Could not fetch Stripe status"}

    # S3 status
    try:
        result["s3"] = s3_status(conn, args)
    except SystemExit:
        raise
    except Exception:
        result["s3"] = {"error": "Could not fetch S3 status"}

    ok(result)


# ===========================================================================
# Action dispatch
# ===========================================================================

ACTIONS = {
    # Plaid (5 actions)
    "configure-plaid": configure_plaid,
    "link-account": link_account,
    "sync-transactions": sync_transactions,
    "match-transactions": match_transactions,
    "list-transactions": list_transactions,
    # Stripe (5 actions)
    "configure-stripe": configure_stripe,
    "create-payment-intent": create_payment_intent,
    "sync-payments": sync_payments,
    "handle-webhook": handle_webhook,
    "list-stripe-payments": list_stripe_payments,
    # S3 (5 actions)
    "configure-s3": configure_s3,
    "upload-backup": upload_backup,
    "list-remote-backups": list_remote_backups,
    "restore-from-s3": restore_from_s3,
    "delete-remote-backup": delete_remote_backup,
    # Unified
    "status": status,
}


def main():
    parser = argparse.ArgumentParser(description="ERPClaw Integrations (Plaid + Stripe + S3)")
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS.keys()))
    parser.add_argument("--db-path", default=None)

    # -- Shared entity IDs ------------------------------------------------
    parser.add_argument("--company-id")
    parser.add_argument("--invoice-id")
    parser.add_argument("--backup-id")

    # -- Plaid configure --------------------------------------------------
    parser.add_argument("--client-id")
    parser.add_argument("--secret")
    parser.add_argument("--environment", default="sandbox")

    # -- Plaid link-account -----------------------------------------------
    parser.add_argument("--institution-name")
    parser.add_argument("--account-name")
    parser.add_argument("--account-type")
    parser.add_argument("--account-mask")
    parser.add_argument("--erp-account-id")

    # -- Plaid transactions -----------------------------------------------
    parser.add_argument("--linked-account-id")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--match-status")

    # -- Stripe configure -------------------------------------------------
    parser.add_argument("--publishable-key")
    parser.add_argument("--secret-key")
    parser.add_argument("--webhook-secret")
    parser.add_argument("--mode")

    # -- Stripe payment intent --------------------------------------------
    parser.add_argument("--amount")
    parser.add_argument("--currency")
    parser.add_argument("--metadata")

    # -- Stripe webhook ---------------------------------------------------
    parser.add_argument("--event-id")
    parser.add_argument("--event-type")
    parser.add_argument("--payload")

    # -- S3 configure -----------------------------------------------------
    parser.add_argument("--bucket-name")
    parser.add_argument("--region")
    parser.add_argument("--access-key-id")
    parser.add_argument("--secret-access-key")
    parser.add_argument("--prefix")

    # -- S3 upload --------------------------------------------------------
    parser.add_argument("--encrypted", default="0")
    parser.add_argument("--backup-type", default="full")

    # -- Shared filters ---------------------------------------------------
    parser.add_argument("--status")
    parser.add_argument("--limit", default="20")
    parser.add_argument("--offset", default="0")

    args, _unknown = parser.parse_known_args()
    check_input_lengths(args)
    action_fn = ACTIONS[args.action]

    db_path = args.db_path or DEFAULT_DB_PATH
    ensure_db_exists(db_path)
    conn = get_connection(db_path)

    # Dependency check
    _dep = check_required_tables(conn, REQUIRED_TABLES)
    if _dep:
        _dep["suggestion"] = "clawhub install " + " ".join(_dep.get("missing_skills", []))
        print(json.dumps(_dep, indent=2))
        conn.close()
        sys.exit(1)

    try:
        action_fn(conn, args)
    except SystemExit:
        raise
    except Exception as e:
        conn.rollback()
        sys.stderr.write(f"[{SKILL_NAME}] {e}\n")
        err("An unexpected error occurred")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
