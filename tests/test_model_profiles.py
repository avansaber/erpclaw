"""Per-model profiles, offline: the real OpenAI SDK over a stubbed HTTP transport (no network, no key).

A profile is data: the model, its backend, the limits, the reasoning setting and the provider pins. Each backend
sends only its own extensions. Each test fails when its rule is removed. A key-shaped value is built at test time,
never written as a literal.
"""
from __future__ import annotations

import json
import secrets
import subprocess
from decimal import Decimal

import httpx
import pytest

from erpclaw_assistant import profiles
from erpclaw_assistant.model import OpenAICompatModel
from erpclaw_assistant.muse import PROVIDERS, LocalModel, MuseModel, model_from_env
from erpclaw_assistant.profiles import ProfileError, ProfileModel
from erpclaw_core.common import Refusal

OPENROUTER = "qwen3_5_9b_openrouter_primeintellect"
DEEPINFRA = "qwen3_5_9b_openrouter_deepinfra"
LMSTUDIO = "qwen3_5_9b_lmstudio"
DUMMY = """
[dummy_other_model]
backend = "openai-compatible"
model = "someone/another-model-7b"
base_url = "https://models.test/v1"
max_tokens = 1024
timeout_s = 30
temperature = 0
"""


def synthetic_key() -> str:
    return "".join(["sk", "-or-v1-"]) + secrets.token_hex(32)


def completion(content: str = "ok", calls: list | None = None, usage: dict | None = None,
               finish: str = "stop", metadata: dict | None = None) -> dict:
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    out = {"id": "gen-1", "object": "chat.completion", "created": 0, "model": "qwen/qwen3.5-9b",
           "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
           "usage": usage if usage is not None else {
               "prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160,
               "completion_tokens_details": {"reasoning_tokens": 25}, "cost": 0.0000123}}
    if metadata is not None:  # the fields OpenRouter adds at the reply's top level
        out.update(metadata)
    return out


# What OpenRouter adds to a reply, in the shape it returned on 2 Nov 2026 under this profile's pins (one live request
# with X-OpenRouter-Metadata enabled; the region left out): the provider at the top level, and the endpoint chosen.
SERVED = {"provider": "PrimeIntellect",
          "openrouter_metadata": {"requested": "qwen/qwen3.5-9b", "strategy": "direct",
                                  "summary": "available=1, selected=PrimeIntellect", "attempt": 1, "is_byok": False,
                                  "endpoints": {"total": 6, "available": [
                                      {"provider": "PrimeIntellect", "model": "qwen/qwen3.5-9b-20260310",
                                       "selected": True}]},
                                  "generation_time": 1184}}


