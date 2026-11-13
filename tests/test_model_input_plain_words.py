"""Model input refusals keep invented names and conversation contents private."""
import logging

import pytest

from erpclaw_assistant import muse
from erpclaw_core.common import Refusal
from owner_text import owner_text_violations

PLAIN = "The assistant asked for something it can't do, so nothing was saved."
PRIVATE = "private-owner-name"


@pytest.mark.parametrize("name", [PRIVATE + ".extra.part", PRIVATE + "/tool"])
def test_restore_old_tool_name_f_string_exposes_invented_name(caplog, name):
    check(caplog, lambda: muse._tool({"function": {"name": name}}), "invalid tool name <key>")


def test_restore_old_unmatched_result_text_exposes_model_internals(caplog):
    check(caplog, lambda: muse.to_responses([
        {"role": "tool", "tool_call_id": PRIVATE, "content": PRIVATE}]), "tool result without its call; call_id=<key>")


def check(caplog, operation, detail):
    caplog.set_level(logging.WARNING, logger=muse.__name__)
    with pytest.raises(Refusal) as caught:
        operation()
    refusal = caught.value
    assert refusal.code == "model_input_invalid"
    assert refusal.message == str(refusal) == PLAIN
    assert not owner_text_violations(refusal.message, code=refusal.code)
    for technical in (PRIVATE, "Muse", "tool", "call_id", "model_input_invalid"):
        assert technical not in refusal.message
    records = [r for r in caplog.records if r.name == muse.__name__]
    assert len(records) == 1
    assert "model_input_invalid" in records[0].getMessage()
    assert detail in records[0].getMessage()
    assert PRIVATE not in repr(records[0].__dict__)
    assert records[0].exc_info is None
