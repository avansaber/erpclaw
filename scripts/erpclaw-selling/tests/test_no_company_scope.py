"""Company scope for selling lists and status (m786r-nocompany-p3-selling-on-main).

A list or report that takes an optional company answers for exactly one
company through resolve_scope_company: no company with zero companies
refuses, no company with one company uses it, no company with several
companies refuses, an unknown --company-id refuses, and --company resolves
an exact case-insensitive name (a miss refuses). The refusal happens before
the action reads its own tables, so every refusal also pins the database
unchanged. Successful results gain no new top-level key.
"""
import json
import os
import subprocess
import sys

import pytest

from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_customer, seed_item, init_all_tables, get_conn,
)
from erpclaw_lib.query import Q, P, Table

mod = load_db_query()

ACME_NAME = "Acme Widgets"
ACME_ABBR = "ACME"
WAYNE_NAME = "Wayne Enterprises"
WAYNE_ABBR = "WAYNE"

ZERO_COMPANY_ERROR = "No company found. Create one first."
ZERO_COMPANY_SUGGESTION = "Run 'tutorial' to create a demo company, or 'setup company' to create your own."
MULTI_COMPANY_ERROR = "Multiple companies found. Please specify the company by name."
MULTI_COMPANY_SUGGESTION = "Pass the company name (e.g. --company \"Acme\"), or use --company-id with one of the IDs above."
NAME_MISS_SUGGESTION = "Use one of the available company names exactly, or run 'list-companies' to see them."

_STATE_TABLES = ("customer", "quotation", "sales_order", "delivery_note",
                 "sales_invoice", "dunning_run", "recurring_invoice_template",
                 "blanket_order", "packing_slip", "company", "audit_log")

ALL_ACTIONS = ["list-customers", "list-quotations", "list-sales-orders",
               "list-delivery-notes", "list-sales-invoices",
               "list-dunning-runs", "list-credit-notes",
               "list-recurring-templates", "list-blanket-orders",
               "list-packing-slips", "status"]

_ACTION_FNS = {
    "list-customers": mod.list_customers,
    "list-quotations": mod.list_quotations,
    "list-sales-orders": mod.list_sales_orders,
    "list-delivery-notes": mod.list_delivery_notes,
    "list-sales-invoices": mod.list_sales_invoices,
    "list-dunning-runs": mod.list_dunning_runs,
    "list-credit-notes": mod.list_credit_notes,
    "list-recurring-templates": mod.list_recurring_templates,
    "list-blanket-orders": mod.list_blanket_orders,
    "list-packing-slips": mod.list_packing_slips,
    "status": mod.status_action,
}


def _seed_exact_companies(conn):
    """Two companies with EXACT names via seed_company.

    seed_company() appends a uuid suffix to name and abbr
    ("Acme Widgets a1b2c3"), which would break exact-name resolution, so
    normalize both rows back to their exact values here.
    """
    acme = seed_company(conn, ACME_NAME, ACME_ABBR)
    wayne = seed_company(conn, WAYNE_NAME, WAYNE_ABBR)
    t = Table("company")
    for cid, name, abbr in ((acme, ACME_NAME, ACME_ABBR),
                            (wayne, WAYNE_NAME, WAYNE_ABBR)):
        uq = (Q.update(t).set("name", P()).set("abbr", P())
              .where(t.id == P()))
        conn.execute(uq.get_sql(), (name, abbr, cid))
    conn.commit()
    return acme, wayne


def _seed_acme_only(conn):
    """One company with its exact name via seed_company (see above)."""
    acme = seed_company(conn, ACME_NAME, ACME_ABBR)
    t = Table("company")
    uq = (Q.update(t).set("name", P()).set("abbr", P())
          .where(t.id == P()))
    conn.execute(uq.get_sql(), (ACME_NAME, ACME_ABBR, acme))
    conn.commit()
    return acme


def _seed_company_books(conn, company_id, customer_name, rate):
    """One customer plus one draft quotation, sales order and standalone
    sales invoice of 1 x <rate>, through the module's own add actions."""
    item = seed_item(conn, "Widget for %s" % customer_name)
    customer = seed_customer(conn, company_id, customer_name)
    items = json.dumps([{"item_id": item, "qty": "1", "rate": rate}])
    created_q = call_action(mod.add_quotation, conn, ns(
        customer_id=customer, company_id=company_id,
        posting_date="2026-06-15", items=items,
        valid_till=None, tax_template_id=None))
    assert is_ok(created_q), created_q
    created_so = call_action(mod.add_sales_order, conn, ns(
        customer_id=customer, company_id=company_id,
        posting_date="2026-06-15", items=items,
        delivery_date=None, tax_template_id=None))
    assert is_ok(created_so), created_so
    created_si = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=customer, company_id=company_id,
        posting_date="2026-06-15", due_date=None,
        items=items, tax_template_id=None, payment_terms_id=None))
    assert is_ok(created_si), created_si
    return customer


