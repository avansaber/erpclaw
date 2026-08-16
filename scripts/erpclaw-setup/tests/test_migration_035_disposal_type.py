"""Part A — migration 035: register `disposal_gain_loss` and retype what is one (M94).

Plan home: `planning/pending_items.md` row M94. SIM:
`planning/simlogs/m94_SIM_2026-08-12.md` §4, which states the matching rule and
every failure mode this file pins.

The migration's whole risk is that it changes the meaning of accounts on someone
else's live books, so the pins are weighted toward what it must NOT do:

  * it must not retype an account whose name does not read as a gain or loss on a
    disposal, however suggestive its number or its posting history. The product's
    own industry seeds ship "Waste Disposal" and "Waste Oil Disposal" as ordinary
    expense accounts, and those are planted here as the offenders they are;
  * it must not write anything at all in `--report-only`;
  * it must not touch `gl_entry`, ever, on any path;
  * it must produce the same end state when run twice, and after a crash.

Every pin runs the REAL migration module against a real database initialized by
`init_schema`, rewound to its genuine pre-035 shape (the type unregistered and the
chart's two disposal accounts typed the way they shipped before M94).
"""
import importlib.util
import io
import json
import os
import sys
import uuid
from contextlib import redirect_stdout

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "035_disposal_gain_loss_account_type.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("migration_035", _MIGRATION)


# ── fixtures: a database rewound to its genuine pre-035 shape ────────────────

def _rewind_to_pre035(conn):
    """Un-register the type and re-type the chart the way it shipped pre-M94.

    A fresh install already has M94 applied (init_db seeds the type,
    setup-chart-of-accounts reads the reclassified chart), so every pin here has
    to put the database back before it can prove the migration does anything.
    """
    conn.execute("DELETE FROM account_type_registry WHERE account_type = ?",
                 (mig.NEW_TYPE,))
    conn.execute("UPDATE account SET account_type = 'revenue' "
                 "WHERE account_type = ? AND root_type = 'income'", (mig.NEW_TYPE,))
    conn.execute("UPDATE account SET account_type = 'expense' "
                 "WHERE account_type = ? AND root_type = 'expense'", (mig.NEW_TYPE,))
    conn.commit()


def _company(conn, name="M94 Co"):
    cid = str(uuid.uuid4())
    conn.execute("INSERT INTO company (id, name, abbr, default_currency) "
                 "VALUES (?, ?, ?, 'USD')", (cid, name, name[:4]))
    conn.commit()
    return cid


