"""Complex validation aliases preserve boolean checks and private diagnostics."""
from __future__ import annotations

import logging
from typing import Annotated, Literal

import pytest
from pydantic import AliasChoices, AliasPath, ConfigDict, Field, create_model

from erpclaw_core import plugins
from erpclaw_core.common import Refusal
from erpclaw_core.contracts import ServiceModel

PLAIN = "Something went wrong inside your books' software, so nothing was changed."
OWNER_KEY = "private owner label"


class Leaf(ServiceModel):
    count: int


class Choices(ServiceModel):
    entries: dict[str, Leaf] = Field(
        validation_alias=AliasChoices("byName", "by_name"), serialization_alias="outEntries")


class Paths(ServiceModel):
    entries: dict[str, Leaf] = Field(validation_alias=AliasPath("payload", "items"))


class BoolLeaf(ServiceModel):
    count: bool


class BoolChoices(Choices):
    entries: dict[str, BoolLeaf] = Field(
        validation_alias=AliasChoices("byName", "by_name"), serialization_alias="outEntries")


class BoolPaths(Paths):
    entries: dict[str, BoolLeaf] = Field(validation_alias=AliasPath("payload", "items"))


def raw_entries(model, value):
    entries = {OWNER_KEY: {"count": value}}
    return {"by_name": entries} if issubclass(model, Choices) else {"payload": {"items": entries}}


@pytest.mark.parametrize("code", ["service_input", "service_output"])
@pytest.mark.parametrize("model", [Choices, Paths])
def test_bool_read_as_number_is_refused_through_complex_alias(model, code, caplog):
    caplog.set_level(logging.WARNING, logger="erpclaw_core.plugins")
    with pytest.raises(Refusal) as caught:
        plugins._validated(model, raw_entries(model, True), "Alias boundary", code)
    refusal = caught.value
    assert refusal.code == code
    assert refusal.message == str(refusal) == PLAIN
    assert refusal.data == {}
    records = [record for record in caplog.records if record.name == "erpclaw_core.plugins"]
    assert len(records) == 1
    record = records[0]
    diagnostic = record.getMessage()
    assert f"Alias boundary refused ({code}):" in diagnostic
    field = "by_name" if model is Choices else "entries"
    assert f"{field}.<key>.count is true or false where a number is required" in diagnostic
    assert OWNER_KEY not in repr(record.__dict__)
    assert OWNER_KEY not in caplog.text
    assert record.exc_info is None


@pytest.mark.parametrize("code", ["service_input", "service_output"])
@pytest.mark.parametrize("model, value", [(Choices, 7), (Paths, 7), (BoolChoices, True), (BoolPaths, True)])
def test_real_int_and_bool_fields_are_accepted_through_complex_alias(model, value, code, caplog):
    result = plugins._validated(model, raw_entries(model, value), "Alias boundary", code)
    count = result.entries[OWNER_KEY].count
    assert type(count) is type(value)
    assert count == value
    assert not [record for record in caplog.records if record.name == "erpclaw_core.plugins"]


