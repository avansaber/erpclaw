"""Muse (Meta's Muse Spark) through the Responses API, and the choice of model provider.

Rules for the Muse route:
  - only Standard-mode models: an id containing "contributor" is refused before any request (Meta may train on
    content sent to a Contributor model)
  - requests are stateless: store is false, no previous response id, no reasoning carried between requests
  - assistant text sent before a tool call is replayed with phase "commentary", a final answer with "final_answer"
  - the reply goes through the same parser as the local route (`clean_reply`): only native tool calls run, a call
    written in the text is refused, numbers are Decimal
  - a refused request is never retried; an unavailable server is retried once; nothing is saved either way
  - the key is read at run time (keychain item or process environment) and never stored, logged or shown
"""
from __future__ import annotations

import logging
import os
import random
import re
import subprocess
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Callable

from erpclaw_core.common import Refusal
from erpclaw_core.plugin_text import say

from . import hosted as guards
from .model import OpenAICompatModel, Reply, _remaining_budget, clean_reply, request_timeout, retry_sleep

log = logging.getLogger(__name__)

BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"
MUSE_ITEM_NAME = "erpclaw-v3-muse"  # the keychain item that holds the key (a name, not a secret)
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")  # "max" is never sent
NO_RETRY = {400, 401, 402, 403, 404, 413}  # Meta: "Do not retry 400, 401, 402, 403, 404, or 413 errors"
RETRY = {429, 500, 503, 504}
MAX_RETRY_WAIT = 30.0  # seconds; a server asking for a longer wait is not retried in the owner's turn
TOOL_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")
PROVIDERS = ("openai-compatible", "muse", "profile")


class MuseConfigError(ValueError):
    """The Muse model cannot start with this configuration; the message says why and what to do."""


def _keychain(run: Callable = subprocess.run) -> str | None:
    """The key from the keychain item, read from the command's output; the key is never an argument."""
    return guards.keychain(MUSE_ITEM_NAME, run)


def read_key(run: Callable = subprocess.run) -> str | None:
    return os.environ.get("ERPCLAW_MUSE_API_KEY") or _keychain(run)


def _tool(t: dict) -> dict:
    f = t["function"]
    name = f["name"]
    if not TOOL_NAME.match(name) or name.count(".") > 1:
        log.warning("model_input_invalid: invalid tool name <key>")
        raise Refusal(say("model_input_invalid"), "model_input_invalid")
    out = {"type": "function", "name": name, "parameters": f.get("parameters") or {"type": "object", "properties": {}},
           "strict": False}  # set explicitly: Meta's pages disagree on the default; the engine validates arguments
    if f.get("description"):
        out["description"] = f["description"]
    return out


def to_responses(messages: list[dict]) -> tuple[str, list[dict]]:
    """Chat-form messages to (instructions, input items), in order: system text becomes the instructions, assistant
    text before a tool call is "commentary" and other assistant text "final_answer"."""
    instructions: list[str] = []
    items: list[dict] = []
    calls: set[str] = set()
    for m in messages:
        role = m["role"]
        if role == "system":
            instructions.append(m["content"])
        elif role == "user":
            items.append({"type": "message", "role": "user", "content": m["content"]})
        elif role == "assistant":
            text, tcs = m.get("content") or "", m.get("tool_calls") or []
            if text:
                items.append({"type": "message", "role": "assistant", "phase": "commentary" if tcs else "final_answer",
                              "content": [{"type": "output_text", "text": text}]})
            for tc in tcs:
                calls.add(tc["id"])
                items.append({"type": "function_call", "call_id": tc["id"], "name": tc["function"]["name"],
                              "arguments": tc["function"]["arguments"] or "{}"})
        elif role == "tool":
            if m["tool_call_id"] not in calls:  # Meta returns HTTP 400 for it; refuse before sending
                log.warning("model_input_invalid: tool result without its call; call_id=<key>")
                raise Refusal(say("model_input_invalid"), "model_input_invalid")
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
    return "\n\n".join(instructions), items


