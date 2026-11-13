"""The full-return flag follows owner words and explicit answers only."""

import pytest

from erpclaw_assistant import resolve
from erpclaw_core.common import company_ctx
from erpclaw_core.plugins import REGISTRY

from test_credit_everything_flag import (
    draft_and_commit_invoice,
    ensure_customer,
    snapshot_business,
)


@pytest.mark.parametrize("phrase, model_value, bound, expected", [
    ("credit everything", False, None, True),
    ("credit EVERYTHING", False, None, True),
    ("credit", True, None, None),
    ("credit everythingness", True, None, None),
    ("credit", False, True, True),
    ("credit everything", True, False, False),
])
def test_flag_words_and_bound_answers(engine, phrase, model_value, bound, expected):
    customer = ensure_customer(engine, "read")
    number = draft_and_commit_invoice(engine, customer, 6, "setup-read")
    before = snapshot_business(engine)
    session = resolve.Session()
    if bound is not None:
        session.bindings["everything"] = bound
    with engine.connect() as conn:
        result = resolve.resolve_args(
            company_ctx(conn), REGISTRY.contracts["create_credit_note"],
            {"invoice": number, "everything": model_value},
            f"{phrase} invoice {number}", session,
        )
    if expected is None:
        assert "everything" not in result.args
    else:
        assert result.args["everything"] is expected
    assert result.args["invoice"] == number
    assert snapshot_business(engine) == before
