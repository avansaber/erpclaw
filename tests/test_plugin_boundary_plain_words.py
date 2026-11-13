"""Owner wording and private diagnostics at each service refusal site."""
from __future__ import annotations

import logging
import re

import pytest
from pydantic import Field, field_serializer, field_validator

import service_plugins as sp
from erpclaw_core import plugins
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.contracts import ServiceModel

PLAIN = "Something went wrong inside your books' software, so nothing was changed."
CURRENCY = "These books do not have the following currencies, so nothing was changed: XYZ."
SECRET = "owner-secret-9f3c"
QUOTE = f"{sp.PROVIDER}.quote"
PROBE = f"{sp.PROVIDER}.probe"
RECORD = f"{sp.PROVIDER}.record"


@pytest.fixture
def books(engine, monkeypatch, caplog):
    sp.install(monkeypatch, engine, sp.provider(), sp.consumer())
    caplog.set_level(logging.WARNING, logger="erpclaw_core.plugins")
    return engine


def refused(books, caplog, service, raw, code, *details, expected=PLAIN):
    with books.connect() as conn:
        with pytest.raises(Refusal) as caught:
            plugins.call_service(company_ctx(conn), sp.CONSUMER, service, raw)
    refusal = caught.value
    assert refusal.code == code
    assert refusal.message == expected
    assert str(refusal) == expected
    for technical in (service, sp.PROVIDER, sp.CONSUMER, code, "ValidationError",
                      "PydanticSerializationError", "ValueError", "RuntimeError",
                      "amount.value", "nested.count", "quoteRef", "note", *details):
        if technical != "XYZ":
            assert technical not in refusal.message
    assert not re.search(r"\b\w+(?:\.\w+)+\b", refusal.message)
    assert refusal.data == {}
    records = [r for r in caplog.records if r.name == "erpclaw_core.plugins"]
    assert len(records) == 1
    diagnostic = records[0].getMessage()
    assert service in diagnostic and code in diagnostic
    for detail in details:
        assert detail in diagnostic
    assert SECRET not in caplog.text
    assert SECRET not in repr(records[0].__dict__)
    assert records[0].exc_info is None


def test_restore_old_non_dict_f_string_leaks_service(books, caplog):
    refused(books, caplog, QUOTE, [SECRET], "service_input", "list")


def test_restore_old_validation_f_string_leaks_field_paths(books, caplog):
    refused(books, caplog, QUOTE,
            {"amount": {"currency": "USD"}, "rate": SECRET, "quoteRef": SECRET},
            "service_input", "amount.value", "rate", "ValidationError", "missing", "decimal_parsing")


class Number(ServiceModel):
    count: int


class NestedInput(ServiceModel):
    nested: Number
    private_note: str


def test_restore_old_bool_f_string_leaks_field_path(books, monkeypatch, caplog):
    monkeypatch.setattr(plugins.REGISTRY.services[QUOTE], "input_model", NestedInput)
    refused(books, caplog, QUOTE, {"nested": {"count": True}, "private_note": SECRET},
            "service_input", "nested.count", "true or false where a number is required")


def test_restore_old_currency_f_string_leaks_service(books, caplog):
    refused(books, caplog, QUOTE,
            {"amount": {"value": "1", "currency": "XYZ"}, "rate": "1", "quoteRef": SECRET},
            "unknown_currency", "XYZ", expected=CURRENCY)


def test_restore_old_undeclared_f_string_leaks_plugin_and_service(books, monkeypatch, caplog):
    monkeypatch.setitem(plugins.REGISTRY.plugins, sp.CONSUMER, sp.consumer([]))
    refused(books, caplog, QUOTE, {"private_note": SECRET}, "undeclared_service", sp.CONSUMER)


def test_restore_old_commit_f_string_leaks_service(books, caplog):
    refused(books, caplog, RECORD, {"voucher": SECRET}, "service_outside_commit")


class BrokenSerialization(ServiceModel):
    total: str

    @field_serializer("total", when_used="json")
    def serialize_total(self, value):
        raise ValueError(value)


def test_restore_old_serialization_f_string_leaks_exception_type(books, monkeypatch, caplog):
    monkeypatch.setattr(plugins.REGISTRY.services[QUOTE], "output_model", BrokenSerialization)
    monkeypatch.setattr(plugins.REGISTRY.services[QUOTE], "fn", lambda ctx, inp: {"total": SECRET})
    refused(books, caplog, QUOTE, {"amount": {"value": "1", "currency": "USD"}, "rate": "1"},
            "service_output", "PydanticSerializationError")


def test_restore_old_serialized_output_f_string_leaks_field_path(books, caplog):
    refused(books, caplog, PROBE, {"give": "serializer_float"},
            "service_output", "note", "floating-point")
    assert "serializer_float" not in caplog.text


class PrivateValidation(ServiceModel):
    private_note: str

    @field_validator("private_note")
    @classmethod
    def reject(cls, value):
        raise ValueError(value)


class PrivateProbe(sp.ProbeIn):
    private_note: str


def test_validation_diagnostics_omit_validator_message_context_and_input(books, monkeypatch, caplog):
    monkeypatch.setattr(plugins.REGISTRY.services[QUOTE], "input_model", PrivateValidation)
    refused(books, caplog, QUOTE, {"private_note": SECRET},
            "service_input", "private_note", "ValidationError", "value_error")


