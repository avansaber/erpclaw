from erpclaw_core.common import Refusal
from erpclaw_stock.setup_names import warehouse_name
import pytest

@pytest.mark.parametrize("value, expected", [("  Main   Warehouse ", "Main Warehouse"), ("a" * 140, "a" * 140), ("Café — №2", "Café — №2"), ("仓库 1", "仓库 1")])
def test_stored_name_rules(value, expected):
    assert warehouse_name(value) == expected

@pytest.mark.parametrize("value", ["", "   ", "a" * 141, "!!!", "___", "Main\tWarehouse", "Main\nWarehouse", "\tMain", "Main\x00Warehouse", "Main\u0085Warehouse", "Main\u2028Warehouse", "Main\u2029Warehouse"])
def test_refusal_has_the_original_value_and_exact_public_shape(value):
    with pytest.raises(Refusal) as caught:
        warehouse_name(value)
    assert caught.value.code == "bad_warehouse_name"
    assert caught.value.message == "A warehouse name needs 1 to 140 characters on one line, with at least one letter or digit."
    assert caught.value.data == {"field": "name", "value": value}

@pytest.mark.parametrize("value", [None, 42, b"Main"])
def test_untyped_call_is_rejected(value):
    with pytest.raises(TypeError):
        warehouse_name(value)
