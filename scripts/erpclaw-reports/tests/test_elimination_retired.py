"""Part A — the four legacy intercompany-elimination actions are RETIRED (M63-C).

The retired pair (`elimination_rule` / `elimination_entry`, owned by the
erpclaw-growth addon) was written by these four foundation actions and by nothing
else. `run-elimination` in particular posted its "balanced" pair straight into
live ``gl_entry`` with raw SQL, across two companies at once — measured before the
retirement: the target company's own trial balance came out 1,000.00 short, and
neither leg passed the constitutional 12-step helper. Group eliminations belong to
the consolidation layer (ADR-0010: "subsidiary books untouched"), which is what
erpclaw-accounting-adv already does behaviorally-tested.

What is pinned here is the RETIREMENT CONTRACT, and it has two halves that must
both hold:

  1. the actions stay ROUTABLE and answer with a helpful steer — a caller (or an
     agent replaying an old invocation, legacy flags and all) gets one JSON object
     naming the replacement flow, exit 1, never a traceback and never
     "Unknown action";
  2. NOTHING LANDS. The fixture deliberately gives the DB the legacy tables (the
     shape a pre-migration install has), so "the table is gone" cannot be what
     makes the test pass. Against the pre-retirement handler this file is red on
     both halves — `run-elimination` returned ``status: ok`` and wrote 2 gl_entry
     rows + 1 elimination_entry row.

The legacy pair is declared as SQLAlchemy metadata and provisioned through the
seam (ADR-0034), not as hand-written DDL — a test fixture is not a licence to
re-declare a table the tree just retired.
"""
import importlib.util
import json
import os
import subprocess
import sys
import uuid

import pytest

from payments_helpers import call_action, ns

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)                 # erpclaw-reports/
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)               # scripts/
_LIB_DIR = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")

from erpclaw_lib import seam  # noqa: E402

RETIRED_ACTIONS = [
    "add-elimination-rule",
    "list-elimination-rules",
    "run-elimination",
    "list-elimination-entries",
]

# The replacement flow, in call order. Every steer must name ALL of it.
#
# approve- and post- are in this list on purpose: `add-ic-transaction` creates a
# draft and `generate-elimination-entries` filters on ic_status = 'posted', so a
# steer that stops at add- routes the caller to entries_created: 0 with a
# status of "ok". An incomplete route is a silent failure wearing a success.
ADVACCT_FLOW = [
    "add-consolidation-group",
    "add-group-entity",
    "add-ic-transaction",
    "approve-ic-transaction",
    "post-ic-transaction",
    "generate-elimination-entries",
]


