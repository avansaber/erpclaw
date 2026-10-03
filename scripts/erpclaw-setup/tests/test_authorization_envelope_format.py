"""Authorization envelope format: one canonical form, one digest.

Pure-function pins for the envelope argument canonicalisation and the
versioned envelope digest. No database is touched here.
"""
import importlib
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import setup_helpers  # noqa: E402  (binds erpclaw_lib to this tree)


@pytest.fixture
def ae():
    return importlib.import_module("erpclaw_lib.authorization_envelope")


V1 = ["--company-id", "c-1", "--amount", "100.10", "--posting-date",
      "2026-09-25", "--remarks", "Rent  Sept"]
V1_PAIRS = [["amount", "100.1"], ["company-id", "c-1"],
            ["posting-date", "2026-09-25"], ["remarks", "Rent  Sept"]]
V1_KEYWORDS = {"money": ("amount",), "bound_args": ("posting-date",)}
V1_DIGEST = ("e7f49ca0e5d735bffb9e4544c9423d27f4f40edcaf286d2845a310a0ecc8ed38")
EMPTY_DIGEST = ("4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945")
ITEMS_DIGEST = ("a9b82054f02d180003b33e6b54959fcaa2762f960c2e306baa3900384169ac57")


def _fields(**over):
    base = {"v": 2, "binding_digest": "a" * 64, "args_digest": "b" * 64,
            "issuer_id": "owner-1", "issued_route": "delegation",
            "reason_code": "month-end-rent", "reason_text": "September rent",
            "idempotency_key": "idem-1", "call_id": None,
            "issued_at": 1790000000000}
    base.update(over)
    return base


def test_golden_args_digest(ae):
    assert ae.canonical_pairs(V1, **V1_KEYWORDS) == V1_PAIRS
    assert ae.args_digest(V1, **V1_KEYWORDS) == V1_DIGEST
    assert ae.args_digest([]) == EMPTY_DIGEST


def test_forms_and_excluded_tokens_bind_the_same(ae):
    v2 = ["--remarks=Rent  Sept", "--posting_date", "2026-09-25", "--amount",
          "100.1", "--company_id=c-1", "--db-path", "/tmp/x.sqlite",
          "--user-confirmed", "--authorization-id", "auth-1"]
    assert ae.args_digest(v2, **V1_KEYWORDS) == V1_DIGEST


def test_tokenisation(ae):
    assert ae.canonical_pairs(
        ["--a", "--b", "--n", "-5", "--c=x=y"]) == [
        ["a", None], ["b", None], ["c", "x=y"], ["n", "-5"]]
    assert ae.canonical_pairs(["--a=b"]) == ae.canonical_pairs(["--a", "b"])


def test_json_keys_and_money_paths(ae):
    argv = ["--company-id", "c-1", "--items",
            '{"rate":"5.50","qty":2,"item":"i-1"}']
    keywords = {"json_args": ("items",),
                "money_json_paths": {"items": [("rate",)]}}
    assert ae.canonical_pairs(argv, **keywords) == [
        ["company-id", "c-1"],
        ["items", '{"item":"i-1","qty":2,"rate":"5.5"}']]
    assert ae.args_digest(argv, **keywords) == ITEMS_DIGEST
    reordered = ["--company-id", "c-1", "--items",
                 '{"item":"i-1","qty":2,"rate":"5.5"}']
    assert ae.args_digest(reordered, **keywords) == ITEMS_DIGEST
    listed = ['[{"item":"i-1","rate":5.5,"qty":2}]']
    assert ae.canonical_pairs(
        ["--items", listed[0]], json_args=("items",),
        money_json_paths={"items": [("*", "rate")]}) == [
        ["items", '[{"item":"i-1","qty":2,"rate":"5.5"}]']]


