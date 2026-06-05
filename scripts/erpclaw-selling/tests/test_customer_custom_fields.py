"""M1 wrapper integration: add/get-customer honor user-defined custom fields."""
import pytest
from erpclaw_lib import custom_fields as cf
from selling_helpers import call_action, ns, is_ok, is_error, load_db_query

mod = load_db_query()


def _def_priority(conn):
    cf.add_custom_field(conn, "customer", "priority", "select", "erpclaw-setup",
                        field_options='{"values": ["Gold", "Silver"]}')
    conn.commit()


def _add(conn, env, **extra):
    base = dict(name="UDF Cust", company_id=env["company_id"], customer_type=None,
                customer_group=None, payment_terms_id=None, credit_limit=None,
                tax_id=None, exempt_from_sales_tax=None, primary_address=None,
                primary_contact=None, custom_fields=None)
    base.update(extra)
    return call_action(mod.add_customer, conn, ns(**base))


def test_add_then_get_returns_custom_field(conn, env):
    _def_priority(conn)
    r = _add(conn, env, custom_fields='{"priority": "Gold"}')
    assert is_ok(r) and r["custom_fields"] == {"priority": "Gold"}
    got = call_action(mod.get_customer, conn, ns(customer_id=r["customer_id"]))
    assert got["custom_fields"] == {"priority": "Gold"}


def test_invalid_value_rolls_back_customer(conn, env):
    _def_priority(conn)
    r = _add(conn, env, name="BadCust", custom_fields='{"priority": "Platinum"}')
    assert is_error(r)
    assert conn.execute(
        "SELECT COUNT(*) FROM customer WHERE name='BadCust'").fetchone()[0] == 0


def test_no_custom_fields_is_unaffected(conn, env):
    r = _add(conn, env, name="Plain")
    assert is_ok(r) and "custom_fields" not in r
