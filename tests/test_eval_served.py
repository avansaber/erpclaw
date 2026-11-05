"""Which server and model answered a hosted eval, in the line the run ledger reads.

On a hosted route both eval runners print one `eval-served:` line, just before their tokens line: every value of the
reply's named provider and served model across the run's requests, with how many requests named it, and `none` for
the requests that named nothing. A route that sends nothing out prints no such line. Quantization is not in it: the
reply does not report one, so it stays the pin the request asked for.
"""
from __future__ import annotations

import pytest

from conftest import PG_URL
from erpclaw_assistant.model import Reply, ScriptedModel, call
from erpclaw_cli.evals import honesty as h
from erpclaw_cli.evals import run
from erpclaw_cli.evals.run import served_line

SERVED = {"served_by": "PrimeIntellect", "served_model": "qwen/qwen3.5-9b-20260310"}


class _Hosted:
    hosted = True

    def __init__(self, usage):
        self.usage = usage


@pytest.mark.parametrize("usage, line", [
    ([SERVED, SERVED, SERVED],
     "eval-served: provider PrimeIntellect ×3; model qwen/qwen3.5-9b-20260310 ×3"),
    ([SERVED, {"served_by": None, "served_model": None}, SERVED, {"statuses": [503]}],
     "eval-served: provider PrimeIntellect ×2, none ×2; model qwen/qwen3.5-9b-20260310 ×2, none ×2"),
    ([{"served_by": "B", "served_model": "m-1"}, {"served_by": "A", "served_model": "m-1"},
      {"served_by": "B", "served_model": "m-2"}, {}],
     "eval-served: provider B ×2, A ×1, none ×1; model m-1 ×2, m-2 ×1, none ×1"),
    ([{"served_by": "PrimeIntellect", "served_model": None}],
     "eval-served: provider PrimeIntellect ×1; model none ×1"),
    ([], "eval-served: no requests"),
], ids=["one_server", "requests_that_named_none", "most_first_then_by_name_none_last", "provider_without_model",
        "no_requests"])
def test_the_served_line_counts_every_value_each_request_named(usage, line):
    assert served_line(_Hosted(usage)) == line


def test_a_route_that_sends_nothing_out_prints_no_served_line():
    class _LocalRoute:
        hosted = False
        usage = [SERVED]

    assert served_line(_LocalRoute()) is None
    assert served_line(ScriptedModel([])) is None  # a model that does not say it is hosted is not


class _Served(ScriptedModel):
    hosted = True
    usage = [dict(SERVED, prompt_tokens=7, completion_tokens=3, reasoning_tokens=2, cost=None, native_calls=1),
             {"served_by": None, "served_model": None, "prompt_tokens": 1, "completion_tokens": 1,
              "reasoning_tokens": None, "cost": None, "native_calls": 0}]

    def check_ready(self):
        return None

    def describe(self):
        return {"provider": "openrouter", "model": "qwen/qwen3.5-9b", "profile": "test", "base_url": "hosted",
                "hosted": self.hosted, "reasoning": {}, "pins": {}, "max_tokens": 4096, "temperature": 0,
                "max_parallel": 1}  # a profile's description, as ProfileModel.describe gives it


class _Unhosted(_Served):
    hosted = False


def _run_both(monkeypatch, tmp_path, capsys, model_class):
    import erpclaw_assistant.muse as muse

    monkeypatch.delenv("ERPCLAW_ALLOW_OWNER_RUNTIME", raising=False)
    monkeypatch.setenv("ERPCLAW_EVAL_PG_URL", PG_URL)
    monkeypatch.setenv("ERPCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ERPCLAW_MODEL_PROVIDER", "openai-compatible")
    outs = []
    monkeypatch.setattr(muse, "model_from_env", lambda: model_class([Reply("", [call(
        "create_invoice", customer="Acme Corporation", items=[{"item": "Consulting", "quantity": 3}])])]))
    h.main(["--k", "1", "--only", "h-001"])
    outs.append(capsys.readouterr().out.splitlines())
    monkeypatch.setattr(muse, "model_from_env", lambda: model_class([]))
    run.main(k=1, only=[run.CASES[0].id])
    outs.append(capsys.readouterr().out.splitlines())
    return outs


def test_both_runners_print_the_served_line_once_just_before_the_tokens_line_on_a_hosted_route(
        monkeypatch, tmp_path, capsys):
    for out in _run_both(monkeypatch, tmp_path, capsys, _Served):
        assert [line for line in out if line.startswith("eval-served:")] == [
            "eval-served: provider PrimeIntellect ×1, none ×1; model qwen/qwen3.5-9b-20260310 ×1, none ×1"]
        tokens = [i for i, line in enumerate(out) if line.startswith("eval-tokens:")]
        assert len(tokens) == 1 and out[tokens[0] - 1].startswith("eval-served:")  # just before it, wherever it is


def test_neither_runner_prints_a_served_line_on_a_route_that_sends_nothing_out(monkeypatch, tmp_path, capsys):
    for out in _run_both(monkeypatch, tmp_path, capsys, _Unhosted):
        assert not any(line.startswith("eval-served:") for line in out)
        assert sum(line.startswith("eval-tokens:") for line in out) == 1
