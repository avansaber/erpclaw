"""Per-model profiles: the model, its backend, the limits, the reasoning setting and the provider pins, as data.

A profile is one table in profiles.toml. A field the engine does not know is refused, so a misspelled pin is never
dropped silently; each backend accepts only its own extensions. The next model is a new table, not a code change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field, replace
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Callable

from . import hosted

PROFILES_FILE = Path(__file__).with_name("profiles.toml")
PROFILE_PINS_FILE = Path(__file__).with_name("tokenizer_pins.toml")
BACKENDS = ("lmstudio", "openrouter", "openai-compatible", "openai", "claude")
FIELDS = {"backend", "model", "base_url", "hosted", "hosted_books", "key_item", "key_env", "max_tokens",
          "timeout_s", "temperature", "reasoning", "provider", "max_parallel",
          "max_input_tokens", "context_window", "admission", "vocab_repo", "vocab_revision", "file", "margin_route",
          "fixed_segments", "parameters", "terms", "consent"}
REASONING = {"lmstudio": {"enabled"}, "openrouter": {"effort", "max_tokens", "enabled", "exclude"},
             "openai": set(), "claude": set()}
PROVIDER_FIELDS = {"order", "allow_fallbacks", "data_collection", "zdr", "quantizations", "only", "ignore",
                   "require_parameters", "sort", "max_price"}
_MODEL_UNAVAILABLE = "The hosted model is unavailable right now. Nothing was saved."

# Entries are added only after the account terms and consent wording have been reviewed.
CONFIRMED_TERMS: frozenset[tuple[str, str, str]] = frozenset()
APPROVED_CONSENT: frozenset[str] = frozenset()
DIRECT_ENDPOINTS = {"openai": "https://api.openai.com/v1", "claude": "https://api.anthropic.com"}
DIRECT_KEYS = {"openai": ("erpclaw-openai", "ERPCLAW_OPENAI_API_KEY"),
               "claude": ("erpclaw-anthropic", "ERPCLAW_ANTHROPIC_API_KEY")}
ROUTER_KEY = ("erpclaw-v3-openrouter", "ERPCLAW_OPENROUTER_API_KEY")
STAGED_NAMES = {"openai_direct", "claude_direct", "openai_openrouter", "claude_openrouter"}


def _refuse_unavailable() -> None:
    from erpclaw_core.common import Refusal

    # Same refusal and wording as model.OpenAICompatModel's unavailable raises;
    # graph.MODEL_REFUSALS already ends the turn on this code.
    raise Refusal(_MODEL_UNAVAILABLE, "model_unavailable")


@lru_cache(maxsize=None)
def _log_nfd_failure(expected: str, computed: str, reason: str) -> None:
    # Deduplicate across profiles, reloads and turns. Never log file content or
    # parser exceptions, which can include untrusted data from the resource.
    logging.getLogger(__name__).warning("NFD table unavailable: expected=%s computed=%s reason=%s",
                                        expected, computed, reason)


def _load_nfd_table(expected) -> tuple[int, ...] | None:
    """Expand and authenticate package data using the verifier's big-endian hash."""
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        _log_nfd_failure("invalid", "unavailable", "manifest hash")
        return None
    try:
        sparse = json.loads(resources.files("erpclaw_assistant").joinpath("nfd_bytes.json").read_text(encoding="utf-8"))
    except OSError:
        _log_nfd_failure(expected, "unavailable", "missing or unreadable resource")
        return None
    except (ValueError, RecursionError):
        _log_nfd_failure(expected, "unavailable", "malformed resource")
        return None
    # The defaults are scalar UTF-8 lengths; surrogate slots are zero sentinels.
    table = ([1] * 0x80 + [2] * (0x800 - 0x80) + [3] * (0xD800 - 0x800)
             + [0] * (0xE000 - 0xD800) + [3] * (0x10000 - 0xE000)
             + [4] * (0x110000 - 0x10000))
    previous = -1
    if not isinstance(sparse, list):
        _log_nfd_failure(expected, "unavailable", "malformed resource")
        return None
    for pair in sparse:
        if (not isinstance(pair, list) or len(pair) != 2 or any(type(n) is not int for n in pair)):
            _log_nfd_failure(expected, "unavailable", "malformed resource")
            return None
        cp, value = pair
        if (not previous < cp < 0x110000 or 0xD800 <= cp < 0xE000
                or not 0 < value <= 0xFFFFFFFF or value == table[cp]):
            _log_nfd_failure(expected, "unavailable", "malformed resource")
            return None
        table[cp] = value
        previous = cp
    computed = hashlib.sha256(b"".join(n.to_bytes(4, "big") for n in table)).hexdigest()
    if computed != expected:
        _log_nfd_failure(expected, computed, "hash mismatch")
        return None
    return tuple(table)


