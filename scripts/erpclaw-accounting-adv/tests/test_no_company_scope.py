"""No-company scope: lease and revenue lists and reports answer for one company.

Every list/report in scope resolves its company through
``erpclaw_lib.query_helpers.resolve_scope_company`` and always filters on it:

- no company with zero companies -> refuse ``No company found.``
- no company with one company -> use it
- no company with two or more -> refuse ``Multiple companies found.``
- unknown ``--company-id`` -> refuse ``Company not found: X``
- ``--company <name>`` resolves case-insensitively; a miss refuses.

``list-performance-obligations`` and ``list-variable-considerations`` with a
``--contract-id`` and no company keep the anchor behaviour: the contract names
one company, so no refusal.
"""
import json
import uuid

import pytest

from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    init_all_tables, get_conn, seed_naming_series,
)
from erpclaw_lib.query import P, Q, Table

mod = load_db_query()

ACME_NAME = "Acme Widgets"
ACME_ABBR = "ACME"
WAYNE_NAME = "Wayne Enterprises"
WAYNE_ABBR = "WAYNE"

NO_COMPANY_ERROR = "No company found. Create one first."
NO_COMPANY_SUGGESTION = (
    "Run 'tutorial' to create a demo company, or 'setup company' to create your own."
)
MULTI_COMPANY_ERROR = "Multiple companies found. Please specify the company by name."
MULTI_COMPANY_SUGGESTION = (
    "Pass the company name (e.g. --company \"Acme\"), "
    "or use --company-id with one of the IDs above."
)
NAME_MISS_SUGGESTION = (
    "Use one of the available company names exactly, "
    "or run 'list-companies' to see them."
)

STATE_TABLES = (
    "advacct_lease",
    "advacct_revenue_contract",
    "advacct_performance_obligation",
    "advacct_variable_consideration",
    "advacct_revenue_schedule",
    "advacct_consolidation_group",
    "company",
    "audit_log",
)


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "no_company_scope.sqlite")
    init_all_tables(path)
    connection = get_conn(path)
    yield connection
    connection.close()


def _insert_company(conn, name, abbr):
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO company (id, name, abbr, default_currency, country,"
        " fiscal_year_start_month) VALUES (?, ?, ?, 'USD', 'United States', 1)",
        (cid, name, abbr),
    )
    conn.commit()
    seed_naming_series(conn, cid)
    return cid


def _insert_schedule(conn, obligation_id, company_id, period_date, amount):
    sched_t = Table("advacct_revenue_schedule")
    conn.execute(
        Q.into(sched_t).columns(
            "id", "obligation_id", "period_date", "amount",
            "recognized", "company_id", "created_at")
        .insert(P(), P(), P(), P(), P(), P(), P()).get_sql(),
        (str(uuid.uuid4()), obligation_id, period_date, amount, 0,
         company_id, "2026-01-01T00:00:00Z"),
    )
    conn.commit()


def _add_lease_row(conn, company_id, lessee, pay):
    result = call_action(mod.add_lease, conn, ns(
        company_id=company_id, lessee_name=lessee,
        lessor_name="Property Holdings LLC",
        asset_description="Office space",
        lease_type="operating", start_date="2026-01-01",
        end_date="2026-12-31", term_months=12, monthly_payment=pay,
        annual_escalation="0", discount_rate="0",
        purchase_option_price=None,
    ))
    assert is_ok(result), result
    return result


def _add_contract_row(conn, company_id, customer, number, total):
    result = call_action(mod.add_revenue_contract, conn, ns(
        company_id=company_id, customer_name=customer,
        total_value=total, contract_number=number,
        start_date="2026-01-01", end_date="2026-12-31",
    ))
    assert is_ok(result), result
    return result


def _add_obligation_row(conn, company_id, contract_id, name, price):
    result = call_action(mod.add_performance_obligation, conn, ns(
        contract_id=contract_id, company_id=company_id,
        name=name, standalone_price=price,
        recognition_method="over_time", recognition_basis="time",
    ))
    assert is_ok(result), result
    return result


def _add_vc_row(conn, company_id, contract_id, description, estimated,
                constraint, probability):
    result = call_action(mod.add_variable_consideration, conn, ns(
        contract_id=contract_id, company_id=company_id,
        description=description, estimated_amount=estimated,
        constraint_amount=constraint, method="expected_value",
        probability=probability,
    ))
    assert is_ok(result), result
    return result


def _add_group_row(conn, company_id, name):
    result = call_action(mod.add_consolidation_group, conn, ns(
        company_id=company_id, name=name,
        parent_company_id=None, consolidation_currency="USD",
    ))
    assert is_ok(result), result
    return result


