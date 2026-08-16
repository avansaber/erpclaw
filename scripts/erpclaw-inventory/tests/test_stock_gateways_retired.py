"""Part A — the two raw stock-ledger gateways are RETIRED (M103, steer shape).

`create-stock-ledger-entries` and `reverse-stock-ledger-entries` wrote
stock-ledger rows with NO balancing general-ledger leg — one call on seeded
books wrote a row worth 1000.00 and turned INV-24 red. They had zero production
callers and could never acquire one: the foundation router dispatches with
``os.execvp``, which replaces the process, so the selling/buying handlers they
were named for cannot call them from inside a transaction.

What is pinned here is the RETIREMENT CONTRACT (Nik ruling 2026-08-13: retired
actions STEER to their replacement — the M63-C shape, one pattern for the whole
tree), and it has two halves that must both hold:

  1. the actions stay ROUTABLE and answer with a helpful steer — a caller (or
     an agent replaying an old invocation, legacy flags and all, label
     voucher_type and all) gets one JSON object naming the sanctioned flow,
     exit 1, never a traceback and never "Unknown action";
  2. NOTHING LANDS. The fixture seeds a live stock ledger AND a live GL row, so
     "the tables were empty anyway" cannot be what makes the test pass.
     Against the pre-retirement handler this file is red on both halves —
     `create-stock-ledger-entries` returned ``status: ok`` and wrote an SLE row
     with no GL leg.

The INV-24 floor for the sanctioned path lives in
``testing/unit/constitution/test_stock_entry_ties_to_the_books.py``; this file
owns the steer contract only.
"""
import json
import os
import subprocess
import sys
import uuid

import pytest

from inventory_helpers import call_action, init_all_tables, load_db_query, ns, \
    seed_account, seed_company, seed_fiscal_year, seed_item, \
    seed_stock_entry_sle, seed_warehouse

inv = load_db_query()

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)                 # erpclaw-inventory/
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)               # scripts/
_ROUTER = os.path.join(_SCRIPTS_DIR, "db_query.py")       # the foundation router
_IN_TREE_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")

RETIRED_ACTIONS = [
    "create-stock-ledger-entries",
    "reverse-stock-ledger-entries",
]

# The replacement routes. Every steer must name ALL of them: the posting flow,
# its reversal, and the two gated correction paths — a retirement without a
# route is a dead end, and a partial route sends the caller somewhere that
# cannot finish the job they actually had.
SANCTIONED_FLOW = [
    "add-stock-entry",
    "submit-stock-entry",
    "cancel-stock-entry",
    "add-stock-reconciliation",
    "revalue-stock",
]


@pytest.fixture
def legacy_env(conn):
    """Seeded books: live stock ledger AND live GL, plus everything a legacy
    invocation used to reference — so the nothing-lands half measures a real
    surface, not an empty one."""
    cid = seed_company(conn)
    fy = seed_fiscal_year(conn, cid)
    stock_acct = seed_account(conn, cid, name="Stock In Hand", root_type="asset")
    iid = seed_item(conn)
    wid = seed_warehouse(conn, cid, account_id=stock_acct)
    seed_stock_entry_sle(conn, iid, wid, qty="100", valuation_rate="10.00")
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        "voucher_type, voucher_id, remarks, fiscal_year, is_cancelled) "
        "VALUES (?, '2026-01-01', ?, '1000.00', '0', 'stock_entry', ?, "
        "'opening stock', 'FY2026', 0)",
        (str(uuid.uuid4()), stock_acct, str(uuid.uuid4())))
    conn.commit()
    return {"company_id": cid, "fiscal_year_id": fy, "item_id": iid,
            "warehouse_id": wid}


def _legacy_args(env):
    """Every legacy flag at once — the old invocation, replayed verbatim.

    The voucher_type is deliberately the LABEL form: the old handler
    canonicalized it, and a steer that chokes on the label before steering
    would fail exactly the caller it exists for.
    """
    return ns(voucher_type="Delivery Note",
              voucher_id="DN-LEGACY-1",
              posting_date="2026-06-01",
              company_id=env["company_id"],
              entries=json.dumps([{"item_id": env["item_id"],
                                   "warehouse_id": env["warehouse_id"],
                                   "actual_qty": "5",
                                   "incoming_rate": "10"}]))


def _counts(conn):
    return {
        "stock_ledger_entry": conn.execute(
            "SELECT COUNT(*) AS n FROM stock_ledger_entry").fetchone()["n"],
        "gl_entry": conn.execute(
            "SELECT COUNT(*) AS n FROM gl_entry").fetchone()["n"],
    }