class ProfileError(ValueError):
    """A profile the engine cannot honour, or cannot choose; the message says which field and why."""


@dataclass(frozen=True)
class Profile:
    name: str
    backend: str
    model: str
    base_url: str
    hosted: bool
    max_input_tokens: int  # structural input bound budget, independent of the reply cap
    context_window: int | None  # an absent window keeps the conservative fallback
    hosted_books: str = hosted.SEEDED_ONLY
    key_item: str | None = None
    key_env: str | None = None
    max_tokens: int = 2048
    max_parallel: int = 1  # requests the backend may take at once; read by whoever runs trials side by side
    timeout_s: float = 180.0
    temperature: float = 0
    reasoning: dict = field(default_factory=dict)
    provider: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    new_route: bool = False
    terms: str | None = None
    consent: str | None = None
    admission: str = "conservative_bound"
    vocab_repo: str | None = None
    vocab_revision: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    margin_route: str | None = None
    margin: dict | None = None
    structural_method: str = "structural-v1"
    structural_constant: int = 0
    renderer: str = "ERPCLAW-COUNT-v1"
    segment_open: str | None = None
    segment_close: str | None = None
    fixed_counts: dict[str, int] = field(default_factory=dict)
    fixed_segments: tuple[str, ...] = ()
    nfd_table: tuple[int, ...] | None = field(default=None, repr=False, compare=False)

    def require_admission(self) -> None:
        if self.admission == "unavailable" or (self.renderer == "ERPCLAW-COUNT-v2" and self.nfd_table is None):
            _refuse_unavailable()

    def nfd_bytes(self, text: str) -> int:
        """The later gate's byte sum; no normalization or token counting at runtime."""
        self.require_admission()
        if self.nfd_table is None:
            _refuse_unavailable()
        text.encode("utf-8", errors="strict")  # refuse lone surrogates before lookup
        return sum(self.nfd_table[ord(char)] for char in text)


def _structural(name: str, t: dict, pin: dict, manifest: dict) -> dict:
    proof = manifest.get("structural")
    if not isinstance(proof, dict) or proof.get("method") != "structural-v1":
        raise ProfileError(f"profile {name}: structural method must be structural-v1")
    constant = proof.get("constant")
    if type(constant) is not int or constant != 0:
        raise ProfileError(f"profile {name}: structural constant must equal the proved zero")
    renderer = proof.get("renderer")
    if renderer not in ("ERPCLAW-COUNT-v1", "ERPCLAW-COUNT-v2"):
        raise ProfileError(f"profile {name}: unknown structural renderer")
    opened, closed = proof.get("segment_open"), proof.get("segment_close")
    if renderer == "ERPCLAW-COUNT-v2":
        for key, value in (("segment_open", opened), ("segment_close", closed)):
            if not isinstance(value, str) or not value.strip() or value == "unmeasured":
                raise ProfileError(f"profile {name}: delimiter {key} is unmeasured")
    counts = proof.get("fixed_counts")
    status = proof.get("fixed_counts_status")
    if not isinstance(counts, dict) or status not in ("unmeasured", "measured"):
        raise ProfileError(f"profile {name}: fixed counts and measurement status are required")
    if any(not isinstance(k, str) or not k or type(v) is not int or v < 0 for k, v in counts.items()):
        raise ProfileError(f"profile {name}: fixed counts must be non-negative whole numbers")
    required = pin.get("fixed_segments", [])
    if not isinstance(required, list) or any(not isinstance(k, str) or not k for k in required):
        raise ProfileError(f"profile {name}: fixed_segments must list segment names")
    if len(set(required)) != len(required) or t.get("fixed_segments", required) != required:
        raise ProfileError(f"profile {name}: fixed_segments must be unique and match the manifest")
    if required and (status != "measured" or set(required) - set(counts)):
        raise ProfileError(f"profile {name}: required fixed count is unmeasured")
    if counts and status != "measured":
        raise ProfileError(f"profile {name}: fixed counts are unmeasured")
    return {"structural_method": proof["method"], "structural_constant": constant,
            "renderer": renderer, "segment_open": opened, "segment_close": closed,
            "fixed_counts": dict(counts), "fixed_segments": tuple(required)}


