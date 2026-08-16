"""Runtime GL invariant verification — the portable SUBSET of the constitution.

Connects to a sandbox (or any) ERPClaw database — SQLite or PostgreSQL — and
verifies five General Ledger claims:

1. Global balance: SUM(debit) == SUM(credit) across all non-cancelled entries
2. Per-voucher balance: each voucher balances independently
3. No zero-zero entries: no gl_entry with debit=0 AND credit=0
4. Valid accounts: every account_id references an existing account
5. Valid fiscal year: every fiscal_year stamp references an existing fiscal_year

All comparisons use Python Decimal — never float.

WHY THIS EXISTS ALONGSIDE testing/invariant_engine.py (M88). The engine is the
constitutional registry and holds far more; it is also stdlib-only, SQLite-only
(19 of its checks read the SQLite catalog directly, ADR-0034 phase 4) and lives
in the test tree. This module is the only invariant checker that runs on
PostgreSQL and the only one reachable from an installed module tree, which is
where ``os-deploy-module`` calls it (erpclaw-os-engine ``sandbox.py``). So the
split is by RUNTIME, never by claim: each of the five above maps to a registered
invariant id, and

    testing/unit/constitution/test_product_checker_claims_are_a_subset.py

fails if this module ever holds a claim the constitution does not. Claims 3 and
5 were product-lib-only until M88 promoted them as INV-28 and INV-29.

ANTI-VACUITY (M88, the mechanism M76 built for the engine). A check whose
subject population is empty returns "nothing wrong" — the same answer a
genuinely satisfied invariant returns. Measured before this control existed: on
a full L3 smoke run, 50 of 62 calls returned ``{"result": "skip"}`` that the
caller read as success, and inside the 12 live calls ``valid_fiscal_year``
reported PASS on a WHERE that matched nothing in 4 of them. Worse, two checks
returned ``{"result": "pass", "detail": "<table> does not exist — skipped"}`` —
an explicit pass on absence, with zero test coverage. So this module now
measures, per check and independently of the check body:

  * ``population`` — how many subject rows exist for that check to examine,
    from the witness declared in ``_POLICY`` below;
  * ``subject_statements`` — how many of the statements the check ACTUALLY ran
    put one of its subject tables in TABLE POSITION, read with the SAME
    statement reader the engine uses (carried below as a guarded verbatim copy).

and refuses to report ``pass`` on zero of either. Statuses are
``pass | vacuous | unexamined | fail``; only ``vacuous`` with a declared reason
is non-fatal, because books where no GL row carries a fiscal-year stamp are a
legitimate state. An ABSENT table is never a pass: absent ``account`` while
gl_entry has rows means every reference dangles, which is a failure.

WHAT THIS DOES NOT SEE, inherited from the engine's control and stated so
nobody reads more into a pass than is there: a predicate inside a check that
drifts to match nothing while still naming its subject table, and a partial
neuter that keeps the queries and drops only the verdict. Both are recorded on
row M76; this floor does not close them.

Dialect note: the connection is opened via the dialect-aware
``erpclaw_lib.db.get_connection`` so ``db_path`` may be a SQLite file path OR a
PostgreSQL URL (``ERPCLAW_DB_DIALECT=postgresql``). Table existence is asked of
``erpclaw_lib.seam``, never of a backend's own catalog (ADR-0034).
"""
import re
from decimal import Decimal, InvalidOperation


# Tolerance for balance comparisons (sub-cent)
_TOLERANCE = Decimal("0.001")

PASS = "pass"
VACUOUS = "vacuous"
UNEXAMINED = "unexamined"
FAIL = "fail"


