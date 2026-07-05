"""L1 unit tests: Wave 2 S7 — item-global alternatives / substitutes.

Covers (per the S7 test plan):
  - add-item-alternative: happy path + error (missing item / unknown alt item)
  - self-reference rejected (item == alternative)
  - duplicate (a,b) pair blocked; reverse (b,a) is a distinct valid row (directional)
  - list-item-alternatives: filter by --item, priority ASC ordering
  - get-best-alternative-for-item: 3 alternatives, 2 in stock; highest-priority
    in-stock alternative returned (the out-of-stock top-priority one is skipped)
  - get-best with no warehouse returns highest priority regardless of stock
  - get-best with no match is a clean exit-0 result (best_alternative is None)
  - remove-item-alternative: soft delete (is_active=0) drops it from get-best
  - manufacturing add-bom-substitute falls back to item_alternative when the BOM
    line has no substitute of its own

Uses the shared inventory test env (build_inventory_env). Exact Decimal-as-text
assertions, fresh DB per test.
"""
import importlib.util
import os
import sys
import uuid

import pytest

from inventory_helpers import (
    load_db_query, call_action, ns, is_ok, is_error,
    seed_item, seed_stock_entry_sle, _uuid,
)

inv = load_db_query()

# Defaults for every flag the S7 actions read, so _ns() needs only what matters.
_DEFAULTS = dict(
    id=None, item_id=None, alternative_item_id=None, priority=None,
    conversion_factor=None, notes=None, qty=None, warehouse_id=None,
    warehouse=None, active_only=False, db_path=None,
)


def _ns(**kw):
    d = dict(_DEFAULTS)
    d.update(kw)
    return ns(**d)


# ── add-item-alternative: happy + errors ────────────────────────────────────

def test_add_item_alternative_happy(conn, env):
    r = call_action(inv.add_item_alternative, conn, _ns(
        item_id=env["item1"], alternative_item_id=env["item2"],
        priority=5, conversion_factor="2", notes="cheaper sub"))
    assert is_ok(r), r
    assert r["item_id"] == env["item1"]
    assert r["alternative_item_id"] == env["item2"]
    assert r["priority"] == 5
    assert r["conversion_factor"] == "2.00"
    row = conn.execute("SELECT * FROM item_alternative WHERE id = ?",
                       (r["item_alternative_id"],)).fetchone()
    assert row["is_active"] == 1
    assert row["notes"] == "cheaper sub"


def test_add_item_alternative_missing_alternative(conn, env):
    r = call_action(inv.add_item_alternative, conn, _ns(item_id=env["item1"]))
    assert is_error(r), r


def test_add_item_alternative_unknown_item(conn, env):
    r = call_action(inv.add_item_alternative, conn, _ns(
        item_id=str(uuid.uuid4()), alternative_item_id=env["item2"]))
    assert is_error(r), r


def test_add_item_alternative_unknown_alternative(conn, env):
    r = call_action(inv.add_item_alternative, conn, _ns(
        item_id=env["item1"], alternative_item_id=str(uuid.uuid4())))
    assert is_error(r), r


def test_self_reference_rejected(conn, env):
    r = call_action(inv.add_item_alternative, conn, _ns(
        item_id=env["item1"], alternative_item_id=env["item1"]))
    assert is_error(r), r
    # nothing was written
    cnt = conn.execute("SELECT COUNT(*) FROM item_alternative").fetchone()[0]
    assert cnt == 0


# ── duplicate-pair / directional semantics ──────────────────────────────────

def test_duplicate_pair_blocked_but_reverse_allowed(conn, env):
    a, b = env["item1"], env["item2"]
    r1 = call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=b))
    assert is_ok(r1), r1
    # exact same direction (a,b) is a duplicate → blocked
    r2 = call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=b))
    assert is_error(r2), r2
    # reverse direction (b,a) is a distinct valid row → allowed
    r3 = call_action(inv.add_item_alternative, conn, _ns(item_id=b, alternative_item_id=a))
    assert is_ok(r3), r3
    cnt = conn.execute("SELECT COUNT(*) FROM item_alternative").fetchone()[0]
    assert cnt == 2


# ── list-item-alternatives ──────────────────────────────────────────────────

def test_list_item_alternatives_filtered_and_ordered(conn, env):
    a = env["item1"]
    b = env["item2"]
    c = seed_item(conn, "Widget C", "Each", "stock", "40.00")
    # add out of priority order; list must come back priority ASC
    call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=c, priority=2))
    call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=b, priority=1))
    r = call_action(inv.list_item_alternatives, conn, _ns(item_id=a))
    assert is_ok(r), r
    assert r["count"] == 2
    assert r["item_alternatives"][0]["alternative_item_id"] == b  # priority 1 first
    assert r["item_alternatives"][1]["alternative_item_id"] == c  # priority 2 second


