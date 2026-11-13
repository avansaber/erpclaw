"""Parameter declarations and single-attempt hosted requests."""
import json

import httpx
import pytest

from erpclaw_assistant import profiles
from erpclaw_assistant.model import OpenAICompatModel
from erpclaw_core.common import Refusal
from test_direct_profiles import MESSAGES, REPLY, TOOL, confirmed, profile_text


def make(confirmed, declaration, backend="openai", replies=None):
    text = profile_text(backend).replace('parameters = {}', 'parameters = ' + declaration)
    profile = profiles.load(text)["sample"]
    requests, queue = [], list(replies or [httpx.Response(200, json=REPLY)])
    def handle(request):
        requests.append(request)
        return queue.pop(0)
    m = OpenAICompatModel(profile=profile, api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handle)), sleep=lambda _: None)
    return m, requests, queue


def test_omits_temperature_and_renames_only_the_reply_cap(confirmed):
    m, requests, _ = make(confirmed, '{ omit = ["temperature"], rename = { max_tokens = "max_completion_tokens" } }')
    m.complete(MESSAGES, [TOOL])
    body = json.loads(requests[0].content)
    assert "temperature" not in body and "max_tokens" not in body
    assert body["max_completion_tokens"] == 42 and body["tools"] == [TOOL]


@pytest.mark.parametrize("declaration", ['{ omit = ["messages"] }', '{ omit = ["top_p"] }',
                                         '{ rename = { model = "x" } }', '{ rename = { max_tokens = "anything" } }',
                                         '{ unknown = true }', '{ omit = "temperature" }'])
def test_invalid_declarations_are_refused(confirmed, declaration):
    with pytest.raises(profiles.ProfileError):
        make(confirmed, declaration)


@pytest.mark.parametrize("param,expected", [("temperature", "temperature"), (None, None), ("model", None), ("not a parameter", None)])
def test_parameter_rejection_is_single_attempt_without_echo(confirmed, param, expected):
    data = {"error": {"message": "untrusted text", "param": param}}
    m, requests, queue = make(confirmed, '{ omit = ["temperature"] }', replies=[httpx.Response(400, json=data), httpx.Response(200, json=REPLY)])
    with pytest.raises(Refusal) as exc:
        m.complete(MESSAGES, [TOOL])
    text = str(exc.value)
    assert "no second request" in text.lower() and "untrusted text" not in text
    assert ("temperature" in text) == (expected is not None)
    assert len(requests) == len(queue) == 1


def test_transient_error_does_not_consume_the_next_response(confirmed):
    m, requests, queue = make(confirmed, '{}', replies=[httpx.Response(503, json={"error": {"message": "busy"}}), httpx.Response(400, json={"error": {"param": "temperature"}})])
    with pytest.raises(Refusal):
        m.complete(MESSAGES, [TOOL])
    assert len(requests) == len(queue) == 1


def test_secret_in_error_body_is_not_echoed_or_recorded(confirmed):
    secret = "sk-" + "x" * 60
    m, requests, queue = make(confirmed, '{ omit = ["temperature"] }', replies=[httpx.Response(400, json={"error": {"param": "temperature", "message": secret}}), httpx.Response(200, json=REPLY)])
    with pytest.raises(Refusal) as exc:
        m.complete(MESSAGES, [])
    assert secret not in str(exc.value) and secret not in repr(m.usage)
    assert "temperature" not in str(exc.value)
    assert m.usage[-1]["refused"] == "openai_key"
    assert len(requests) == len(queue) == 1


def test_new_openrouter_keeps_safety_pins_and_tools_on_rejection(confirmed):
    m, requests, queue = make(confirmed, '{ omit = ["temperature"] }', backend="openrouter", replies=[httpx.Response(400, json={"error": {"param": "temperature"}}), httpx.Response(200, json=REPLY)])
    with pytest.raises(Refusal):
        m.complete(MESSAGES, [TOOL])
    body = json.loads(requests[0].content)
    assert body["tools"] == [TOOL]
    assert body["provider"]["require_parameters"] is True
    assert body["provider"]["allow_fallbacks"] is False
    assert body["provider"]["data_collection"] == "deny" and body["provider"]["zdr"] is True
    assert len(requests) == len(queue) == 1


def test_timeout_on_new_route_records_failure_even_when_turn_budget_expires(confirmed):
    from erpclaw_assistant.model import turn_budget

    profile = profiles.load(profile_text())["sample"]
    clock = {"expired": False}
    requests = []
    def timeout(request):
        requests.append(request)
        clock["expired"] = True
        raise httpx.ReadTimeout("timeout", request=request)
    m = OpenAICompatModel(profile=profile, api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(timeout)))
    with pytest.raises(Refusal) as exc:
        with turn_budget(lambda: 0 if clock["expired"] else 10):
            m.complete(MESSAGES, [])
    assert exc.value.code == "turn_timeout" and len(requests) == 1
    assert m.usage[-1]["statuses"] == [504] and m.usage[-1]["prompt_tokens"] is None
