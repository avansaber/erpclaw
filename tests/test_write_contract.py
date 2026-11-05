"""A write built as a WriteContract says whether it moves money, and why it takes no date when it takes none: the card's
date line is the contract's own sentence, never text from the input the model built. A write built as a plain Contract
(an older plugin) still loads, counts as moving money, and gets one generic sentence."""
from __future__ import annotations

import dataclasses

import pytest

from erpclaw_core import approvals
from erpclaw_core.common import company_ctx
from erpclaw_core.contracts import Contract, Input, Preview, Shown, WriteContract, moves_money
from erpclaw_core.manifest import FIRST_PARTY_DISTRIBUTIONS
from erpclaw_core.plugins import REGISTRY, PluginRefused, Registry
from test_plugins import plug

DRAFT = "No date applies: cancelling a draft changes no books."
GENERIC = "No date applies: this change posts nothing to the books."


class _Dateless(Input):
    note: str = ""
    no_date_reason: str = ""  # a field the model fills: the card must never read it

    def takes_no_date(self, ctx) -> bool:
        return True


class _Dated(Input):
    note: str = ""


def _preview(ctx, inp) -> Preview:
    sh = Shown(ctx.currency)
    sh.title("Planted settings")
    return Preview(sh.card(), "t")


WRITE_PARTS = {"preview": _preview, "commit": lambda ctx, inp, key: None, "find_by_operation_key": lambda ctx, key: None,
               "read_set": lambda ctx, inp: []}  # what every write needs to load


def _write(name="set_things", reason=None, money=False, inp=_Dateless, cls=WriteContract) -> Contract:
    if cls is Contract:
        return Contract(name, "extra", "write", "Planted.", inp, **WRITE_PARTS)
    return WriteContract(name, "extra", "write", "Planted.", inp, **WRITE_PARTS, moves_money=money,
                         no_date_reason=reason)


def _load(c: Contract) -> None:
    Registry().fill([REGISTRY.plugins[n] for n in REGISTRY.order] + [plug("extra", contracts=[c])])


def _line(engine, c: Contract, inp) -> list[str]:
    with engine.connect() as conn:
        card = approvals._preview(company_ctx(conn), c, inp, date_source={"not_applicable": True})
    return card["lines"][-1]


# --- the contract --------------------------------------------------------------------------------------------------

def test_a_write_contract_cannot_be_made_without_saying_whether_it_moves_money():
    with pytest.raises(TypeError):
        WriteContract("x", "extra", "write", "Planted.", _Dated, **WRITE_PARTS)


def test_contracts_own_fields_are_unchanged_and_the_two_declarations_are_keyword_only():
    assert "moves_money" not in {f.name for f in dataclasses.fields(Contract)}
    new = [f for f in dataclasses.fields(WriteContract) if f.name in ("moves_money", "no_date_reason")]
    assert [f.name for f in new] == ["moves_money", "no_date_reason"] and all(f.kw_only for f in new)


def test_an_older_write_counts_as_moving_money():
    assert moves_money(_write(cls=Contract, inp=_Dated)) is True
    assert moves_money(_write(money=False, inp=_Dated)) is False


# --- checked at load -----------------------------------------------------------------------------------------------

def test_a_well_formed_write_contract_loads():
    _load(_write(reason={"en": "these are the company's own settings, not books."}))
    _load(_write(money=True, inp=_Dated))


@pytest.mark.parametrize("reason, why", [
    (None, "takes no date but declares no no_date_reason"),
    ({}, "no no_date_reason sentence for en"),
    ({"en": "these settings changed 3 times."}, "holds a digit"),
    ({"en": "these are the company's own settings, not books"}, "ends without a full stop"),
    ({"en": "x" * 120 + "."}, "is longer than 120 characters"),
    ({"en": " "}, "is empty"),
])
def test_a_no_date_write_whose_reason_breaks_the_rules_is_refused_at_load(reason, why):
    with pytest.raises(PluginRefused, match=why):
        _load(_write(reason=reason))


def test_a_reason_on_a_write_that_always_takes_a_date_is_refused_at_load():
    with pytest.raises(PluginRefused, match="declares a no_date_reason, but its input always takes a date"):
        _load(_write(reason={"en": "nothing here is dated."}, inp=_Dated))


def test_an_older_write_with_no_reason_still_loads():
    _load(_write(cls=Contract))


def test_every_write_of_core_or_a_first_party_plugin_is_a_write_contract():
    """No first-party write sits on the older fallback by oversight; a write core registers itself counts too."""
    first_party = {n for n, p in REGISTRY.plugins.items() if n == "core" or p.manifest.distribution in
                   FIRST_PARTY_DISTRIBUTIONS}
    plain = [c.name for c in REGISTRY.contracts.values() if c.effect != "read"
             and REGISTRY.contract_plugin[c.name] in first_party and not isinstance(c, WriteContract)]
    assert plain == []


# --- the card's line -----------------------------------------------------------------------------------------------

def test_a_no_date_card_shows_its_own_contracts_sentence_never_the_inputs_text(engine):
    c = _write(reason={"en": "these are the company's own settings, not books."})
    inp = _Dateless(note="x", no_date_reason="the model wrote this.")
    assert _line(engine, c, inp) == ["Date source", "No date applies: these are the company's own settings, not books."]


def test_an_older_no_date_write_shows_the_generic_sentence_never_the_draft_cancels(engine):
    assert _line(engine, _write(cls=Contract), _Dateless()) == ["Date source", GENERIC]


def test_sales_draft_cancel_keeps_its_sentence():
    cancel = REGISTRY.contracts["cancel_invoice"]
    assert isinstance(cancel, WriteContract) and cancel.moves_money is True
    assert "No date applies: " + cancel.no_date_reason["en"] == DRAFT
