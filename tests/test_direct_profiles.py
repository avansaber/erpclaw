"""Offline checks for staged provider profiles and the existing wire format."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from erpclaw_assistant import profiles
from erpclaw_assistant.model import OpenAICompatModel
from erpclaw_core.common import Refusal

STAGED = Path(profiles.__file__).with_name("profiles_staged.toml")
MESSAGES = [{"role": "system", "content": "You are the bookkeeper."},
            {"role": "user", "content": "Give a total."}]
TOOL = {"type": "function", "function": {"name": "total", "description": "Get a total",
        "parameters": {"type": "object", "properties": {}}}}
CONVERSATION = MESSAGES + [{"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "total", "arguments": "{}"}}]},
                           {"role": "tool", "tool_call_id": "call_1", "content": "5"}]
REPLY = {"id": "gen", "object": "chat.completion", "created": 0, "model": "m",
         "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}

# Raw bodies captured from the original client before the change.
GOLDEN = {}
GOLDEN[('qwen3_5_9b_lmstudio', 'empty')] = b'{"model":"qwen3.5-9b-mlx","messages":[{"role":"system","content":"You are the bookkeeper."},{"role":"user","content":"Give a total."}],"max_tokens":2048,"temperature":0,"tools":null,"chat_template_kwargs":{"enable_thinking":false}}'
GOLDEN[('qwen3_5_9b_lmstudio', 'tools')] = b'{"model":"qwen3.5-9b-mlx","messages":[{"role":"system","content":"You are the bookkeeper."},{"role":"user","content":"Give a total."},{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"total","arguments":"{}"}}]},{"role":"tool","tool_call_id":"call_1","content":"5"}],"max_tokens":2048,"temperature":0,"tools":[{"type":"function","function":{"name":"total","description":"Get a total","parameters":{"type":"object","properties":{}}}}],"chat_template_kwargs":{"enable_thinking":false}}'
GOLDEN[('qwen3_5_9b_openrouter_primeintellect', 'empty')] = b'{"model":"qwen/qwen3.5-9b","messages":[{"role":"system","content":"You are the bookkeeper."},{"role":"user","content":"Give a total."}],"max_tokens":4096,"temperature":0,"tools":null,"provider":{"order":["primeintellect"],"allow_fallbacks":false,"data_collection":"deny","zdr":true,"quantizations":["bf16"],"require_parameters":true},"reasoning":{"effort":"low"}}'
GOLDEN[('qwen3_5_9b_openrouter_primeintellect', 'tools')] = b'{"model":"qwen/qwen3.5-9b","messages":[{"role":"system","content":"You are the bookkeeper."},{"role":"user","content":"Give a total."},{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"total","arguments":"{}"}}]},{"role":"tool","tool_call_id":"call_1","content":"5"}],"max_tokens":4096,"temperature":0,"tools":[{"type":"function","function":{"name":"total","description":"Get a total","parameters":{"type":"object","properties":{}}}}],"provider":{"order":["primeintellect"],"allow_fallbacks":false,"data_collection":"deny","zdr":true,"quantizations":["bf16"],"require_parameters":true},"reasoning":{"effort":"low"}}'
GOLDEN[('qwen3_5_9b_openrouter_deepinfra', 'empty')] = b'{"model":"qwen/qwen3.5-9b","messages":[{"role":"system","content":"You are the bookkeeper."},{"role":"user","content":"Give a total."}],"max_tokens":4096,"temperature":0,"tools":null,"provider":{"order":["deepinfra"],"allow_fallbacks":false,"data_collection":"deny","zdr":true,"quantizations":["bf16"],"require_parameters":true},"reasoning":{"effort":"low"}}'
GOLDEN[('qwen3_5_9b_openrouter_deepinfra', 'tools')] = b'{"model":"qwen/qwen3.5-9b","messages":[{"role":"system","content":"You are the bookkeeper."},{"role":"user","content":"Give a total."},{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"total","arguments":"{}"}}]},{"role":"tool","tool_call_id":"call_1","content":"5"}],"max_tokens":4096,"temperature":0,"tools":[{"type":"function","function":{"name":"total","description":"Get a total","parameters":{"type":"object","properties":{}}}}],"provider":{"order":["deepinfra"],"allow_fallbacks":false,"data_collection":"deny","zdr":true,"quantizations":["bf16"],"require_parameters":true},"reasoning":{"effort":"low"}}'


def profile_text(backend="openai", extra="", *, name="sample"):
    key_item = {"openai": "erpclaw-openai", "claude": "erpclaw-anthropic", "openrouter": "erpclaw-v3-openrouter"}[backend]
    key_env = {"openai": "ERPCLAW_OPENAI_API_KEY", "claude": "ERPCLAW_ANTHROPIC_API_KEY", "openrouter": "ERPCLAW_OPENROUTER_API_KEY"}[backend]
    url = {"openai": "https://api.openai.com/v1", "claude": "https://api.anthropic.com", "openrouter": "https://openrouter.ai/api/v1"}[backend]
    provider_name = "anthropic" if backend == "claude" else backend
    terms = f'terms = "D1:{provider_name}:test-account"\n' if backend != "openrouter" else ""
    provider = ('provider = { order = ["example"], quantizations = ["example"], allow_fallbacks = false, '
                'data_collection = "deny", zdr = true, require_parameters = true }\n') if backend == "openrouter" else ""
    return (f'[{name}]\nbackend = "{backend}"\nmodel = "test-model"\nbase_url = "{url}"\nhosted = true\n'
            f'hosted_books = "seeded_only"\nkey_item = "{key_item}"\nkey_env = "{key_env}"\n'
            'max_tokens = 42\nmax_input_tokens = 100\ncontext_window = 200\ntimeout_s = 10\ntemperature = 0\n'
            'admission = "conservative_bound"\nconsent = "approved-test"\nparameters = {}\n' + terms + provider + extra)


@pytest.fixture
def confirmed(monkeypatch):
    monkeypatch.setattr(profiles, "CONFIRMED_TERMS", {("D1", "openai", "test-account"), ("D1", "anthropic", "test-account")})
    monkeypatch.setattr(profiles, "APPROVED_CONSENT", {"approved-test"})


def test_shared_profiles_are_the_original_bytes_and_staged_values_fail_closed():
    assert hashlib.sha256(profiles.PROFILES_FILE.read_bytes()).hexdigest() == "f4f00ffe11a6a2c595f7dd077c7d6fc64504f7e4014ebc2021f8e2be58005ef6"
    assert len(profiles.load()) == 3
    with pytest.raises(profiles.ProfileError):
        profiles.load(STAGED.read_text())


@pytest.mark.parametrize("name,label", sorted(GOLDEN))
def test_qwen_wire_bodies_are_frozen(name, label):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=REPLY)
    m = OpenAICompatModel(profile=profiles.load()[name], api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    m.complete(MESSAGES if label == "empty" else CONVERSATION, [] if label == "empty" else [TOOL])
    assert requests[0].content == GOLDEN[name, label]
    assert requests[0].headers.get("x-openrouter-metadata") == ("enabled" if "openrouter" in name else None)


@pytest.mark.parametrize("backend", ["openai", "claude", "openrouter"])
def test_synthetic_confirmed_profile_loads_with_conservative_admission(confirmed, backend):
    p = profiles.load(profile_text(backend))["sample"]
    assert p.hosted and p.admission == "conservative_bound"


@pytest.mark.parametrize("backend", ["openai", "claude"])
@pytest.mark.parametrize("mutation", [lambda s: s.replace('hosted = true', 'hosted = false'),
                                      lambda s: s.replace('terms = "D1:', 'terms = "bad:'),
                                      lambda s: s.replace('terms = "D1:', 'terms = "D2:'),
                                      lambda s: '\n'.join(line for line in s.splitlines() if not line.startswith('terms ='))])
def test_direct_guards_reject_mutated_profiles(confirmed, backend, mutation):
    with pytest.raises(profiles.ProfileError):
        profiles.load(mutation(profile_text(backend)))


@pytest.mark.parametrize("field", ["model", "base_url", "hosted", "hosted_books", "key_item", "key_env", "max_tokens", "max_input_tokens", "context_window", "timeout_s", "temperature", "consent", "parameters", "provider"])
def test_new_openrouter_requires_each_field(confirmed, field):
    text = '\n'.join(line for line in profile_text("openrouter").splitlines() if not line.startswith(field + " ="))
    with pytest.raises(profiles.ProfileError):
        profiles.load(text)


@pytest.mark.parametrize("change", ['allow_fallbacks = true', 'data_collection = "allow"', 'zdr = false', 'require_parameters = false'])
def test_new_openrouter_requires_all_safety_pins(confirmed, change):
    field = change.split(' = ')[0]
    originals = {"allow_fallbacks": "allow_fallbacks = false", "data_collection": 'data_collection = "deny"', "zdr": "zdr = true", "require_parameters": "require_parameters = true"}
    with pytest.raises(profiles.ProfileError):
        profiles.load(profile_text("openrouter").replace(originals[field], change))


def test_claude_fails_closed_without_compatible_client(confirmed, monkeypatch):
    p = profiles.load(profile_text("claude"))["sample"]
    monkeypatch.setattr(profiles, "load", lambda: {"sample": p})
    monkeypatch.setenv("ERPCLAW_MODEL_PROFILE", "sample")
    with pytest.raises(profiles.ProfileError, match="claude"):
        profiles.from_env()


def test_direct_usage_has_no_invented_cost_and_served_secrets_are_screened(confirmed):
    p = profiles.load(profile_text())["sample"]
    secret = "sk-" + "x" * 60
    reply = dict(REPLY, provider=secret, usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16, "cost": 5})
    m = OpenAICompatModel(profile=p, api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply))))
    with pytest.raises(Refusal) as exc:
        m.complete(MESSAGES, [])
    assert exc.value.code == "credential_refused"
    assert secret not in repr(m.usage)
    assert m.usage[0]["cost"] is None and m.usage[0]["prompt_tokens"] == 12


def test_each_staged_table_is_refused_until_values_are_confirmed(confirmed):
    import tomllib
    text = STAGED.read_text()
    tables = tomllib.loads(text)
    assert set(tables) == {"openai_direct", "claude_direct", "openai_openrouter", "claude_openrouter"}
    for name, table in tables.items():
        section = "[" + name + "]" + text.split("[" + name + "]", 1)[1].split("\n[", 1)[0]
        with pytest.raises(profiles.ProfileError):
            profiles.load(section)
        section = section.replace('"UNCONFIRMED_MODEL"', '"test-model"')
        section = section.replace('"UNCONFIRMED_LIMIT"', '100')
        section = section.replace('"UNCONFIRMED_TIMEOUT"', '10')
        section = section.replace('"UNCONFIRMED_TEMPERATURE"', '0')
        section = section.replace('"UNCONFIRMED_CONSENT"', '"approved-test"')
        provider_name = "anthropic" if table["backend"] == "claude" else table["backend"]
        section = section.replace('"UNCONFIRMED_TERMS"', f'"D1:{provider_name}:test-account"')
        section = section.replace('"UNCONFIRMED_PROVIDER"', '"example"')
        section = section.replace('"UNCONFIRMED_QUANTIZATION"', '"example"')
        assert profiles.load(section)[name].admission == "conservative_bound"


def test_runtime_selection_never_reads_staged_file(monkeypatch):
    monkeypatch.setenv("ERPCLAW_MODEL_PROFILE", "openai_direct")
    with pytest.raises(profiles.ProfileError):
        profiles.from_env()


@pytest.mark.parametrize("backend", ["openai", "claude", "openrouter"])
@pytest.mark.parametrize("field,value", [("max_tokens", "0"), ("max_tokens", "true"), ("max_input_tokens", "0"),
                                        ("context_window", "-1"), ("timeout_s", "0"), ("temperature", "true")])
def test_new_profile_number_types_and_ranges(confirmed, backend, field, value):
    text = profile_text(backend)
    text = '\n'.join((field + " = " + value) if line.startswith(field + " =") else line for line in text.splitlines())
    with pytest.raises(profiles.ProfileError):
        profiles.load(text)


def test_key_precedence_and_provider_isolation(confirmed, monkeypatch):
    openai = profiles.load(profile_text("openai"))["sample"]
    claude = profiles.load(profile_text("claude"))["sample"]
    router = profiles.load(profile_text("openrouter"))["sample"]
    assert len({openai.key_item, claude.key_item, router.key_item}) == 3
    assert len({openai.key_env, claude.key_env, router.key_env, "OPENAI_API_KEY"}) == 4
    monkeypatch.setenv(openai.key_env, "environment-value")
    monkeypatch.delenv(claude.key_env, raising=False)
    class Result:
        returncode = 0
        stdout = "keychain-value"
    assert profiles.read_key(openai, run=lambda *a, **k: Result()) == "keychain-value"
    monkeypatch.setattr(profiles.hosted, "keychain", lambda *args: None)
    assert profiles.read_key(openai) == "environment-value"
    assert profiles.read_key(claude) is None
    with pytest.raises(profiles.ProfileError, match=openai.key_item) as exc:
        OpenAICompatModel(profile=openai, key_reader=lambda: None)
    assert "environment-value" not in str(exc.value)


def test_served_identity_is_retained_only_after_the_reply_screen(confirmed):
    p = profiles.load(profile_text())["sample"]
    reply = dict(REPLY, model="served-model", provider="served-provider", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
    m = OpenAICompatModel(profile=p, api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply))))
    m.complete(MESSAGES, [])
    assert (m.usage[0]["served_by"], m.usage[0]["served_model"], m.usage[0]["cost"]) == ("served-provider", "served-model", None)


def test_router_environment_key_alone_cannot_bypass_new_route_validation():
    text = profile_text("openrouter")
    text = '\n'.join(line for line in text.splitlines()
                     if not line.startswith(("key_item =", "consent =", "parameters =")))
    with pytest.raises(profiles.ProfileError):
        profiles.load(text)


def test_malformed_usage_count_cannot_store_a_credential(confirmed):
    p = profiles.load(profile_text())["sample"]
    secret = "sk-" + "x" * 60
    reply = dict(REPLY, usage={"prompt_tokens": secret, "completion_tokens": 4, "total_tokens": 4})
    m = OpenAICompatModel(profile=p, api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply))))
    m.complete(MESSAGES, [])
    assert m.usage[0]["prompt_tokens"] is None
    assert m.usage[0]["completion_tokens"] == 4 and secret not in repr(m.usage)