def _account(conn, cid, name, root_type, account_type, number=None, is_group=0):
    aid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO account (id, name, account_number, root_type, account_type, "
        " is_group, company_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (aid, name, number or f"X-{aid[:6]}", root_type, account_type, is_group, cid))
    conn.commit()
    return aid


def _type_of(conn, aid):
    return conn.execute("SELECT account_type FROM account WHERE id = ?",
                        (aid,)).fetchone()[0]


def _registered(conn):
    return conn.execute(
        "SELECT 1 FROM account_type_registry WHERE account_type = ? AND is_active = 1",
        (mig.NEW_TYPE,)).fetchone() is not None


def _run(db_path, report_only=False):
    """Run the migration, returning (result, printed output)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = mig.run_migration(db_path, report_only=report_only)
    return result, buf.getvalue()


def _gl_fingerprint(conn):
    """Everything about gl_entry that a migration could possibly disturb."""
    return conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(id || debit || credit || account_id)), 0) "
        "FROM gl_entry").fetchone()[:2]


@pytest.fixture
def chart(conn, db_path):
    """A pre-035 install carrying the shipped US GAAP chart for one company."""
    cid = _company(conn)
    gl = _load("db_query_gl_035",
               os.path.join(os.path.dirname(_SETUP_DIR), "erpclaw-gl", "db_query.py"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            gl.ACTIONS["setup-chart-of-accounts"](
                conn, _Args(company_id=cid, template="us_gaap"))
        except SystemExit:
            pass
    _rewind_to_pre035(conn)
    return {"company_id": cid}


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, k):
        return None


# ── the name rule, exercised directly ────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "Gain on Asset Disposal",              # us_gaap 4220
    "Loss on Asset Disposal",              # us_gaap 5340
    "Gain on Disposal of Fixed Assets",    # uk_coa_frs102
    "Gain on Disposal of Assets",          # eu / ca templates
    "Gain/(Loss) on Disposal",             # the combined-account shape
    "PROFIT ON DISPOSAL OF PLANT",         # case-insensitive
])
def test_a_disposal_gain_loss_name_is_recognized(name):
    assert mig.reads_as_disposal_gain_loss(name)


@pytest.mark.parametrize("name", [
    "Waste Disposal",              # industry_configs.py:167, restaurant, expense
    "Waste Oil Disposal",          # industry_configs.py:304, automotive, expense
    "Disposal Fees Revenue",       # a waste hauler's operating income
    "Sales Revenue",
    "Rent Expense",
    "Gain on Foreign Exchange",    # a gain, but not a disposal
    "",
    None,
])
def test_a_name_that_is_not_a_disposal_gain_loss_is_not_recognized(name):
    assert not mig.reads_as_disposal_gain_loss(name)


# ── the real thing, against the shipped chart ────────────────────────────────

def test_the_shipped_charts_two_disposal_accounts_are_retyped(conn, db_path, chart):
    cid = chart["company_id"]
    before = {n: conn.execute(
        "SELECT account_type FROM account WHERE company_id = ? AND account_number = ?",
        (cid, n)).fetchone()[0] for n in ("4220", "5340")}
    assert before == {"4220": "revenue", "5340": "expense"}
    assert not _registered(conn)

    result, out = _run(db_path)

    assert _registered(conn), "the type must be registered"
    after = {n: conn.execute(
        "SELECT account_type FROM account WHERE company_id = ? AND account_number = ?",
        (cid, n)).fetchone()[0] for n in ("4220", "5340")}
    assert after == {"4220": mig.NEW_TYPE, "5340": mig.NEW_TYPE}
    assert len(result["retyped"]) == 2
    assert "Gain on Asset Disposal" in out and "Loss on Asset Disposal" in out


def test_nothing_else_in_the_shipped_chart_moves(conn, db_path, chart):
    """94 accounts go in; exactly 2 change type. Anything else is a defect that
    would silently restate somebody's books."""
    cid = chart["company_id"]
    before = {r[0]: r[1] for r in conn.execute(
        "SELECT id, account_type FROM account WHERE company_id = ?", (cid,))}
    _run(db_path)
    after = {r[0]: r[1] for r in conn.execute(
        "SELECT id, account_type FROM account WHERE company_id = ?", (cid,))}

    changed = {k for k in before if before[k] != after[k]}
    assert len(changed) == 2, {
        conn.execute("SELECT name FROM account WHERE id = ?", (k,)).fetchone()[0]
        for k in changed}
    # Named, so a future widening cannot pass by changing WHICH two.
    names = sorted(conn.execute("SELECT name FROM account WHERE id = ?", (k,)).fetchone()[0]
                   for k in changed)
    assert names == ["Gain on Asset Disposal", "Loss on Asset Disposal"]
    # Interest Income sits under the same "Other Income" parent and stays revenue.
    assert conn.execute(
        "SELECT account_type FROM account WHERE company_id = ? AND account_number = '4210'",
        (cid,)).fetchone()[0] == "revenue"


# ── the offenders, planted ───────────────────────────────────────────────────

def test_a_waste_disposal_expense_account_is_never_retyped(conn, db_path):
    """The mutation this rule exists to survive: 'disposal' alone would reclassify
    a restaurant's waste-hauling expense into a disposal gain/loss account."""
    cid = _company(conn)
    waste = _account(conn, cid, "Waste Disposal", "expense", "expense")
    oil = _account(conn, cid, "Waste Oil Disposal", "expense", "expense")
    fees = _account(conn, cid, "Disposal Fees Revenue", "income", "revenue")

    result, _ = _run(db_path)

    assert result["retyped"] == []
    assert _type_of(conn, waste) == "expense"
    assert _type_of(conn, oil) == "expense"
    assert _type_of(conn, fees) == "revenue"


def test_a_re_tenanted_4220_is_never_retyped_but_is_reported(conn, db_path):
    """The renumbering trap. An install that moved its disposal account away and
    let something else take 4220 must not have that something else reclassified —
    but the operator has to be told, or a renamed account disappears silently."""
    cid = _company(conn)
    royalty = _account(conn, cid, "Royalty Income", "income", "revenue", number="4220")

    result, out = _run(db_path)

    assert result["retyped"] == []
    assert royalty in result["near_misses"]
    assert _type_of(conn, royalty) == "revenue"
    assert "Royalty Income" in out
    assert "does not read as a disposal" in out
    assert "update-account" in out


def test_an_account_used_for_a_disposal_leg_is_reported_not_retyped(conn, db_path):
    """Usage is evidence for a human, not a licence. Under the defect M61 fixed,
    EVERY disposal leg went to the category's depreciation account; retyping that
    would be a worse defect than the one being fixed."""
    cid = _company(conn)
    dep = _account(conn, cid, "Depreciation Expense", "expense", "expense")
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        " voucher_type, voucher_id, is_cancelled) "
        "VALUES (?, '2026-03-31', ?, '100.00', '0', 'asset_disposal', ?, 0)",
        (str(uuid.uuid4()), dep, str(uuid.uuid4())))
    conn.commit()

    result, out = _run(db_path)

    assert result["retyped"] == []
    assert dep in result["used_unmatched"]
    assert _type_of(conn, dep) == "expense"
    assert "has carried a disposal gain/loss leg" in out


