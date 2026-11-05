"""Model clients and the reply parser.

Rules for the local model route:
  - output is capped and every request has a wall-clock deadline
  - reasoning text is stripped and never stored
  - a tool call runs only if it arrived on the native tool-call channel and its
    arguments are one complete JSON object
  - a call written into the text or the reasoning (e.g. Qwen's <tool_call>) is
    NEVER executed; it is counted and the model
    gets one precise re-prompt
  - an unfinished <think> block is never shown as the answer
JSON numbers are parsed as Decimal, never float.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
TOOL_CALL_TEXT = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
# Terminal escape sequences (CSI, OSC, other ESC sequences), then any other C0 or C1 control character but
# newline and tab: model text never moves the owner's cursor, recolours their terminal or rewrites a line.
ESCAPE_SEQUENCE = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"           # CSI, 7-bit or 8-bit
                             r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"  # OSC, ended by BEL or ST
                             r"|\x1b[@-_]?")
CONTROL_CHAR = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict | None          # None when the arguments were not one complete JSON object
    raw: str = ""
    error: str | None = None


@dataclass
class Reply:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    finish: str = ""
    reasoning_chars: int = 0
    reasoning_as_answer: bool = False   # kept for the record; always False since r2
    text_calls: int = 0                 # calls found in text/reasoning, refused
    control_chars: int = 0              # terminal escapes and control characters removed (text and call arguments)


class Model(Protocol):
    name: str

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply: ...


def parse_args(raw) -> tuple[dict | None, str | None]:
    if isinstance(raw, dict):
        return json.loads(json.dumps(raw), parse_float=Decimal), None
    try:
        val = json.loads(raw or "", parse_float=Decimal)
    except (json.JSONDecodeError, TypeError):
        return None, "the tool arguments were not complete JSON"
    if not isinstance(val, dict):
        return None, "the tool arguments must be a JSON object"
    return val, None


def strip_controls(text: str) -> tuple[str, int]:
    """The text without terminal escape sequences or control characters (newline and tab kept), and how many
    characters were removed."""
    removed = sum(len(m) for m in ESCAPE_SEQUENCE.findall(text))
    text = ESCAPE_SEQUENCE.sub("", text)
    removed += len(CONTROL_CHAR.findall(text))
    return CONTROL_CHAR.sub("", text), removed


def _clean_args(value):
    """A tool call's arguments with every string stripped of control characters: the engine never stores or shows
    a model-written name or note that could drive the owner's terminal. Returns the arguments and the count."""
    if isinstance(value, str):
        return strip_controls(value)
    if isinstance(value, dict):
        out, n = {}, 0
        for k, v in value.items():
            (ck, a), (cv, b) = _clean_args(k), _clean_args(v)
            out[ck] = cv
            n += a + b
        return out, n
    if isinstance(value, list):
        items = [_clean_args(v) for v in value]
        return [v for v, _ in items], sum(n for _, n in items)
    return value, 0


def clean_reply(content: str | None, native_calls: list[tuple[str, str, object]], finish: str = "",
                reasoning: str | None = None) -> Reply:
    text = content or ""
    reasoning_chars = len(reasoning or "")
    # strip complete think blocks
    for blk in THINK_BLOCK.findall(text):
        reasoning_chars += len(blk)
    text = THINK_BLOCK.sub("", text)
    calls: list[ToolCall] = []
    removed = 0
    for cid, name, raw in native_calls:
        args, err = parse_args(raw)
        if args is not None:
            args, n = _clean_args(args)
            removed += n
        calls.append(ToolCall(cid, name, args, raw if isinstance(raw, str) else json.dumps(raw), err))
    # A call written as text is refused, never run.
    text_calls = len(TOOL_CALL_TEXT.findall(text)) + len(TOOL_CALL_TEXT.findall(reasoning or ""))
    text = TOOL_CALL_TEXT.sub("", text)
    as_answer = False
    if "<think>" in text:  # unclosed think block: drop it, never promote it to the answer
        before, _, after = text.partition("<think>")
        reasoning_chars += len(after)
        text = before
    text, n = strip_controls(text.replace("</think>", ""))
    removed += n
    text = text.strip()
    reply = Reply(text, calls, finish, reasoning_chars, as_answer)
    reply.text_calls = text_calls
    reply.control_chars = removed
    return reply


