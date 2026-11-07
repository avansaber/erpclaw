# SPDX-License-Identifier: GPL-3.0-only
"""stock/2 producer database fixtures; requires the ordinary guarded PostgreSQL suite.

This module is integration-test source. Pure-model checks do not qualify these
cases, their real approvals, database effects or the consumer acknowledgement.
"""
import json

import pytest

from erpclaw_core.common import Refusal
from service_plugins import install
from stock_setup_fixtures import FixtureError, SERVICES, all_cases, load_text
from stock_setup_producer import SetupBooks, consumer


@pytest.mark.parametrize("case", all_cases(), ids=lambda case: case["name"])
def test_every_setup_fixture_against_the_producer(engine, monkeypatch, case):
    books = SetupBooks(engine, case)
    install(monkeypatch, engine, consumer())
    before = books.snapshot()
    if "refusal" in case:
        with pytest.raises(Refusal) as caught:
            books.call_setup()
        refusal = caught.value
        expected = books.resolve(case["refusal"])
        assert refusal.code == expected["code"]
        assert refusal.data == {key: value for key, value in expected.items() if key != "code"}
        assert refusal.options == [choice["label"] for choice in expected.get("choices", [])]
        assert refusal.capability is None
        assert books.proposal is None  # matching preview refused before a card could be persisted
        books.assert_effects(before, books.snapshot())
    else:
        assert books.call_setup() == books.resolve(case["output"])
        books.assert_effects(before, books.snapshot())
    after_call = books.snapshot()
    books.assert_after()
    assert books.snapshot() == after_call  # after-state reads and verification must also be read-only


def test_setup_fixture_coverage_is_exact():
    cases = all_cases()
    assert len(cases) == 22
    assert len({case["name"] for case in cases}) == 22
    assert {case["service"] for case in cases} == SERVICES
    assert {case["books"]["currency"] for case in cases} == {"USD", "JPY", "KWD"}


@pytest.mark.parametrize("number", ["1", "1.5", "1e2", "NaN", "Infinity"])
def test_setup_loader_rejects_json_numbers(number):
    text = json.dumps(all_cases()[0])
    with pytest.raises(FixtureError):
        load_text(text[:-1] + ', "unexpected_number": ' + number + '}')


def test_setup_loader_requires_stock_two_and_every_field():
    case = all_cases()[0]
    for field in ("books", "before", "input", "after", "service", "name", "contract"):
        changed = dict(case)
        del changed[field]
        with pytest.raises(FixtureError):
            load_text(json.dumps(changed))
    for contract in ("stock/1", "stock/next"):
        with pytest.raises(FixtureError):
            load_text(json.dumps({**case, "contract": contract}))
