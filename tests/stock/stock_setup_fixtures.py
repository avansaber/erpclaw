"""Pure stock/2 fixture loading; producer database execution is deliberately a separate gate."""
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "stock" / "v2"
SERVICES = {"list_warehouses", "preview_create_warehouse", "create_warehouse",
            "preview_make_stock_item", "make_stock_item"}
REQUIRED = {"contract", "service", "name", "books", "before", "input", "after"}


class FixtureError(ValueError):
    pass


def _reject_number(text):
    raise FixtureError(f"stock/2 setup figures are strings, not JSON number {text}")


def load_text(text: str) -> dict:
    case = json.loads(text, parse_int=_reject_number, parse_float=_reject_number, parse_constant=_reject_number)
    if not isinstance(case, dict) or not REQUIRED <= case.keys():
        raise FixtureError("stock/2 fixture missing required fields")
    if case["contract"] != "stock/2" or case["service"] not in SERVICES:
        raise FixtureError("stock/2 fixture names an unknown contract or setup service")
    if ("output" in case) == ("refusal" in case):
        raise FixtureError("stock/2 fixture requires exactly one output or refusal")
    return case


def all_cases() -> list[dict]:
    return [load_text(path.read_text()) for path in sorted(FIXTURES.glob("*.json"))]
