"""Pure warehouse-name validation; duplicate/account checks belong to the service."""
from erpclaw_core.common import Refusal
import re
import unicodedata

_MSG = "A warehouse name needs 1 to 140 characters on one line, with at least one letter or digit."
_CODE = "bad_warehouse_name"

def warehouse_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("name must be str")
    for ch in name:
        if unicodedata.category(ch) == "Cc" or ch in ("\u2028", "\u2029"):
            raise Refusal(_MSG, code=_CODE, field="name", value=name)
    normalized = re.sub(" +", " ", name.strip(" "))
    if not normalized or len(normalized) > 140:
        raise Refusal(_MSG, code=_CODE, field="name", value=name)
    if not any(ch.isalnum() for ch in normalized):
        raise Refusal(_MSG, code=_CODE, field="name", value=name)
    return normalized
