"""M1 wrapper integration: add/get-item honor user-defined custom fields."""
import pytest
from erpclaw_lib import custom_fields as cf
from inventory_helpers import call_action, ns, is_ok, is_error, load_db_query

mod = load_db_query()


def _def_hs(conn):
    cf.add_custom_field(conn, "item", "hs_code", "text", "erpclaw-setup")
    conn.commit()


def _add(conn, **extra):
    base = dict(item_code="UDF-1", item_name="UDF Item", item_type=None,
                valuation_method=None, item_group=None, stock_uom=None,
                has_batch=None, has_serial=None, standard_rate=None,
                custom_fields=None)
    base.update(extra)
    return call_action(mod.add_item, conn, ns(**base))


def test_add_then_get_returns_custom_field(conn, env):
    _def_hs(conn)
    r = _add(conn, custom_fields='{"hs_code": "8471"}')
    assert is_ok(r) and r["custom_fields"] == {"hs_code": "8471"}
    got = call_action(mod.get_item, conn, ns(item_id=r["item_id"]))
    assert got["custom_fields"] == {"hs_code": "8471"}


def test_unknown_field_rolls_back_item(conn, env):
    # no field defined -> an unknown custom field is rejected and the item is not created
    r = _add(conn, item_code="BAD-1", custom_fields='{"ghost": "x"}')
    assert is_error(r)
    assert conn.execute(
        "SELECT COUNT(*) FROM item WHERE item_code='BAD-1'").fetchone()[0] == 0


def test_no_custom_fields_is_unaffected(conn, env):
    r = _add(conn, item_code="PLAIN-1")
    assert is_ok(r) and "custom_fields" not in r
