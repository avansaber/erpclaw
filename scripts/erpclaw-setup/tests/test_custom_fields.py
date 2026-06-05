"""Tests for the M1 custom-field admin actions (erpclaw-setup).

add/list/remove-custom-field, set/get-custom-field-value(s).
"""
import argparse
import pytest
from setup_helpers import call_action, is_ok, is_error, load_db_query

mod = load_db_query()


def _ns(**kw):
    base = dict(table=None, field_name=None, field_type=None, label=None,
                default=None, required=False, options=None, skill_name=None,
                row_id=None, value=None, confirm=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _call(fn, conn, **kw):
    return call_action(getattr(mod, fn), conn, _ns(**kw))


class TestAddCustomField:
    def test_add_and_list(self, conn):
        r = _call("add_custom_field_action", conn, table="customer",
                  field_name="priority", field_type="select", options="Gold,Silver")
        assert is_ok(r) and r["result"] == "registered"
        lst = _call("list_custom_fields_action", conn, table="customer")
        assert lst["count"] == 1
        # the comma list became the lib's JSON options
        assert '"values"' in lst["custom_fields"][0]["field_options"]

    def test_duplicate_rejected(self, conn):
        _call("add_custom_field_action", conn, table="customer",
              field_name="priority", field_type="text")
        assert is_error(_call("add_custom_field_action", conn, table="customer",
                              field_name="priority", field_type="text"))

    def test_bad_type_rejected(self, conn):
        assert is_error(_call("add_custom_field_action", conn, table="customer",
                              field_name="x", field_type="bogus"))

    def test_missing_args(self, conn):
        assert is_error(_call("add_custom_field_action", conn, table="customer"))


class TestSetGetValue:
    def test_set_get_roundtrip(self, conn):
        _call("add_custom_field_action", conn, table="customer",
              field_name="tier", field_type="select", options="A,B")
        assert is_ok(_call("set_custom_field_value_action", conn, table="customer",
                          row_id="c1", field_name="tier", value="A"))
        got = _call("get_custom_field_values_action", conn, table="customer", row_id="c1")
        assert got["custom_fields"] == {"tier": "A"}

    def test_set_invalid_value_rejected(self, conn):
        _call("add_custom_field_action", conn, table="customer",
              field_name="tier", field_type="select", options="A,B")
        assert is_error(_call("set_custom_field_value_action", conn, table="customer",
                            row_id="c1", field_name="tier", value="Z"))

    def test_set_unknown_field_rejected(self, conn):
        assert is_error(_call("set_custom_field_value_action", conn, table="customer",
                            row_id="c1", field_name="ghost", value="x"))


class TestRemoveCustomField:
    def test_remove_unused(self, conn):
        _call("add_custom_field_action", conn, table="item", field_name="hs_code",
              field_type="text")
        assert is_ok(_call("remove_custom_field_action", conn, table="item",
                          field_name="hs_code"))
        assert _call("list_custom_fields_action", conn, table="item")["count"] == 0

    def test_remove_with_values_guarded(self, conn):
        _call("add_custom_field_action", conn, table="item", field_name="hs_code",
              field_type="text")
        _call("set_custom_field_value_action", conn, table="item", row_id="i1",
              field_name="hs_code", value="8471")
        # blocked without --confirm
        assert is_error(_call("remove_custom_field_action", conn, table="item",
                            field_name="hs_code"))
        # still there
        assert _call("list_custom_fields_action", conn, table="item")["count"] == 1
        # confirmed removal cascades the stored value
        assert is_ok(_call("remove_custom_field_action", conn, table="item",
                          field_name="hs_code", confirm=True))
        assert _call("get_custom_field_values_action", conn, table="item",
                     row_id="i1")["custom_fields"] == {}

    def test_remove_missing_field(self, conn):
        assert is_error(_call("remove_custom_field_action", conn, table="item",
                            field_name="nope"))