# ── BEGIN CANONICAL SQL STATEMENT READER ─────────────────────────────────────
# THE SAME BLOCK LIVES IN testing/invariant_engine.py AND IN
# erpclaw_lib/gl_invariants.py — the L0 drift guard
# (testing/unit/L0/test_statement_reader_sync.py) compares the two byte for
# byte. Edit BOTH or the gate goes red; never edit only one.
#
# Two copies exist because neither file can import the other: the invariant
# engine is stdlib-only by design so it runs without erpclaw_lib on sys.path,
# and the product lib runs on PostgreSQL and inside an installed module tree
# where testing/ does not exist. A copy under a byte-for-byte guard is the
# house pattern for exactly that constraint (see the party-ledger predicate).

_SQL_NOISE_RE = re.compile(
    r"'(?:[^']|'')*'"          # single-quoted literal (SQL '' escape)
    r"|--[^\n]*"               # line comment
    r"|/\*.*?\*/",             # block comment
    re.S)

# Double quotes are NOT stripped: in SQLite they quote an IDENTIFIER, so
# ``FROM "gl_entry"`` is a real table position and must still be seen. A
# double-quoted string used as a VALUE cannot reach a table position, so keeping
# it costs nothing.
_TABLE_POSITION_RE = re.compile(
    r"(?:\b(?:from|join|into|update|table)\s+|\btable_info\s*\(\s*)"
    r"[\"`\[]?(?P<first>[A-Za-z_][A-Za-z0-9_$]*)[\"`\]]?"
    r"(?:\s*\.\s*[\"`\[]?(?P<second>[A-Za-z_][A-Za-z0-9_$]*)[\"`\]]?)?",
    re.I)


def _readable_sql(sql):
    """The statement with everything it merely MENTIONS removed."""
    return _SQL_NOISE_RE.sub(" ", str(sql)).lower()


def _tables_in(readable):
    """Table names in table position, over already-normalized text."""
    return {(m.group("second") or m.group("first")).lower()
            for m in _TABLE_POSITION_RE.finditer(readable)}


def tables_named(sql):
    """The set of table names this statement actually puts in table position.

    Public because the vacuity gates reuse it to assert that every population
    witness queries a table its own check declares — one reader for "what does
    this SQL examine", never two that can drift apart.
    """
    return _tables_in(_readable_sql(sql))
# ── END CANONICAL SQL STATEMENT READER ───────────────────────────────────────


class _Meter:
    """Counts, per check, what its own statements touched.

    The counter lives here rather than in the checks on purpose: a check cannot
    over-report what it examined, and a check reduced to ``return None`` reports
    zero by construction.
    """

    __slots__ = ("subjects", "statements", "subject_statements", "subjects_seen")

    def __init__(self, subject_tables):
        self.subjects = frozenset(t.lower() for t in subject_tables)
        self.statements = 0
        self.subject_statements = 0
        self.subjects_seen = set()

    def record(self, sql):
        self.statements += 1
        named = tables_named(sql) & self.subjects
        if named:
            self.subject_statements += 1
            self.subjects_seen |= named


