"""Stock valuation, the arithmetic alone: moving average, FIFO groups and returns at their issue's cost, every
figure half-up at the company currency's scale. The figures are the answer keys' (tests/expected_books/), and each
test fails when its rule is changed."""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from erpclaw_stock.valuation import Group, fifo_take, moving_average_cost, receipt_value, return_cost


def test_moving_average_takes_value_times_qty_over_on_hand_never_a_rounded_rate():
    # 100 @ 10.00, 50 @ 13.00, 30 @ 11.50: 180 worth 1995.00; 120 out costs 1330.00 (rate 11.0833..., unrounded)
    assert moving_average_cost(D("180"), D("1995.00"), D("120"), "USD") == D("1330.00")
    # rounding the rate first (11.08 x 120 = 1329.60) is the wrong answer the keys show
    assert moving_average_cost(D("180"), D("1995.00"), D("120"), "USD") != D("11.08") * 120
    # after a return of 10 at 110.83: 70 worth 775.83; 45 out costs 498.75
    assert moving_average_cost(D("70"), D("775.83"), D("45"), "USD") == D("498.75")


def test_moving_average_last_units_take_the_whole_remaining_value():
    assert moving_average_cost(D("3"), D("100"), D("3"), "JPY") == D("100")
    assert moving_average_cost(D("25"), D("277.08"), D("25"), "USD") == D("277.08")


def test_currency_scale_jpy_whole_yen_and_kwd_three_places():
    assert moving_average_cost(D("3"), D("100"), D("1"), "JPY") == D("33")
    assert moving_average_cost(D("2"), D("1.002"), D("1"), "KWD") == D("0.501")
    assert receipt_value(D("2"), D("0.501"), "KWD") == D("1.002")
    assert receipt_value(D("3"), D("33.5"), "JPY") == D("101")  # 100.5 half-up to whole yen


def test_zero_value_issue_and_the_last_units_carry_it():
    # 0.125 of one unit worth 0.01 rounds to nothing; the remaining 0.875 then takes the whole 0.01
    assert moving_average_cost(D("1"), D("0.01"), D("0.125"), "USD") == D("0.00")
    assert moving_average_cost(D("0.875"), D("0.01"), D("0.875"), "USD") == D("0.01")
    assert receipt_value(D("0.0001"), D("0.01"), "USD") == D("0.00")  # the caller refuses such a receipt


def test_fifo_takes_the_oldest_group_first_whole_groups_whole_value():
    groups = [Group("g1", D("100"), D("1000.00")), Group("g2", D("50"), D("650.00")), Group("g3", D("30"), D("345.00"))]
    takes = fifo_take(groups, D("120"), "USD")
    assert takes == [("g1", D("100"), D("1000.00")), ("g2", D("20"), D("260.00"))]
    assert sum(v for _, _, v in takes) == D("1260.00")


def test_fifo_part_of_a_group_is_rounded_on_the_groups_remaining_value_and_qty():
    # after 120 out and a return of 10 at 105.00: 30 @ 390.00, 30 @ 345.00, 10 @ 105.00; 45 out costs 562.50
    groups = [Group("g2", D("30"), D("390.00")), Group("g3", D("30"), D("345.00")), Group("g4", D("10"), D("105.00"))]
    takes = fifo_take(groups, D("45"), "USD")
    assert takes == [("g2", D("30"), D("390.00")), ("g3", D("15"), D("172.50"))]


def test_fifo_skips_empty_groups_and_never_takes_more_than_there_is():
    groups = [Group("g0", D("0"), D("0.00")), Group("g1", D("10"), D("100.00"))]
    assert fifo_take(groups, D("4"), "USD") == [("g1", D("4"), D("40.00"))]
    with pytest.raises(ValueError):
        fifo_take(groups, D("11"), "USD")


def test_a_return_costs_its_issue_cumulatively_and_three_parts_add_up_exactly():
    # the issue: 120 worth 1330.00 (moving average); returns of 40, 40 and 40
    a = return_cost(D("120"), D("1330.00"), D("0"), D("0.00"), D("40"), "USD")
    b = return_cost(D("120"), D("1330.00"), D("40"), a, D("40"), "USD")
    c = return_cost(D("120"), D("1330.00"), D("80"), a + b, D("40"), "USD")
    assert (a, b, c) == (D("443.33"), D("443.34"), D("443.33"))
    assert a + b + c == D("1330.00")  # no residue stays in cost of goods sold


def test_a_return_is_never_at_the_current_cost_or_the_selling_rate():
    # credit_note_partial_return_after_cost_change.md: 5 back of an issue of 10 worth 150.00 costs 75.00, not 81.88
    assert return_cost(D("10"), D("150.00"), D("0"), D("0.00"), D("5"), "USD") == D("75.00")
    # credit_note_full_return.md: 8 of 8 worth 1680.00 come back at 1680.00 (210.00 each), never 349.00 each
    assert return_cost(D("8"), D("1680.00"), D("0"), D("0.00"), D("8"), "USD") == D("1680.00")


def test_floats_are_refused_everywhere():
    with pytest.raises(TypeError):
        moving_average_cost(180.0, D("1995.00"), D("1"), "USD")
    with pytest.raises(TypeError):
        receipt_value(D("1"), 10.5, "USD")
    with pytest.raises(TypeError):
        return_cost(D("1"), D("1.00"), D("0"), D("0.00"), 0.5, "USD")


# The largest quantities and values the stock rows accept (14 whole digits and 4 places; values below 10^15): the
# product of a value and a quantity has more digits than Decimal's default context keeps, so the allocation must be
# computed exactly before it is rounded to the currency (here 292741813140.605 rounds up; a 28-digit product gives .60).
BIG_QTY, BIG_VALUE, HALF = D("72543443807.4548"), D("585483626281.21"), D("36271721903.7274")


def test_a_large_allocation_is_exact_before_its_currency_rounding():
    assert receipt_value(BIG_QTY, D("8.0708"), "USD") == BIG_VALUE
    assert moving_average_cost(BIG_QTY, BIG_VALUE, HALF, "USD") == D("292741813140.61")
    assert fifo_take([Group("g", BIG_QTY, BIG_VALUE)], HALF, "USD") == [("g", HALF, D("292741813140.61"))]
    assert return_cost(BIG_QTY, BIG_VALUE, D("0"), D("0.00"), HALF, "USD") == D("292741813140.61")
    second = return_cost(BIG_QTY, BIG_VALUE, HALF, D("292741813140.61"), HALF, "USD")
    assert second == D("292741813140.60") and second + D("292741813140.61") == BIG_VALUE  # all back: V exactly