def _seed_all(conn):
    acme = _insert_company(conn, ACME_NAME, ACME_ABBR)
    wayne = _insert_company(conn, WAYNE_NAME, WAYNE_ABBR)
    lease_acme = _add_lease_row(conn, acme, "Acme Tenant", "1000.00")
    lease_wayne = _add_lease_row(conn, wayne, "Wayne Tenant", "7777.77")
    contract_acme = _add_contract_row(
        conn, acme, "Acme Customer", "C-A", "120000.00")
    contract_wayne = _add_contract_row(
        conn, wayne, "Wayne Customer", "C-W", "50000.00")
    obligation_acme = _add_obligation_row(
        conn, acme, contract_acme["id"], "Acme Service", "60000.00")
    obligation_wayne = _add_obligation_row(
        conn, wayne, contract_wayne["id"], "Wayne Service", "25000.00")
    vc_acme = _add_vc_row(conn, acme, contract_acme["id"], "Acme Bonus",
                          "5000.00", "1000.00", "0.5")
    vc_wayne = _add_vc_row(conn, wayne, contract_wayne["id"], "Wayne Bonus",
                           "7000.00", "2000.00", "0.6")
    _insert_schedule(conn, obligation_acme["id"], acme,
                     "2026-01-01", "1000.00")
    _insert_schedule(conn, obligation_wayne["id"], wayne,
                     "2026-02-01", "2000.00")
    group_acme = _add_group_row(conn, acme, "Acme Group")
    group_wayne = _add_group_row(conn, wayne, "Wayne Group")
    return {
        "acme": acme, "wayne": wayne,
        "lease_acme": lease_acme["id"], "lease_wayne": lease_wayne["id"],
        "contract_acme": contract_acme["id"],
        "contract_wayne": contract_wayne["id"],
        "obligation_acme": obligation_acme["id"],
        "obligation_wayne": obligation_wayne["id"],
        "vc_acme": vc_acme["id"], "vc_wayne": vc_wayne["id"],
        "group_acme": group_acme["id"], "group_wayne": group_wayne["id"],
    }


def _seed_acme_only(conn):
    acme = _insert_company(conn, ACME_NAME, ACME_ABBR)
    lease_acme = _add_lease_row(conn, acme, "Acme Tenant", "1000.00")
    contract_acme = _add_contract_row(
        conn, acme, "Acme Customer", "C-A", "120000.00")
    obligation_acme = _add_obligation_row(
        conn, acme, contract_acme["id"], "Acme Service", "60000.00")
    vc_acme = _add_vc_row(conn, acme, contract_acme["id"], "Acme Bonus",
                          "5000.00", "1000.00", "0.5")
    _insert_schedule(conn, obligation_acme["id"], acme,
                     "2026-01-01", "1000.00")
    group_acme = _add_group_row(conn, acme, "Acme Group")
    return {
        "acme": acme,
        "lease_acme": lease_acme["id"],
        "contract_acme": contract_acme["id"],
        "obligation_acme": obligation_acme["id"],
        "vc_acme": vc_acme["id"],
        "group_acme": group_acme["id"],
    }


@pytest.fixture
def seeded(conn):
    return _seed_all(conn)


def _state(conn):
    out = {}
    for table in STATE_TABLES:
        tbl = Table(table)
        rows = conn.execute(
            Q.from_(tbl).select(tbl.star).get_sql()).fetchall()
        out[table] = sorted(
            json.dumps(dict(row), sort_keys=True, default=str)
            for row in rows)
    return out


def _ns_leases(company_id=None, company_name=None):
    return ns(company_id=company_id, company_name=company_name,
              lease_type=None, lease_status=None, search=None,
              limit=50, offset=0)


def _ns_maturity(company_id=None, company_name=None):
    return ns(company_id=company_id, company_name=company_name)


def _ns_disclosure(company_id=None, company_name=None):
    return ns(company_id=company_id, company_name=company_name)


def _ns_summary(company_id=None, company_name=None):
    return ns(company_id=company_id, company_name=company_name)


def _ns_contracts(company_id=None, company_name=None):
    return ns(company_id=company_id, company_name=company_name,
              contract_status=None, search=None, limit=50, offset=0)


def _ns_obligations(company_id=None, company_name=None, contract_id=None):
    return ns(company_id=company_id, company_name=company_name,
              contract_id=contract_id, obligation_status=None,
              limit=50, offset=0)


def _ns_vc(company_id=None, company_name=None, contract_id=None):
    return ns(company_id=company_id, company_name=company_name,
              contract_id=contract_id, limit=50, offset=0)


