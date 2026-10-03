"""Common database query helpers for ERPClaw skills.

Centralises frequently duplicated lookups (fiscal year, cost center, company
auto-detection) so that every skill uses the same logic and query shape.
"""
import json
import sys

# Dialect-neutral query builder (the vendored, PG-safe layer the rest of the
# codebase uses; raw `pypika` is NOT importable here). fn.Lower(field) == P()
# is the proven cross-DB case-insensitive pattern (see resolve_item, FINDING-008
# / FINDING-005/006). Never .ilike()/COLLATE NOCASE — those break Postgres.
from erpclaw_lib.query import Q as _Q, Table as _T, fn as _fn, P as _P, Case as _Case, Order as _Order


def _refuse(payload: dict) -> None:
    """Print a standard error envelope on stdout and exit 1.

    Every refusal carries ``status``, ``error`` and ``message`` (``message``
    repeats ``error``); every other key of ``payload`` passes through
    unchanged. Prints with ``print(json.dumps(...))`` and exits with
    ``sys.exit(1)``, the historical call shape, so harnesses patching
    ``sys.exit`` see no difference.
    """
    data = {"status": "error"}
    data.update(payload)
    data["message"] = data["error"]
    print(json.dumps(data))
    sys.exit(1)


def resolve_company_id(conn, company_id: str | None = None,
                       company_name: str | None = None) -> str:
    """Resolve a company to its UUID.

    Resolution priority (strict):
      1. ``company_id`` truthy  -> returned as-is (UUID path; unchanged).
      2. ``company_name`` truthy -> exact case-insensitive name lookup.
         Exactly one row: return its id.
         Zero rows: HARD ERROR listing available company names. NEVER falls
         back to auto-detect — a named-but-missing company must fail loudly so
         we never post one company's books to another (wrong-entity failure).
      3. Neither: sole-company auto-detect (unchanged) — single company is
         returned automatically; multiple companies emit a helpful error.

    At ACTIVE the neither-given branch answers from the acting principal's
    scope instead of the install: one in-scope company returns automatically,
    several in scope refuse as ambiguous listing those companies, and none
    in scope refuses.

    Every refusal carries ``status``, ``error`` and ``message``.

    Args:
        conn: SQLite/Postgres connection with dict-like row access.
        company_id: Explicit UUID (may be None/empty).
        company_name: Human company name from NL (may be None/empty).

    Returns:
        A valid company UUID string.

    Raises:
        SystemExit: via JSON error on stdout for every non-resolving case.
    """
    # 1. Explicit UUID wins.
    if company_id:
        return company_id

    # 2. Name branch — exact case-insensitive, dialect-neutral (fn.Lower),
    #    parameterized. Hard error on miss; NEVER auto-detect from here.
    if company_name:
        term = company_name.strip()
        c = _T("company")
        q = (_Q.from_(c)
               .select(c.id)
               .where(_fn.Lower(c.name) == _P()))  # _P() -> dialect placeholder
        rows = conn.execute(q.get_sql(), [term.lower()]).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        # 0 rows (UNIQUE name means >1 is impossible) -> loud, actionable.
        avail_q = _Q.from_(c).select(c.name).orderby(c.name).limit(25)
        avail = conn.execute(avail_q.get_sql()).fetchall()
        names = [r["name"] for r in avail]
        _refuse({
            "error": f"Company '{term}' not found.",
            "available_companies": names,
            "suggestion": ("Use one of the available company names exactly, "
                           "or run 'list-companies' to see them.")})

    # 3. Neither given -> sole-company auto-detect (unchanged behavior
    # at STAGED); at ACTIVE the acting principal's scope replaces the
    # install-wide rule.
    from erpclaw_lib import actor as _actor
    from erpclaw_lib import company_scope as _company_scope
    try:
        scope = _company_scope.resolution_scope(conn, _actor.current())
    except _company_scope.ScopeRefused as exc:
        _refuse({"error": exc.code})
    if scope is None:
        c = _T("company")
        scope_q = _Q.from_(c).select(c.id, c.name).orderby(c.name).limit(10)
        rows = conn.execute(scope_q.get_sql()).fetchall()
        if not rows:
            _refuse({"error": "No company found. Create one first.",
                     "suggestion": "Run 'tutorial' to create a demo company, or 'setup company' to create your own."})
        if len(rows) == 1:
            return rows[0]["id"]
        companies = [{"id": r["id"], "name": r["name"]} for r in rows]
        _refuse({"error": "Multiple companies found. Please specify the company by name.",
                 "companies": companies,
                 "suggestion": "Pass the company name (e.g. --company \"Acme\"), or use --company-id with one of the IDs above."})
    ids = sorted(scope)
    c = _T("company")
    scope_q = (_Q.from_(c).select(c.id, c.name)
               .where(c.id.isin([_P() for _ in ids]))
               .orderby(c.name).limit(10))
    rows = conn.execute(scope_q.get_sql(), ids).fetchall()
    if not rows:
        _refuse({"error": _company_scope.REFUSAL_CODE})
    if len(rows) == 1:
        return rows[0]["id"]
    companies = [{"id": r["id"], "name": r["name"]} for r in rows]
    _refuse({"error": _company_scope.SCOPE_AMBIGUOUS,
             "companies": companies,
             "suggestion": "Pass the company name (e.g. --company \"Acme\"), or use --company-id with one of the IDs above."})