def _files(name: str, entries) -> dict[str, str]:
    if not isinstance(entries, list) or not entries:
        raise ProfileError(f"profile {name}: file is required")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "sha256"}:
            raise ProfileError(f"profile {name}: file requires name and sha256")
        filename, sha = entry["name"], entry["sha256"]
        if not isinstance(filename, str) or not filename or filename in result:
            raise ProfileError(f"profile {name}: file name must be non-empty and unique")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ProfileError(f"profile {name}: sha256 for {filename} must be 64 lowercase hex digits")
        result[filename] = sha
    if "tokenizer.json" not in result:
        raise ProfileError(f"profile {name}: file tokenizer.json is required")
    return result


def _admission(name: str, t: dict, manifest: dict) -> dict:
    """Resolve declarations against the committed pins; tokenizer verification stays offline.

    No counting or admission decision is performed by the profile loader.
    """
    pin = manifest.get(name)
    if not isinstance(pin, dict):
        if t.get("admission") != "conservative_bound":
            raise ProfileError(f"profile {name}: admission requires a manifest pin table")
        pin = {"admission": "conservative_bound"}
    admission = pin.get("admission")
    if admission not in ("bounded", "conservative_bound"):
        raise ProfileError(f"profile {name}: admission must be bounded or conservative_bound")
    if t.get("admission", admission) != admission:
        raise ProfileError(f"profile {name}: admission must match the manifest")
    structural = _structural(name, t, pin, manifest)
    if admission == "conservative_bound":
        for key in ("vocab_repo", "vocab_revision", "file", "margin_route"):
            if key in t or key in pin:
                raise ProfileError(f"profile {name}: {key} is not used with conservative_bound")
        return {"admission": admission, **structural}

    # An explicit vocabulary declaration must be complete; partial pins are never repaired with defaults.
    keys = ("vocab_repo", "vocab_revision", "file")
    declared = t if any(k in t for k in keys) else pin
    for key in ("vocab_repo", "vocab_revision"):
        if not declared.get(key) or not pin.get(key):
            raise ProfileError(f"profile {name}: {key} is required")
        if declared[key] != pin[key]:
            raise ProfileError(f"profile {name}: {key} must match the manifest")
    if not isinstance(pin["vocab_repo"], str):
        raise ProfileError(f"profile {name}: vocab_repo must be a repository name")
    if not isinstance(pin["vocab_revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", pin["vocab_revision"]):
        raise ProfileError(f"profile {name}: vocab_revision must be a pinned commit")
    files, expected = _files(name, declared.get("file")), _files(name, pin.get("file"))
    for filename in sorted(set(files) | set(expected)):
        if files.get(filename) != expected.get(filename):
            raise ProfileError(f"profile {name}: sha256 for {filename} must match the manifest")
    route = t.get("margin_route", pin.get("margin_route"))
    if not isinstance(route, str) or not route:
        raise ProfileError(f"profile {name}: margin_route is required")
    margin = None
    if route != "unmeasured":
        margin = manifest.get("route", {}).get(route)
        if not isinstance(margin, dict):
            raise ProfileError(f"profile {name}: unknown margin_route {route!r}")
        factor, constant = margin.get("factor"), margin.get("constant")
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
            raise ProfileError(f"profile {name}: margin_route {route!r} factor must be positive and finite")
        if isinstance(constant, bool) or not isinstance(constant, int) or constant < 0:
            raise ProfileError(f"profile {name}: margin_route {route!r} constant must be non-negative")
        if margin.get("method") != "envelope-v1":
            raise ProfileError(f"profile {name}: margin_route {route!r} method must be envelope-v1")
        margin = dict(margin)
    if route != pin.get("margin_route"):
        raise ProfileError(f"profile {name}: margin_route {route!r} must match the manifest")
    return {"admission": "bounded" if margin is not None else "conservative_bound",
            "vocab_repo": pin["vocab_repo"], "vocab_revision": pin["vocab_revision"], "files": files,
            "margin_route": route, "margin": margin, **structural}


def _profile(name: str, t: dict, manifest: dict) -> Profile:
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
    if backend in ("openrouter", "openai", "claude") and not is_hosted:
        raise ProfileError(f"profile {name}: hosted cannot be false for a hosted profile")
    new_route = (backend in DIRECT_ENDPOINTS or name in STAGED_NAMES or "parameters" in t or "consent" in t
                 or (backend == "openrouter" and bool(t.get("key_item") or t.get("key_env"))
                     and t.get("model") != "qwen/qwen3.5-9b"))
    if new_route:
        _new_route(name, t, backend)
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
    for key in ("max_input_tokens", "context_window"):
        if key == "context_window" and key not in t:
            continue
        value = t.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ProfileError(f"profile {name}: {key} must be a positive whole number, not {value!r}")
    admission = _admission(name, t, manifest)
    if "context_window" not in t and admission["admission"] == "bounded":
        admission["admission"] = "conservative_bound"
    return Profile(name=name, backend=backend, model=t["model"], base_url=t["base_url"], hosted=bool(is_hosted),
                   max_input_tokens=t["max_input_tokens"], context_window=t.get("context_window"),
                   hosted_books=books, key_item=t.get("key_item"), key_env=t.get("key_env"),
                   max_tokens=int(t.get("max_tokens", 2048)), max_parallel=parallel, timeout_s=float(t.get("timeout_s", 180)),
                   temperature=t.get("temperature", 0), reasoning=reasoning, provider=provider,
                   parameters=t.get("parameters", {}), new_route=new_route, terms=t.get("terms"),
                   consent=t.get("consent"),
                   **admission)


def _new_route(name: str, t: dict, backend: str) -> None:
    def unconfirmed(value) -> bool:
        if isinstance(value, str):
            return "UNCONFIRMED" in value.upper()
        if isinstance(value, dict):
            return any(unconfirmed(item) for item in value.values())
        if isinstance(value, list):
            return any(unconfirmed(item) for item in value)
        return False

    for field_name, value in t.items():
        if unconfirmed(value):
            raise ProfileError(f"profile {name}: {field_name} is not confirmed")
    required = {"backend", "model", "base_url", "hosted", "hosted_books", "key_item", "key_env", "max_tokens",
                "max_input_tokens", "context_window", "timeout_s", "temperature", "parameters", "consent"}
    if backend in DIRECT_ENDPOINTS:
        required.add("terms")
    elif backend == "openrouter":
        required.add("provider")
    else:
        raise ProfileError(f"profile {name}: parameter declarations are not supported on this backend")
    missing = required - set(t)
    if missing:
        raise ProfileError(f"profile {name}: required fields are missing: {', '.join(sorted(missing))}")
    if t["hosted"] is not True:
        raise ProfileError(f"profile {name}: hosted must be true")
    for field_name in ("model", "key_item", "key_env", "consent"):
        v = t[field_name]
        if not isinstance(v, str) or not v.strip() or "UNCONFIRMED" in v.upper():
            raise ProfileError(f"profile {name}: {field_name} is not confirmed")
    endpoint = DIRECT_ENDPOINTS.get(backend, "https://openrouter.ai/api/v1")
    if t["base_url"] != endpoint:
        raise ProfileError(f"profile {name}: the endpoint is not approved for this backend")
    if (t["key_item"], t["key_env"]) != DIRECT_KEYS.get(backend, ROUTER_KEY):
        raise ProfileError(f"profile {name}: the credential names do not match the backend")
    if t["consent"] not in APPROVED_CONSENT:
        raise ProfileError(f"profile {name}: consent wording is not approved")
    if backend != "openrouter" and "provider" in t:
        raise ProfileError(f"profile {name}: provider pins are OpenRouter's only")
    if "reasoning" in t and not isinstance(t["reasoning"], dict):
        raise ProfileError(f"profile {name}: reasoning must be a table")
    for key in ("max_tokens", "max_input_tokens", "context_window"):
        if type(t[key]) is not int or t[key] <= 0:
            raise ProfileError(f"profile {name}: {key} must be a positive whole number")
    for key in ("timeout_s", "temperature"):
        v = t[key]
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or (v <= 0 if key == "timeout_s" else not 0 <= v <= 2):
            raise ProfileError(f"profile {name}: {key} is invalid")
    if backend in DIRECT_ENDPOINTS:
        terms = t["terms"]
        if not isinstance(terms, str) or not re.fullmatch(r"D[1-9][0-9]*:(openai|anthropic):[A-Za-z0-9][A-Za-z0-9_.-]*", terms):
            raise ProfileError(f"profile {name}: terms must identify a confirmed decision, provider and account")
        decision, provider, account = terms.split(":")
        if provider != ("anthropic" if backend == "claude" else backend) or (decision, provider, account) not in CONFIRMED_TERMS:
            raise ProfileError(f"profile {name}: account terms are not confirmed")
    elif "terms" in t:
        raise ProfileError(f"profile {name}: direct account terms do not apply to this backend")
    if backend == "openrouter":
        provider = t["provider"]
        if not isinstance(provider, dict):
            raise ProfileError(f"profile {name}: provider pins are required")
        for key, value in (("allow_fallbacks", False), ("data_collection", "deny"), ("zdr", True),
                           ("require_parameters", True)):
            actual = provider.get(key)
            if (actual is not value if isinstance(value, bool) else actual != value):
                raise ProfileError(f"profile {name}: provider safety pins are required")
        for key in ("order", "quantizations"):
            values = provider.get(key)
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() or "UNCONFIRMED" in v.upper() for v in values):
                raise ProfileError(f"profile {name}: {key} must be confirmed")
    params = t["parameters"]
    if not isinstance(params, dict) or set(params) - {"omit", "rename"}:
        raise ProfileError(f"profile {name}: invalid parameters declaration")
    omit, rename = params.get("omit", []), params.get("rename", {})
    if not isinstance(omit, list) or any(x != "temperature" for x in omit) or len(omit) > 1:
        raise ProfileError(f"profile {name}: only temperature may be omitted")
    if not isinstance(rename, dict) or set(rename) - {"max_tokens"}:
        raise ProfileError(f"profile {name}: only the reply cap may be renamed")
    if rename and (backend == "claude" or rename.get("max_tokens") != "max_completion_tokens"):
        raise ProfileError(f"profile {name}: unsupported reply cap name")


