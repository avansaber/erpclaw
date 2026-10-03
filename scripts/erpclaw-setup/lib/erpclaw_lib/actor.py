"""Per-process actor context for audit rows.

Every audit row written through a normal connection records five facts about
the process that wrote it, resolved once per process by the reader here. The
operating-system account is a fact the process cannot change. The channel, the
principal claim and the hop list arrive from the launcher through the
environment, so on shell runtimes the model writes both the command line and
the environment and everything except the operating-system account is a claim,
not evidence. No caller may treat the channel or the principal as proof of who
acted. The status value ``attested`` is reserved for a later change that adds
server-created sessions, the only thing that can attest an identity; it is
never produced here. No function here takes an actor from a caller: the reader
below derives the context from the process itself.
"""

import json
import os
import re
from dataclasses import dataclass, field

try:
    import pwd
except ImportError:
    pwd = None

ENV_VAR = "ERPCLAW_ACTOR_CONTEXT"
MAX_BYTES = 1024
MAX_HOPS = 4
MAX_HOP_TEXT = 64
CHANNELS = ("mcp", "cross-skill", "cli")
ABSENT = "absent"
CLAIMED = "claimed"
INVALID = "invalid"
ATTESTED = "attested"

# Must equal the principal id syntax used by the consumption primitive.
_PRINCIPAL_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

_REQUIRED_KEYS = ("v", "channel", "principal", "hop")


@dataclass(frozen=True)
class ActorContext:
    os_account: object
    channel: object
    principal_claim: object
    hop: tuple = field(default_factory=tuple)
    status: str = ABSENT


def os_account():
    """The account the process runs as, or a uid label, or None.

    Never raises.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return None
    try:
        uid = geteuid()
    except Exception:
        return None
    if pwd is None:
        return "uid:%d" % uid
    try:
        return pwd.getpwuid(uid).pw_name
    except Exception:
        return "uid:%d" % uid


def _invalid(account):
    return ActorContext(account, None, None, (), INVALID)


def resolve(environ=None):
    """Read the actor context from a mapping (default the process environment).

    Never raises. Anything present but malformed resolves to ``invalid``.
    """
    if environ is None:
        environ = os.environ
    try:
        account = os_account()
    except Exception:
        account = None
    try:
        if ENV_VAR not in environ:
            return ActorContext(account, None, None, (), ABSENT)
        raw = environ[ENV_VAR]
        if not isinstance(raw, str):
            return _invalid(account)
        if len(raw.encode("utf-8", errors="surrogateescape")) > MAX_BYTES:
            return _invalid(account)
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            return _invalid(account)
        if type(value) is not dict:
            return _invalid(account)
        for key in _REQUIRED_KEYS:
            if key not in value:
                return _invalid(account)
        version = value["v"]
        if type(version) is not int or version != 1:
            return _invalid(account)
        channel = value["channel"]
        if channel not in CHANNELS:
            return _invalid(account)
        principal = value["principal"]
        if principal is not None:
            if type(principal) is not str:
                return _invalid(account)
            if _PRINCIPAL_RE.fullmatch(principal) is None:
                return _invalid(account)
        hop = value["hop"]
        if type(hop) is not list:
            return _invalid(account)
        if len(hop) > MAX_HOPS:
            return _invalid(account)
        for entry in hop:
            if type(entry) is not str:
                return _invalid(account)
        status = CLAIMED if principal is not None else ABSENT
        return ActorContext(account, channel, principal, tuple(hop), status)
    except Exception:
        try:
            account = os_account()
        except Exception:
            account = None
        return _invalid(account)


_cached = None
_have_cached = False


def current():
    """The process context, computed once and memoised."""
    global _cached, _have_cached
    if _have_cached:
        return _cached
    _cached = resolve(os.environ)
    _have_cached = True
    return _cached


def _reset_cache():
    """Clear the memoised process context (for tests only)."""
    global _cached, _have_cached
    _cached = None
    _have_cached = False


def encode(channel, principal, hop):
    """Render a context value for a child environment."""
    return json.dumps(
        {"v": 1, "channel": channel, "principal": principal, "hop": list(hop)},
        separators=(",", ":"),
        sort_keys=True,
    )


def _clean(text):
    out = []
    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7E and ch != '"' and ch != "\\":
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)[:MAX_HOP_TEXT]


def child_env(base, hop):
    """Derive a child environment with the hop appended.

    Returns a new dict and never mutates ``base``.
    """
    env = dict(base)
    parent = resolve(base)
    if parent.status == INVALID:
        return env
    if parent.status == ABSENT:
        env.pop(ENV_VAR, None)
        return env
    if parent.channel == "cli":
        channel = "cross-skill"
    else:
        channel = parent.channel
    hops = [_clean(h) for h in parent.hop + (hop,)][-MAX_HOPS:]
    env[ENV_VAR] = encode(channel, parent.principal_claim, hops)
    return env


def audit_values(ctx):
    """The five stored values for an actor context."""
    if ctx.channel is None:
        hop_text = None
    else:
        hop_text = json.dumps(list(ctx.hop), separators=(",", ":"))
    return (ctx.os_account, ctx.channel, ctx.principal_claim, ctx.status, hop_text)
