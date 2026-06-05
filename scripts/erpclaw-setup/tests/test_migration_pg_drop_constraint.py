"""Live-Postgres regression test for the M0 migrations 003-006 DROP-CONSTRAINT path.

Wave 0 cross-DB add-on B (ERP-27). The displacement migrations 003-006 drop the
hardcoded CHECK constraints on account / gl_entry / stock_ledger_entry / the three
payment tables via ``ALTER TABLE ... DROP CONSTRAINT IF EXISTS <name>`` on Postgres.
That path hinges on the hardcoded ``<name>`` matching the constraint name Postgres
auto-assigns to an unnamed inline column CHECK (``<table>_<column>_check``). The
rest of the suite only mocks psycopg2 (see test_db_connection.py), so this is the
one place the real names are proven against a live server.

GATED: skipped unless ``ERPCLAW_PG_TEST_URL`` points at a reachable, EXPENDABLE
Postgres database. The test drops and recreates the ``public`` schema, so never
point it at a database with real data. CI has no Postgres, so this stays skipped
there; run it on the OpenClaw PG box or a throwaway local cluster:

    ERPCLAW_PG_TEST_URL='postgresql://postgres@/erpclaw_verify?host=/tmp/pg&port=5433' \
        pytest source/erpclaw/scripts/erpclaw-setup/tests/test_migration_pg_drop_constraint.py
"""
import importlib.util
import os

import pytest

PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required for the DROP-CONSTRAINT path)",
)

_SETUP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MIG_DIR = os.path.join(_SETUP_DIR, "migrations")

# Registry tables the migrations seed into (verbatim from init_schema.py).
_REGISTRY_DDL = """
CREATE TABLE voucher_type_registry (
    voucher_type TEXT NOT NULL,
    skill_name   TEXT NOT NULL,
    label        TEXT NOT NULL,
    target_table TEXT NOT NULL CHECK(target_table IN ('gl_entry','stock_ledger_entry','payment_allocation')),
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    PRIMARY KEY (voucher_type, target_table)
);
CREATE TABLE party_type_registry (
    party_type  TEXT PRIMARY KEY,
    skill_name  TEXT NOT NULL,
    label       TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))
);
CREATE TABLE account_type_registry (
    account_type TEXT PRIMARY KEY,
    skill_name   TEXT NOT NULL,
    label        TEXT NOT NULL,
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))
);
"""