NO_RETRY = {400, 401, 402, 403, 404, 413}
RETRY = {429, 500, 502, 503, 504}


class OpenAICompatModel:
    """LM Studio (default), OpenRouter or any OpenAI-compatible Chat Completions server, from a profile or, with
    none, from the environment as before. Each backend gets only its own extensions: LM Studio's thinking flag,
    OpenRouter's provider pins and reasoning setting; any other server neither."""

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None,
                 max_tokens: int | None = None, timeout_s: float | None = None, thinking: bool | None = None,
                 profile=None, http_client=None, key_reader=None, sleep=None):
        import time

        from openai import OpenAI

        from . import hosted

        self.profile = profile
        self._sleep = sleep or time.sleep
        if profile is None:
            self.backend, self.hosted, self.hosted_books = "lmstudio", False, hosted.SEEDED_ONLY
            self.base_url = base_url or os.environ.get("ERPCLAW_MODEL_BASE_URL", "http://127.0.0.1:1234/v1")
            self.name = model or os.environ.get("ERPCLAW_MODEL", "qwen3.5-9b-mlx")
            self.max_tokens = max_tokens or int(os.environ.get("ERPCLAW_MAX_TOKENS", "2048"))
            self.thinking = thinking if thinking is not None else os.environ.get("ERPCLAW_THINKING", "0") == "1"
            self.temperature, self.reasoning, self.pins = 0, {}, {}
            timeout = timeout_s or float(os.environ.get("ERPCLAW_TIMEOUT_S", "180"))
            key = api_key or os.environ.get("ERPCLAW_MODEL_API_KEY", "lm-studio")
        else:
            from .profiles import ProfileError, read_key

            self.backend, self.hosted, self.hosted_books = profile.backend, profile.hosted, profile.hosted_books
            self.base_url, self.name, self.max_tokens = profile.base_url, profile.model, profile.max_tokens
            self.thinking = bool(profile.reasoning.get("enabled", False)) if profile.backend == "lmstudio" else False
            self.temperature, self.reasoning, self.pins = profile.temperature, dict(profile.reasoning), dict(profile.provider)
            timeout = timeout_s or profile.timeout_s
            if self.hosted:
                hosted.refuse_real_home()
                key = api_key or (key_reader or (lambda: read_key(profile)))()
                if not key:
                    raise ProfileError(f"The {profile.backend} key is not set up (keychain item {profile.key_item}).")
            else:
                key = api_key or "lm-studio"
        self.usage: list[dict] = []  # per request: counts only, never text
        self.client = OpenAI(base_url=self.base_url, api_key=key, timeout=timeout, max_retries=0,
                             http_client=http_client)
        del key

    def __repr__(self) -> str:
        return f"OpenAICompatModel(backend={self.backend!r}, model={self.name!r}, base_url={self.base_url!r})"

    def _extensions(self) -> tuple[dict, dict]:
        """The request body's extra fields and the extra headers, for this backend only."""
        if self.backend == "lmstudio":
            return {"chat_template_kwargs": {"enable_thinking": self.thinking}}, {}
        if self.backend == "openrouter":
            body = {"provider": self.pins} if self.pins else {}
            if self.reasoning:
                body["reasoning"] = self.reasoning
            return body, {"X-OpenRouter-Metadata": "enabled"}
        return {}, {}

    def _create(self, messages: list[dict], tools: list[dict]):
        import openai

        from erpclaw_core.common import Refusal

        body, headers = self._extensions()
        kw = dict(model=self.name, messages=messages, tools=tools or None, temperature=self.temperature,
                  max_tokens=self.max_tokens, extra_body=body or None, extra_headers=headers or None)
        create = self.client.chat.completions.with_raw_response.create  # the raw body keeps the cost exact
        if not self.hosted:  # the local route as before: a failure is the server's own error
            return create(**kw)
        for attempt in (1, 2):
            try:
                return create(**kw)
            except openai.APIStatusError as e:
                if e.status_code in RETRY and attempt == 1:
                    self._sleep(1.0)
                    continue
                self.usage.append({"statuses": [e.status_code], "native_calls": 0, "reasoning_tokens": None,
                                   "prompt_tokens": None, "completion_tokens": None, "cost": None, "served_by": None})
                if e.status_code == 404:  # fallbacks are off: no endpoint matched the pins
                    raise Refusal("No provider in this profile's pins can take this request (its data policy, "
                                  "quantization, or the tools the request needs). Nothing was sent elsewhere and "
                                  "nothing was saved.", "model_refused") from None
                if e.status_code in RETRY:
                    raise Refusal("The hosted model is unavailable right now. Nothing was saved.",
                                  "model_unavailable") from None
                raise Refusal(f"The hosted model refused the request ({e.status_code}). Nothing was saved.",
                              "model_refused") from None
            except (openai.APITimeoutError, openai.APIConnectionError):
                if attempt == 1:
                    self._sleep(1.0)
                    continue
                self.usage.append({"statuses": [504], "native_calls": 0, "reasoning_tokens": None,
                                   "prompt_tokens": None, "completion_tokens": None, "cost": None, "served_by": None})
                raise Refusal("The hosted model is unavailable right now. Nothing was saved.",
                              "model_unavailable") from None

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        if self.hosted:
            from . import hosted

            hosted.refuse_credentials(self.usage, messages, tools)  # before anything is sent
        raw = self._create(messages, tools)
        resp = raw.parse()
        choice = resp.choices[0]
        msg = choice.message
        u = resp.usage
        det = getattr(u, "completion_tokens_details", None) if u else None
        cost = _exact_cost(raw.text)
        served_by, served_model = _served(getattr(resp, "model_extra", None) or {})
        self.usage.append({"reasoning_tokens": getattr(det, "reasoning_tokens", None),
                           "prompt_tokens": getattr(u, "prompt_tokens", None),
                           "completion_tokens": getattr(u, "completion_tokens", None),
                           "cost": cost,
                           "served_by": served_by, "served_model": served_model,
                           "native_calls": len(msg.tool_calls or [])})
        native = [(tc.id, tc.function.name, tc.function.arguments) for tc in (msg.tool_calls or [])]
        extra_fields = getattr(msg, "model_extra", None) or {}
        reasoning = extra_fields.get("reasoning_content") or extra_fields.get("reasoning")
        return clean_reply(msg.content, native, choice.finish_reason or "", reasoning)