REFUSAL_CASES = [
    ("money-exponent", lambda ae: ae.normalise_money("1E+2")),
    ("money-nan", lambda ae: ae.normalise_money("NaN")),
    ("money-infinity", lambda ae: ae.normalise_money("Infinity")),
    ("money-empty", lambda ae: ae.normalise_money("")),
    ("money-space", lambda ae: ae.normalise_money(" 100")),
    ("money-underscore", lambda ae: ae.normalise_money("1_000")),
    ("argv-float", lambda ae: ae.canonical_pairs([100.1])),
    ("bound-missing", lambda ae: ae.canonical_pairs(
        [t for t in V1 if t != "2026-09-25"], **V1_KEYWORDS)),
    ("leading-stray", lambda ae: ae.canonical_pairs(["stray"] + V1)),
    ("user-confirmed-value", lambda ae: ae.canonical_pairs(
        ["--user-confirmed", "yes"])),
    ("json-duplicate-key", lambda ae: ae.canonical_pairs(
        ["--items", '{"a":1,"a":2}'], json_args=("items",))),
    ("json-nan", lambda ae: ae.canonical_pairs(
        ["--items", '{"a":NaN}'], json_args=("items",))),
    ("json-money-exponent", lambda ae: ae.canonical_pairs(
        ["--items", '{"rate":1E+2}'], json_args=("items",),
        money_json_paths={"items": [("rate",)]})),
    ("json-money-object", lambda ae: ae.canonical_pairs(
        ["--items", '{"rate":{}}'], json_args=("items",),
        money_json_paths={"items": [("rate",)]})),
    ("json-truncated-object", lambda ae: ae.canonical_pairs(
        ["--items", '{"a":'], json_args=("items",))),
    ("json-truncated-list", lambda ae: ae.canonical_pairs(
        ["--items", '[1,'], json_args=("items",))),
]


@pytest.mark.parametrize("name,case", [c for c in REFUSAL_CASES],
                         ids=[c[0] for c in REFUSAL_CASES])
def test_refusals(ae, name, case):
    with pytest.raises(ValueError) as excinfo:
        case(ae)
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)


def test_unknown_token_changes_the_digest(ae):
    assert ae.args_digest(V1 + ["--extra", "x"], **V1_KEYWORDS) != V1_DIGEST
    assert ae.normalise_money("-0.00") == "0"
    assert ae.normalise_money("007.50") == "7.5"
    assert ae.normalise_money("-12.340") == "-12.34"


def test_golden_envelope_digest(ae):
    assert ae.envelope_digest(_fields()) == (
        "d32424a2190c4f9a62e5c545f001640c965e19ecef68e28cabdf37f3054da4e1")
    assert ae.envelope_digest(
        _fields(issued_route="staged_unattested")) == (
        "6020fe73e9519835a3241335613ca9b358bc3210608020f0950dee188774d21d")


ENVELOPE_REFUSAL_CASES = [
    ("version-1", {"v": 1}),
    ("version-3", {"v": 3}),
    ("version-bool", {"v": True}),
    ("version-text", {"v": "2"}),
    ("route-other", {"issued_route": "other"}),
    ("reason-long", {"reason_text": "x" * 281}),
    ("reason-empty", {"reason_text": ""}),
    ("issued-negative", {"issued_at": -1}),
    ("issued-bool", {"issued_at": True}),
]


@pytest.mark.parametrize("name,over", ENVELOPE_REFUSAL_CASES,
                         ids=[c[0] for c in ENVELOPE_REFUSAL_CASES])
def test_envelope_refusals(ae, name, over):
    with pytest.raises(ValueError) as excinfo:
        ae.envelope_digest(_fields(**over))
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)


def test_envelope_key_set_refusals(ae):
    dropped = _fields()
    del dropped["call_id"]
    with pytest.raises(ValueError) as excinfo:
        ae.envelope_digest(dropped)
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
    extra = _fields(other="x")
    with pytest.raises(ValueError) as excinfo:
        ae.envelope_digest(extra)
    assert excinfo.value.args == ("AUTHORIZATION_INPUT_INVALID",)