class MuseModel:
    """Muse through the Responses API. Implements the `Model` protocol of `model.py`."""

    provider = "muse"
    hosted = True  # a hosted service: it meets only demo or eval books, and no credential is sent to it
    hosted_books = guards.SEEDED_ONLY

    def __init__(self, model: str | None = None, effort: str | None = None, max_tokens: int | None = None,
                 timeout_s: float | None = None, base_url: str | None = None, api_key: str | None = None,
                 http_client=None, sleep: Callable[[float], None] = time.sleep, key_reader: Callable = read_key,
                 now: Callable[[], float] = time.time):
        from openai import OpenAI

        guards.refuse_real_home()
        self.name = model or os.environ.get("ERPCLAW_MUSE_MODEL", DEFAULT_MODEL)
        if "contributor" in self.name.lower():
            raise MuseConfigError("ERPClaw does not send your books to a Contributor model: Meta may use that "
                                  "content for training. Use a Standard model such as muse-spark-1.3.")
        self.effort = effort or os.environ.get("ERPCLAW_MUSE_EFFORT", "medium")
        if self.effort not in EFFORTS:
            raise MuseConfigError(f"Muse reasoning effort must be one of {', '.join(EFFORTS)}, not {self.effort!r}.")
        self.base_url = base_url or os.environ.get("ERPCLAW_MUSE_BASE_URL", BASE_URL)
        self.max_tokens = max_tokens or int(os.environ.get("ERPCLAW_MAX_TOKENS", "8192"))
        key = api_key or key_reader()
        if not key:
            raise MuseConfigError(f"The Muse key is not set up (keychain item {MUSE_ITEM_NAME}).")
        self._sleep, self._now = sleep, now
        self.usage: list[dict] = []  # per request: counts and status codes only, never text
        self.client = OpenAI(base_url=self.base_url, api_key=key, max_retries=0, http_client=http_client,
                             timeout=timeout_s or float(os.environ.get("ERPCLAW_TIMEOUT_S", "120")))
        del key

    def __repr__(self) -> str:
        return f"MuseModel(model={self.name!r}, base_url={self.base_url!r})"

    def describe(self) -> dict:
        return {"provider": "muse", "model": self.name, "base_url": self.base_url, "reasoning_effort": self.effort,
                "store": False}

    def check_ready(self) -> str | None:
        import openai

        try:
            self.client.models.list()
        except openai.APIStatusError as e:
            return f"The Muse model refused the request ({e.status_code})."
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            return f"The Muse model at {self.base_url} is not reachable ({type(e).__name__})."
        return None

    def request(self, messages: list[dict], tools: list[dict]) -> dict:
        instructions, items = to_responses(messages)
        body = {"model": self.name, "input": items, "store": False, "reasoning": {"effort": self.effort},
                "max_output_tokens": self.max_tokens}
        if instructions:
            body["instructions"] = instructions
        if tools:
            body["tools"] = [_tool(t) for t in tools]
            body["parallel_tool_calls"] = False  # one call per step, as the local route
        return body

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        import openai

        body = self.request(messages, tools)  # refuses before any HTTP request if the input is invalid
        guards.refuse_credentials(self.usage, messages, tools)  # before anything is sent
        statuses: list[int] = []
        for attempt in (1, 2):
            try:
                resp = self.client.responses.create(**body, **request_timeout(self.client.timeout))
                break
            except openai.APIStatusError as e:
                statuses.append(e.status_code)
                wait = _retry_after(e.response.headers.get("retry-after"), self._now())
                if e.status_code in RETRY and attempt == 1 and wait is not None:
                    retry_sleep(self._sleep, wait)
                    continue
                self.usage.append({"statuses": statuses, "native_calls": 0, "reasoning_tokens": None})
                if e.status_code == 429:
                    raise Refusal("The model service is busy right now.", "model_rate_limited") from None
                if e.status_code in RETRY:
                    raise Refusal("The Muse model is unavailable right now. Nothing was saved.",
                                  "model_unavailable") from None
                raise Refusal(f"The Muse model refused the request ({e.status_code}). Nothing was saved.",
                              "model_refused") from None
            except (openai.APITimeoutError, openai.APIConnectionError):
                _remaining_budget()
                statuses.append(504)  # a request with no answer counts as a gateway timeout
                if attempt == 1:
                    retry_sleep(self._sleep, _retry_after(None, self._now()))
                    continue
                self.usage.append({"statuses": statuses, "native_calls": 0, "reasoning_tokens": None})
                raise Refusal("The Muse model is unavailable right now. Nothing was saved.",
                              "model_unavailable") from None
        return self._reply(resp.model_dump(), statuses)

    def _reply(self, d: dict, statuses: list[int]) -> Reply:
        status, reason = d.get("status"), (d.get("incomplete_details") or {}).get("reason")
        if d.get("error") or not (status == "completed" or (status == "incomplete" and reason == "max_output_tokens")):
            # a failed, cancelled or otherwise unfinished response is not an answer: nothing in it is read or run
            self.usage.append({"statuses": statuses + [200], "native_calls": 0, "reasoning_tokens": None,
                               "response_status": status})
            raise Refusal("The Muse model could not answer this request. Nothing was saved.", "model_unavailable")
        texts: list[str] = []
        native: list[tuple[str, str, object]] = []
        reasoning_chars = other = 0
        for item in d.get("output") or []:
            kind = item.get("type")
            if kind == "message":
                texts += [p.get("text") or "" for p in item.get("content") or [] if p.get("type") == "output_text"]
            elif kind == "function_call":
                native.append((item["call_id"], item["name"], item.get("arguments") or ""))
            elif kind == "reasoning":
                reasoning_chars += sum(len(s.get("text") or "") for s in item.get("summary") or [])
            else:
                other += 1  # a built-in tool the engine never offers: ignored, counted
        reply = clean_reply("".join(texts), native, "length" if status == "incomplete" else "stop")
        reply.reasoning_chars += reasoning_chars
        u = d.get("usage") or {}
        self.usage.append({"input_tokens": u.get("input_tokens"), "prompt_tokens": u.get("input_tokens"),
                           "completion_tokens": u.get("output_tokens"),
                           "reasoning_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
                           "cost": None, "served_by": None,  # Muse reports no cost per request
                           "native_calls": len(native), "other_items": other, "statuses": statuses + [200]})
        return reply