class NamedFields(ServiceModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


ALIASED_ROWS = [
    pytest.param(Field(alias="rowsIn"), lambda rows: {"rowsIn": rows}, "rowsIn", id="plain_alias"),
    pytest.param(Field(validation_alias="rowsIn", serialization_alias="rowsOut"),
                 lambda rows: {"rowsIn": rows}, "rowsIn", id="validation_string"),
    pytest.param(Field(validation_alias=AliasChoices("first", "second"), serialization_alias="rowsOut"),
                 lambda rows: {"first": rows}, "first", id="first_choice"),
    pytest.param(Field(validation_alias=AliasChoices("missing", AliasPath("payload", "items"))),
                 lambda rows: {"payload": {"items": rows}}, "rows", id="path_choice"),
    pytest.param(Field(validation_alias=AliasChoices(AliasPath("payload", "absent"), "second")),
                 lambda rows: {"second": rows}, "second", id="choice_after_missing_path"),
    pytest.param(Field(validation_alias=AliasPath("payload", 1)),
                 lambda rows: {"payload": [None, rows]}, "rows", id="indexed_path"),
    pytest.param(Field(validation_alias=AliasPath("payload", -1)),
                 lambda rows: {"payload": [None, rows]}, "rows", id="negative_indexed_path"),
    pytest.param(Field(validation_alias=AliasPath("payload", "items"), serialization_alias="rowsOut"),
                 lambda rows: {"rows": rows}, "rows", id="field_name_fallback"),
    pytest.param(Field(validation_alias="", serialization_alias=""),
                 lambda rows: {"": rows}, "", id="empty_alias"),
]


@pytest.mark.parametrize("field, wrap, diagnostic", ALIASED_ROWS)
def test_other_alias_forms_keep_list_indices_and_mapping_keys_private(field, wrap, diagnostic, caplog):
    model = create_model("AliasedRows", __base__=NamedFields, rows=(list[dict[str, Leaf]], field))
    raw = wrap([{OWNER_KEY: {"count": True}}])
    with pytest.raises(Refusal) as caught:
        plugins._validated(model, raw, "Alias boundary", "service_input")
    assert (caught.value.code, caught.value.message) == ("service_input", PLAIN)
    record, = [record for record in caplog.records if record.name == "erpclaw_core.plugins"]
    assert f"{diagnostic}[0].<key>.count is true or false where a number is required" in record.getMessage()
    assert OWNER_KEY not in repr(record.__dict__)


def test_alias_choices_use_first_present_path():
    class OrderedPaths(ServiceModel):
        entries: dict[str, Leaf] = Field(validation_alias=AliasChoices(
            AliasPath("payload", "first"), AliasPath("payload", "second")))

    raw = {"payload": {"first": {OWNER_KEY: {"count": 7}}, "second": {OWNER_KEY: {"count": True}}}}
    result = plugins._validated(OrderedPaths, raw, "Alias boundary", "service_input")
    assert type(result.entries[OWNER_KEY].count) is int
    assert result.entries[OWNER_KEY].count == 7


@pytest.mark.parametrize("code", ["service_input", "service_output"])
@pytest.mark.parametrize("field, wrap, diagnostic", ALIASED_ROWS)
def test_aliases_disabled_implicitly_validate_field_names(field, wrap, diagnostic, code, caplog):
    model = create_model("NameOnlyRows", __base__=ServiceModel,
                         __config__=ConfigDict(validate_by_alias=False),
                         rows=(list[dict[str, Leaf]], field))
    raw = {"rows": [{OWNER_KEY: {"count": True}}]}
    # Pydantic accepts the name without validate_by_name or populate_by_name being set.
    assert model.model_validate(raw).rows[0][OWNER_KEY].count == 1
    with pytest.raises(Refusal) as caught:
        plugins._validated(model, raw, "Alias boundary", code)
    assert (caught.value.code, caught.value.message, caught.value.data) == (code, PLAIN, {})
    record, = [record for record in caplog.records if record.name == "erpclaw_core.plugins"]
    assert "rows[0].<key>.count is true or false where a number is required" in record.getMessage()
    assert OWNER_KEY not in repr(record.__dict__)


class Count(ServiceModel):
    kind: Literal["count"]
    count: int = Field(validation_alias=AliasPath("payload", "count"))


class Flag(ServiceModel):
    kind: Literal["flag"]
    count: bool = Field(validation_alias=AliasPath("payload", "flag"))


class SmartCount(ServiceModel):
    count: int = Field(validation_alias=AliasPath("payload", "count"))
    detail: int = Field(validation_alias=AliasPath("payload", "detail"))


class SmartFlag(ServiceModel):
    count: bool = Field(validation_alias=AliasPath("payload", "flag"))


@pytest.mark.parametrize("code", ["service_input", "service_output"])
@pytest.mark.parametrize("variant, count_model", [
    pytest.param(Annotated[Count | Flag, Field(discriminator="kind")], Count, id="discriminated"),
    pytest.param(SmartCount | SmartFlag, SmartCount, id="smart"),
])
@pytest.mark.parametrize("value", [7, True], ids=["unused_bool_alias", "selected_bool_alias"])
def test_union_checks_only_the_validated_variant(variant, count_model, value, code, caplog):
    model = create_model("UnionRows", __base__=ServiceModel, rows=(list[dict[str, variant]], ...))
    entry = {"payload": {"count": value, "flag": True, "detail": 7}}
    if count_model is Count:
        entry["kind"] = "count"
    # Both smart-union variants validate; Pydantic chooses the one with more matched fields.
    else:
        SmartCount.model_validate(entry)
        SmartFlag.model_validate(entry)
    raw = {"rows": [{OWNER_KEY: entry}]}
    assert type(model.model_validate(raw).rows[0][OWNER_KEY]) is count_model
    if type(value) is bool:
        with pytest.raises(Refusal) as caught:
            plugins._validated(model, raw, "Alias boundary", code)
        assert (caught.value.code, caught.value.message, caught.value.data) == (code, PLAIN, {})
        record, = [record for record in caplog.records if record.name == "erpclaw_core.plugins"]
        assert "rows[0].<key>.count is true or false where a number is required" in record.getMessage()
        assert OWNER_KEY not in repr(record.__dict__)
    else:
        result = plugins._validated(model, raw, "Alias boundary", code)
        assert type(result.rows[0][OWNER_KEY]) is count_model
        assert type(result.rows[0][OWNER_KEY].count) is int
        assert result.rows[0][OWNER_KEY].count == value
        assert not [record for record in caplog.records if record.name == "erpclaw_core.plugins"]
