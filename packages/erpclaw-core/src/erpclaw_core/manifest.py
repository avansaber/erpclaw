"""Plugin manifests: what a plugin is, needs, owns and offers.

Every plugin ships one, validated when it loads; a missing or unknown field
refuses the plugin. Tier 1 plugins are first-party code running in the
engine's process: the manifest makes our own mistakes fail loudly, it does
not contain hostile code.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator

PUBLISHER = "AvanSaber Inc"
# Third-party execution is off in v3.0: only these distributions may load.
FIRST_PARTY_DISTRIBUTIONS = frozenset({"erpclaw-sales", "erpclaw-email-folder", "erpclaw-stock", "erpclaw-email"})
LICENCES = frozenset({"GPL-3.0-only", "GPL-3.0-or-later", "LGPL-3.0-or-later", "MIT", "BSD-2-Clause",
                      "BSD-3-Clause", "Apache-2.0"})
EFFECTS = ("read", "draft", "write", "destructive")
Effect = Literal["read", "draft", "write", "destructive"]
SERVICE_EFFECTS = ("read", "write")
NAME = r"[a-z][a-z0-9_]{1,39}"
# The optional fields a manifest leaves out of its digest while they are empty, so a manifest written before they
# existed keeps the digest its books recorded.
DIGEST_OPTIONAL = ("services", "uses")


def parse_use(entry: str) -> tuple[str, bool]:
    """A uses entry as (the plain "<plugin>.<service>", whether it is optional): the one place that reads the
    optional mark, a single trailing "?". Every reader of uses goes through it."""
    return (entry[:-1], True) if entry.endswith("?") else (entry, False)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Strict):
    repository: str
    commit: str


class SlotDecl(_Strict):
    cardinality: Literal["one", "many"]
    selection: str


class Events(_Strict):
    emits: list[str]
    consumes: list[str]


class Capabilities(_Strict):
    network: list[str]
    files: list[str]


class ServiceDecl(_Strict):
    """A service this plugin offers other plugins: called inside the caller's transaction, never by the model."""
    name: str = Field(pattern=rf"^{NAME}$")
    effect: Literal["read", "write"]


class Manifest(_Strict):
    name: str = Field(pattern=rf"^{NAME}$")
    version: str
    publisher: str
    distribution: str
    licence: str
    source: Source
    engine: str
    after_core_revision: str
    depends: dict[str, str]
    conflicts: list[str]
    owned_tables: list[str]
    contracts: dict[str, Effect]
    calls: list[str]
    events: Events
    slots: dict[str, SlotDecl]
    voucher_types: list[str]
    capabilities: Capabilities
    metrics: list[str]
    services: list[ServiceDecl] = Field(default_factory=list)
    # "<plugin>.<service>", or "<plugin>.<service>?" for an optional use (plugin/3): see parse_use
    uses: list[Annotated[str, Field(pattern=rf"^{NAME}\.{NAME}\??$")]] = Field(default_factory=list)

    @field_validator("services")
    @classmethod
    def _services(cls, v: list[ServiceDecl]) -> list[ServiceDecl]:
        names = [s.name for s in v]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValueError(f"service {twice[0]} is declared more than once")
        return v

    @field_validator("uses")
    @classmethod
    def _uses(cls, v: list[str]) -> list[str]:
        names = [parse_use(u)[0] for u in v]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValueError(f"{twice[0]} is listed in uses more than once")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        try:
            Version(v)
        except InvalidVersion:
            raise ValueError(f"{v!r} is not a PEP 440 version") from None
        return v

    @field_validator("engine")
    @classmethod
    def _engine(cls, v: str) -> str:
        try:
            SpecifierSet(v)
        except InvalidSpecifier:
            raise ValueError(f"{v!r} is not a version specifier") from None
        return v

    @field_validator("depends")
    @classmethod
    def _depends(cls, v: dict[str, str]) -> dict[str, str]:
        for name, spec in v.items():
            try:
                SpecifierSet(spec)
            except InvalidSpecifier:
                raise ValueError(f"dependency {name}: {spec!r} is not a version specifier") from None
        return v

    @field_validator("licence")
    @classmethod
    def _licence(cls, v: str) -> str:
        if v not in LICENCES:
            raise ValueError(f"licence {v!r} is not in the allowed set")
        return v

    def digest(self) -> str:
        """sha256 of the manifest's canonical JSON. `services` and `uses` are left out while empty, so a manifest
        without them has the digest it had before they existed, and books installed then still open."""
        body = self.model_dump(mode="json")
        for key in DIGEST_OPTIONAL:
            if not body[key]:
                del body[key]
        body = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()


def artifact_digest(package_dir: Path) -> str:
    """sha256 over (relative path, sha256 of bytes) for every file of a
    plugin's top-level package, compiled caches excluded. The same for a
    wheel and a workspace install, so changed code changes it."""
    entries = []
    for f in sorted(package_dir.rglob("*")):
        if f.is_dir() or "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        entries.append([f.relative_to(package_dir).as_posix(), hashlib.sha256(f.read_bytes()).hexdigest()])
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()