def test_a_group_account_is_never_retyped(conn, db_path):
    """A group account cannot post, so retyping one changes nothing and would only
    put a leaf-shaped type on a structural node."""
    cid = _company(conn)
    grp = _account(conn, cid, "Gain on Asset Disposal", "income", "revenue", is_group=1)
    result, _ = _run(db_path)
    assert result["retyped"] == []
    assert _type_of(conn, grp) == "revenue"


def test_an_account_already_typed_for_other_machinery_is_never_retyped(conn, db_path):
    """`exchange_gain_loss` on an account named for disposal is a deliberate (if
    odd) choice. Only the plain mistyping M94 is about gets corrected."""
    cid = _company(conn)
    fx = _account(conn, cid, "Gain on Asset Disposal", "income", "exchange_gain_loss")
    result, _ = _run(db_path)
    assert result["retyped"] == []
    assert _type_of(conn, fx) == "exchange_gain_loss"


def test_an_untyped_disposal_account_is_retyped(conn, db_path):
    """M91 called the untyped account the sharpest edge of its change: legal to
    create, refused by the gate, and unfixable in place. It is typed here."""
    cid = _company(conn)
    untyped = _account(conn, cid, "Loss on Asset Disposal", "expense", None)
    result, _ = _run(db_path)
    assert result["retyped"] == [untyped]
    assert _type_of(conn, untyped) == mig.NEW_TYPE


def test_the_wrong_side_is_never_retyped(conn, db_path):
    """A disposal-named account on the wrong root is not a disposal account of
    ours; the UK/EU templates land theirs on root_type='asset' and must be left
    alone rather than guessed at."""
    cid = _company(conn)
    wrong = _account(conn, cid, "Gain on Disposal of Fixed Assets", "asset", None)
    result, _ = _run(db_path)
    assert result["retyped"] == []
    assert _type_of(conn, wrong) is None


# ── multiplicity ─────────────────────────────────────────────────────────────

def test_every_company_is_retyped_not_just_the_first(conn, db_path):
    """One `account` table serves every company; a per-company install must not be
    half-migrated."""
    a = _account(conn, _company(conn, "Alpha"), "Gain on Asset Disposal",
                 "income", "revenue")
    b = _account(conn, _company(conn, "Beta"), "Gain on Asset Disposal",
                 "income", "revenue")
    result, _ = _run(db_path)
    assert set(result["retyped"]) == {a, b}
    assert _type_of(conn, a) == _type_of(conn, b) == mig.NEW_TYPE


def test_two_matches_on_one_side_are_both_retyped_and_flagged(conn, db_path):
    cid = _company(conn)
    one = _account(conn, cid, "Gain on Asset Disposal", "income", "revenue")
    two = _account(conn, cid, "Profit on Disposal of Equipment", "income", "revenue")
    result, out = _run(db_path)
    assert set(result["retyped"]) == {one, two}
    assert "2 accounts matching the gain side" in out


# ── the migration-class properties ───────────────────────────────────────────

def test_report_only_writes_nothing_at_all(conn, db_path, chart):
    """Not the registry, not one account. Compared as a canonical dump, not
    eyeballed."""
    import sqlite3 as _sqlite3
    before = "\n".join(_sqlite3.connect(db_path).iterdump())

    result, out = _run(db_path, report_only=True)

    after = "\n".join(_sqlite3.connect(db_path).iterdump())
    assert before == after
    assert result["report_only"] is True
    assert len(result["retyped"]) == 2, "it still says what it would do"
    assert "would retype" in out
    assert "gl_entry is NOT touched" in out


