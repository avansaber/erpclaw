"""Resolve issued payment/delivery numbers without changing their stored spelling."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from erpclaw_core import core_contracts
from erpclaw_core.common import Refusal, company_ctx
from erpclaw_core.numbering import next_number
from erpclaw_core.payable_tables import supplier_payment
from erpclaw_stock import reverse_goods_received, tables
from test_goods_received_reversal_contract import seed_delivery
from test_supplier_payment_reversal_contract import ORIGINAL_DAY, seed_supplier_payment


@pytest.fixture(params=["payment", "delivery"])
def document(request):
    if request.param == "payment":
        return SimpleNamespace(kind="payment", prefix="SPAY", table=supplier_payment,
                               seed=seed_supplier_payment, resolve=core_contracts._supplier_payment_number)
    return SimpleNamespace(kind="delivery", prefix="GR", table=tables.voucher,
                           seed=seed_delivery, resolve=reverse_goods_received._delivery_number)


@pytest.mark.parametrize("spelling", ["issued", "lower", "mixed"])
def test_issued_number_round_trips(engine, document, spelling):
    # Use the real issuer used by both forward commits, including its year and
    # sequence rows; the forward cards are not public, so seed the posted row.
    with engine.begin() as conn:
        number = next_number(company_ctx(conn), document.prefix, ORIGINAL_DAY)
    assert number == f"{document.prefix}-2026-00001"
    document.seed(engine, number=number)
    said = {"issued": number, "lower": number.lower(),
            "mixed": number[0] + number[1:].lower()}[spelling]
    with engine.connect() as conn:
        assert document.resolve(company_ctx(conn), said) == number


@pytest.mark.parametrize("suffix", ["1", "00001"])
def test_unique_numeric_suffix_resolves(engine, document, suffix):
    with engine.begin() as conn:
        number = next_number(company_ctx(conn), document.prefix, ORIGINAL_DAY)
    document.seed(engine, number=number)
    with engine.connect() as conn:
        assert document.resolve(company_ctx(conn), suffix) == number


def test_ambiguous_suffix_keeps_its_question_and_choices(engine, document):
    numbers = [f"{document.prefix}-{year}-00001" for year in (2025, 2026)]
    document.seed(engine, number=numbers[1])
    extra = {"operation_key": "other-forward"} if document.kind == "payment" else {}
    document.seed(engine, tag="other", number=numbers[0], **extra)
    with engine.connect() as conn, pytest.raises(Refusal) as caught:
        document.resolve(company_ctx(conn), "00001")
    refusal = caught.value
    assert refusal.code == f"{document.kind}_ambiguous"
    assert refusal.message == (
        f"More than one {document.kind} ends in 00001: {', '.join(numbers)}. Which one?")
    assert refusal.options == numbers
    assert refusal.data["field"] == document.kind
    assert refusal.data["choices"] == [
        {"label": number, "ref": {document.kind: number}} for number in numbers]


def test_mixed_case_stored_number_is_not_an_issued_number(engine, document):
    number = document.prefix[0] + document.prefix[1:].lower() + "-2026-00001"
    document.seed(engine, number=number)
    with engine.connect() as conn:
        ctx = company_ctx(conn)
        # The storage schema permits this spelling, but neither issuer makes it.
        assert conn.execute(select(document.table.c.number).where(
            document.table.c.company_id == ctx.company_id,
            document.table.c.number == number)).scalar_one() == number
        for said in (number, number.lower(), number.upper()):
            with pytest.raises(Refusal) as caught:
                document.resolve(ctx, said)
            assert caught.value.code == f"{document.kind}_unknown"
        # Numeric suffixes still return the actual stored number unchanged.
        assert document.resolve(ctx, "00001") == number