# OLD (pre-M0) base tables: the migrations' post-state DDL with the dropped CHECK(s)
# re-added and REFERENCES stripped so the tables stand alone. The IN-list values do
# not affect Postgres constraint auto-naming (table+column only); all SIBLING CHECKs
# are kept so naming/collision behaviour matches a real pre-M0 database.
_OLD_TABLES_DDL = """
CREATE TABLE account (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, account_number TEXT, parent_id TEXT,
    root_type TEXT NOT NULL CHECK(root_type IN ('asset','liability','equity','income','expense')),
    account_type TEXT CHECK(account_type IN ('bank','cash','receivable','payable','stock','trust')),
    currency TEXT NOT NULL DEFAULT 'USD',
    is_group INTEGER NOT NULL DEFAULT 0 CHECK(is_group IN (0,1)),
    is_frozen INTEGER NOT NULL DEFAULT 0 CHECK(is_frozen IN (0,1)),
    disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
    balance_direction TEXT NOT NULL DEFAULT 'debit_normal' CHECK(balance_direction IN ('debit_normal','credit_normal')),
    balance_must_be TEXT CHECK(balance_must_be IN ('debit','credit')),
    company_id TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0, lft INTEGER, rgt INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_number, company_id)
);
CREATE TABLE gl_entry (
    id TEXT PRIMARY KEY, posting_date TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    account_id TEXT NOT NULL,
    party_type TEXT CHECK(party_type IN ('customer','supplier','employee')), party_id TEXT,
    debit TEXT NOT NULL DEFAULT '0', credit TEXT NOT NULL DEFAULT '0', currency TEXT NOT NULL DEFAULT 'USD',
    debit_base TEXT NOT NULL DEFAULT '0', credit_base TEXT NOT NULL DEFAULT '0', exchange_rate TEXT NOT NULL DEFAULT '1',
    voucher_type TEXT NOT NULL CHECK(voucher_type IN ('journal_entry','sales_invoice','payment_entry')),
    voucher_id TEXT NOT NULL, entry_set TEXT NOT NULL DEFAULT 'primary', cost_center_id TEXT, project_id TEXT,
    remarks TEXT, fiscal_year TEXT,
    is_cancelled INTEGER NOT NULL DEFAULT 0 CHECK(is_cancelled IN (0,1)),
    cancelled_by TEXT, sequence INTEGER, gl_checksum TEXT, dimensions_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE stock_ledger_entry (
    id TEXT PRIMARY KEY, posting_date TEXT NOT NULL, posting_time TEXT, item_id TEXT NOT NULL, warehouse_id TEXT NOT NULL,
    actual_qty TEXT NOT NULL DEFAULT '0', qty_after_transaction TEXT NOT NULL DEFAULT '0',
    valuation_rate TEXT NOT NULL DEFAULT '0', stock_value TEXT NOT NULL DEFAULT '0',
    stock_value_difference TEXT NOT NULL DEFAULT '0',
    voucher_type TEXT NOT NULL CHECK(voucher_type IN ('stock_entry','purchase_receipt','delivery_note')),
    voucher_id TEXT NOT NULL, batch_id TEXT, serial_number TEXT, incoming_rate TEXT NOT NULL DEFAULT '0',
    is_cancelled INTEGER NOT NULL DEFAULT 0 CHECK(is_cancelled IN (0,1)), fiscal_year TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE payment_entry (
    id TEXT PRIMARY KEY, naming_series TEXT,
    payment_type TEXT NOT NULL CHECK(payment_type IN ('receive','pay','internal_transfer')),
    posting_date TEXT NOT NULL,
    party_type TEXT CHECK(party_type IN ('customer','supplier','employee')), party_id TEXT,
    paid_from_account TEXT NOT NULL, paid_to_account TEXT NOT NULL,
    paid_amount TEXT NOT NULL DEFAULT '0', received_amount TEXT NOT NULL DEFAULT '0',
    payment_currency TEXT NOT NULL DEFAULT 'USD', exchange_rate TEXT NOT NULL DEFAULT '1',
    reference_number TEXT, reference_date TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','submitted','cancelled')),
    unallocated_amount TEXT NOT NULL DEFAULT '0', company_id TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, payment_method TEXT DEFAULT ''
);
CREATE TABLE payment_allocation (
    id TEXT PRIMARY KEY, payment_entry_id TEXT NOT NULL,
    voucher_type TEXT NOT NULL CHECK(voucher_type IN ('sales_invoice','purchase_invoice','credit_note','debit_note')),
    voucher_id TEXT NOT NULL, allocated_amount TEXT NOT NULL DEFAULT '0',
    exchange_gain_loss TEXT NOT NULL DEFAULT '0', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE payment_ledger_entry (
    id TEXT PRIMARY KEY, posting_date TEXT NOT NULL, account_id TEXT NOT NULL,
    party_type TEXT NOT NULL CHECK(party_type IN ('customer','supplier','employee')),
    party_id TEXT NOT NULL, voucher_type TEXT NOT NULL, voucher_id TEXT NOT NULL,
    against_voucher_type TEXT, against_voucher_id TEXT,
    amount TEXT NOT NULL DEFAULT '0', amount_in_account_currency TEXT NOT NULL DEFAULT '0', currency TEXT NOT NULL DEFAULT 'USD',
    delinked INTEGER NOT NULL DEFAULT 0 CHECK(delinked IN (0,1)), remarks TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# (migration stem, [(table, hardcoded-constraint-name-it-drops), ...])
_MIGRATIONS = [
    ("003_displace_account_check", [("account", "account_account_type_check")]),
    ("004_displace_gl_entry_checks", [("gl_entry", "gl_entry_voucher_type_check"),
                                      ("gl_entry", "gl_entry_party_type_check")]),
    ("005_displace_sle_check", [("stock_ledger_entry", "stock_ledger_entry_voucher_type_check")]),
    ("006_displace_payment_checks", [("payment_entry", "payment_entry_party_type_check"),
                                     ("payment_allocation", "payment_allocation_voucher_type_check"),
                                     ("payment_ledger_entry", "payment_ledger_entry_party_type_check")]),
]


def _check_names(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = %s AND con.contype = 'c'",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def _load_migration(stem):
    path = os.path.join(_MIG_DIR, f"{stem}.py")
    spec = importlib.util.spec_from_file_location(f"_pgtest_{stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def pg(monkeypatch):
    """Reset the target Postgres to the pre-M0 OLD schema and yield a connection."""
    import psycopg2
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", PG_URL)

    setup = psycopg2.connect(PG_URL)
    setup.autocommit = True
    with setup.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(_REGISTRY_DDL)
        cur.execute(_OLD_TABLES_DDL)
    setup.close()

    conn = psycopg2.connect(PG_URL)
    yield conn
    conn.close()


def test_old_schema_constraint_names_match_hardcoded(pg):
    """Every name the migrations DROP must be the name Postgres actually assigned."""
    for _stem, drops in _MIGRATIONS:
        for table, conname in drops:
            assert conname in _check_names(pg, table), (
                f"{table}: Postgres did not auto-name the CHECK '{conname}'; "
                f"present: {sorted(_check_names(pg, table))}"
            )


def test_migrations_drop_constraints_and_are_idempotent(pg):
    """run_migration(postgresql) drops each CHECK; a second run is a clean no-op."""
    for stem, drops in _MIGRATIONS:
        mod = _load_migration(stem)
        mod.run_migration(PG_URL)
        for table, conname in drops:
            assert conname not in _check_names(pg, table), f"{stem}: {conname} still present after migrate"
        # idempotent re-run (DROP ... IF EXISTS) must not raise
        mod.run_migration(PG_URL)
        for table, conname in drops:
            assert conname not in _check_names(pg, table), f"{stem}: {conname} reappeared on re-run"


def test_registry_seeds_present_after_migration(pg):
    """003/004/005 seed their registries on the Postgres path (006 drops only)."""
    for stem, _ in _MIGRATIONS:
        _load_migration(stem).run_migration(PG_URL)
    with pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM account_type_registry")
        assert cur.fetchone()[0] == 24
        cur.execute("SELECT COUNT(*) FROM voucher_type_registry")
        assert cur.fetchone()[0] == 33  # 19 gl_entry + 10 sle + 4 payment_allocation
        cur.execute("SELECT COUNT(*) FROM party_type_registry")
        assert cur.fetchone()[0] == 3