def test_report_only_then_real_run_does_what_it_said(conn, db_path, chart):
    planned, _ = _run(db_path, report_only=True)
    done, _ = _run(db_path)
    assert sorted(planned["retyped"]) == sorted(done["retyped"])


def test_a_second_run_changes_nothing(conn, db_path, chart):
    import sqlite3 as _sqlite3
    first, _ = _run(db_path)
    after_first = "\n".join(_sqlite3.connect(db_path).iterdump())

    second, out = _run(db_path)

    assert second["retyped"] == []
    assert "\n".join(_sqlite3.connect(db_path).iterdump()) == after_first
    assert "already registered" in out
    assert len(first["retyped"]) == 2


def test_a_second_run_does_not_slander_the_accounts_it_just_fixed(conn, db_path, chart):
    """Caught by driving the migration rather than by reading it: on the second
    run the retyped account is no longer a candidate, and without an "already
    typed" bucket it fell through to the not-retyped reports and was described as
    an account "whose name does not read as a disposal gain/loss account" — a
    false statement, printed on the one run where everything went right, next to
    an instruction to go fix it by hand."""
    cid = chart["company_id"]
    gain = conn.execute(
        "SELECT id FROM account WHERE company_id = ? AND account_number = '4220'",
        (cid,)).fetchone()[0]
    # Give it a disposal history, which is what made it look "used but unmatched".
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        " voucher_type, voucher_id, is_cancelled) "
        "VALUES (?, '2026-03-31', ?, '0', '1083.33', 'asset_disposal', ?, 0)",
        (str(uuid.uuid4()), gain, str(uuid.uuid4())))
    conn.commit()

    _run(db_path)
    second, out = _run(db_path)

    assert gain in second["already"]
    assert gain not in second["used_unmatched"]
    assert gain not in second["near_misses"]
    assert "already typed 'disposal_gain_loss', left alone" in out
    assert "does not read as a disposal" not in out
    assert "no account matches the disposal gain/loss rule" not in out
    assert "every disposal account already carries" in out


def _account_snapshot(conn):
    """Every column of every account row, keyed by id."""
    cur = conn.execute("SELECT * FROM account")
    columns = [d[0] for d in cur.description]
    return {row["id"]: dict(zip(columns, tuple(row))) for row in cur.fetchall()}


def test_the_migration_changes_account_type_and_nothing_else(conn, db_path, chart):
    """The SIM's headline safety claim, pinned instead of asserted (M94 rider R7).

    "It does not move any money, rename any account, or change any `root_type`:
    every report that classifies income and expense does so by `root_type`, so
    the P&L, balance sheet, trial balance, cash flow and profitability ratios all
    produce IDENTICAL numbers before and after." That claim is the entire reason
    this migration is safe to run on live books, and until this test nothing
    checked it: a migration that also wrote `root_type = 'income'` moved 5340
    Loss on Asset Disposal from the expense side of the P&L to the income side —
    flipping the sign of a real balance on every report — and the whole suite
    stayed green.

    Column-by-column over the WHOLE table rather than root_type alone, because
    `root_type` was only the column that happened to be mutated: `is_group`,
    `balance_direction`, `parent_id`, `company_id` and `currency` would each do
    comparable damage and none of them had a witness either.
    """
    before = _account_snapshot(conn)

    result, _ = _run(db_path)

    after = _account_snapshot(conn)
    assert set(before) == set(after), "no account was created or deleted"
    retyped = set(result["retyped"])
    assert len(retyped) == 2

    moved = {}
    for aid, old in before.items():
        for column, old_value in old.items():
            if column == "updated_at":
                continue  # the retype stamps it; that is the point of the stamp
            new_value = after[aid][column]
            if new_value != old_value:
                moved.setdefault(column, []).append((aid, old_value, new_value))

    assert set(moved) == {"account_type"}, (
        "migration 035 changed a column other than account_type: "
        + ", ".join(sorted(moved)))
    assert {aid for aid, _o, _n in moved["account_type"]} == retyped
    for aid, old_value, new_value in moved["account_type"]:
        assert old_value in ("revenue", "expense")
        assert new_value == mig.NEW_TYPE
    # named explicitly too, so a mutation that widened the rule cannot pass by
    # moving root_type on some OTHER account that was never a candidate.
    assert all(after[aid]["root_type"] == before[aid]["root_type"]
               for aid in before), "root_type moved on some account"