def _ns_waterfall(company_id=None, company_name=None):
    return ns(company_id=company_id, company_name=company_name)


def _ns_recognition(company_id=None, company_name=None):
    return ns(company_id=company_id, company_name=company_name)


def _ns_dashboard(company_id=None, company_name=None):
    return ns(company_id=company_id, company_name=company_name)


def _ns_groups(company_id=None, company_name=None):
    return ns(company_id=company_id, company_name=company_name,
              group_status=None, search=None, limit=50, offset=0)


ACTION_TABLE = [
    ("list-leases", "list_leases", _ns_leases, "rows"),
    ("lease-maturity-report", "lease_maturity_report", _ns_maturity, "rows"),
    ("lease-disclosure-report", "lease_disclosure_report", _ns_disclosure,
     "rows"),
    ("lease-summary", "lease_summary", _ns_summary, "total_leases"),
    ("list-revenue-contracts", "list_revenue_contracts", _ns_contracts,
     "rows"),
    ("list-performance-obligations", "list_performance_obligations",
     _ns_obligations, "rows"),
    ("list-variable-considerations", "list_variable_considerations", _ns_vc,
     "rows"),
    ("revenue-waterfall-report", "revenue_waterfall_report", _ns_waterfall,
     "rows"),
    ("revenue-recognition-summary", "revenue_recognition_summary",
     _ns_recognition, "rows"),
    ("standards-compliance-dashboard", "standards_compliance_dashboard",
     _ns_dashboard, "asc_606"),
    ("list-consolidation-groups", "list_consolidation_groups", _ns_groups,
     "rows"),
]

ACTION_IDS = [key for key, _, _, _ in ACTION_TABLE]


def _handler(key):
    for name, attr, builder, absent in ACTION_TABLE:
        if name == key:
            return getattr(mod, attr), builder, absent
    raise KeyError(key)


def _no_company_ns(key):
    _, builder, _ = _handler(key)
    return builder(company_id=None, company_name=None)


@pytest.mark.parametrize("key", ACTION_IDS)
def test_zero_companies_refuses(conn, key):
    handler, _, absent = _handler(key)
    before = _state(conn)
    result = call_action(handler, conn, _no_company_ns(key))
    assert result == {
        "status": "error",
        "error": NO_COMPANY_ERROR,
        "message": NO_COMPANY_ERROR,
        "suggestion": NO_COMPANY_SUGGESTION,
    }
    assert absent not in result
    assert _state(conn) == before


@pytest.mark.parametrize("key", ACTION_IDS)
def test_two_companies_no_company_refuses(conn, seeded, key):
    handler, _, absent = _handler(key)
    before = _state(conn)
    result = call_action(handler, conn, _no_company_ns(key))
    assert result == {
        "status": "error",
        "error": MULTI_COMPANY_ERROR,
        "message": MULTI_COMPANY_ERROR,
        "companies": [
            {"id": seeded["acme"], "name": ACME_NAME},
            {"id": seeded["wayne"], "name": WAYNE_NAME},
        ],
        "suggestion": MULTI_COMPANY_SUGGESTION,
    }
    assert absent not in result
    assert _state(conn) == before


@pytest.mark.parametrize("key", ACTION_IDS)
def test_unknown_company_refuses(conn, seeded, key):
    handler, builder, absent = _handler(key)
    before = _state(conn)
    result = call_action(handler, conn, builder(company_id="no-such-company"))
    assert result == {
        "status": "error",
        "error": "Company not found: no-such-company",
        "message": "Company not found: no-such-company",
    }
    assert absent not in result
    assert _state(conn) == before