def _seed_both_books(conn, acme, wayne):
    _seed_company_books(conn, acme, "Bruce Wayne", "100.00")
    _seed_company_books(conn, wayne, "Alfred Pennyworth", "150.00")


def _state(conn):
    """Every scoped row plus company and audit rows, PyPika-read and sorted."""
    out = {}
    for name in _STATE_TABLES:
        t = Table(name)
        rows = conn.execute(Q.from_(t).select(t.star).get_sql()).fetchall()
        out[name] = sorted(
            json.dumps(dict(r), sort_keys=True, default=str) for r in rows)
    return out


def _list_ns(action, company_id=None, company_name=None):
    base = {"company_id": company_id, "company_name": company_name}
    if action == "list-customers":
        return ns(customer_group=None, search=None,
                  limit=None, offset=None, **base)
    if action in ("list-quotations", "list-sales-orders"):
        return ns(customer_id=None, doc_status=None, from_date=None,
                  to_date=None, search=None, limit=None, offset=None, **base)
    if action == "list-delivery-notes":
        return ns(customer_id=None, sales_order_id=None, doc_status=None,
                  from_date=None, to_date=None, search=None,
                  limit=None, offset=None, **base)
    if action == "list-sales-invoices":
        return ns(customer_id=None, sales_order_id=None, doc_status=None,
                  from_date=None, to_date=None, search=None,
                  limit=None, offset=None, **base)
    if action == "list-dunning-runs":
        return ns(customer_id=None, limit=None, **base)
    if action == "list-credit-notes":
        return ns(customer_id=None, doc_status=None, from_date=None,
                  to_date=None, limit=None, offset=None, **base)
    if action == "list-recurring-templates":
        return ns(customer_id=None, template_status=None,
                  limit=None, offset=None, **base)
    if action == "list-blanket-orders":
        return ns(customer_id=None, doc_status=None,
                  limit="20", offset="0", **base)
    if action == "list-packing-slips":
        return ns(delivery_note_id=None, limit="20", offset="0", **base)
    if action == "status":
        return ns(**base)
    raise AssertionError(action)


def _zero_dict():
    return {"status": "error", "error": ZERO_COMPANY_ERROR,
            "message": ZERO_COMPANY_ERROR,
            "suggestion": ZERO_COMPANY_SUGGESTION}


def _multi_dict(acme, wayne):
    return {"status": "error", "error": MULTI_COMPANY_ERROR,
            "message": MULTI_COMPANY_ERROR,
            "companies": [{"id": acme, "name": ACME_NAME},
                          {"id": wayne, "name": WAYNE_NAME}],
            "suggestion": MULTI_COMPANY_SUGGESTION}


# ---------------------------------------------------------------------------
# 1. Zero companies refuse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_zero_companies_refuses(conn, action):
    """No company in the install: every list and status refuses with the
    exact zero-company dict and writes nothing."""
    before = _state(conn)
    result = call_action(_ACTION_FNS[action], conn, _list_ns(action))
    assert result == _zero_dict()
    assert _state(conn) == before


# ---------------------------------------------------------------------------
# 2. Two companies, no company: refuse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_two_companies_no_company_refuses(conn, action):
    """Two companies and no company given: every list and status refuses
    with the exact multiple-company dict and writes nothing."""
    acme, wayne = _seed_exact_companies(conn)
    _seed_both_books(conn, acme, wayne)
    before = _state(conn)
    result = call_action(_ACTION_FNS[action], conn, _list_ns(action))
    assert result == _multi_dict(acme, wayne)
    assert _state(conn) == before


# ---------------------------------------------------------------------------
# 3. Unknown company id refuses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_unknown_company_refuses(conn, action):
    """An explicit company id that does not exist refuses (never an empty
    page) and writes nothing."""
    acme, wayne = _seed_exact_companies(conn)
    _seed_both_books(conn, acme, wayne)
    before = _state(conn)
    result = call_action(_ACTION_FNS[action], conn,
                         _list_ns(action, company_id="no-such-company"))
    assert result == {"status": "error",
                      "error": "Company not found: no-such-company",
                      "message": "Company not found: no-such-company"}
    assert _state(conn) == before


# ---------------------------------------------------------------------------
# 4. Explicit second company scopes
# ---------------------------------------------------------------------------