def test_root_type_survives_every_shape_the_migration_touches(conn, db_path):
    """The same pin on the accounts a hand-built chart plants: both sides, the
    untyped case, the combined account, and one it must not touch at all."""
    cid = _company(conn)
    planted = {
        _account(conn, cid, "Gain on Asset Disposal", "income", "revenue"): "income",
        _account(conn, cid, "Loss on Asset Disposal", "expense", "expense"): "expense",
        _account(conn, cid, "Gain/(Loss) on Disposal", "income", None): "income",
        _account(conn, cid, "Waste Disposal", "expense", "expense"): "expense",
    }
    _run(db_path)
    for aid, root_type in planted.items():
        assert conn.execute("SELECT root_type FROM account WHERE id = ?",
                            (aid,)).fetchone()[0] == root_type


def test_gl_entry_is_never_touched(conn, db_path, chart):
    """The house rule is cancel = reverse, never edit. A migration that silently
    rewrote posted ledger rows would be a worse defect than the one it fixes."""
    cid = chart["company_id"]
    acct = conn.execute(
        "SELECT id FROM account WHERE company_id = ? AND account_number = '4220'",
        (cid,)).fetchone()[0]
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        " voucher_type, voucher_id, is_cancelled) "
        "VALUES (?, '2026-03-31', ?, '0', '1083.33', 'asset_disposal', ?, 0)",
        (str(uuid.uuid4()), acct, str(uuid.uuid4())))
    conn.commit()
    before = _gl_fingerprint(conn)

    _run(db_path)

    assert _gl_fingerprint(conn) == before
    assert _type_of(conn, acct) == mig.NEW_TYPE, "the account moved, the entry did not"


def test_an_operator_deactivated_type_stops_the_whole_migration(conn, db_path, chart):
    """`deactivate-account-type` is a deliberate act. Retyping accounts to a type
    this install has switched off would leave dispose-asset refusing them."""
    conn.execute("INSERT INTO account_type_registry "
                 "(account_type, skill_name, label, is_active) VALUES (?, ?, ?, 0)",
                 (mig.NEW_TYPE, "erpclaw-assets", "Disposal Gain/Loss"))
    conn.commit()

    result, out = _run(db_path)

    assert result["retyped"] == []
    assert result["reason"] == "type deactivated by operator"
    assert "INACTIVE" in out
    cid = chart["company_id"]
    assert conn.execute(
        "SELECT account_type FROM account WHERE company_id = ? AND account_number = '4220'",
        (cid,)).fetchone()[0] == "revenue"


def test_matching_nothing_is_a_clean_run_that_says_so(conn, db_path):
    """A hand-built or non-English chart. The type is still registered, so the
    remedies the message names actually work."""
    cid = _company(conn)
    _account(conn, cid, "Anlagenabgang Gewinn", "income", "revenue")

    result, out = _run(db_path)

    assert result["retyped"] == []
    assert _registered(conn), "registration still happened"
    assert "nothing retyped" in out
    assert "add-account --account-type disposal_gain_loss" in out
    assert "update-account --account-type disposal_gain_loss" in out


# ── the erpclaw-ops version requirement the retyping creates (M94 rider R1) ──
#
# SIM §2 names three mitigations for the one skew this migration cannot close
# from the assets side (foundation migrated, erpclaw-ops stale). The first of
# them is this print. It was claimed in the SIM and did not exist in the code —
# a false mitigation for an admitted breakage window — so these pin the real one.


def _seed_ops_module(conn, version, status="installed"):
    """A module-catalog row for erpclaw-ops, as module_manager writes one."""
    conn.execute(
        "INSERT INTO erpclaw_module (id, name, display_name, version, category, "
        " install_status) VALUES (?, 'erpclaw-ops', 'Operations', ?, 'expansion', ?)",
        (str(uuid.uuid4()), version, status))
    conn.commit()


def test_the_retyping_run_prints_the_erpclaw_ops_version_requirement(conn, db_path, chart):
    """The mitigation itself. An operator who runs this migration must be told,
    on the run that creates the skew, that a stale erpclaw-ops refuses the very
    accounts it just designated."""
    result, out = _run(db_path)

    assert len(result["retyped"]) == 2
    assert mig._OPS_MODULE in out
    assert ">= %s" % mig._OPS_GATE_VERSION in out
    assert "REFUSES it" in out