@pytest.mark.parametrize("give, details", [
    ("float_nested", ("parts[1].amount", "floating-point")),
    ("missing", ("total", "ValidationError", "missing")),
    ("bool_as_number", ("count", "true or false where a number is required")),
])
def test_shared_validation_sites_keep_output_diagnostics_private(books, monkeypatch, caplog, give, details):
    monkeypatch.setattr(plugins.REGISTRY.services[PROBE], "input_model", PrivateProbe)
    refused(books, caplog, PROBE, {"give": give, "private_note": SECRET}, "service_output", *details)


def test_unknown_output_currency_keeps_specific_plain_sentence(books, monkeypatch, caplog):
    monkeypatch.setattr(plugins.REGISTRY.services[QUOTE], "fn", lambda ctx, inp: {
        "total": {"value": "1", "currency": "XYZ"}, "quoteRef": SECRET})
    refused(books, caplog, QUOTE,
            {"amount": {"value": "1", "currency": "USD"}, "rate": "1", "quoteRef": SECRET},
            "unknown_currency", "XYZ", expected=CURRENCY)


class KeyedBoundary(ServiceModel):
    entries: dict[str, Number] = {}
    aliased_entries: dict[str, Number] = Field(default_factory=dict, alias="entriesByName")
    nested: dict[str, dict[str, Number]] = {}
    rows: list[Number] = []


@pytest.mark.parametrize("side", ["input", "output"])
@pytest.mark.parametrize("raw, details", [
    pytest.param({"entries": {SECRET: {"count": "bad"}}},
                 ("entries.<key>.count", "int_parsing"), id="dict_key"),
    pytest.param({"rows": [{SECRET: "private", "count": "bad"}]},
                 ("rows[0].<key>", "rows[0].count", "extra_forbidden"), id="unknown_field_name"),
    pytest.param({"rows": [{"count": "bad"}], SECRET: "private"},
                 ("rows[0].count", "<key>: extra_forbidden"), id="unknown_root_field_name"),
    pytest.param({"nested": {SECRET: {SECRET: {"count": "bad"}}}},
                 ("nested.<key>.<key>.count", "int_parsing"), id="nested_mapping_key"),
    pytest.param({"entries": {SECRET: {"count": 0.5}}},
                 ("entries.<key>.count", "floating-point"), id="float_path_key"),
    pytest.param({"entries": {SECRET: {"count": True}}},
                 ("entries.<key>.count", "true or false where a number is required"), id="bool_as_number_path_key"),
    pytest.param({"rows": [{"count": 1, SECRET: {SECRET: 0.5}}]},
                 ("rows[0].<key>.<key>", "floating-point"), id="unknown_nested_float_key"),
    pytest.param({"entriesByName": {SECRET: {"count": "bad"}}},
                 ("entriesByName.<key>.count", "int_parsing"), id="aliased_field_dict_key"),
])
def test_restore_verbatim_key_discloses_private_input_or_output_path(
        books, monkeypatch, caplog, side, raw, details):
    if side == "input":
        monkeypatch.setattr(plugins.REGISTRY.services[QUOTE], "input_model", KeyedBoundary)
        service, request, code = QUOTE, raw, "service_input"
    else:
        monkeypatch.setattr(plugins.REGISTRY.services[PROBE], "output_model", KeyedBoundary)
        monkeypatch.setattr(plugins.REGISTRY.services[PROBE], "fn", lambda ctx, inp: raw)
        service, request, code = PROBE, {"give": "good"}, "service_output"
    refused(books, caplog, service, request, code, *details)
    record = next(r for r in caplog.records if r.name == "erpclaw_core.plugins")
    for part in (record.getMessage(), record.msg, record.args, record.exc_info, record.__dict__):
        assert SECRET not in repr(part)


class SerializedPrivateKey(KeyedBoundary):
    @field_serializer("entries", when_used="json")
    def serialize_entries(self, value):
        return {SECRET: {"count": 0.5}}


def test_restore_verbatim_serialized_output_key_discloses_private_path(books, monkeypatch, caplog):
    monkeypatch.setattr(plugins.REGISTRY.services[PROBE], "output_model", SerializedPrivateKey)
    monkeypatch.setattr(plugins.REGISTRY.services[PROBE], "fn", lambda ctx, inp: {"entries": {}})
    refused(books, caplog, PROBE, {"give": "good"}, "service_output",
            "entries.<key>.count", "floating-point")
    record = next(r for r in caplog.records if r.name == "erpclaw_core.plugins")
    for part in (record.getMessage(), record.msg, record.args, record.exc_info, record.__dict__):
        assert SECRET not in repr(part)


@pytest.mark.parametrize("value, reason", [
    ("bad", "int_parsing"), (0.5, "floating-point"),
    (True, "true or false where a number is required"),
])
def test_restore_verbatim_mapping_key_matching_schema_field_discloses_key(
        books, monkeypatch, caplog, value, reason):
    monkeypatch.setattr(plugins.REGISTRY.services[QUOTE], "input_model", KeyedBoundary)
    refused(books, caplog, QUOTE, {"entries": {"count": {"count": value}}},
            "service_input", "entries.<key>.count", reason)
    assert "entries.count.count" not in caplog.text
