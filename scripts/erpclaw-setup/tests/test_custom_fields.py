"""Tests for the M1 custom-field admin actions (erpclaw-setup).

add/list/remove-custom-field, set/get-custom-field-value(s).
"""
import argparse
import pytest
from setup_helpers import call_action, is_ok, is_error, load_db_query
from decimal import Decimal
from erpclaw_lib import custom_fields

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


class TestExtendedCustomTypes:
    @pytest.mark.parametrize("field_type,value,options", [
        ("percent", "0", None), ("percent", "100.000000", None),
        ("percent", "12.345678", None), ("duration", "0", None),
        ("duration", "3661", None), ("rating", "5", None),
        ("rating", "0", None), ("rating", "10", '{"max":10}'),
        ("time", "23:59:59", None), ("time", "00:00", None),
    ])
    def test_register_set_and_fetch_exact_text(self, conn, field_type, value, options):
        registered = _call("add_custom_field_action", conn, table="customer",
                           field_name="extra", field_type=field_type, options=options)
        assert is_ok(registered)
        stored = _call("set_custom_field_value_action", conn, table="customer",
                       row_id="example", field_name="extra", value=value)
        assert is_ok(stored)
        assert custom_fields.fetch_custom_field_values(conn, "customer", "example") == {"extra": value}
        definitions = custom_fields.get_custom_fields(conn, "customer")
        assert definitions[0]["field_type"] == field_type
        if field_type == "percent":
            assert Decimal(stored["value"]) == Decimal(value)

    @pytest.mark.parametrize("field_type,value", [
        ("percent", "100.000001"), ("percent", "-1"), ("percent", "1e2"),
        ("percent", "NaN"), ("percent", "Infinity"), ("percent", "1.1234567"),
        ("percent", " 1"), ("percent", "١"),
        ("duration", "-1"), ("duration", "1.5"), ("duration", "PT1H"),
        ("duration", "1000000000000000000"),
        ("rating", "6"), ("rating", "2.5"), ("rating", "-1"),
        ("time", "24:00"), ("time", "12:60"), ("time", "12:00:60"),
        ("time", "12:00Z"), ("time", "1:00"), ("time", "12:00:00.5"),
    ])
    def test_refused_value_preserves_existing_text(self, conn, field_type, value):
        assert is_ok(_call("add_custom_field_action", conn, table="customer",
                           field_name="extra", field_type=field_type))
        original = "12:00" if field_type == "time" else "1"
        assert is_ok(_call("set_custom_field_value_action", conn, table="customer",
                           row_id="example", field_name="extra", value=original))
        before = custom_fields.fetch_custom_field_values(conn, "customer", "example")
        assert is_error(_call("set_custom_field_value_action", conn, table="customer",
                              row_id="example", field_name="extra", value=value))
        assert custom_fields.fetch_custom_field_values(conn, "customer", "example") == before

    @pytest.mark.parametrize("field_type,value", [
        ("percent", 1.25), ("duration", True), ("rating", False), ("time", []),
    ])
    def test_wrapper_refuses_lossy_or_wrong_json_types(self, conn, field_type, value):
        import json
        assert is_ok(_call("add_custom_field_action", conn, table="customer",
                           field_name="extra", field_type=field_type))
        errors = custom_fields.store_from_arg(conn, "customer", "example", json.dumps({"extra": value}))
        assert errors
        assert custom_fields.fetch_custom_field_values(conn, "customer", "example") == {}

    @pytest.mark.parametrize("field_type,options,default", [
        ("rating", '{"max":0}', None), ("rating", '{"max":101}', None),
        ("rating", '{"max":true}', None), ("rating", '{"max":5.0}', None),
        ("rating", '{"max":5,"min":1}', None), ("rating", '[]', None),
        ("rating", 'invalid', None), ("percent", '{"max":1}', None),
        ("percent", None, "101"), ("time", None, "25:00"),
        ("duration", None, "1.1"), ("rating", None, "6"),
    ])
    def test_definition_refusal_creates_no_field(self, conn, field_type, options, default):
        before = custom_fields.get_custom_fields(conn, "customer")
        assert is_error(_call("add_custom_field_action", conn, table="customer",
                              field_name="extra", field_type=field_type,
                              options=options, default=default))
        assert custom_fields.get_custom_fields(conn, "customer") == before

    def test_exact_default_flows_through_existing_document_wrapper(self, conn):
        assert is_ok(_call("add_custom_field_action", conn, table="customer",
                           field_name="extra", field_type="percent", default="12.500000"))
        assert custom_fields.store_from_arg(conn, "customer", "example", '{}') == []
        assert custom_fields.fetch_custom_field_values(conn, "customer", "example") == {"extra": "12.500000"}

    def test_required_empty_refuses_and_optional_empty_is_preserved(self, conn):
        assert is_ok(_call("add_custom_field_action", conn, table="customer",
                           field_name="extra", field_type="time", required=True))
        assert is_error(_call("set_custom_field_value_action", conn, table="customer",
                              row_id="example", field_name="extra", value=""))
        assert custom_fields.fetch_custom_field_values(conn, "customer", "example") == {}

    def test_currency_remains_excluded(self, conn):
        assert is_error(_call("add_custom_field_action", conn, table="customer",
                              field_name="money", field_type="currency"))
        with pytest.raises(ValueError, match="Unsupported custom field type"):
            custom_fields.add_custom_field(conn, "customer", "money", "currency", "erpclaw-setup")
        assert custom_fields.get_custom_fields(conn, "customer") == []

    @pytest.mark.parametrize("field_type,value", [
        ("percent", "12.345678"), ("duration", "7200"),
        ("rating", "4"), ("time", "17:30:01"),
    ])
    def test_actual_foundation_route_stores_new_type(self, db_path, field_type, value):
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        scripts = Path(__file__).resolve().parents[2]
        env = dict(os.environ, PYTHONPATH=str(scripts / "erpclaw-setup" / "lib"),
                   ERPCLAW_DB_PATH=db_path)

        def run(action, *args):
            proc = subprocess.run(
                [sys.executable, "-B", str(scripts / "db_query.py"),
                 "--action", action, "--db-path", db_path, "--table", "customer", *args],
                env=env, capture_output=True, text=True, timeout=30)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            return json.loads(proc.stdout)

        assert run("add-custom-field", "--field-name", "extra", "--field-type", field_type)["result"] == "registered"
        assert run("set-custom-field-value", "--field-name", "extra", "--row-id", "example", "--value", value)["result"] == "stored"
        assert run("get-custom-field-values", "--row-id", "example")["custom_fields"] == {"extra": value}