def test_a_stale_erpclaw_ops_is_named_with_its_version_and_the_update_command(
        conn, db_path, chart):
    """The version is compared, not just recited: the catalog says 2.2.0, the gate
    needs 2.3.0, so the run says REFUSE and names both the update and the undo."""
    _seed_ops_module(conn, "2.2.0")

    result, out = _run(db_path)

    assert "records erpclaw-ops 2.2.0 (install_status installed)" in out
    assert "OLDER than %s" % mig._OPS_GATE_VERSION in out
    assert "will REFUSE the account(s) above" in out
    assert "--action update-module --module-name erpclaw-ops" in out
    assert "--reclassify-posted" in out, "the undo has to be reachable too"


def test_an_up_to_date_erpclaw_ops_is_told_it_is_fine_rather_than_warned(
        conn, db_path, chart):
    """A warning that fires on every install is a warning nobody reads. The
    catalog at the required version gets the requirement and a clean verdict."""
    _seed_ops_module(conn, mig._OPS_GATE_VERSION)

    _, out = _run(db_path)

    assert "meets the requirement" in out
    assert "OLDER than" not in out
    assert "will REFUSE" not in out


def test_an_install_without_erpclaw_ops_is_not_told_its_addon_is_stale(conn, db_path, chart):
    """No catalog row means nothing enforces the gate here at all, which is a
    different sentence from "yours is out of date" and must not be confused with
    it — the fixture install has no erpclaw_module row."""
    _, out = _run(db_path)

    assert "is not installed here" in out
    assert "OLDER than" not in out


def test_an_uncomparable_version_says_so_instead_of_guessing(conn, db_path, chart):
    """A version string nobody can parse must not be reported as satisfying the
    requirement; silence in the safe direction is the whole point of the print."""
    _seed_ops_module(conn, "nightly")

    _, out = _run(db_path)

    assert "cannot be compared" in out
    assert "meets the requirement" not in out
    assert "--action update-module --module-name erpclaw-ops" in out


def test_report_only_states_the_requirement_in_the_conditional(conn, db_path, chart):
    """--report-only exists so an operator can see the whole consequence before
    committing to it, and the erpclaw-ops skew IS the consequence."""
    _seed_ops_module(conn, "2.2.0")

    _, out = _run(db_path, report_only=True)

    assert "would be typed 'disposal_gain_loss'" in out
    assert "OLDER than %s" % mig._OPS_GATE_VERSION in out


def test_a_second_run_repeats_the_requirement_for_the_already_typed_accounts(
        conn, db_path, chart):
    """The skew does not go away on a re-run; the accounts are still typed for a
    gate the stale addon does not have. Silence on the second run would be the
    same false comfort in a different place."""
    _seed_ops_module(conn, "2.2.0")
    _run(db_path)

    second, out = _run(db_path)

    assert second["retyped"] == []
    assert len(second["already"]) == 2
    assert "OLDER than %s" % mig._OPS_GATE_VERSION in out


def test_a_run_that_retypes_nothing_does_not_raise_the_requirement(conn, db_path):
    """Nothing was designated, so nothing is refused, so there is nothing to warn
    about. An unconditional print would train operators to ignore it."""
    _seed_ops_module(conn, "2.2.0")
    cid = _company(conn)
    _account(conn, cid, "Waste Disposal", "expense", "expense")

    result, out = _run(db_path)

    assert result["retyped"] == []
    assert "OLDER than" not in out
    assert "dispose-asset accepts that type only" not in out


@pytest.mark.parametrize("text,expected", [
    ("2.3.0", (2, 3, 0)),
    ("2.3", (2, 3, 0)),
    ("v2.3.1", (2, 3, 1)),
    ("2.10.0", (2, 10, 0)),
    ("nightly", None),
    ("", None),
    (None, None),
])
def test_the_version_parser_reads_what_it_can_and_refuses_the_rest(text, expected):
    assert mig.version_tuple(text) == expected


def test_2_10_0_is_newer_than_2_9_0(conn, db_path, chart):
    """String comparison would call 2.10.0 older than 2.9.0. Pinned because the
    verdict this print gives is only as good as the comparison behind it."""
    _seed_ops_module(conn, "2.10.0")
    _, out = _run(db_path)
    assert "meets the requirement" in out