class FakeServer:
    """Answers each POST with the next scripted reply: a dict (JSON 200) or an int (that status)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies: list[dict] = []
        self.headers: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen/qwen3.5-9b", "object": "model"}]})
        self.bodies.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        r = self.replies.pop(0) if self.replies else completion()
        if isinstance(r, int):
            return httpx.Response(r, json={"error": {"message": "x", "code": r}})
        if isinstance(r, str):  # a raw body, byte for byte
            return httpx.Response(200, content=r.encode(), headers={"content-type": "application/json"})
        return httpx.Response(200, json=r)


def model(name: str, fake: FakeServer, text: str | None = None, **kw) -> OpenAICompatModel:
    p = (profiles.load(text) if text else profiles.load())[name]
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return OpenAICompatModel(profile=p, http_client=client, api_key=kw.pop("api_key", synthetic_key()),
                             sleep=lambda s: None, **kw)


MSGS = [{"role": "system", "content": "You are the bookkeeper."},
        {"role": "user", "content": "invoice Acme Corporation for 5 copper wire"}]


# --- profiles are data -------------------------------------------------------------------------------------------

def test_each_shipped_profile_loads():
    shipped = profiles.load()
    assert {OPENROUTER, LMSTUDIO} <= set(shipped)
    assert shipped[OPENROUTER].backend == "openrouter" and shipped[OPENROUTER].hosted is True
    assert shipped[LMSTUDIO].backend == "lmstudio" and shipped[LMSTUDIO].hosted is False


def test_a_second_dummy_profile_loads_through_the_same_path_and_sends_neither_extension():
    fake = FakeServer()
    model("dummy_other_model", fake, DUMMY).complete(MSGS, [])
    [b] = fake.bodies
    assert b["model"] == "someone/another-model-7b" and b["max_tokens"] == 1024
    for absent in ("chat_template_kwargs", "provider", "reasoning"):
        assert absent not in b


@pytest.mark.parametrize("bad, says", [
    ('[p]\nbackend = "openrouter"\nmodel = "m"\nbase_url = "https://x.test/v1"\nmax_token = 5\n', "max_token"),
    ('[p]\nbackend = "lmstudio"\nmodel = "m"\nbase_url = "http://127.0.0.1:1234/v1"\n'
     'provider = { order = ["parasail"] }\n', "provider"),
    ('[p]\nbackend = "openrouter"\nmodel = "m"\nbase_url = "https://x.test/v1"\nhosted = false\n', "hosted"),
    ('[p]\nbackend = "llamafile"\nmodel = "m"\nbase_url = "https://x.test/v1"\n', "backend"),
    ('[p]\nbackend = "openrouter"\nmodel = "m"\nbase_url = "https://x.test/v1"\nhosted_books = "owner_consented"\n',
     "not available yet"),
])
def test_a_profile_the_engine_cannot_honour_is_refused_at_load(bad, says):
    with pytest.raises(ProfileError, match=says):
        profiles.load(bad)


def test_a_hosted_profile_with_no_books_policy_runs_as_seeded_only():
    p = profiles.load('[p]\nbackend = "openrouter"\nmodel = "m"\nbase_url = "https://x.test/v1"\n')["p"]
    assert p.hosted_books == "seeded_only"
    assert profiles.load()[OPENROUTER].hosted_books == "seeded_only"


def test_with_no_profile_named_today_s_routes_are_unchanged(monkeypatch):
    monkeypatch.delenv("ERPCLAW_MODEL_PROFILE", raising=False)
    monkeypatch.setenv("ERPCLAW_MODEL_PROVIDER", "openai-compatible")
    m = model_from_env()
    assert isinstance(m, LocalModel) and m.describe()["provider"] == "openai-compatible"
    assert m._inner.backend == "lmstudio" and m._inner.hosted is False


def test_a_named_profile_is_chosen_through_the_profile_provider(monkeypatch):
    monkeypatch.setenv("ERPCLAW_MODEL_PROVIDER", "profile")
    monkeypatch.setenv("ERPCLAW_MODEL_PROFILE", LMSTUDIO)
    assert "profile" in PROVIDERS
    m = model_from_env()
    assert isinstance(m, ProfileModel)
    d = m.describe()
    assert (d["provider"], d["model"], d["profile"]) == ("lmstudio", "qwen3.5-9b-mlx", LMSTUDIO)


def test_a_profile_named_beside_another_provider_is_refused(monkeypatch):
    monkeypatch.setenv("ERPCLAW_MODEL_PROVIDER", "muse")
    monkeypatch.setenv("ERPCLAW_MODEL_PROFILE", LMSTUDIO)
    with pytest.raises(ProfileError, match="ERPCLAW_MODEL_PROVIDER"):
        model_from_env()


# --- each backend sends only its own extensions ------------------------------------------------------------------

def test_lm_studio_gets_its_thinking_flag_and_nothing_of_openrouter_s():
    fake = FakeServer()
    model(LMSTUDIO, fake).complete(MSGS, [])
    [b] = fake.bodies
    assert b["chat_template_kwargs"] == {"enable_thinking": False}
    assert "provider" not in b and "reasoning" not in b


def test_openrouter_gets_its_provider_and_reasoning_and_never_the_lm_studio_flag():
    fake = FakeServer(completion(metadata=SERVED))
    model(OPENROUTER, fake).complete(MSGS, [])
    [b] = fake.bodies
    assert "chat_template_kwargs" not in b
    assert "provider" in b and "reasoning" in b
    assert fake.headers[0].get("x-openrouter-metadata") == "enabled"


# --- the pins, each in the request body --------------------------------------------------------------------------

@pytest.fixture
def sent():
    fake = FakeServer(completion(metadata=SERVED))
    model(OPENROUTER, fake).complete(MSGS, [])
    return fake.bodies[0]


def test_the_provider_order_is_pinned_to_primeintellect(sent):
    assert sent["provider"]["order"] == ["primeintellect"]


def test_an_endpoint_that_would_drop_a_parameter_is_never_used(sent):
    assert sent["provider"]["require_parameters"] is True


def test_the_deepinfra_alternative_differs_only_in_its_order():
    shipped = profiles.load()
    alt, pinned = shipped[DEEPINFRA], shipped[OPENROUTER]
    assert alt.provider == {**pinned.provider, "order": ["deepinfra"]}
    assert (alt.model, alt.reasoning, alt.max_tokens, alt.temperature, alt.hosted) == \
        (pinned.model, pinned.reasoning, pinned.max_tokens, pinned.temperature, pinned.hosted)


def test_fallbacks_are_off(sent):
    assert sent["provider"]["allow_fallbacks"] is False


def test_providers_that_collect_data_are_excluded(sent):
    assert sent["provider"]["data_collection"] == "deny"


def test_only_zero_retention_endpoints_are_used(sent):
    assert sent["provider"]["zdr"] is True


def test_the_quantization_is_pinned_to_bf16(sent):
    assert sent["provider"]["quantizations"] == ["bf16"]


def test_reasoning_is_set_explicitly(sent):
    assert sent["reasoning"] == {"effort": "low"}


def test_every_reply_is_capped(sent):
    assert sent["max_tokens"] == 4096 and sent["temperature"] == 0 and sent["model"] == "qwen/qwen3.5-9b"


# --- what each request records -----------------------------------------------------------------------------------

def test_an_openrouter_reply_s_counts_cost_and_server_are_recorded():
    m = model(OPENROUTER, FakeServer(completion(metadata=SERVED)))
    m.complete(MSGS, [])
    [u] = m.usage
    assert (u["prompt_tokens"], u["completion_tokens"], u["reasoning_tokens"]) == (120, 40, 25)
    assert u["cost"] == Decimal("0.0000123") and u["served_by"] == "PrimeIntellect"
    assert u["served_model"] == "qwen/qwen3.5-9b-20260310"


def test_without_its_top_level_provider_the_server_is_the_endpoint_the_metadata_marks_selected():
    m = model(OPENROUTER, FakeServer(completion(metadata={"openrouter_metadata": SERVED["openrouter_metadata"]})))
    m.complete(MSGS, [])
    assert (m.usage[-1]["served_by"], m.usage[-1]["served_model"]) == ("PrimeIntellect", "qwen/qwen3.5-9b-20260310")


@pytest.mark.parametrize("extra", [None, {"openrouter_metadata": {"attempts": [{"provider": "Elsewhere"}]}},
                                   {"openrouter_metadata": {"endpoints": {"available": [{"provider": "Elsewhere"}]}}}],
                         ids=["no_fields", "an_attempts_list_openrouter_does_not_send", "no_endpoint_selected"])
def test_a_reply_that_names_no_server_records_none(extra):
    m = model(OPENROUTER, FakeServer(completion(metadata=extra)))
    m.complete(MSGS, [])
    assert (m.usage[-1]["served_by"], m.usage[-1]["served_model"]) == (None, None)


def test_the_cost_is_read_exactly_from_the_reply_never_through_a_float():
    body = json.dumps(completion(metadata=SERVED)).replace('"cost": 1.23e-05', '"cost": 0.00001234567890123456789')
    assert "0.00001234567890123456789" in body
    m = model(OPENROUTER, FakeServer(body))
    m.complete(MSGS, [])
    assert m.usage[-1]["cost"] == Decimal("0.00001234567890123456789")


def test_a_reply_without_cost_records_none_never_zero():
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    m = model(LMSTUDIO, FakeServer(completion(usage=usage)))
    m.complete(MSGS, [])
    [u] = m.usage
    assert u["cost"] is None and u["served_by"] is None and u["reasoning_tokens"] is None and u["served_model"] is None


def test_the_description_names_the_profile_s_settings_and_never_the_key():
    key = synthetic_key()
    m = ProfileModel(model(OPENROUTER, FakeServer(), api_key=key))
    d = m.describe()
    assert (d["provider"], d["model"], d["profile"]) == ("openrouter", "qwen/qwen3.5-9b", OPENROUTER)
    assert d["reasoning"] == {"effort": "low"} and d["max_tokens"] == 4096
    assert d["pins"] == {"order": ["primeintellect"], "allow_fallbacks": False, "data_collection": "deny", "zdr": True,
                         "quantizations": ["bf16"], "require_parameters": True}
    assert key not in repr(m) and key not in json.dumps(d) and key not in repr(m._inner)


# --- the key -----------------------------------------------------------------------------------------------------

def test_the_key_comes_from_its_keychain_item_and_is_never_an_argument(monkeypatch):
    key, seen = synthetic_key(), []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=key + "\n", stderr="")

    monkeypatch.delenv("ERPCLAW_OPENROUTER_API_KEY", raising=False)
    p = profiles.load()[OPENROUTER]
    assert profiles.read_key(p, run=run) == key
    assert seen == [["security", "find-generic-password", "-s", "erpclaw-v3-openrouter", "-w"]]
    assert all(key not in a for a in seen[0])


def test_the_environment_is_the_key_s_fallback(monkeypatch):
    key = synthetic_key()
    monkeypatch.setenv("ERPCLAW_OPENROUTER_API_KEY", key)
    p = profiles.load()[OPENROUTER]
    assert profiles.read_key(p, run=lambda *a, **k: subprocess.CompletedProcess(a, 44, "", "")) == key


def test_a_hosted_profile_with_no_key_is_refused(monkeypatch):
    monkeypatch.delenv("ERPCLAW_OPENROUTER_API_KEY", raising=False)
    p = profiles.load()[OPENROUTER]
    with pytest.raises(ProfileError, match="erpclaw-v3-openrouter"):
        OpenAICompatModel(profile=p, key_reader=lambda: None)


# --- errors ------------------------------------------------------------------------------------------------------

def test_a_reply_spent_on_reasoning_is_unfinished_never_an_answer():
    m = model(OPENROUTER, FakeServer(completion(content="", finish="length", metadata=SERVED)))
    r = m.complete(MSGS, [])
    assert r.text == "" and not r.calls and r.finish == "length"


def test_no_endpoint_matching_the_pins_is_said_as_such_and_not_retried():
    fake = FakeServer(404, completion())
    with pytest.raises(Refusal, match="No provider in this profile's pins can take this request "
                                      r"\(its data policy, quantization, or the tools the request needs\)") as e:
        model(OPENROUTER, fake).complete(MSGS, [])
    assert e.value.code == "model_refused" and len(fake.bodies) == 1


def test_a_busy_provider_is_retried_once():
    fake = FakeServer(429, completion(metadata=SERVED))
    model(OPENROUTER, fake).complete(MSGS, [])
    assert len(fake.bodies) == 2


def test_muse_keeps_its_own_route_untouched():
    assert MuseModel.provider == "muse"


# --- the eval report names a profile route as it is ---------------------------------------------------------------

def test_the_report_names_a_profile_its_backend_reasoning_and_pins_never_muse_s_wording():
    from erpclaw_cli.evals.run import _model_line, _settings_sentence

    d = ProfileModel(model(OPENROUTER, FakeServer())).describe()
    line, sentence = _model_line(d), _settings_sentence(d)
    assert f"profile `{OPENROUTER}`" in line and "openrouter" in line and "qwen/qwen3.5-9b" in line
    assert "effort" in sentence and "primeintellect" in sentence and "zdr" in sentence
    assert "Muse" not in line + sentence and "store false" not in line + sentence


def test_the_report_keeps_the_local_and_muse_wording_byte_for_byte():
    from erpclaw_cli.evals.run import _model_line, _settings_sentence

    local = {"provider": "openai-compatible", "model": "qwen3.5-9b-mlx", "base_url": "http://127.0.0.1:1234/v1",
             "thinking": "off"}
    assert _model_line(local) == ("Model `qwen3.5-9b-mlx` at `http://127.0.0.1:1234/v1`, thinking requested off "
                                  "(see the reasoning count below), ")
    assert _settings_sentence(local) == (". Thinking was requested off; a non-zero reasoning count means the server "
                                         "did not honour 'off'.")
    m = {"provider": "muse", "model": "muse-spark-1.3", "base_url": "https://api.meta.ai/v1",
         "reasoning_effort": "medium"}
    assert _model_line(m) == "Model `muse-spark-1.3` at `https://api.meta.ai/v1`, reasoning effort medium, store false, "
    assert _settings_sentence(m) == ". Reasoning effort medium; Muse reasons privately and reports only its token count."


# --- how many requests a backend may take at once, as profile data -----------------------------------------------

def test_max_parallel_is_profile_data_one_by_default_and_a_positive_integer():
    shipped = profiles.load()
    assert (shipped[LMSTUDIO].max_parallel, shipped[OPENROUTER].max_parallel) == (1, 8)
    assert profiles.load(DUMMY)["dummy_other_model"].max_parallel == 1
    for bad in ("0", "-2", '"8"', "true", "2.5"):
        with pytest.raises(ProfileError, match="max_parallel"):
            profiles.load(DUMMY + f"max_parallel = {bad}\n")
