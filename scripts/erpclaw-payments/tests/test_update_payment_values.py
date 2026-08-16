"""Part A — VALUE tests for `update-payment` (Wave G F21).

`update-payment` rewrites `payment_allocation` rows and `payment_entry` money
columns, and the only test it had was the generated contract assert that the
action dispatches. It is the last un-pinned writer of the residual that INV-27's
right-hand side reads (`payment_entry.unallocated_amount` for submitted
payments), so a wrong residual written here is a party-ledger defect of exactly
the M38 family.

Register row: `planning/wave_g/F21_TEST_DEPTH_REGISTER_2026-08-11.json`
(`update-payment`, class `routability-only`, ledger reach `payment_allocation`,
`payment_entry`).

M60 (2026-08-12) repaired F21-FINDING-1: `--paid-amount` now recomputes the
residual from the detail rows, and an edit that would push it below zero is
refused with nothing written. The pins below moved with it — the `xfail(strict)`
became a plain assertion and the companion pin on the defective reading became
the regression pin on the repaired one. SIM: `planning/simlogs/m60_SIM_2026-08-12.md`.
"""
import json
from decimal import Decimal

import pytest
from payments_helpers import (build_ar_env, call_action, is_error, is_ok,
                              load_db_query, ns, seed_sales_invoice)

pay = load_db_query()

D = Decimal


def _msg(result: dict) -> str:
    return result.get("message", "") + result.get("error", "")


@pytest.fixture
def env(conn):
    return build_ar_env(conn)


def _add_payment(conn, env, paid, allocations=None, deductions=None):
    created = call_action(pay.add_payment, conn, ns(
        company_id=env["company_id"], payment_type="receive",
        posting_date="2026-06-01", party_type="customer",
        party_id=env["customer"], paid_from_account=env["ar"],
        paid_to_account=env["bank"], paid_amount=str(paid),
        exchange_rate=None, payment_currency=None,
        reference_number="WIRE-1", reference_date=None,
        allocations=json.dumps(allocations) if allocations else None,
        deductions=json.dumps(deductions) if deductions else None))
    assert is_ok(created), created
    return created["payment_entry_id"]


def _update(conn, pe_id, **kw):
    args = {"payment_entry_id": pe_id, "paid_amount": None,
            "reference_number": None, "allocations": None}
    args.update(kw)
    return call_action(pay.update_payment, conn, ns(**args))


def _pe(conn, pe_id):
    return conn.execute(
        "SELECT paid_amount, received_amount, unallocated_amount, status, "
        "reference_number FROM payment_entry WHERE id = ?", (pe_id,)).fetchone()


def _allocs(conn, pe_id):
    return conn.execute(
        "SELECT voucher_type, voucher_id, allocated_amount, delinked "
        "FROM payment_allocation WHERE payment_entry_id = ? "
        "ORDER BY allocated_amount", (pe_id,)).fetchall()


