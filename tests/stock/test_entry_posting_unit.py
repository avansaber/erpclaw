"""Posting-only units: real valued Move/Pair objects, no SQL or costing."""
import copy
from decimal import Decimal, localcontext

import pytest
from erpclaw_stock.entry_posting import EntryPostingError, entry_posting_lines
from erpclaw_stock.service import Move, Pair

D = Decimal
ROLES = {"opening_balance": "equity", "stock_adjustment": "adjustment", "stock_write_off": "write-off"}


def move(direction, value, account="raw", qty="1", **kwargs):
    pair = Pair({"method": "fifo", "standard_rate": "999999.99"}, {"id": "warehouse"}, account,
                None, D("100"), D("500"))
    return Move(pair, direction, D(qty), D(value), 1, **kwargs)


def plan(kind, moves, currency="USD", roles=ROLES):
    return entry_posting_lines(kind, moves, currency, roles)


def line(account, debit="0.00", credit="0.00"):
    return {"account": account, "debit": debit, "credit": credit, "currency": "USD"}


def test_original_k7_opening_posts_two_inventory_debits_and_summed_equity():
    rows = [move("in", "1185.00", qty="120"), move("in", "493.33", account="finished", qty="40")]
    assert plan("opening", rows) == [line("raw", "1185.00"), line("finished", "493.33"),
                                    line("equity", credit="1678.33")]


@pytest.mark.parametrize("value", ["44.33", "44.40"])
def test_original_k3_count_gain_uses_prevalued_cost_not_selling_price(value):
    assert plan("count", [move("in", value)]) == [line("raw", value), line("adjustment", credit=value)]


@pytest.mark.parametrize("value", ["80.00", "88.67", "24.67"])
def test_original_k4_fifo_average_and_k7_count_loss_values_are_not_recomputed(value):
    assert plan("count", [move("out", value)]) == [line("adjustment", value), line("raw", credit=value)]


@pytest.mark.parametrize("value, qty", [("22.56", "3"), ("466.19", "62")])
def test_original_k5_partial_and_remaining_writeoff_use_writeoff_role(value, qty):
    assert plan("write_off", [move("out", value, qty=qty)]) == [line("write-off", value), line("raw", credit=value)]


def test_original_k8_offsetting_count_keeps_four_gross_lines_in_both_input_orders():
    gain, loss = move("in", "10.00", qty="2"), move("out", "10.00", qty="5")
    expected = [line("raw", "10.00"), line("adjustment", credit="10.00"),
                line("adjustment", "10.00"), line("raw", credit="10.00")]
    assert plan("count", [gain, loss]) == expected
    assert plan("count", [loss, gain]) == expected
    assert sum(D(x["debit"]) for x in expected) == sum(D(x["credit"]) for x in expected) == D("20.00")


def test_same_account_same_direction_sums_but_opposite_direction_remains_distinct():
    rows = [move("in", "10.00"), move("in", "2.34"), move("out", "3.00"),
            move("in", "4.56", account="finished"), move("out", "1.00", account="finished")]
    assert plan("count", rows) == [line("raw", "12.34"), line("finished", "4.56"),
        line("adjustment", credit="16.90"), line("adjustment", "4.00"),
        line("raw", credit="3.00"), line("finished", credit="1.00")]


@pytest.mark.parametrize("kind", ["count", "write_off"])
def test_legitimate_zero_cost_issue_has_no_posting_line(kind):
    assert plan(kind, [move("out", "0.00")]) == []
    assert plan(kind, [move("out", "-0.00")]) == []


def test_zero_cost_issue_does_not_remove_other_nonzero_movements():
    assert plan("count", [move("out", "0.00"), move("in", "10.00")]) == [
        line("raw", "10.00"), line("adjustment", credit="10.00")]


@pytest.mark.parametrize("kind", ["opening", "count"])
def test_zero_value_incoming_receipt_is_refused(kind):
    with pytest.raises(EntryPostingError, match="positive rounded"):
        plan(kind, [move("in", "0.00")])


def test_empty_count_is_no_adjustment_while_other_kinds_require_a_movement():
    assert plan("count", []) == []
    for kind in ("opening", "write_off"):
        with pytest.raises(EntryPostingError, match="require a movement"):
            plan(kind, [])


@pytest.mark.parametrize("currency, value, zero", [("JPY", "12", "0"), ("KWD", "12.345", "0.000"),
                                                   ("USD", "12.30", "0.00")])