def _served(extra: dict) -> tuple[str | None, str | None]:
    """The provider that served a reply and the model id it served, as OpenRouter names them: the reply's own top-level
    provider, else the endpoint its metadata marks selected. (None, None) from a server that names neither."""
    endpoints = ((extra.get("openrouter_metadata") or {}).get("endpoints") or {}).get("available") or []
    chosen = next((e for e in endpoints if isinstance(e, dict) and e.get("selected") is True), {})
    provider = extra.get("provider") if isinstance(extra.get("provider"), str) else None
    return provider or chosen.get("provider"), chosen.get("model")


def _exact_cost(body: str) -> Decimal | None:
    """The reply's usage.cost as an exact Decimal, read from the body itself (never through a float); None when the
    backend reported none."""
    try:
        cost = (json.loads(body, parse_float=Decimal).get("usage") or {}).get("cost")
    except (ValueError, AttributeError):
        return None
    if isinstance(cost, bool) or not isinstance(cost, (Decimal, int)):
        return None
    return Decimal(cost)


class ScriptedModel:
    """Deterministic stand-in for tests: returns queued replies in order."""

    name = "scripted"

    def __init__(self, replies: list[Reply]):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []
        self.tools_seen: list[list[str]] = []  # the tool names offered at each call

    def complete(self, messages: list[dict], tools: list[dict]) -> Reply:
        self.seen.append(messages)
        self.tools_seen.append([t["function"]["name"] for t in tools])
        if not self.replies:
            return Reply("I have nothing more to add.")
        return self.replies.pop(0)


def call(name: str, **args) -> ToolCall:
    """Test helper: a native tool call."""
    return ToolCall(f"call_{name}", name, json.loads(json.dumps(args), parse_float=Decimal), json.dumps(args))