class _MeteredCursor:
    """A cursor proxy that reports every statement to the same _Meter."""

    __slots__ = ("_cursor", "_meter")

    def __init__(self, cursor, meter):
        self._cursor = cursor
        self._meter = meter

    def execute(self, sql, *args, **kwargs):
        self._meter.record(sql)
        self._cursor.execute(sql, *args, **kwargs)
        return self

    def executemany(self, sql, *args, **kwargs):
        self._meter.record(sql)
        self._cursor.executemany(sql, *args, **kwargs)
        return self

    def __iter__(self):
        return iter(self._cursor)

    def __next__(self):
        return next(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _MeteredConnection:
    """A connection proxy that reports every statement to a _Meter.

    ``execute``, ``executemany`` and ``cursor`` are intercepted; everything else
    passes straight through, so a check cannot tell the difference. Cursors are
    metered too — otherwise a check written ``conn.cursor().execute(...)`` would
    read as ``unexamined``, a false red aimed at its author (M76 rider R5).
    """

    __slots__ = ("_conn", "_meter")

    def __init__(self, conn, meter):
        self._conn = conn
        self._meter = meter

    def execute(self, sql, *args, **kwargs):
        self._meter.record(sql)
        return _MeteredCursor(self._conn.execute(sql, *args, **kwargs),
                              self._meter)

    def executemany(self, sql, *args, **kwargs):
        self._meter.record(sql)
        return _MeteredCursor(self._conn.executemany(sql, *args, **kwargs),
                              self._meter)

    def cursor(self, *args, **kwargs):
        return _MeteredCursor(self._conn.cursor(*args, **kwargs), self._meter)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _Ctx:
    """What a check may ask about the database WITHOUT examining it.

    Existence is a catalog question and goes to the seam, never to a backend's
    own catalog relation. It is deliberately not routed through the meter: "does
    this table exist?" is exactly the question that used to hide a no-op here.
    """

    __slots__ = ("db_path", "_cache")

    def __init__(self, db_path):
        self.db_path = db_path
        self._cache = {}

    def table_exists(self, name):
        if name not in self._cache:
            from erpclaw_lib import seam
            self._cache[name] = seam.table_exists(name, self.db_path)
        return self._cache[name]


class _Policy:
    """What a check examines, so a pass can be told from a no-op.

    subject_tables    the tables this check exists to examine.
    required_subjects the subset its CLAIM is about; all must be queried.
                      Defaults to every subject, because each of the five checks
                      here queries all of its own subjects whenever its
                      population is non-empty.
    population        (table, SQL) clauses counting the rows available to
                      examine, summed. A clause whose table is absent
                      contributes 0, so a missing dependency reads as vacuous
                      or fails — never as a pass.
    vacuous_ok        why an empty population is a legitimate state of real
                      books. None means the opposite: this module only runs at
                      all when gl_entry has rows, so zero there is drift.
    """

    __slots__ = ("subject_tables", "required_subjects", "population",
                 "vacuous_ok")

    def __init__(self, subject_tables, population, vacuous_ok=None,
                 required_subjects=None):
        self.subject_tables = tuple(subject_tables)
        self.population = tuple(population)
        self.vacuous_ok = vacuous_ok
        self.required_subjects = tuple(
            t.lower() for t in
            (required_subjects if required_subjects is not None
             else self.subject_tables))

    def witness(self):
        return " + ".join(sql for _table, sql in self.population)


_GL_ACTIVE = ("gl_entry", "SELECT COUNT(*) FROM gl_entry WHERE is_cancelled = 0")
_GL_STAMPED = ("gl_entry",
               "SELECT COUNT(*) FROM gl_entry "
               "WHERE is_cancelled = 0 AND fiscal_year IS NOT NULL")

# Each entry maps 1:1 onto a registered invariant id; the mapping itself is
# asserted by testing/unit/constitution/test_product_checker_claims_are_a_subset.py.
ENGINE_CLAIM_IDS = {
    "global_balance": "INV-01",
    "per_voucher_balance": "INV-02",
    "no_zero_zero_entries": "INV-28",
    "valid_accounts": "INV-06",
    "valid_fiscal_year": "INV-29",
}


def check_gl_invariants(db_path: str) -> dict:
    """Run GL invariant checks against a database.

    Args:
        db_path: SQLite database file path, or a PostgreSQL URL when
                 ``ERPCLAW_DB_DIALECT=postgresql``.

    Returns:
        {
            "result": "pass" | "fail" | "skip",
            "reason": str (only if result == "skip"),
            "checks": [
                {"name": str,
                 "result": "pass" | "vacuous" | "unexamined" | "fail",
                 "detail": str,
                 "population": int,
                 "statements": int,
                 "subject_statements": int},
                ...
            ],
            "violations": [str, ...],
            "verified": int,          # checks that examined a real population
            "vacuous": [str, ...],    # examined nothing, and said why
            "unexamined": [str, ...], # had rows and did not look — a failure
        }

    ``result`` is "skip" ONLY when there is no ledger at all. A skip means
    nothing was verified and callers must treat it as such — it is not a pass.
    """
    from erpclaw_lib.db import get_connection
    ctx = _Ctx(db_path)

    if not ctx.table_exists("gl_entry"):
        return _skipped("gl_entry table does not exist")

    conn = get_connection(db_path)
    try:
        entry_count = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()[0]
        if entry_count == 0:
            return _skipped("no GL entries")

        checks, violations = [], []
        for name, check_fn in _CHECKS:
            outcome, check_violations = _evaluate_one(conn, ctx, name, check_fn)
            checks.append(outcome)
            violations.extend(check_violations)

        failing = [c for c in checks if _is_failure(c)]
        return {
            "result": FAIL if failing else PASS,
            "checks": checks,
            "violations": violations,
            "verified": sum(1 for c in checks if c["result"] == PASS),
            "vacuous": [c["name"] for c in checks if c["result"] == VACUOUS],
            "unexamined": [c["name"] for c in checks
                           if c["result"] == UNEXAMINED],
        }
    finally:
        conn.close()


def _skipped(reason):
    """No ledger to judge. Verified nothing, and says so in every field."""
    return {
        "result": "skip",
        "reason": reason,
        "checks": [],
        "violations": [],
        "verified": 0,
        "vacuous": [],
        "unexamined": [],
    }


def _is_failure(check):
    """A vacuous check fails unless its emptiness was declared legitimate."""
    if check["result"] in (FAIL, UNEXAMINED):
        return True
    return check["result"] == VACUOUS and not check.get("vacuous_ok")


def _population(conn, ctx, policy):
    """Rows available for this check to examine, summed over its witness.

    A clause whose table does not exist contributes 0 — a missing dependency
    makes the check vacuous or failing, never passing. Existence is asked of the
    seam BEFORE the statement runs, rather than by catching the error the
    statement would raise: on PostgreSQL a failed statement aborts the
    surrounding transaction and every later probe would fail with it.
    """
    total = 0
    for table, sql in policy.population:
        if not ctx.table_exists(table):
            continue
        row = conn.execute(sql).fetchone()
        if row is None:
            continue
        try:
            total += int(row[0] or 0)
        except (TypeError, ValueError):
            continue
    return total


def _evaluate_one(conn, ctx, name, check_fn):
    """Run one check under metering and classify the result."""
    policy = _POLICY[name]
    meter = _Meter(policy.subject_tables)
    outcome = check_fn(_MeteredConnection(conn, meter), ctx)
    detail, violations = outcome if outcome is not None else (None, [])

    population = _population(conn, ctx, policy)
    missing = tuple(t for t in policy.required_subjects
                    if t not in meter.subjects_seen)
    common = {
        "name": name,
        "population": population,
        "statements": meter.statements,
        "subject_statements": meter.subject_statements,
        "vacuous_ok": policy.vacuous_ok,
    }

    # A real violation is classified BEFORE vacuity: a check that found a
    # divergence found one, whatever its population witness says.
    if detail is not None:
        return dict(common, result=FAIL, detail=detail), list(violations)

    if population == 0:
        message = (f"vacuous: 0 rows examined — the subject population "
                   f"({policy.witness()}) is empty, so this check asserted "
                   f"nothing")
        if policy.vacuous_ok:
            message += f" [expected here: {policy.vacuous_ok}]"
        else:
            message += ("; this module only runs when gl_entry has rows, so an "
                        "empty population here is drift, not a quiet books "
                        "state")
        return dict(common, result=VACUOUS, detail=message), []

    if meter.subject_statements == 0 or missing:
        looked_at = ", ".join(sorted(meter.subjects_seen)) or "nothing"
        message = (f"examined nothing in "
                   f"{', '.join(missing) or '/'.join(policy.subject_tables)}: "
                   f"{population} row(s) were available and the check ran "
                   f"{meter.statements} statement(s), querying {looked_at}")
        return dict(common, result=UNEXAMINED, detail=message), []

    return dict(common, result=PASS, detail=_PASS_DETAIL[name]), []


# ── The checks ───────────────────────────────────────────────────────────────
#
# Each returns None when satisfied, or ``(detail, [violation, ...])``. The
# framework above decides pass / vacuous / unexamined — a check body cannot
# award itself a pass.

def _check_global_balance(conn, ctx):
    """Check 1: SUM(debit) == SUM(credit) globally for non-cancelled entries."""
    rows = conn.execute(
        "SELECT debit, credit FROM gl_entry WHERE is_cancelled = 0"
    ).fetchall()

    total_debit = Decimal("0")
    total_credit = Decimal("0")
    for row in rows:
        total_debit += Decimal(str(row["debit"]))
        total_credit += Decimal(str(row["credit"]))

    diff = abs(total_debit - total_credit)
    if diff <= _TOLERANCE:
        return None
    detail = (f"IMBALANCE: total debit={total_debit}, "
              f"credit={total_credit}, diff={diff}")
    return detail, [f"Global GL imbalance: {detail}"]


def _check_per_voucher_balance(conn, ctx):
    """Check 2: Each voucher (type+id) must balance independently."""
    rows = conn.execute(
        "SELECT voucher_type, voucher_id, debit, credit "
        "FROM gl_entry WHERE is_cancelled = 0"
    ).fetchall()

    voucher_totals = {}
    for row in rows:
        key = (row["voucher_type"], row["voucher_id"])
        if key not in voucher_totals:
            voucher_totals[key] = {"debit": Decimal("0"), "credit": Decimal("0")}
        voucher_totals[key]["debit"] += Decimal(str(row["debit"]))
        voucher_totals[key]["credit"] += Decimal(str(row["credit"]))

    violations = []
    for (vtype, vid), totals in voucher_totals.items():
        diff = abs(totals["debit"] - totals["credit"])
        if diff > _TOLERANCE:
            violations.append(
                f"Voucher {vtype}:{vid} imbalanced: "
                f"debit={totals['debit']}, credit={totals['credit']}, "
                f"diff={diff}"
            )

    if not violations:
        return None
    return f"{len(violations)} imbalanced voucher(s)", violations


def _check_no_zero_zero(conn, ctx):
    """Check 3: No gl_entry with both debit=0 AND credit=0 (non-cancelled).

    Compared as Decimal, never as SQL text or REAL — '0.00', '0' and '0.000'
    are all zero and only a Decimal comparison says so.
    """
    rows = conn.execute(
        "SELECT id, voucher_type, voucher_id, debit, credit "
        "FROM gl_entry WHERE is_cancelled = 0"
    ).fetchall()

    violations = []
    for row in rows:
        try:
            d = Decimal(str(row["debit"]))
            c = Decimal(str(row["credit"]))
        except InvalidOperation:
            continue          # unparseable money is not this check's claim
        if d == 0 and c == 0:
            violations.append(
                f"Zero-zero GL entry: id={row['id']}, "
                f"voucher={row['voucher_type']}:{row['voucher_id']}"
            )

    if not violations:
        return None
    return f"{len(violations)} zero-zero GL entry(ies) found", violations


def _check_valid_accounts(conn, ctx):
    """Check 4: Every account_id in gl_entry references a valid account.

    An ABSENT ``account`` table is a FAILURE, not a skip: this module only runs
    when gl_entry has rows, so with no account table every one of those rows
    references something that does not exist. Returning "pass — table does not
    exist" is the defect M88 removed.
    """
    if not ctx.table_exists("account"):
        count = conn.execute(
            "SELECT COUNT(*) FROM gl_entry WHERE is_cancelled = 0"
        ).fetchone()[0]
        detail = (f"account table does not exist, so all {count} live GL "
                  f"entry(ies) reference a non-existent account")
        return detail, [f"Invalid account_id: {detail}"]

    orphans = conn.execute(
        "SELECT ge.id, ge.account_id, ge.voucher_type, ge.voucher_id "
        "FROM gl_entry ge "
        "LEFT JOIN account a ON ge.account_id = a.id "
        "WHERE a.id IS NULL AND ge.is_cancelled = 0"
    ).fetchall()

    if not orphans:
        return None
    return (
        f"{len(orphans)} GL entry(ies) with invalid account_id",
        [f"Invalid account_id: gl_entry.id={o['id']}, "
         f"account_id={o['account_id']}, "
         f"voucher={o['voucher_type']}:{o['voucher_id']}" for o in orphans],
    )


def _check_valid_fiscal_year(conn, ctx):
    """Check 5: Every fiscal_year stamp in gl_entry references a fiscal year.

    ``gl_entry.fiscal_year`` carries the fiscal year's NAME, and the stamp is
    OPTIONAL (``gl_posting`` writes ``entry.get("fiscal_year")``), so books
    where nothing is stamped are a legitimate state — the framework reports
    that as a declared vacuity rather than as a pass. An absent ``fiscal_year``
    table while rows DO claim one is a failure: every one of those stamps
    dangles. That branch used to return "pass — table does not exist" (M88).
    """
    stamped = conn.execute(
        "SELECT COUNT(*) FROM gl_entry "
        "WHERE is_cancelled = 0 AND fiscal_year IS NOT NULL"
    ).fetchone()[0]

    if not ctx.table_exists("fiscal_year"):
        if not stamped:
            return None       # nothing claims a fiscal year; nothing can dangle
        detail = (f"fiscal_year table does not exist, so all {stamped} "
                  f"fiscal-year stamp(s) on live GL entries dangle")
        return detail, [f"Invalid fiscal_year: {detail}"]

    orphans = conn.execute(
        "SELECT ge.id, ge.fiscal_year, ge.voucher_type, ge.voucher_id "
        "FROM gl_entry ge "
        "WHERE ge.fiscal_year IS NOT NULL "
        "AND ge.fiscal_year NOT IN (SELECT name FROM fiscal_year) "
        "AND ge.is_cancelled = 0"
    ).fetchall()

    if not orphans:
        return None
    return (
        f"{len(orphans)} GL entry(ies) with invalid fiscal_year",
        [f"Invalid fiscal_year: gl_entry.id={o['id']}, "
         f"fiscal_year={o['fiscal_year']}, "
         f"voucher={o['voucher_type']}:{o['voucher_id']}" for o in orphans],
    )


_CHECKS = (
    ("global_balance", _check_global_balance),
    ("per_voucher_balance", _check_per_voucher_balance),
    ("no_zero_zero_entries", _check_no_zero_zero),
    ("valid_accounts", _check_valid_accounts),
    ("valid_fiscal_year", _check_valid_fiscal_year),
)

_POLICY = {
    "global_balance": _Policy(("gl_entry",), (_GL_ACTIVE,)),
    "per_voucher_balance": _Policy(("gl_entry",), (_GL_ACTIVE,)),
    "no_zero_zero_entries": _Policy(("gl_entry",), (_GL_ACTIVE,)),
    # `account` is NOT a required subject: when the table is absent the check
    # reports a real failure from gl_entry alone, and requiring a query it
    # cannot make would turn that failure into a false UNEXAMINED.
    "valid_accounts": _Policy(("gl_entry", "account"), (_GL_ACTIVE,),
                              required_subjects=("gl_entry",)),
    # The witness is the STAMPED subset, not all GL: the stamp is optional, so
    # unstamped books must read as a declared vacuity rather than a pass. Same
    # reasoning, same words, as the engine's INV-29 policy row.
    "valid_fiscal_year": _Policy(
        ("gl_entry", "fiscal_year"), (_GL_STAMPED,),
        "no GL row carries a fiscal-year stamp, so no stamp can dangle",
        required_subjects=("gl_entry",)),
}

_PASS_DETAIL = {
    "global_balance": "Total debit equals total credit",
    "per_voucher_balance": "All vouchers balanced",
    "no_zero_zero_entries": "No zero-debit zero-credit entries found",
    "valid_accounts": "All GL entries reference valid accounts",
    "valid_fiscal_year": "All GL entries reference valid fiscal years",
}
