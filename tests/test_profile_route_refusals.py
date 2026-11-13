"""Regressions for new-route settings and unconfirmed profile values."""
import json

import httpx
import pytest

from erpclaw_assistant import profiles
from erpclaw_assistant.model import OpenAICompatModel
from erpclaw_core.common import Refusal
from test_direct_profiles import MESSAGES, confirmed, profile_text


@pytest.mark.parametrize("backend", ["openai", "openrouter"])
@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize("parameter", ["temperature", "max_tokens"])
def test_sent_standard_parameter_rejection_names_setting_without_resending(confirmed, backend, status, parameter):
    profile = profiles.load(profile_text(backend))["sample"]
    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"param": parameter, "message": "untrusted provider text"}})

    client = httpx.Client(transport=httpx.MockTransport(reject))
    model = OpenAICompatModel(profile=profile, api_key="test", http_client=client)
    with pytest.raises(Refusal) as error:
        model.complete(MESSAGES, [])
    assert error.value.code == "model_refused"
    assert parameter in str(error.value)
    assert "untrusted provider text" not in str(error.value)
    assert "no second request" in str(error.value).lower()
    assert len(requests) == 1
    assert parameter in json.loads(requests[0].content)


@pytest.mark.parametrize("field", ["effort", "max_tokens", "enabled", "exclude"])
def test_unconfirmed_reasoning_values_are_refused(confirmed, field):
    text = profile_text("openrouter", extra=f'reasoning = {{ {field} = "uNcOnFiRmEd_value" }}\n')
    with pytest.raises(profiles.ProfileError, match="confirmed"):
        profiles.load(text)


@pytest.mark.parametrize("field", [
    'ignore = ["unconfirmed_provider"]',
    'sort = { nested = ["UNCONFIRMED_SORT"] }',
    'max_price = { prompt = "UNCONFIRMED_PRICE" }',
])
def test_unconfirmed_optional_provider_values_are_refused(confirmed, field):
    text = profile_text("openrouter").replace('require_parameters = true }', f'require_parameters = true, {field} }}')
    with pytest.raises(profiles.ProfileError, match="confirmed"):
        profiles.load(text)


def test_untrusted_parameter_name_is_not_echoed(confirmed):
    profile = profiles.load(profile_text())["sample"]
    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"param": "temperature and arbitrary provider text"}})

    model = OpenAICompatModel(profile=profile, api_key="test",
                             http_client=httpx.Client(transport=httpx.MockTransport(reject)))
    with pytest.raises(Refusal) as error:
        model.complete(MESSAGES, [])
    assert "arbitrary provider text" not in str(error.value)
    assert "no second request" in str(error.value).lower()
    assert len(requests) == 1


def test_renamed_reply_cap_rejection_names_sent_setting_without_resending(confirmed):
    text = profile_text().replace('parameters = {}',
                                  'parameters = { rename = { max_tokens = "max_completion_tokens" } }')
    profile = profiles.load(text)["sample"]
    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"param": "max_completion_tokens"}})

    model = OpenAICompatModel(profile=profile, api_key="test",
                             http_client=httpx.Client(transport=httpx.MockTransport(reject)))
    with pytest.raises(Refusal) as error:
        model.complete(MESSAGES, [])
    assert "max_completion_tokens" in str(error.value)
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["max_completion_tokens"] == 42 and "max_tokens" not in body


def test_nested_reasoning_placeholder_is_refused(confirmed):
    text = profile_text("openrouter", extra='reasoning = { effort = { nested = ["UNCONFIRMED"] } }\n')
    with pytest.raises(profiles.ProfileError, match="reasoning.*confirmed"):
        profiles.load(text)


def test_existing_routes_keep_their_reasoning_settings():
    profiles_by_name = profiles.load()
    router = profiles_by_name["qwen3_5_9b_openrouter_primeintellect"]
    assert not router.new_route and router.reasoning == {"effort": "low"}