def _check_wayne_rows(key, result, seeded):
    wayne = seeded["wayne"]
    if key == "list-leases":
        assert is_ok(result), result
        assert result["total_count"] == 1
        assert result["rows"][0]["lessee_name"] == "Wayne Tenant"
        assert result["rows"][0]["monthly_payment"] == "7777.77"
        assert result["rows"][0]["company_id"] == wayne
    elif key == "lease-maturity-report":
        assert is_ok(result), result
        assert result["total_leases"] == 1
        assert result["rows"][0]["lessee_name"] == "Wayne Tenant"
        assert result["rows"][0]["monthly_payment"] == "7777.77"
    elif key == "lease-disclosure-report":
        assert is_ok(result), result
        assert len(result["rows"]) == 1
        row = result["rows"][0]
        assert row["lease_type"] == "operating"
        assert row["lease_count"] == 1
        assert row["total_monthly_payments"] == "7777.77"
    elif key == "lease-summary":
        assert is_ok(result), result
        assert result["total_leases"] == 1
    elif key == "list-revenue-contracts":
        assert is_ok(result), result
        assert result["total_count"] == 1
        assert result["rows"][0]["contract_number"] == "C-W"
        assert result["rows"][0]["total_value"] == "50000.00"
        assert result["rows"][0]["company_id"] == wayne
    elif key == "list-performance-obligations":
        assert is_ok(result), result
        assert result["total_count"] == 1
        assert result["rows"][0]["standalone_price"] == "25000.00"
        assert result["rows"][0]["company_id"] == wayne
    elif key == "list-variable-considerations":
        assert is_ok(result), result
        assert result["total_count"] == 1
        assert result["rows"][0]["description"] == "Wayne Bonus"
        assert result["rows"][0]["estimated_amount"] == "7000.00"
        assert result["rows"][0]["company_id"] == wayne
    elif key == "revenue-waterfall-report":
        assert is_ok(result), result
        assert result["total_contracts"] == 1
        assert result["rows"][0]["contract_number"] == "C-W"
        assert result["rows"][0]["obligation_count"] == 1
    elif key == "revenue-recognition-summary":
        assert is_ok(result), result
        assert result["total_periods"] == 1
        row = result["rows"][0]
        assert row["period_date"] == "2026-02-01"
        assert row["total_amount"] == "2000.00"
        assert row["recognized_amount"] == "0.00"
        assert row["unrecognized_amount"] == "2000.00"
    elif key == "standards-compliance-dashboard":
        assert is_ok(result), result
        assert result["report"] == "standards_compliance_dashboard"
        assert result["asc_606"] == {
            "revenue_contracts": 1, "unsatisfied_obligations": 1}
        assert result["asc_842"] == {
            "active_leases": 0, "leases_without_rou_calculation": 0}
        assert result["intercompany"] == {"unposted_transactions": 0}
        assert result["consolidation"] == {"active_groups": 1}
    elif key == "list-consolidation-groups":
        assert is_ok(result), result
        assert result["total_count"] == 1
        assert result["rows"][0]["name"] == "Wayne Group"
        assert result["rows"][0]["company_id"] == wayne
    else:
        raise KeyError(key)


@pytest.mark.parametrize("key", ACTION_IDS)
def test_explicit_second_company_scopes(conn, seeded, key):
    handler, builder, _ = _handler(key)
    result = call_action(handler, conn, builder(company_id=seeded["wayne"]))
    _check_wayne_rows(key, result, seeded)


@pytest.mark.parametrize("key", ACTION_IDS)
def test_one_company_uses_it(conn, key):
    own = _seed_acme_only(conn)
    handler, builder, _ = _handler(key)
    implicit = call_action(handler, conn, builder())
    explicit = call_action(
        handler, conn, builder(company_id=own["acme"]))
    assert is_ok(implicit), implicit
    assert implicit == explicit


def test_contract_anchor_needs_no_company(conn, seeded):
    result = call_action(
        mod.list_performance_obligations, conn,
        ns(company_id=None, company_name=None,
           contract_id=seeded["contract_wayne"],
           obligation_status=None, limit=50, offset=0))
    assert is_ok(result), result
    assert result["total_count"] == 1
    assert result["rows"][0]["standalone_price"] == "25000.00"
    assert result["rows"][0]["company_id"] == seeded["wayne"]
    vc_result = call_action(
        mod.list_variable_considerations, conn,
        ns(company_id=None, company_name=None,
           contract_id=seeded["contract_wayne"], limit=50, offset=0))
    assert is_ok(vc_result), vc_result
    assert vc_result["total_count"] == 1
    assert vc_result["rows"][0]["description"] == "Wayne Bonus"
    assert vc_result["rows"][0]["company_id"] == seeded["wayne"]


def test_company_flag_resolves_name(conn, seeded):
    flag = mod._resolve_company_flag
    args = ns(company_id=None, company_name="wayne enterprises")
    flag(conn, args)
    assert args.company_id == seeded["wayne"]
    args = ns(company_id=None, company_name=seeded["wayne"])
    flag(conn, args)
    assert args.company_id == seeded["wayne"]
    args = ns(company_id="kept-id", company_name=ACME_NAME)
    flag(conn, args)
    assert args.company_id == "kept-id"
    args = ns(company_id=None, company_name="Wayne")
    result = call_action(flag, conn, args)
    assert result == {
        "status": "error",
        "error": "Company 'Wayne' not found.",
        "message": "Company 'Wayne' not found.",
        "available_companies": [ACME_NAME, WAYNE_NAME],
        "suggestion": NAME_MISS_SUGGESTION,
    }