def test_replacing_allocations_rewrites_the_residual_exactly(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(conn, env, "1000.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("700.00")

    res = _update(conn, pe_id, allocations=json.dumps([
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "450.00"}]))
    assert is_ok(res), res
    assert res["updated_fields"] == ["allocations"]

    rows = _allocs(conn, pe_id)
    assert len(rows) == 1, "the old allocation must be replaced, not doubled"
    assert D(rows[0]["allocated_amount"]) == D("450.00")
    assert rows[0]["voucher_id"] == si
    assert rows[0]["delinked"] == 0
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("550.00")


def test_clearing_allocations_returns_the_whole_amount_to_the_residual(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(conn, env, "800.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "800.00"}])
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("0")

    res = _update(conn, pe_id, allocations=json.dumps([
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "125.25"}]))
    assert is_ok(res), res
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("674.75")
    assert D(_allocs(conn, pe_id)[0]["allocated_amount"]) == D("125.25")


def test_reference_number_edit_touches_no_money_column(conn, env):
    pe_id = _add_payment(conn, env, "500.00")
    before = _pe(conn, pe_id)

    res = _update(conn, pe_id, reference_number="WIRE-2")
    assert is_ok(res), res

    after = _pe(conn, pe_id)
    assert after["reference_number"] == "WIRE-2"
    assert D(after["paid_amount"]) == D(before["paid_amount"])
    assert D(after["unallocated_amount"]) == D(before["unallocated_amount"])


def test_reducing_paid_amount_recomputes_the_residual(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(conn, env, "1000.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("700.00")

    res = _update(conn, pe_id, paid_amount="500.00")
    assert is_ok(res), res

    row = _pe(conn, pe_id)
    assert D(row["paid_amount"]) == D("500.00")
    # paid_amount = allocations + deductions + unallocated  =>  500 - 300 = 200
    assert D(row["unallocated_amount"]) == D("200.00")


def test_the_whole_money_row_after_a_paid_amount_edit(conn, env):
    """Every money column of the edited row at once (M60 regression pin).

    Was `test_the_stale_residual_is_pinned_as_observed`, the companion pin on
    F21-FINDING-1's defective reading: it asserted `unallocated_amount ==
    700.00` after the amount had been reduced to 500.00, so that whoever
    repaired the finding saw exactly one red test plus one xfail flip rather
    than a silent behaviour change. That is what happened; the pin now asserts
    the repaired reading, and keeps `received_amount` in the same assertion
    because the paid-amount branch writes both columns and only one of them was
    ever wrong.
    """
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(conn, env, "1000.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    assert is_ok(_update(conn, pe_id, paid_amount="500.00"))
    row = _pe(conn, pe_id)
    assert D(row["paid_amount"]) == D("500.00")
    assert D(row["received_amount"]) == D("500.00")
    assert D(row["unallocated_amount"]) == D("200.00")
    assert (D(row["paid_amount"])
            - D(_allocs(conn, pe_id)[0]["allocated_amount"])
            - D(row["unallocated_amount"])) == D("0")


def test_lowering_paid_below_the_allocated_total_is_refused(conn, env):
    """The refusal path, and it must leave NOTHING behind (M60).

    The recompute runs after the write, so the refused paid_amount physically
    exists in the transaction at the moment it is refused; `update-payment`
    rolls back before erroring. This test holds the connection the action wrote
    on, so an uncommitted survivor would be visible here — which is the only
    way the rollback is worth asserting.
    """
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(conn, env, "1000.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    bad = _update(conn, pe_id, paid_amount="200.00")
    assert is_error(bad)
    assert "paid_amount = allocations + deductions + unallocated" in _msg(bad)
    assert "300.00" in _msg(bad), "the refusal must name what is consumed"

    row = _pe(conn, pe_id)
    assert D(row["paid_amount"]) == D("1000.00"), "a refused update must not land"
    assert D(row["received_amount"]) == D("1000.00")
    assert D(row["unallocated_amount"]) == D("700.00")
    assert row["status"] == "draft"
    rows = _allocs(conn, pe_id)
    assert len(rows) == 1 and D(rows[0]["allocated_amount"]) == D("300.00")


def test_lowering_paid_to_exactly_the_allocated_total_is_allowed(conn, env):
    """Zero is a legal residual; only a negative one is refused."""
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(conn, env, "1000.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    res = _update(conn, pe_id, paid_amount="300.00")
    assert is_ok(res), res
    row = _pe(conn, pe_id)
    assert D(row["paid_amount"]) == D("300.00")
    assert D(row["unallocated_amount"]) == D("0")


def test_the_recompute_subtracts_deductions_as_well_as_allocations(conn, env):
    """Deductions are the non-cash slice of paid_amount and consume it too.

    `update-payment` has no `--deductions` flag, so the deduction rows are a
    constant across the edit — which is exactly why a recompute that read only
    allocations would look right on the common fixture and be wrong here.
    """
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(
        conn, env, "1000.00",
        [{"voucher_type": "sales_invoice", "voucher_id": si,
          "allocated_amount": "300.00"}],
        [{"account_id": env["discount"], "amount": "20.00",
          "type": "early_payment_discount"}])
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("680.00")

    res = _update(conn, pe_id, paid_amount="500.00")
    assert is_ok(res), res
    # 500 − 300 allocated − 20 deducted = 180
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("180.00")


def test_the_refusal_counts_deductions_toward_what_is_consumed(conn, env):
    """320.00 consumed (300 allocated + 20 deducted), so 300.00 is not enough."""
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(
        conn, env, "1000.00",
        [{"voucher_type": "sales_invoice", "voucher_id": si,
          "allocated_amount": "300.00"}],
        [{"account_id": env["discount"], "amount": "20.00",
          "type": "early_payment_discount"}])

    bad = _update(conn, pe_id, paid_amount="300.00")
    assert is_error(bad)
    assert "320.00" in _msg(bad), "the refusal must include the deducted total"
    assert D(_pe(conn, pe_id)["paid_amount"]) == D("1000.00")
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("680.00")


def test_the_residual_follows_paid_amount_with_no_allocations_at_all(conn, env):
    """The identity breaks with zero allocations too, so the recompute is
    unconditional rather than conditioned on having allocations."""
    pe_id = _add_payment(conn, env, "400.00")
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("400.00")

    res = _update(conn, pe_id, paid_amount="900.00")
    assert is_ok(res), res
    assert D(_pe(conn, pe_id)["unallocated_amount"]) == D("900.00")


def test_paid_amount_and_allocations_in_one_call_use_the_new_allocations(conn, env):
    """A legal edit the guard must NOT refuse.

    200.00 is below the CURRENT 300.00 allocation but not below the 100.00 the
    same call installs. The recompute and the guard therefore run once, after
    every branch, against the post-change detail rows.
    """
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(conn, env, "1000.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])

    res = _update(conn, pe_id, paid_amount="200.00", allocations=json.dumps([
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "100.00"}]))
    assert is_ok(res), res
    assert res["updated_fields"] == ["paid_amount", "allocations"]
    row = _pe(conn, pe_id)
    assert D(row["paid_amount"]) == D("200.00")
    assert D(row["unallocated_amount"]) == D("100.00")
    assert D(_allocs(conn, pe_id)[0]["allocated_amount"]) == D("100.00")


def test_submitted_payment_cannot_be_updated(conn, env):
    si = seed_sales_invoice(conn, env, "1000.00")
    pe_id = _add_payment(conn, env, "300.00", [
        {"voucher_type": "sales_invoice", "voucher_id": si,
         "allocated_amount": "300.00"}])
    assert is_ok(call_action(pay.submit_payment, conn, ns(payment_entry_id=pe_id)))

    bad = _update(conn, pe_id, paid_amount="900.00")
    assert is_error(bad)
    assert "draft" in _msg(bad)
    row = _pe(conn, pe_id)
    assert D(row["paid_amount"]) == D("300.00"), "a refused update must not land"
    assert row["status"] == "submitted"


def test_non_positive_paid_amount_is_refused(conn, env):
    pe_id = _add_payment(conn, env, "400.00")
    bad = _update(conn, pe_id, paid_amount="0")
    assert is_error(bad)
    assert "> 0" in _msg(bad)
    assert D(_pe(conn, pe_id)["paid_amount"]) == D("400.00")


def test_an_update_with_no_fields_is_refused(conn, env):
    pe_id = _add_payment(conn, env, "400.00")
    bad = _update(conn, pe_id)
    assert is_error(bad)
    assert "No fields to update" in _msg(bad)