def _load(name, rel_path):
    path = os.path.join(_SCRIPTS_DIR, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rep = _load("db_query_reports_m63c", "erpclaw-reports/db_query.py")


def _legacy_metadata():
    """The retired pair, in the shape erpclaw-growth's init_db used to create.

    Declared, never hand-written: `seam.provision` emits the dialect-correct DDL.
    """
    sa = seam._sqlalchemy()
    md = sa.MetaData()
    sa.Table(
        "elimination_rule", md,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("source_company_id", sa.Text, nullable=False),
        sa.Column("target_company_id", sa.Text, nullable=False),
        sa.Column("source_account_id", sa.Text, nullable=False),
        sa.Column("target_account_id", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_at", sa.Text),
        sa.Column("updated_at", sa.Text),
    )
    sa.Table(
        "elimination_entry", md,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("elimination_rule_id", sa.Text,
                  sa.ForeignKey("elimination_rule.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("fiscal_year_id", sa.Text),
        sa.Column("posting_date", sa.Text, nullable=False),
        sa.Column("amount", sa.Text, nullable=False, server_default="0"),
        sa.Column("source_gl_entry_id", sa.Text),
        sa.Column("target_gl_entry_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="posted"),
        sa.Column("created_at", sa.Text),
    )
    return md


@pytest.fixture
def legacy_env(conn, db_path):
    """A PRE-migration install: legacy tables present, seeded, with a live rule.

    Two companies, an income account in one and an expense account in the other,
    a fiscal year, one 1,000.00 intercompany revenue credit, one active rule —
    the exact input the legacy engine used to act on.
    """
    seam.provision(_legacy_metadata(), db_path)

    co_a, co_b = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, 'Parent Co', 'PC')", (co_a,))
    conn.execute("INSERT INTO company (id, name, abbr) VALUES (?, 'Sub Co', 'SC')", (co_b,))
    inc, exp = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute("INSERT INTO account (id, name, root_type, account_type, company_id) "
                 "VALUES (?, 'IC Revenue', 'income', 'revenue', ?)", (inc, co_a))
    conn.execute("INSERT INTO account (id, name, root_type, account_type, company_id) "
                 "VALUES (?, 'IC Expense', 'expense', 'expense', ?)", (exp, co_b))
    fy = str(uuid.uuid4())
    conn.execute("INSERT INTO fiscal_year (id, name, start_date, end_date, company_id) "
                 "VALUES (?, 'FY2026', '2026-01-01', '2026-12-31', ?)", (fy, co_a))
    conn.execute(
        "INSERT INTO gl_entry (id, posting_date, account_id, debit, credit, "
        "voucher_type, voucher_id, remarks, fiscal_year, is_cancelled) "
        "VALUES (?, '2026-06-30', ?, '0', '1000.00', 'sales_invoice', ?, 'IC sale', 'FY2026', 0)",
        (str(uuid.uuid4()), inc, str(uuid.uuid4())))
    rule = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO elimination_rule (id, name, source_company_id, target_company_id, "
        "source_account_id, target_account_id, status) VALUES (?,?,?,?,?,?, 'active')",
        (rule, "IC sales elimination", co_a, co_b, inc, exp))
    conn.commit()
    return {"company_id": co_a, "target_company_id": co_b, "source_account_id": inc,
            "target_account_id": exp, "fiscal_year_id": fy, "rule_id": rule}


def _legacy_args(env):
    """Every legacy flag at once — the old invocation, replayed verbatim."""
    return ns(name="IC sales elimination",
              company_id=env["company_id"],
              target_company_id=env["target_company_id"],
              source_account_id=env["source_account_id"],
              target_account_id=env["target_account_id"],
              fiscal_year_id=env["fiscal_year_id"],
              posting_date="2026-12-31",
              as_of_date=None)


def _counts(conn):
    return {
        "elimination_rule": conn.execute(
            "SELECT COUNT(*) AS n FROM elimination_rule").fetchone()["n"],
        "elimination_entry": conn.execute(
            "SELECT COUNT(*) AS n FROM elimination_entry").fetchone()["n"],
        "gl_entry": conn.execute("SELECT COUNT(*) AS n FROM gl_entry").fetchone()["n"],
    }


# ──────────────────────────────────────────────────────────────────────────────
# 1. The steer
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", RETIRED_ACTIONS)
def test_retired_action_returns_a_steer_not_a_result(action, conn, legacy_env):
    """Each retired action answers with one JSON error that names its replacement."""
    result = call_action(rep.ACTIONS[action], conn, _legacy_args(legacy_env))

    assert result["status"] == "error", (
        f"{action} still returned a result: {json.dumps(result)[:300]}")
    assert "retired" in result["message"].lower()
    assert action in result["message"], "the message must name the action the caller typed"
    suggestion = result.get("suggestion", "")
    for replacement in ADVACCT_FLOW:
        assert replacement in suggestion, (
            f"the steer for {action} does not name {replacement}; a retirement "
            f"without a route is a dead end. Got: {suggestion}")


def test_all_four_share_one_steer(conn, legacy_env):
    """One retirement, one message shape — four hand-written variants would drift.

    Asserts the shared value is the REAL steer, not a shared absence: a set of
    four ``None``s also has size 1, and that is exactly the pre-retirement state.
    """
    suggestions = {
        call_action(rep.ACTIONS[a], conn, _legacy_args(legacy_env)).get("suggestion")
        for a in RETIRED_ACTIONS
    }
    assert len(suggestions) == 1, f"steers have drifted apart: {suggestions}"
    shared = suggestions.pop()
    assert shared and all(r in shared for r in ADVACCT_FLOW), (
        f"the shared steer does not route anywhere: {shared!r}")


# ──────────────────────────────────────────────────────────────────────────────
# 2. Nothing lands
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", RETIRED_ACTIONS)
def test_retired_action_writes_nothing(action, conn, legacy_env):
    """The tables are PRESENT and seeded; the retired action must still not touch
    them, and must not post GL. This is the half that was red before M63-C:
    run-elimination wrote 2 gl_entry rows + 1 elimination_entry row here."""
    before = _counts(conn)
    call_action(rep.ACTIONS[action], conn, _legacy_args(legacy_env))
    conn.commit()
    after = _counts(conn)

    assert after == before, (
        f"{action} wrote to the retired surface: {before} -> {after}")
    assert after["elimination_entry"] == 0
    assert after["gl_entry"] == 1, "the seeded IC revenue credit must be the only GL row"


def test_no_elimination_voucher_reaches_the_ledger(conn, legacy_env):
    """The specific shape M63-C removes: a gl_entry row with the group-elimination
    voucher type, written outside insert_gl_entries."""
    call_action(rep.ACTIONS["run-elimination"], conn, _legacy_args(legacy_env))
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM gl_entry WHERE voucher_type = 'elimination_entry'"
    ).fetchone()["n"]
    assert n == 0, f"{n} elimination gl_entry row(s) posted by a retired action"


def _sql_text(src):
    """Every STRING literal in `src`, concatenated — where table names really live.

    Two exclusions fall out of scanning tokens instead of raw text, and both are
    load-bearing here:

    * **comments** — the retirement is documented in a block that necessarily
      NAMES the table it retires; a raw substring scan would police its own
      documentation (the ratchet's comment-stripper lesson);
    * **identifiers** — the retired handlers keep the names the router maps to
      (`add_elimination_rule`), and a function name is not a table reference.
    """
    import io
    import tokenize

    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.STRING:
                out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src  # unparseable: scan it whole rather than skip it
    return "\n".join(out)


def _offending_references(src):
    """Live SQL references to the ledger or to the retired growth-owned tables."""
    sql = _sql_text(src)
    hits = []
    if "INSERT INTO gl_entry" in sql:
        hits.append("INSERT INTO gl_entry")
    for table in ("elimination_rule", "elimination_entry"):
        if table in sql:
            hits.append(table)
    return hits


def test_reports_module_no_longer_writes_the_ledger_directly():
    """erpclaw-reports is a read-only module by its own docstring. The retirement
    removes its only two raw ledger INSERTs — the live offender named by M79 —
    and its last reference to a table another module owns."""
    src = open(os.path.join(_SCRIPTS_DIR, "erpclaw-reports", "db_query.py")).read()
    assert _offending_references(src) == [], (
        "erpclaw-reports references the ledger or the retired pair in LIVE CODE "
        "again: route GL through erpclaw_lib.gl_posting.insert_gl_entries "
        "(constitution Article 6), and never read a growth-owned table here")


def test_the_reference_detector_is_not_vacuous():
    """D10 — the check above must actually catch what it claims to catch, and
    must not fire on prose about it or on the handler names the router needs.
    Planted forms, not assumed behavior."""
    # caught: the real shapes
    assert _offending_references(
        'conn.execute("INSERT INTO gl_entry (id) VALUES (?)", (x,))'
    ) == ["INSERT INTO gl_entry"]
    assert _offending_references('r_t = Table("elimination_rule")') == ["elimination_rule"]
    assert _offending_references('q = "SELECT * FROM elimination_entry"') == ["elimination_entry"]
    assert _offending_references(
        'conn.execute("""INSERT INTO gl_entry\n   (id, debit)\n   VALUES (?, ?)""", p)'
    ) == ["INSERT INTO gl_entry"], "the multi-line form is the one that was there"
    # not caught: prose about it, and the retired handler names themselves
    assert _offending_references(
        "# the retired elimination_rule / elimination_entry pair once took an\n"
        "# INSERT INTO gl_entry here\n"
        "x = 1\n"
    ) == [], "a comment about the retirement must not read as the retirement"
    assert _offending_references(
        "def add_elimination_rule(conn, args):\n    _retired_elimination('add-elimination-rule')\n"
    ) == [], "the router still maps to these function names; they are not table refs"


# ──────────────────────────────────────────────────────────────────────────────
# 3. The CLI contract (routable, legacy flags parse, no traceback)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", RETIRED_ACTIONS)
def test_cli_steers_with_the_legacy_flags(action, db_path, legacy_env):
    """Driven through the real dispatcher with the OLD flags. Proves three things
    at once: the action is still routable, the retired flags still parse (an old
    script gets the steer, not "unrecognized arguments"), and the process exits
    1 with JSON — never a traceback."""
    script = os.path.join(_SCRIPTS_DIR, "erpclaw-reports", "db_query.py")
    env = {**os.environ, "ERPCLAW_DB_PATH": db_path,
           # Bind erpclaw_lib to THIS tree, not the deployed ~/.openclaw symlink
           # (F21-FINDING-3): a subprocess would otherwise measure another checkout.
           "PYTHONPATH": _LIB_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.run(
        [sys.executable, script, "--action", action,
         "--company-id", legacy_env["company_id"],
         "--target-company-id", legacy_env["target_company_id"],
         "--source-account-id", legacy_env["source_account_id"],
         "--target-account-id", legacy_env["target_account_id"],
         "--fiscal-year-id", legacy_env["fiscal_year_id"],
         "--name", "IC sales elimination",
         "--posting-date", "2026-12-31"],
        capture_output=True, text=True, timeout=60, env=env)

    assert "Traceback" not in proc.stderr, proc.stderr[-800:]
    assert proc.returncode == 1, f"expected a JSON error exit, got {proc.returncode}"
    payload = json.loads(proc.stdout)                 # raises if it is not pure JSON
    assert payload["status"] == "error"
    assert "Unknown action" not in payload["message"], (
        "the action must stay ROUTABLE — a retired action that 404s gives the "
        "agent nowhere to go (this is what the L2 contract assertion checks)")
    assert "retired" in payload["message"].lower()
    assert "add-consolidation-group" in payload["suggestion"]