def resolve_scope_company(conn, company_id: str | None = None,
                         company_name: str | None = None) -> str:
    """Resolve the company scope of a list or report.

    A list or report that takes an optional company uses the only company
    when exactly one exists and refuses when there are none or more than
    one. An explicit company id that does not exist is refused, never
    answered with an empty page. A given ``company_name`` is ignored when
    ``company_id`` is given. This helper is for lists and reports; write
    paths keep :func:`resolve_company_id`.

    Args:
        conn: SQLite/Postgres connection with dict-like row access.
        company_id: Explicit UUID (may be None/empty); when truthy it is
            checked against the company table.
        company_name: Human company name from NL (may be None/empty).

    Returns:
        A valid company UUID string.

    Raises:
        SystemExit: via JSON error on stdout for every non-resolving case.
    """
    if company_id:
        c = _T("company")
        q = _Q.from_(c).select(c.id).where(c.id == _P())
        rows = conn.execute(q.get_sql(), [company_id]).fetchall()
        if rows:
            return company_id
        _refuse({"error": "Company not found: %s" % company_id})
    return resolve_company_id(conn, None, company_name)


def resolve_account_by_name(conn, company_id: str,
                            account_id: str | None = None,
                            account_name: str | None = None,
                            account_type: str = "bank") -> str:
    """Resolve a (bank) account to its UUID within a company.

    The account-side analogue of :func:`resolve_company_id` (ADR-0015). NL
    business users name their bank account ("Checking Account"), never its UUID.
    Resolution priority (strict):

      1. ``account_id`` truthy  -> returned as-is (UUID path; unchanged).
      2. ``account_name`` truthy -> exact case-insensitive name lookup, scoped
         to ``company_id`` and ``account_type`` (default ``bank``). Exactly one
         row: return its id. Zero rows: HARD ERROR listing the company's accounts
         of that type. NEVER falls back to a different account — importing a
         statement into the wrong ledger account is a wrong-entity failure, the
         same class ADR-0015 guards for companies.
      3. Neither given -> HARD ERROR. We do NOT auto-pick even a sole bank
         account: which account a statement belongs to must be grounded
         explicitly by the caller, otherwise the NL agent guesses and the
         request lands in the FINDING-002 grounding-nondeterminism family.

    Args:
        conn: SQLite/Postgres connection with dict-like row access.
        company_id: UUID of the resolved company (required — scopes the search).
        account_id: Explicit UUID (may be None/empty).
        account_name: Human account name from NL (may be None/empty).
        account_type: account_type to constrain to (default ``bank``).

    Returns:
        A valid account UUID string.

    Raises:
        SystemExit: via JSON error on stdout for every non-resolving case.
    """
    # 1. Explicit UUID wins.
    if account_id:
        return account_id

    def _available():
        rows = conn.execute(
            "SELECT name FROM account WHERE company_id = ? AND account_type = ? "
            "AND disabled = 0 ORDER BY name LIMIT 25",
            (company_id, account_type)).fetchall()
        return [r["name"] for r in rows]

    # 2. Name branch — exact case-insensitive, dialect-neutral (fn.Lower),
    #    parameterized, scoped to company + account_type. Hard error on miss.
    if account_name:
        term = account_name.strip()
        a = _T("account")
        q = (_Q.from_(a)
               .select(a.id)
               .where((_fn.Lower(a.name) == _P())
                      & (a.company_id == _P())
                      & (a.account_type == _P())
                      & (a.disabled == _P())))
        rows = conn.execute(q.get_sql(),
                            [term.lower(), company_id, account_type, 0]).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        # 0 rows (or, defensively, >1 duplicate-named) -> loud, actionable.
        print(json.dumps({
            "error": f"{account_type.capitalize()} account '{term}' not found "
                     f"for this company.",
            "available_accounts": _available(),
            "suggestion": (f"Use one of the available {account_type} account "
                           "names exactly, or pass --bank-account-id with its ID.")}))
        sys.exit(1)

    # 3. Neither given -> hard error (no auto-pick; see docstring rationale).
    print(json.dumps({
        "error": "No bank account specified.",
        "available_accounts": _available(),
        "suggestion": ("Pass --bank-account-name (e.g. --bank-account-name "
                       "\"Checking Account\") or --bank-account-id. The statement's "
                       "ledger account must be named explicitly.")}))
    sys.exit(1)


