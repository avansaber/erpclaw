"""Tests for the status action.

Test ID: S-ST-01
"""
import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# S-ST-01: Create company, seed defaults, verify status counts
# ---------------------------------------------------------------------------
def test_status(fresh_db):
    # Set up data
    _call_action(db_query.setup_company, fresh_db, name="Status Co")
    _call_action(db_query.seed_defaults, fresh_db)

    result = _call_action(db_query.status, fresh_db)

    assert result["status"] == "ok"
    assert result["companies"] == 1
    # seed_defaults loads currencies with enabled=0/1 — status counts enabled=1 only
    assert result["currencies"] >= 1  # at least USD is enabled
    assert result["uoms"] > 0
    assert result["payment_terms"] > 0
    assert isinstance(result["schema_versions"], dict)