def load(text: str | None = None) -> dict[str, Profile]:
    """Every profile, by name: from `text` when given, else from the shipped file."""
    raw = tomllib.loads(text if text is not None else PROFILES_FILE.read_text())
    try:
        manifest = tomllib.loads(PROFILE_PINS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ProfileError(f"admission manifest {PROFILE_PINS_FILE}: {e}") from None
    loaded = {name: _profile(name, t, manifest) for name, t in raw.items()}
    if any(p.renderer == "ERPCLAW-COUNT-v2" for p in loaded.values()):
        table = _load_nfd_table(manifest["structural"].get("nfd_table_sha256"))
        loaded = {name: replace(p, nfd_table=table, admission=p.admission if table is not None else "unavailable")
                  if p.renderer == "ERPCLAW-COUNT-v2" else p for name, p in loaded.items()}
    return loaded


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
        self._inner.profile.require_admission()
        return self._inner.complete(messages, tools)

    def describe(self) -> dict:
        p = self._inner.profile
        description = {"provider": p.backend, "model": p.model, "profile": p.name, "base_url": p.base_url,
                       "hosted": p.hosted, "reasoning": dict(p.reasoning), "pins": dict(p.provider),
                       "max_tokens": p.max_tokens, "temperature": p.temperature, "max_parallel": p.max_parallel}
        if p.new_route:
            description["parameters"] = dict(p.parameters)
        return description

    def check_ready(self) -> str | None:
        from erpclaw_core.common import Refusal

        try:
            self._inner.profile.require_admission()
        except Refusal as e:
            return str(e)
        try:
            ids = [x.id for x in self._inner.client.models.list().data]
        except Exception as e:  # noqa: BLE001  (any failure to reach the server is reported, not raised)
            return f"The model server at {self._inner.base_url} is not reachable ({type(e).__name__})."
        if self._inner.name not in ids:
            return f"Model {self._inner.name!r} is not offered at {self._inner.base_url}."
        return None


class _UnavailableModel:
    """Keep startup independent of a failed package-data installation."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.hosted, self.hosted_books = profile.hosted, profile.hosted_books
        self.name, self.base_url = profile.model, profile.base_url
        self.usage: list[dict] = []

    def complete(self, messages: list[dict], tools: list[dict]):
        _refuse_unavailable()


def from_env() -> ProfileModel:
    """The model named by ERPCLAW_MODEL_PROFILE."""
    from .model import OpenAICompatModel

    name = os.environ.get("ERPCLAW_MODEL_PROFILE", "")
    shipped = load()
    if name not in shipped:
        raise ProfileError(f"ERPCLAW_MODEL_PROFILE must name one of {', '.join(sorted(shipped))}, not {name!r}.")
    profile = shipped[name]
    if profile.backend == "claude":
        raise ProfileError("The claude model adapter is not available yet.")
    if profile.admission == "unavailable":
        return ProfileModel(_UnavailableModel(profile))
    return ProfileModel(OpenAICompatModel(profile=profile))