# ── get-best-alternative-for-item: 3 alts, 2 in stock ───────────────────────

def test_get_best_alternative_skips_out_of_stock(conn, env):
    """3 alternatives for item1. Priority 1 alt (B) is OUT of stock at the
    warehouse, priority 2 (C) and priority 3 (D) are IN stock. Best returned must
    be C — the highest-priority alternative that actually has stock."""
    a, wh = env["item1"], env["warehouse"]
    b = env["item2"]                                   # no stock seeded → 0 at wh
    c = seed_item(conn, "Widget C", "Each", "stock", "40.00")
    d = seed_item(conn, "Widget D", "Each", "stock", "30.00")
    seed_stock_entry_sle(conn, c, wh, "50", "40.00")   # C in stock
    seed_stock_entry_sle(conn, d, wh, "50", "30.00")   # D in stock

    call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=b, priority=1))
    call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=c, priority=2))
    call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=d, priority=3))

    r = call_action(inv.get_best_alternative_for_item, conn, _ns(
        item_id=a, qty="10", warehouse_id=wh))
    assert is_ok(r), r
    assert r["best_alternative"] is not None
    assert r["best_alternative"]["alternative_item_id"] == c  # B skipped (no stock)
    assert r["best_alternative"]["available_qty"] == "50.00"


def test_get_best_alternative_no_warehouse_ignores_stock(conn, env):
    a, b = env["item1"], env["item2"]
    c = seed_item(conn, "Widget C", "Each", "stock", "40.00")
    call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=b, priority=1))
    call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=c, priority=2))
    # no warehouse → highest priority wins regardless of stock
    r = call_action(inv.get_best_alternative_for_item, conn, _ns(item_id=a))
    assert is_ok(r), r
    assert r["best_alternative"]["alternative_item_id"] == b


def test_get_best_alternative_no_match_is_ok(conn, env):
    a, wh = env["item1"], env["warehouse"]
    b = env["item2"]  # out of stock at wh
    call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=b, priority=1))
    # require more than exists → no candidate, but clean exit-0
    r = call_action(inv.get_best_alternative_for_item, conn, _ns(
        item_id=a, qty="5", warehouse_id=wh))
    assert is_ok(r), r
    assert r["best_alternative"] is None


def test_get_best_alternative_conversion_factor(conn, env):
    """conversion_factor 2 means the substitute needs 2x units to replace 1.
    With 10 units of stock and required_qty 6, needed = 6*2 = 12 > 10 → skipped."""
    a, wh = env["item1"], env["warehouse"]
    b = env["item2"]
    seed_stock_entry_sle(conn, b, wh, "10", "100.00")
    call_action(inv.add_item_alternative, conn, _ns(
        item_id=a, alternative_item_id=b, priority=1, conversion_factor="2"))
    r = call_action(inv.get_best_alternative_for_item, conn, _ns(
        item_id=a, qty="6", warehouse_id=wh))
    assert is_ok(r), r
    assert r["best_alternative"] is None  # 12 needed > 10 available
    # required_qty 4 → 8 needed <= 10 available → returned
    r2 = call_action(inv.get_best_alternative_for_item, conn, _ns(
        item_id=a, qty="4", warehouse_id=wh))
    assert r2["best_alternative"]["alternative_item_id"] == b
    assert r2["best_alternative"]["required_substitute_qty"] == "8.00"


# ── remove-item-alternative (soft delete) ───────────────────────────────────

def test_remove_item_alternative_soft_deletes(conn, env):
    a, b = env["item1"], env["item2"]
    r = call_action(inv.add_item_alternative, conn, _ns(item_id=a, alternative_item_id=b, priority=1))
    alt_id = r["item_alternative_id"]
    rm = call_action(inv.remove_item_alternative, conn, _ns(id=alt_id))
    assert is_ok(rm), rm
    row = conn.execute("SELECT is_active FROM item_alternative WHERE id = ?", (alt_id,)).fetchone()
    assert row["is_active"] == 0
    # soft-deleted row no longer surfaces in get-best
    best = call_action(inv.get_best_alternative_for_item, conn, _ns(item_id=a))
    assert best["best_alternative"] is None


def test_remove_item_alternative_unknown_id(conn, env):
    r = call_action(inv.remove_item_alternative, conn, _ns(id=str(uuid.uuid4())))
    assert is_error(r), r


