"""Per-model profiles: the model, its backend, the limits, the reasoning setting and the provider pins, as data.

A profile is one table in profiles.toml. A field the engine does not know is refused, so a misspelled pin is never
dropped silently; each backend accepts only its own extensions. The next model is a new table, not a code change.
"""
from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import hosted

PROFILES_FILE = Path(__file__).with_name("profiles.toml")
BACKENDS = ("lmstudio", "openrouter", "openai-compatible")
FIELDS = {"backend", "model", "base_url", "hosted", "hosted_books", "key_item", "key_env", "max_tokens",
          "timeout_s", "temperature", "reasoning", "provider", "max_parallel"}
REASONING = {"lmstudio": {"enabled"}, "openrouter": {"effort", "max_tokens", "enabled", "exclude"}}
PROVIDER_FIELDS = {"order", "allow_fallbacks", "data_collection", "zdr", "quantizations", "only", "ignore",
                   "require_parameters", "sort", "max_price"}


class ProfileError(ValueError):
    """A profile the engine cannot honour, or cannot choose; the message says which field and why."""


@dataclass(frozen=True)
class Profile:
    name: str
    backend: str
    model: str
    base_url: str
    hosted: bool
    hosted_books: str = hosted.SEEDED_ONLY
    key_item: str | None = None
    key_env: str | None = None
    max_tokens: int = 2048
    max_parallel: int = 1  # requests the backend may take at once; read by whoever runs trials side by side
    timeout_s: float = 180.0
    temperature: float = 0
    reasoning: dict = field(default_factory=dict)
    provider: dict = field(default_factory=dict)


def _profile(name: str, t: dict) -> Profile:
    unknown = sorted(set(t) - FIELDS)
    if unknown:
        raise ProfileError(f"profile {name}: unknown field {', '.join(unknown)}")
    backend = t.get("backend")
    if backend not in BACKENDS:
        raise ProfileError(f"profile {name}: backend must be one of {', '.join(BACKENDS)}, not {backend!r}")
    for need in ("model", "base_url"):
        if not t.get(need):
            raise ProfileError(f"profile {name}: {need} is required")
    is_hosted = t.get("hosted", backend != "lmstudio")
    if backend == "openrouter" and not is_hosted:
        raise ProfileError(f"profile {name}: hosted cannot be false for an OpenRouter profile")
    reasoning, provider = dict(t.get("reasoning") or {}), dict(t.get("provider") or {})
    if reasoning and backend not in REASONING:
        raise ProfileError(f"profile {name}: reasoning is not sent to a {backend} backend")
    if set(reasoning) - REASONING.get(backend, set()):
        raise ProfileError(f"profile {name}: unknown reasoning field "
                           f"{', '.join(sorted(set(reasoning) - REASONING[backend]))}")
    if provider and backend != "openrouter":
        raise ProfileError(f"profile {name}: a provider block is OpenRouter's only, not {backend}'s")
    if set(provider) - PROVIDER_FIELDS:
        raise ProfileError(f"profile {name}: unknown provider field {', '.join(sorted(set(provider) - PROVIDER_FIELDS))}")
    parallel = t.get("max_parallel", 1)
    if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
        raise ProfileError(f"profile {name}: max_parallel must be a positive whole number, not {parallel!r}")
    try:
        books = hosted.check_policy(t.get("hosted_books", hosted.SEEDED_ONLY))
    except hosted.HostedRefused as e:
        raise ProfileError(f"profile {name}: {e}") from None
    return Profile(name=name, backend=backend, model=t["model"], base_url=t["base_url"], hosted=bool(is_hosted),
                   hosted_books=books, key_item=t.get("key_item"), key_env=t.get("key_env"),
                   max_tokens=int(t.get("max_tokens", 2048)), max_parallel=parallel, timeout_s=float(t.get("timeout_s", 180)),
                   temperature=t.get("temperature", 0), reasoning=reasoning, provider=provider)


def load(text: str | None = None) -> dict[str, Profile]:
    """Every profile, by name: from `text` when given, else from the shipped file."""
    raw = tomllib.loads(text if text is not None else PROFILES_FILE.read_text())
    return {name: _profile(name, t) for name, t in raw.items()}


def read_key(p: Profile, run: Callable = subprocess.run) -> str | None:
    """A hosted profile's key: its keychain item, else its environment variable; never stored, logged or shown."""
    if p.key_item:
        key = hosted.keychain(p.key_item, run)
        if key:
            return key
    return os.environ.get(p.key_env) if p.key_env else None


class ProfileModel:
    """A model chosen by profile, with the description the eval runner reads. Implements `model.Model`."""

    def __init__(self, inner):
        self._inner = inner
        self.provider = inner.profile.backend

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __repr__(self) -> str:
        return f"ProfileModel(profile={self._inner.profile.name!r})"

    def complete(self, messages: list[dict], tools: list[dict]):
        return self._inner.complete(messages, tools)

    def describe(self) -> dict:
        p = self._inner.profile
        return {"provider": p.backend, "model": p.model, "profile": p.name, "base_url": p.base_url,
                "hosted": p.hosted, "reasoning": dict(p.reasoning), "pins": dict(p.provider),
                "max_tokens": p.max_tokens, "temperature": p.temperature, "max_parallel": p.max_parallel}

    def check_ready(self) -> str | None:
        try:
            ids = [x.id for x in self._inner.client.models.list().data]
        except Exception as e:  # noqa: BLE001  (any failure to reach the server is reported, not raised)
            return f"The model server at {self._inner.base_url} is not reachable ({type(e).__name__})."
        if self._inner.name not in ids:
            return f"Model {self._inner.name!r} is not offered at {self._inner.base_url}."
        return None


def from_env() -> ProfileModel:
    """The model named by ERPCLAW_MODEL_PROFILE."""
    from .model import OpenAICompatModel

    name = os.environ.get("ERPCLAW_MODEL_PROFILE", "")
    shipped = load()
    if name not in shipped:
        raise ProfileError(f"ERPCLAW_MODEL_PROFILE must name one of {', '.join(sorted(shipped))}, not {name!r}.")
    return ProfileModel(OpenAICompatModel(profile=shipped[name]))
