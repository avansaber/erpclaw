"""Tests for GL integrity checksums (SHA-256 chain hash).

Tests: chain intact after posting, detect tampered entry, detect deleted
entry, chain after cancel/reverse, empty company.
"""
import hashlib
import json
import uuid

import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_cost_center,
)
from erpclaw_lib.gl_posting import insert_gl_entries


def _setup_gl_env(conn):
    """Create a company with accounts, cost center, and fiscal year."""
    co = create_test_company(conn)
    create_test_fiscal_year(conn, co)
    cc = create_test_cost_center(conn, co)
    cash = create_test_account(conn, co, "Cash", "asset", "bank",
                               balance_direction="debit_normal")
    revenue = create_test_account(conn, co, "Revenue", "income", "revenue",
                                  balance_direction="credit_normal")
    return co, cash, revenue, cc


def test_chain_intact_after_posting(fresh_db):
    """After normal GL posting, check-gl-integrity should report chain intact."""
    co, cash, revenue, cc = _setup_gl_env(fresh_db)

    entries = [
        {"account_id": cash, "debit": "1000", "credit": "0"},
        {"account_id": revenue, "debit": "0", "credit": "1000", "cost_center_id": cc},
    ]
    insert_gl_entries(fresh_db, entries, "journal_entry", str(uuid.uuid4()),
                      "2026-06-15", co)
    fresh_db.commit()

    result = _call_action(db_query.check_gl_integrity, fresh_db, company_id=co)
    assert result["status"] == "ok"
    assert result["chain_intact"] is True
    assert result["broken_links"] == 0
    assert result["total_entries"] == 2


def test_detect_tampered_entry(fresh_db):
    """If a GL entry's debit is manually changed, chain should break."""
    co, cash, revenue, cc = _setup_gl_env(fresh_db)

    entries = [
        {"account_id": cash, "debit": "5000", "credit": "0"},
        {"account_id": revenue, "debit": "0", "credit": "5000", "cost_center_id": cc},
    ]
    gl_ids = insert_gl_entries(fresh_db, entries, "journal_entry", str(uuid.uuid4()),
                                "2026-06-15", co)
    fresh_db.commit()

    # Tamper with the first entry's debit (direct DB edit)
    fresh_db.execute("UPDATE gl_entry SET debit = '9999' WHERE id = ?", (gl_ids[0],))
    fresh_db.commit()

    result = _call_action(db_query.check_gl_integrity, fresh_db, company_id=co)
    assert result["status"] == "ok"
    assert result["chain_intact"] is False
    assert result["broken_links"] >= 1


def test_detect_deleted_entry(fresh_db):
    """If a GL entry is deleted from the chain, integrity should fail."""
    co, cash, revenue, cc = _setup_gl_env(fresh_db)

    v1 = str(uuid.uuid4())
    v2 = str(uuid.uuid4())
    insert_gl_entries(fresh_db,
                      [{"account_id": cash, "debit": "1000", "credit": "0"},
                       {"account_id": revenue, "debit": "0", "credit": "1000", "cost_center_id": cc}],
                      "journal_entry", v1, "2026-06-15", co)
    insert_gl_entries(fresh_db,
                      [{"account_id": cash, "debit": "2000", "credit": "0"},
                       {"account_id": revenue, "debit": "0", "credit": "2000", "cost_center_id": cc}],
                      "journal_entry", v2, "2026-06-16", co)
    fresh_db.commit()

    # Delete one entry from the middle of the chain
    entry = fresh_db.execute(
        "SELECT id FROM gl_entry WHERE voucher_id = ? LIMIT 1", (v1,)
    ).fetchone()
    fresh_db.execute("DELETE FROM gl_entry WHERE id = ?", (entry["id"],))
    fresh_db.commit()

    result = _call_action(db_query.check_gl_integrity, fresh_db, company_id=co)
    assert result["status"] == "ok"
    assert result["chain_intact"] is False


def test_chain_after_cancel_reverse(fresh_db):
    """Reversing entries should not break the chain for original entries."""
    co, cash, revenue, cc = _setup_gl_env(fresh_db)
    from erpclaw_lib.gl_posting import reverse_gl_entries

    v1 = str(uuid.uuid4())
    insert_gl_entries(fresh_db,
                      [{"account_id": cash, "debit": "3000", "credit": "0"},
                       {"account_id": revenue, "debit": "0", "credit": "3000", "cost_center_id": cc}],
                      "journal_entry", v1, "2026-06-15", co)
    fresh_db.commit()

    reverse_gl_entries(fresh_db, "journal_entry", v1, "2026-06-15")
    fresh_db.commit()

    result = _call_action(db_query.check_gl_integrity, fresh_db, company_id=co)
    assert result["status"] == "ok"
    assert result["total_entries"] >= 2


def test_empty_company(fresh_db):
    """Company with no GL entries should report as intact."""
    co, cash, revenue, cc = _setup_gl_env(fresh_db)

    result = _call_action(db_query.check_gl_integrity, fresh_db, company_id=co)
    assert result["status"] == "ok"
    assert result["chain_intact"] is True
    assert result["broken_links"] == 0
    assert result["total_entries"] == 0