# ──────────────────────────────────────────────────────────────────────────────
# 1. The steer
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", RETIRED_ACTIONS)
def test_retired_action_returns_a_steer_not_a_result(action, conn, legacy_env):
    """Each retired action answers with one JSON error that names its replacement."""
    result = call_action(inv.ACTIONS[action], conn, _legacy_args(legacy_env))

    assert result["status"] == "error", (
        f"{action} still returned a result: {json.dumps(result)[:300]}")
    assert "retired" in result["message"].lower()
    assert action in result["message"], "the message must name the action the caller typed"
    suggestion = result.get("suggestion", "")
    for replacement in SANCTIONED_FLOW:
        assert replacement in suggestion, (
            f"the steer for {action} does not name {replacement}; a retirement "
            f"without a route is a dead end. Got: {suggestion}")


def test_both_share_one_steer(conn, legacy_env):
    """One retirement, one message shape — two hand-written variants would drift.

    Asserts the shared value is the REAL steer, not a shared absence: a set of
    two ``None``s also has size 1, and that is exactly the pre-retirement state.
    """
    suggestions = {
        call_action(inv.ACTIONS[a], conn, _legacy_args(legacy_env)).get("suggestion")
        for a in RETIRED_ACTIONS
    }
    assert len(suggestions) == 1, f"steers have drifted apart: {suggestions}"
    shared = suggestions.pop()
    assert shared and all(r in shared for r in SANCTIONED_FLOW), (
        f"the shared steer does not route anywhere: {shared!r}")


# ──────────────────────────────────────────────────────────────────────────────
# 2. Nothing lands
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", RETIRED_ACTIONS)
def test_retired_action_writes_nothing(action, conn, legacy_env):
    """Both ledgers are PRESENT and populated; the retired action must touch
    neither. This is the half that was red before M103: the old
    create-stock-ledger-entries wrote an SLE row here with no GL leg."""
    before = _counts(conn)
    call_action(inv.ACTIONS[action], conn, _legacy_args(legacy_env))
    conn.commit()
    after = _counts(conn)

    assert after == before, (
        f"{action} wrote to a ledger: {before} -> {after}")
    assert after["stock_ledger_entry"] == 1, "the seeded opening SLE must be the only stock row"
    assert after["gl_entry"] == 1, "the seeded opening GL must be the only GL row"


# ──────────────────────────────────────────────────────────────────────────────
# 3. The whole caller path, replayed
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", RETIRED_ACTIONS)
def test_full_legacy_invocation_reaches_the_steer_through_the_router(
        action, tmp_path):
    """Drive the FOUNDATION router exactly as a legacy caller would — every old
    flag, ``--entries`` included — and read what comes back. This pins three
    things at once: the name still routes (never "Unknown action"), the legacy
    flags still parse (an argparse usage error would exit 2 before the JSON
    contract), and what routes is the steer.

    Hermetic per the M54/M97 discipline: ERPCLAW_HOME is redirected at a temp
    dir so nothing touches the developer's real install, and PYTHONPATH binds
    the IN-TREE erpclaw_lib so find_spec resolves the tree under test rather
    than whatever the deployed symlink points at. The temp home gets a
    PROVISIONED database on purpose: the router's requires-setup pre-flight
    runs before dispatch, and the steer contract is about what an INSTALLED
    caller gets — an uninstalled one correctly gets the setup pointer instead.
    """
    home = tmp_path / "home"
    (home / "lib").mkdir(parents=True)
    init_all_tables(str(home / "data.sqlite"))
    env = dict(os.environ, ERPCLAW_HOME=str(home), PYTHONPATH=_IN_TREE_LIB)
    proc = subprocess.run(
        [sys.executable, _ROUTER, "--action", action,
         "--voucher-type", "Delivery Note",
         "--voucher-id", "DN-LEGACY-1",
         "--posting-date", "2026-06-01",
         "--company-id", str(uuid.uuid4()),
         "--entries", '[{"item_id": "x", "warehouse_id": "y", '
                      '"actual_qty": "5", "incoming_rate": "10"}]'],
        capture_output=True, text=True, env=env, timeout=120)

    assert proc.returncode == 1, (proc.returncode, proc.stdout[-400:],
                                  proc.stderr[-400:])
    assert "Traceback" not in proc.stderr, proc.stderr[-800:]
    payload = json.loads(proc.stdout)
    assert payload.get("status") == "error", payload
    assert "retired" in payload.get("message", "").lower(), payload
    assert action in payload.get("message", ""), payload
    for replacement in SANCTIONED_FLOW:
        assert replacement in payload.get("suggestion", ""), (replacement, payload)