# ── manufacturing cross-module fallback ─────────────────────────────────────

# The fallback tests below reach across the monorepo into the erpclaw-ops addon
# (erpclaw-manufacturing lives in a different published repo). In a standalone
# `avansaber/erpclaw` checkout that sibling tree is absent, so these two tests
# skip rather than fail. In the monorepo the path resolves and they run.
_MFG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))),  # repo source/
    "erpclaw-addons", "erpclaw-ops", "scripts",
    "erpclaw-manufacturing", "db_query.py")
_MFG_ABSENT = not os.path.exists(_MFG_PATH)
_MFG_SKIP_REASON = (
    "erpclaw-manufacturing (erpclaw-ops addon) not present — cross-repo "
    "fallback test only runs in the monorepo checkout")


def _load_manufacturing():
    spec = importlib.util.spec_from_file_location(
        "db_query_manufacturing", _MFG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_bom_with_line(conn, env, primary_item_id):
    """Insert a minimal active BOM + one bom_item line for primary_item_id.
    Returns the bom_item id."""
    bom_id = _uuid()
    conn.execute(
        "INSERT INTO bom (id, item_id, quantity, is_active, company_id) "
        "VALUES (?, ?, '1', 1, ?)",
        (bom_id, env["item1"], env["company_id"]))
    bom_item_id = _uuid()
    conn.execute(
        "INSERT INTO bom_item (id, bom_id, item_id, quantity) VALUES (?, ?, ?, '1')",
        (bom_item_id, bom_id, primary_item_id))
    conn.commit()
    return bom_item_id


@pytest.mark.skipif(_MFG_ABSENT, reason=_MFG_SKIP_REASON)
def test_manufacturing_falls_back_to_item_alternative(conn, env):
    """add-bom-substitute on a BOM line that has NO substitute of its own must
    surface the item-global alternatives (item_alternative) for the line's
    primary item via a read-only cross-module lookup."""
    mfg = _load_manufacturing()
    primary = env["item1"]
    sub = env["item2"]
    alt = seed_item(conn, "Global Alt", "Each", "stock", "33.00")

    # An item-global alternative for the primary item (inventory writes it).
    call_action(inv.add_item_alternative, conn, _ns(
        item_id=primary, alternative_item_id=alt, priority=7))

    bom_item_id = _seed_bom_with_line(conn, env, primary)

    mfg_args = ns(bom_item_id=bom_item_id, substitute_item_id=sub,
                  conversion_factor=None, priority=None, db_path=None)
    r = call_action(mfg.add_bom_substitute, conn, mfg_args)
    assert is_ok(r), r
    # this was the FIRST substitute on the line → fallback surfaced
    assert "item_alternative_fallback" in r, r
    fb = r["item_alternative_fallback"]
    assert len(fb) == 1
    assert fb[0]["alternative_item_id"] == alt
    assert fb[0]["priority"] == 7
    # the bom_item_substitute row was still written by manufacturing
    cnt = conn.execute(
        "SELECT COUNT(*) FROM bom_item_substitute WHERE bom_item_id = ?",
        (bom_item_id,)).fetchone()[0]
    assert cnt == 1


@pytest.mark.skipif(_MFG_ABSENT, reason=_MFG_SKIP_REASON)
def test_manufacturing_no_fallback_when_line_already_has_substitute(conn, env):
    """When the BOM line already has a substitute, a second add does NOT surface
    the item-global fallback (the line is already substitute-aware)."""
    mfg = _load_manufacturing()
    primary = env["item1"]
    sub1 = env["item2"]
    sub2 = seed_item(conn, "Sub Two", "Each", "stock", "22.00")
    alt = seed_item(conn, "Global Alt", "Each", "stock", "33.00")
    call_action(inv.add_item_alternative, conn, _ns(
        item_id=primary, alternative_item_id=alt, priority=7))
    bom_item_id = _seed_bom_with_line(conn, env, primary)

    r1 = call_action(mfg.add_bom_substitute, conn, ns(
        bom_item_id=bom_item_id, substitute_item_id=sub1,
        conversion_factor=None, priority=None, db_path=None))
    assert is_ok(r1)
    assert "item_alternative_fallback" in r1  # first add: fallback shown
    r2 = call_action(mfg.add_bom_substitute, conn, ns(
        bom_item_id=bom_item_id, substitute_item_id=sub2,
        conversion_factor=None, priority=None, db_path=None))
    assert is_ok(r2)
    assert "item_alternative_fallback" not in r2  # line now has its own → no fallback
