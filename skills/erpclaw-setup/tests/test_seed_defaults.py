"""Tests for seed-defaults action.

Test IDs: S-SD-01 through S-SD-02

seed_defaults loads currencies, UoMs, and payment terms from
the assets/ JSON files. It is idempotent (INSERT OR IGNORE).
"""
import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# S-SD-01: Create company, seed defaults, verify counts > 0
# ---------------------------------------------------------------------------
def test_seed_defaults(fresh_db):
    # Must have a company first
    _call_action(db_query.setup_company, fresh_db, name="Seed Test Co")

    result = _call_action(db_query.seed_defaults, fresh_db)

    assert result["status"] == "ok"
    assert result["currencies_seeded"] > 0
    assert result["uoms_seeded"] > 0
    assert result["payment_terms_seeded"] > 0

    # Verify actual rows in DB
    cur_count = fresh_db.execute("SELECT COUNT(*) as cnt FROM currency").fetchone()["cnt"]
    uom_count = fresh_db.execute("SELECT COUNT(*) as cnt FROM uom").fetchone()["cnt"]
    pt_count = fresh_db.execute("SELECT COUNT(*) as cnt FROM payment_terms").fetchone()["cnt"]

    assert cur_count > 0
    assert uom_count > 0
    assert pt_count > 0


# ---------------------------------------------------------------------------
# S-SD-02: Seed twice — no errors, same DB counts
# ---------------------------------------------------------------------------
def test_seed_idempotent(fresh_db):
    _call_action(db_query.setup_company, fresh_db, name="Idempotent Co")

    # First seed
    result1 = _call_action(db_query.seed_defaults, fresh_db)
    assert result1["status"] == "ok"

    count_after_first = {
        "currencies": fresh_db.execute("SELECT COUNT(*) as cnt FROM currency").fetchone()["cnt"],
        "uoms": fresh_db.execute("SELECT COUNT(*) as cnt FROM uom").fetchone()["cnt"],
        "payment_terms": fresh_db.execute("SELECT COUNT(*) as cnt FROM payment_terms").fetchone()["cnt"],
    }

    # Second seed — should succeed without errors
    result2 = _call_action(db_query.seed_defaults, fresh_db)
    assert result2["status"] == "ok"

    count_after_second = {
        "currencies": fresh_db.execute("SELECT COUNT(*) as cnt FROM currency").fetchone()["cnt"],
        "uoms": fresh_db.execute("SELECT COUNT(*) as cnt FROM uom").fetchone()["cnt"],
        "payment_terms": fresh_db.execute("SELECT COUNT(*) as cnt FROM payment_terms").fetchone()["cnt"],
    }

    # Counts should be identical
    assert count_after_first == count_after_second