def _retry_after(header: str | None, now: float) -> float | None:
    """Seconds to wait before the one retry: the server's Retry-After (delay-seconds or an HTTP date), else about
    half a second with jitter; None when the server asks for longer than MAX_RETRY_WAIT (then there is no retry)."""
    if header is None:
        return 0.5 + random.uniform(0, 0.25)
    try:
        wait = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:  # an obsolete asctime date carries no zone; HTTP dates are always GMT
                when = when.replace(tzinfo=timezone.utc)
            wait = when.timestamp() - now
        except (TypeError, ValueError, IndexError):
            return 0.5 + random.uniform(0, 0.25)
    wait = max(wait, 0.0)
    return wait if wait <= MAX_RETRY_WAIT else None


class LocalModel:
    """The OpenAI-compatible client with the provider description the eval runner reads (`model.py` unchanged)."""

    provider = "openai-compatible"

    def __init__(self, inner: OpenAICompatModel | None = None):
        self._inner = inner or OpenAICompatModel()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        return self._inner.complete(messages, tools)

    def describe(self) -> dict:
        return {"provider": "openai-compatible", "model": self._inner.name, "base_url": self._inner.base_url,
                "thinking": "on" if self._inner.thinking else "off"}

    def check_ready(self) -> str | None:
        m = self._inner
        try:
            ids = [x.id for x in m.client.models.list().data]
        except Exception as e:  # noqa: BLE001  (any failure to reach the server is reported, not raised)
            return f"The model server at {m.base_url} is not reachable ({type(e).__name__}). Start LM Studio first."
        if m.name not in ids:
            return f"Model {m.name!r} is not loaded at {m.base_url}. Loaded: {ids}"
        return None


def model_from_env():
    """The model the engine talks to, chosen by ERPCLAW_MODEL_PROVIDER (default: the OpenAI-compatible route)."""
    provider = os.environ.get("ERPCLAW_MODEL_PROVIDER", "openai-compatible")
    if os.environ.get("ERPCLAW_MODEL_PROFILE") or provider == "profile":
        from .profiles import ProfileError, from_env

        if provider != "profile":
            raise ProfileError(f"ERPCLAW_MODEL_PROFILE names a profile, so ERPCLAW_MODEL_PROVIDER must be 'profile', "
                               f"not {provider!r}.")
        return from_env()
    if provider == "openai-compatible":
        return LocalModel()
    if provider == "muse":
        return MuseModel()
    raise MuseConfigError(f"ERPCLAW_MODEL_PROVIDER must be one of {', '.join(PROVIDERS)}, not {provider!r}.")
