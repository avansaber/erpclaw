"""M1 wrapper integration: add/get-supplier honor user-defined custom fields."""
import pytest
from erpclaw_lib import custom_fields as cf
from buying_helpers import call_action, ns, is_ok, is_error, load_db_query

mod = load_db_query()


def _def_rating(conn):
    cf.add_custom_field(conn, "supplier", "rating", "select", "erpclaw-setup",
                        field_options='{"values": ["A", "B", "C"]}')
    conn.commit()


def _add(conn, env, **extra):
    base = dict(name="UDF Supp", company_id=env["company_id"], supplier_type=None,
                supplier_group=None, payment_terms_id=None, tax_id=None,
                is_1099_vendor=None, primary_address=None, custom_fields=None)
    base.update(extra)
    return call_action(mod.add_supplier, conn, ns(**base))


def test_add_then_get_returns_custom_field(conn, env):
    _def_rating(conn)
    r = _add(conn, env, custom_fields='{"rating": "A"}')
    assert is_ok(r) and r["custom_fields"] == {"rating": "A"}
    got = call_action(mod.get_supplier, conn, ns(supplier_id=r["supplier_id"]))
    assert got["custom_fields"] == {"rating": "A"}


def test_invalid_value_rolls_back_supplier(conn, env):
    _def_rating(conn)
    r = _add(conn, env, name="BadSupp", custom_fields='{"rating": "Z"}')
    assert is_error(r)
    assert conn.execute(
        "SELECT COUNT(*) FROM supplier WHERE name='BadSupp'").fetchone()[0] == 0


def test_no_custom_fields_is_unaffected(conn, env):
    r = _add(conn, env, name="PlainSupp")
    assert is_ok(r) and "custom_fields" not in r