def test_company_scale_is_exact_on_both_sides(currency, value, zero):
    assert plan("opening", [move("in", value)], currency) == [
        {"account": "raw", "debit": value, "credit": zero, "currency": currency},
        {"account": "equity", "debit": zero, "credit": value, "currency": currency}]


@pytest.mark.parametrize("value", [D("-1"), D("NaN"), D("Infinity"), D("-Infinity"), D("sNaN"),
                                   D("0.001"), D("1000000000000000"), 1, "1.00", 1.0, True, None, {}, []])
def test_malformed_nonfinite_negative_offscale_or_oversized_value_is_refused(value):
    row = move("out", "1.00")
    row.value = value
    with pytest.raises(EntryPostingError): plan("count", [row])


@pytest.mark.parametrize("currency,value", [("JPY", "1.1"), ("KWD", "1.0001")])
def test_currency_specific_subminor_value_is_not_rounded(currency, value):
    with pytest.raises(EntryPostingError): plan("count", [move("out", value)], currency)


def test_summed_line_cannot_exceed_core_posted_amount_bound():
    with pytest.raises(EntryPostingError, match="too large"):
        plan("opening", [move("in", "600000000000000.00"), move("in", "600000000000000.00")])
    with pytest.raises(EntryPostingError, match="too large"):
        plan("opening", [move("in", "600000000000000.00"),
                         move("in", "600000000000000.00", account="finished")])


def test_decimal_accumulation_is_exact_even_with_low_callers_context_precision():
    with localcontext() as ctx:
        ctx.prec = 5
        result = plan("opening", [move("in", "999999999999998.99"), move("in", "1.00")])
        assert ctx.prec == 5
    assert result == [line("raw", "999999999999999.99"), line("equity", credit="999999999999999.99")]


@pytest.mark.parametrize("kind", ["transfer", "goods_received", "cancellation", "stock_count", None, {}])
def test_only_original_bounded_posting_kinds_are_supported(kind):
    with pytest.raises(EntryPostingError, match="kind"):
        plan(kind, [move("in", "1.00")])


@pytest.mark.parametrize("kind,direction", [("opening", "out"), ("write_off", "in"),
                                           ("count", "sideways"), ("count", None)])
def test_direction_must_match_the_original_kind(kind, direction):
    with pytest.raises(EntryPostingError, match="direction"):
        plan(kind, [move(direction, "1.00")])


@pytest.mark.parametrize("change", [{"original": {}}, {"reverses": "entry"}, {"against": "entry"},
    {"take_layer": "layer"}, {"give_backs": [("group", D("1"), D("1"))]}])
def test_reversal_or_return_input_is_rejected_without_guessing_counter_policy(change):
    with pytest.raises(EntryPostingError, match="own posting policy"):
        plan("count", [move("out", "1.00", **change)])


@pytest.mark.parametrize("qty", [D("0"), D("-1"), D("NaN"), D("Infinity"), D("0.00001"), 1.0, True, "1"])
def test_malformed_movement_quantity_is_refused_without_revaluing(qty):
    row = move("out", "1.00")
    row.qty = qty
    with pytest.raises(EntryPostingError): plan("count", [row])


@pytest.mark.parametrize("roles", [{}, {"opening_balance": ""}, {"opening_balance": None},
                                   {"opening_balance": " raw "}, {"opening_balance": "raw"}, None])
def test_resolved_counter_role_account_is_required_and_distinct_from_inventory(roles):
    with pytest.raises(EntryPostingError): plan("opening", [move("in", "1.00")], roles=roles)


@pytest.mark.parametrize("account", [None, "", " raw ", 1])
def test_inventory_account_must_be_a_resolved_nonempty_id(account):
    with pytest.raises(EntryPostingError): plan("count", [move("out", "1.00", account=account)])


@pytest.mark.parametrize("currency", ["XXX", None, {}])
def test_unsupported_company_currency_is_refused(currency):
    with pytest.raises(EntryPostingError): plan("count", [move("out", "1.00")], currency)


@pytest.mark.parametrize("rows", [None, "moves", {"value": "1.00"}, [None]])
def test_malformed_movement_container_is_refused(rows):
    with pytest.raises(EntryPostingError): plan("count", rows)


def test_no_input_mutation_and_no_cached_mutable_output():
    rows = [move("in", "10.00"), move("out", "10.00")]
    original = copy.deepcopy(rows)
    roles = dict(ROLES)
    first = plan("count", rows, roles=roles)
    first[0]["debit"] = "999"
    assert rows == original and roles == ROLES
    assert plan("count", tuple(rows))[0]["debit"] == "10.00"
