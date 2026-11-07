"""Pure posting lines for opening stock, counts and write-offs.

Inputs are engine-valued Move/Pair objects, not model arguments. The caller must
resolve and qualify inventory/counter accounts for the company, role and frozen
state; this helper cannot verify account membership, book dates, approval, stock
availability, read sets or transaction authority. It calculates no stock cost,
reads/writes nothing, registers no contract, and is not a cancellation planner.
"""
from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext

from erpclaw_core.common import Refusal
from erpclaw_core.money import MINOR_UNITS, MoneyError, canonical, to_amount
from .service import Move, Pair, parse_qty


class EntryPostingError(ValueError):
    """The supplied engine movement cannot form this original stock posting."""


_ROLE_BY_KIND = {"opening": "opening_balance", "count": "stock_adjustment", "write_off": "stock_write_off"}


def _account(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise EntryPostingError("A resolved account id must be a nonempty string.")
    return value


def entry_posting_lines(kind: str, moves: Sequence[Move], currency: str,
                        role_accounts: Mapping[str, str]) -> list[dict[str, str]]:
    """Return exact ledger lines, summed per account AND direction, never netted.

    Only opening/count/write_off are supported. Opening movements are incoming;
    write-offs are outgoing; a count may contain both. Already-rounded values
    must obey core's posted-amount domain. Zero-cost outgoing issues give no
    ledger line; incoming values must be positive under the receipt rule.

    Gains are listed inventory debits then counter credit; losses are counter
    debit then inventory credits. First appearance orders inventory accounts
    within each direction. Thus an offsetting count keeps all four gross lines.
    An empty count represents no adjustment and returns no posting lines.
    """
    if not isinstance(kind, str) or kind not in _ROLE_BY_KIND:
        raise EntryPostingError("Unsupported stock posting kind.")
    if not isinstance(currency, str) or currency not in MINOR_UNITS:
        raise EntryPostingError("Unknown company currency.")
    if not isinstance(role_accounts, Mapping) or _ROLE_BY_KIND[kind] not in role_accounts:
        raise EntryPostingError("The caller must supply the resolved counter-role account.")
    counter = _account(role_accounts[_ROLE_BY_KIND[kind]])
    if not isinstance(moves, (list, tuple)):
        raise EntryPostingError("Movements must be a list or tuple of valued Move objects.")
    if not moves and kind != "count":
        raise EntryPostingError("Opening stock and write-offs require a movement.")
    # Match stock's exact-arithmetic precision; core's amount bound is enforced
    # both on each movement and each summed posting line, rather than rounding.
    with localcontext() as context:
        context.prec = 60
        gains: dict[str, Decimal] = {}
        losses: dict[str, Decimal] = {}
        for move in moves:
            if not isinstance(move, Move) or not isinstance(move.pair, Pair):
                raise EntryPostingError("Each movement must contain a stock Move and Pair.")
            if (move.original is not None or move.reverses is not None or move.against is not None
                    or move.take_layer is not None or move.give_backs):
                raise EntryPostingError("Returns and reversal movements require their own posting policy.")
            if move.direction not in ("in", "out") or (kind == "opening" and move.direction != "in") or (
                    kind == "write_off" and move.direction != "out"):
                raise EntryPostingError("The movement direction is not allowed for this stock posting.")
            inventory = _account(move.pair.account)
            if inventory == counter:
                raise EntryPostingError("Inventory and counter-role accounts must be distinct.")
            if not isinstance(move.qty, Decimal) or not move.qty.is_finite() or move.qty <= 0:
                raise EntryPostingError("A valued movement must have a positive exact Decimal quantity.")
            # Reuse stock's quantity domain without computing a cost from it.
            try:
                parse_qty(move.qty)
            except Refusal as exc:
                raise EntryPostingError(exc.message) from None
            if not isinstance(move.value, Decimal):
                raise EntryPostingError("A valued movement must have an exact Decimal value.")
            try:
                value = to_amount(move.value, currency)
            except MoneyError as exc:
                raise EntryPostingError(str(exc)) from None
            if value == 0:
                if move.direction == "in":
                    raise EntryPostingError("An incoming movement must have a positive rounded value.")
                continue
            bucket = gains if move.direction == "in" else losses
            bucket[inventory] = bucket.get(inventory, Decimal(0)) + value

        def line(account: str, side: str, value: Decimal) -> dict[str, str]:
            try:
                checked = to_amount(value, currency)
            except MoneyError as exc:
                raise EntryPostingError(str(exc)) from None
            zero = canonical(Decimal(0), currency)
            return {"account": account, "debit": canonical(checked, currency) if side == "debit" else zero,
                    "credit": canonical(checked, currency) if side == "credit" else zero}

        result = [line(account, "debit", value) for account, value in gains.items()]
        if gains:
            result.append(line(counter, "credit", sum(gains.values(), Decimal(0))))
        if losses:
            result.append(line(counter, "debit", sum(losses.values(), Decimal(0))))
        result.extend(line(account, "credit", value) for account, value in losses.items())
        return result