def get_fiscal_year(conn, posting_date: str, company_id: str) -> str | None:
    """Return the fiscal year name for a posting date, or None.

    Company scoping is mandatory: only the posting company's open years
    count. The legacy cross-company lookup is gone. A None or empty
    ``company_id`` raises ``ValueError``.

    Args:
        conn: SQLite connection with row_factory = sqlite3.Row.
        posting_date: ISO 8601 date string (YYYY-MM-DD).
        company_id: UUID of the posting company (required).

    Returns:
        The fiscal year ``name`` string, or ``None`` if no matching open
        fiscal year exists.

    Raises:
        ValueError: If ``company_id`` is None or empty.
    """
    if not company_id:
        raise ValueError("get_fiscal_year needs the posting company")
    fy = conn.execute(
        "SELECT name FROM fiscal_year WHERE company_id = ? AND start_date <= ? AND end_date >= ? AND is_closed = 0",
        (company_id, posting_date, posting_date),
    ).fetchone()
    return fy["name"] if fy else None


def get_default_cost_center(conn, company_id: str) -> str | None:
    """Return the company's default cost center ID, or None.

    The company's configured default cost center is used when it names a
    non-group cost center of that company; otherwise the company's non-group
    cost centers are ordered with rows missing a creation timestamp last,
    then by creation timestamp, name and id, and the first row is used.

    Args:
        conn: SQLite connection with row_factory = sqlite3.Row.
        company_id: UUID of the company.

    Returns:
        The cost center ``id`` string, or ``None`` when the company has no
        non-group cost center.
    """
    co = _T("company")
    q = _Q.from_(co).select(co.default_cost_center_id).where(co.id == _P())
    row = conn.execute(q.get_sql(), (company_id,)).fetchone()
    default_id = row["default_cost_center_id"] if row else None
    if default_id:
        cc = _T("cost_center")
        q = (
            _Q.from_(cc)
            .select(cc.id)
            .where(cc.id == _P())
            .where(cc.company_id == _P())
            .where(cc.is_group == 0)
        )
        hit = conn.execute(q.get_sql(), (default_id, company_id)).fetchone()
        if hit:
            return hit["id"]
    cc = _T("cost_center")
    null_last = _Case().when(cc.created_at.isnull(), 1).else_(0)
    q = (
        _Q.from_(cc)
        .select(cc.id)
        .where(cc.company_id == _P())
        .where(cc.is_group == 0)
        .orderby(null_last)
        .orderby(cc.created_at)
        .orderby(cc.name)
        .orderby(cc.id)
        .limit(1)
    )
    hit = conn.execute(q.get_sql(), (company_id,)).fetchone()
    return hit["id"] if hit else None