def test_explicit_second_company_scopes(conn):
    """list-customers for Wayne returns exactly Alfred Pennyworth; the three
    seeded document lists each return one row at 150.00 for Wayne."""
    acme, wayne = _seed_exact_companies(conn)
    _seed_both_books(conn, acme, wayne)
    customers = call_action(mod.list_customers, conn,
                            _list_ns("list-customers", company_id=wayne))
    assert is_ok(customers), customers
    assert customers["total_count"] == 1
    assert [c["name"] for c in customers["customers"]] == ["Alfred Pennyworth"]
    for action, key in (("list-quotations", "quotations"),
                        ("list-sales-orders", "sales_orders"),
                        ("list-sales-invoices", "sales_invoices")):
        result = call_action(_ACTION_FNS[action], conn,
                             _list_ns(action, company_id=wayne))
        assert is_ok(result), result
        assert result["total_count"] == 1, result
        row = result[key][0]
        assert row["grand_total"] == "150.00", row
        assert row["company_id"] == wayne, row


# ---------------------------------------------------------------------------
# 5. One company: no-company equals explicit (guard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_one_company_uses_it(conn, action):
    """Guard: with a single company, omitting the company answers exactly
    as passing its id. This pins that single-company installs see no
    behaviour change."""
    acme = _seed_acme_only(conn)
    _seed_company_books(conn, acme, "Bruce Wayne", "100.00")
    implicit = call_action(_ACTION_FNS[action], conn, _list_ns(action))
    explicit = call_action(_ACTION_FNS[action], conn,
                           _list_ns(action, company_id=acme))
    assert is_ok(implicit), implicit
    assert implicit == explicit


# ---------------------------------------------------------------------------
# 6. Status never answers for an arbitrary company
# ---------------------------------------------------------------------------

def test_status_never_picks_a_company(conn):
    """Two companies and no company given: status refuses with the
    multiple-company dict instead of answering for an arbitrary one."""
    acme, wayne = _seed_exact_companies(conn)
    _seed_both_books(conn, acme, wayne)
    before = _state(conn)
    result = call_action(mod.status_action, conn, _list_ns("status"))
    assert result == _multi_dict(acme, wayne)
    assert _state(conn) == before


# ---------------------------------------------------------------------------
# 7. --company flag resolution
# ---------------------------------------------------------------------------

def test_company_flag_resolves_name(conn):
    """_resolve_company_flag maps an exact case-insensitive name to its id,
    passes an id straight through, and refuses a miss before writing."""
    acme, wayne = _seed_exact_companies(conn)
    _seed_both_books(conn, acme, wayne)
    args = ns(company_id=None, company_name="acme widgets")
    mod._resolve_company_flag(conn, args)
    assert args.company_id == acme
    args_id = ns(company_id=None, company_name=acme)
    mod._resolve_company_flag(conn, args_id)
    assert args_id.company_id == acme
    before = _state(conn)
    result = call_action(mod._resolve_company_flag, conn,
                         ns(company_id=None, company_name="Acme"))
    assert result == {"status": "error",
                      "error": "Company 'Acme' not found.",
                      "message": "Company 'Acme' not found.",
                      "available_companies": [ACME_NAME, WAYNE_NAME],
                      "suggestion": NAME_MISS_SUGGESTION}
    assert _state(conn) == before


# ---------------------------------------------------------------------------
# 8. CLI: --company through the real router
# ---------------------------------------------------------------------------

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)
_SELLING_ROUTER = os.path.join(_MODULE_DIR, "db_query.py")
_IN_TREE_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")


def _cli_run(dbfile, home, *cli_args):
    env = dict(os.environ, ERPCLAW_HOME=str(home), PYTHONPATH=_IN_TREE_LIB)
    env.pop("ERPCLAW_DB_PATH", None)
    env.pop("ERPCLAW_DB_URL", None)
    return subprocess.run(
        [sys.executable, _SELLING_ROUTER, "--db-path", dbfile] + list(cli_args),
        capture_output=True, text=True, env=env, timeout=120)


def test_cli_company_name_lists_that_company(tmp_path):
    """Through the real router, hermetic: a temp ERPCLAW_HOME, PYTHONPATH
    bound to the in-tree library, and a provisioned database. --company
    with an exact name lists that company's customers; a near-miss exits 1
    with the name-miss error."""
    dbfile = str(tmp_path / "cli.sqlite")
    init_all_tables(dbfile)
    conn = get_conn(dbfile)
    try:
        acme, wayne = _seed_exact_companies(conn)
        _seed_company_books(conn, acme, "Bruce Wayne", "100.00")
        _seed_company_books(conn, wayne, "Alfred Pennyworth", "150.00")
    finally:
        conn.close()
    home = tmp_path / "home"
    home.mkdir()
    proc = _cli_run(dbfile, home, "--action", "list-customers",
                    "--company", "Acme Widgets")
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-500:],
                                  proc.stderr[-500:])
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok", payload
    assert [c["name"] for c in payload["customers"]] == ["Bruce Wayne"]
    miss = _cli_run(dbfile, home, "--action", "list-customers",
                    "--company", "Acme")
    assert miss.returncode == 1, (miss.returncode, miss.stdout[-500:],
                                  miss.stderr[-500:])
    payload = json.loads(miss.stdout)
    assert payload["error"] == "Company 'Acme' not found.", payload