def test_the_printed_ops_requirement_matches_the_version_erpclaw_ops_declares():
    """The number the foundation prints and the number the addon calls itself must
    be the same number, or the migration sends operators after a release that does
    not exist. Dev-tree only: the published erpclaw repo has no addon beside it."""
    import re as _re
    # _SETUP_DIR = source/erpclaw/scripts/erpclaw-setup; three levels up is source/.
    skill = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(_SETUP_DIR))),
        "erpclaw-addons", "erpclaw-ops", "SKILL.md")
    if not os.path.isfile(skill):
        pytest.skip("erpclaw-ops not present beside this checkout")
    with open(skill) as fh:
        declared = _re.search(r"^version:\s*(\S+)\s*$", fh.read(), _re.MULTILINE)
    assert declared, "erpclaw-ops SKILL.md has no version frontmatter"
    assert declared.group(1) == mig._OPS_GATE_VERSION, (
        "migration 035 tells operators to run erpclaw-ops >= %s, but erpclaw-ops "
        "declares %s" % (mig._OPS_GATE_VERSION, declared.group(1)))


def test_the_retype_is_reversible_through_update_account(conn, db_path):
    """The rollback claim in SIM §8, driven rather than asserted: the only data
    footprint is a column the shipped action can put back."""
    cid = _company(conn)
    aid = _account(conn, cid, "Gain on Asset Disposal", "income", "revenue")
    _run(db_path)
    assert _type_of(conn, aid) == mig.NEW_TYPE

    gl = _load("db_query_gl_035_rollback",
               os.path.join(os.path.dirname(_SETUP_DIR), "erpclaw-gl", "db_query.py"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            gl.ACTIONS["update-account"](conn, _Args(
                account_id=aid, account_type="revenue", reclassify_posted=False))
        except SystemExit:
            pass
    assert _type_of(conn, aid) == "revenue"


# ── the audit trail (M102) ───────────────────────────────────────────────────
#
# The reversal above is the reason this section exists. It needs the account id
# and the type that account carried BEFORE the run, and until M102 the only
# places those two facts lived were `account.updated_at` (which says when, not
# what) and a line printed to a terminal. SIM: planning/simlogs/m102_SIM_2026-08-12.md.

def _trail(conn):
    """Every audit_log row this migration wrote, oldest first."""
    cur = conn.execute(
        "SELECT entity_type, entity_id, old_values, new_values, description, skill "
        "FROM audit_log WHERE action = ? ORDER BY timestamp, entity_type",
        ("migration:" + mig.MIGRATION_ID,))
    columns = [d[0] for d in cur.description]
    rows = [dict(zip(columns, tuple(r))) for r in cur.fetchall()]
    for row in rows:
        for key in ("old_values", "new_values"):
            row[key] = json.loads(row[key]) if row[key] else None
    return rows


def test_the_trail_describes_exactly_the_accounts_that_changed(conn, db_path, chart):
    """The row-level check the static L0 gate cannot make: the trail is compared
    against the ACTUAL before/after diff of the `account` table, not against what
    the migration says it did.

    This is what catches a row that LIES. A trail built from a different variable
    than the UPDATE, or one that reports the new type as the old one, or one that
    covers three accounts when two moved, is a defect the whole rest of this file
    passes over — every other test here reads the migration's own return value or
    its own printed output, which a wrong trail would agree with.
    """
    before = _account_snapshot(conn)
    result, _ = _run(db_path)
    after = _account_snapshot(conn)

    moved = {aid: (before[aid]["account_type"], after[aid]["account_type"])
             for aid in before
             if before[aid]["account_type"] != after[aid]["account_type"]}
    assert moved, "nothing moved, so this test would pass vacuously"

    account_rows = {r["entity_id"]: r for r in _trail(conn)
                    if r["entity_type"] == "account"}
    assert set(account_rows) == set(moved), (
        "the trail names a different set of accounts than the ones that changed: "
        f"trail={sorted(account_rows)} moved={sorted(moved)}")
    for aid, (old_type, new_type) in moved.items():
        row = account_rows[aid]
        assert row["old_values"] == {"account_type": old_type}, (aid, row)
        assert row["new_values"] == {"account_type": new_type}, (aid, row)
        assert row["skill"] == "erpclaw-setup"
    assert result["audit_rows"] == len(moved) + 1  # + the type registration


def test_the_trail_carries_the_command_that_reverses_each_retype(conn, db_path):
    """M102's whole point: the reversal must not depend on scrollback.

    The description is not decoration — it holds the account id and the type to
    put back, which is exactly the argument list `update-account` needs.
    """
    cid = _company(conn)
    aid = _account(conn, cid, "Gain on Asset Disposal", "income", "revenue")
    _run(db_path)

    row = next(r for r in _trail(conn) if r["entity_id"] == aid)
    assert "update-account" in row["description"]
    assert aid in row["description"]
    assert "--account-type revenue" in row["description"]
    assert "--reclassify-posted" in row["description"]


def test_the_registration_is_recorded_too(conn, db_path, chart):
    """The retype is only reversible while the type it moved to still exists, so
    the registration belongs in the same trail."""
    _run(db_path)
    rows = [r for r in _trail(conn) if r["entity_type"] == "account_type_registry"]
    assert len(rows) == 1
    assert rows[0]["entity_id"] == mig.NEW_TYPE
    assert rows[0]["old_values"] is None
    assert rows[0]["new_values"]["skill_name"] == "erpclaw-assets"


def test_report_only_writes_no_trail_at_all(conn, db_path, chart):
    """A trail for a change that did not happen is worse than no trail."""
    _run(db_path, report_only=True)
    assert _trail(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM account WHERE account_type = ?",
                        (mig.NEW_TYPE,)).fetchone()[0] == 0


def test_a_second_run_does_not_duplicate_the_trail(conn, db_path, chart):
    """Idempotence of the trail is a property of the migration, not of the audit
    code: the second run has an empty change set, so it writes nothing."""
    _run(db_path)
    first = _trail(conn)
    assert len(first) == 3

    result, _ = _run(db_path)

    assert result["audit_rows"] == 0
    assert _trail(conn) == first, "the second run wrote or altered trail rows"


def test_a_failed_migration_leaves_no_trail(conn, db_path, chart):
    """Same transaction, proven by breaking it.

    The audit write and the retype commit together or not at all. Here the
    retype's own UPDATE is made to fail after the registration and the first
    audit row have been executed; both must roll back, or the install ends up
    with a trail describing a change it does not carry.
    """
    before = _account_snapshot(conn)
    original = mig._UPDATE_ACCOUNT_TYPE
    mig._UPDATE_ACCOUNT_TYPE = "UPDATE account SET account_type = ?, no_such_column = ? WHERE id = ?"
    try:
        with pytest.raises(Exception):
            _run(db_path)
    finally:
        mig._UPDATE_ACCOUNT_TYPE = original

    assert _trail(conn) == [], "a rolled-back run left audit rows behind"
    assert _account_snapshot(conn) == before, "the failed run changed an account"
    assert not _registered(conn), "the registration survived a rolled-back run"


def test_the_run_tells_the_operator_where_the_trail_is(conn, db_path, chart):
    """A trail nobody knows about is worth as little as no trail. The one line
    that survives into the operator's terminal is the query that finds it later."""
    _result, out = _run(db_path)
    assert "audit trail: 3 audit_log row(s)" in out
    assert 'get-audit-log --audit-action "migration:%s"' % mig.MIGRATION_ID in out


def test_the_migration_id_is_the_stem_the_runner_ledgers_it_under(conn, db_path):
    """The action string is derived from the filename, and the runner's ledger id
    is the same stem (`migration_runner.discover` takes `fn[:-3]`). If they ever
    disagree, `get-audit-log --audit-action` finds nothing and nothing else fails.
    """
    assert mig.MIGRATION_ID == "035_disposal_gain_loss_account_type"
    assert os.path.basename(_MIGRATION) == mig.MIGRATION_ID + ".py"


def test_the_trail_is_retrievable_through_the_shipped_read_action(conn, db_path, chart):
    """Driven through `get-audit-log`, not through raw SQL.

    This is the answer to "an audit row the operator cannot see is worth little"
    (M99): the row is NOT delivered through the runner's stdout, which M99 shows
    goes to stderr. It is delivered through a routable, documented action that
    works months later.
    """
    _run(db_path)
    setup_dq = _load("db_query_setup_m102", os.path.join(_SETUP_DIR, "db_query.py"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            setup_dq.ACTIONS["get-audit-log"](conn, _Args(
                audit_action="migration:" + mig.MIGRATION_ID, limit=50))
        except SystemExit:
            pass
    payload = json.loads(buf.getvalue())
    entries = payload.get("data", payload).get("entries")
    assert len(entries) == 3, payload
    assert {e["entity_type"] for e in entries} == {"account", "account_type_registry"}
    # the JSON columns come back parsed, so the operator reads values not blobs
    account_entry = next(e for e in entries if e["entity_type"] == "account")
    assert account_entry["new_values"]["account_type"] == mig.NEW_TYPE
