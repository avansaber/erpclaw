"""Literal, exhaustive snapshots with bound generated values, including JSON references."""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta

from sqlalchemy import DateTime

from erpclaw_core.backup import books_tables
from .run import snapshot

UUID_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
TOKEN = re.compile(rf"{UUID_TEXT}|[0-9a-f]{{64}}|[0-9a-f]{{32}}", re.I)
PLACEHOLDER = re.compile(r"<(uuid|hex32|hex64):([1-9][0-9]*)>")
ORIGIN = "<chain-origin>"
PREFIX = re.compile(r"(<hex64:[1-9][0-9]*):prefix16>")
PATTERNS = {"uuid": UUID_TEXT, "hex32": r"[0-9a-f]{32}", "hex64": r"[0-9a-f]{64}"}


class Mismatch(ValueError):
    pass


def effective_rule(table, column, storage_rule):
    if table == "audit_log" and column == "detail":
        return "json-bound-tokens"
    if column.endswith(("_digest", "_sha256", "_hash")):
        return "hex64"
    if table == "approval" and column == "impl_version":
        return "implementation-version"
    return storage_rule


def take_snapshot(engine):
    return {t: list(rows.values()) for t, rows in snapshot(engine).items()}


def column_rules(engine):
    """A rule for EVERY column, including columns in currently empty tables."""
    rules = {}
    with engine.connect() as conn:
        for table in books_tables(conn):
            for c in table.columns:
                if isinstance(c.type, DateTime):
                    rule = "timestamp+600s" if c.name == "expires_at" else "timestamp"
                elif c.name.endswith("_json"):
                    rule = "json-bound-tokens"
                elif c.name == "nonce":
                    rule = "hex32"
                elif c.name in {"hash", "prev_hash", "last_hash", "digest", "card_digest", "live_token",
                                "request_card_token"}:
                    rule = "hex64"
                elif getattr(c.type, "length", None) == 36:
                    rule = "uuid"
                else:
                    rule = "literal"
                rules[f"{table.name}.{c.name}"] = rule
    return rules


class FixtureCodec:
    """Encode or validate bindings in chronological snapshot/event order."""

    def __init__(self, *, validate=False):
        self.validate = validate
        self.names = {}
        self.counts = dict.fromkeys(PATTERNS, 0)

    def generated(self, value, kinds):
        if self.validate:
            match = PLACEHOLDER.fullmatch(value)
            if not match or match[1] not in kinds:
                raise ValueError("fixture generated value must be a typed placeholder")
            kind = match[1]
            if value not in self.names:
                self.counts[kind] += 1
                if int(match[2]) != self.counts[kind]:
                    raise ValueError("fixture placeholders must follow first-appearance order")
                self.names[value] = value
            return value
        kind = next((k for k in kinds if re.fullmatch(PATTERNS[k], value, re.I)), None)
        if kind is None:
            raise ValueError("invalid generated value in capture")
        if value not in self.names:
            self.counts[kind] += 1
            self.names[value] = f"<{kind}:{self.counts[kind]}>"
        return self.names[value]

    def text(self, value):
        if self.validate:
            if TOKEN.search(value):
                raise ValueError("fixture contains a literal generated value")
            return PLACEHOLDER.sub(lambda m: self.generated(m[0], PATTERNS), value)
        return TOKEN.sub(lambda m: ORIGIN if m[0] == "0" * 64 else self.generated(m[0], PATTERNS), value)

    def event(self, value):
        if isinstance(value, dict):
            return {k: self.event(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.event(v) for v in value]
        return self.text(value) if isinstance(value, str) else value

    def value(self, value, rule):
        if value is None:
            return None
        if rule == "uuid":
            return self.generated(value, ("uuid",))
        if rule.startswith("hex"):
            if value == ORIGIN or (not self.validate and value == "0" * 64):
                return ORIGIN
            return self.generated(value, ("hex32",) if rule == "hex32" else ("hex32", "hex64"))
        if rule == "json-bound-tokens":
            return json.dumps(self.event(json.loads(value)), separators=(",", ":"))
        if rule == "implementation-version":
            version, sep, suffix = value.partition("+")
            if self.validate:
                match = PREFIX.fullmatch(suffix)
                if not sep or not match or match[1] + ">" not in self.names:
                    raise ValueError("fixture implementation prefix must reference a bound artifact")
                return value
            full = next((v for v in self.names if len(v) == 64 and v.startswith(suffix)), None)
            if not sep or len(suffix) != 16 or full is None:
                raise ValueError("capture implementation prefix has no artifact binding")
            return version + "+" + self.names[full][:-1] + ":prefix16>"
        return value

    def books(self, books, rules):
        return {table: [{c: self.value(v, effective_rule(table, c, rules[f"{table}.{c}"]))
                         for c, v in row.items()} for row in rows] for table, rows in books.items()}

    def trial(self, capture):
        out = copy.deepcopy(capture)
        out["before"] = self.books(capture["before"], capture["rules"])
        out["steps"] = [{"owner": self.event(s["owner"]), "events": self.event(s["events"]),
                         "books": self.books(s["books"], capture["rules"])} for s in capture["steps"]]
        out["after"] = self.books(capture["after"], capture["rules"])
        # Metadata and literal columns must not conceal raw generated tokens either.
        if TOKEN.search(json.dumps(out)):
            raise ValueError("fixture contains a literal generated value")
        return out


def placeholder_fixture(capture):
    return FixtureCodec().trial(capture)


def validate_fixture(fixture):
    FixtureCodec(validate=True).trial(fixture)


class Matcher:
    """Bindings span seed, all turns, card and commit: a changed reference cannot hide behind UUID shape."""

    def __init__(self, rules, window):
        self.rules, self.window = rules, window
        self.bindings = {}
        self.reverse = {}

    def bind(self, expected, actual, pattern, path):
        if not PLACEHOLDER.fullmatch(expected):
            raise Mismatch(f"{path}: fixture requires a generated-value placeholder")
        if not isinstance(actual, str) or not re.fullmatch(pattern, actual, re.I):
            raise Mismatch(f"{path}: invalid generated value {actual!r}")
        if expected in self.bindings:
            if self.bindings[expected] != actual:
                raise Mismatch(f"{path}: broken generated-value reference")
        else:
            if actual in self.reverse and self.reverse[actual] != expected:
                raise Mismatch(f"{path}: generated values alias")
            self.bindings[expected] = actual
            self.reverse[actual] = expected

    def text(self, expected, actual, path):
        if not isinstance(actual, str):
            raise Mismatch(f"{path}: expected text")
        parts = re.split(r"(<(?:uuid|hex32|hex64):[1-9][0-9]*>|<chain-origin>)", expected)
        pattern = "".join(f"((?i:{PATTERNS[PLACEHOLDER.fullmatch(p)[1]]}))" if PLACEHOLDER.fullmatch(p)
                          else "(" + "0" * 64 + ")" if p == ORIGIN else re.escape(p) for p in parts)
        match = re.fullmatch(pattern, actual)
        if not match:
            raise Mismatch(f"{path}: text differs: {actual!r}")
        for e, a in zip(parts[1::2], match.groups()):
            if e != ORIGIN:
                self.bind(e, a, PATTERNS[PLACEHOLDER.fullmatch(e)[1]], path)

    def value(self, expected, actual, path, rule="literal"):
        if expected is None:
            if actual is not None:
                raise Mismatch(f"{path}: expected NULL, got {actual!r}")
            return
        if rule == "uuid":
            self.bind(expected, actual, UUID_TEXT, path)
        elif rule.startswith("hex"):
            if expected == ORIGIN:
                self.value("0" * 64, actual, path)
            else:
                # The schema's capacity is 64; request tokens in this engine contain 32 hex characters.
                # Preserve the captured width rather than assuming storage capacity is token width.
                match = PLACEHOLDER.fullmatch(expected)
                if not match or match[1] not in ({"hex32"} if rule == "hex32" else {"hex32", "hex64"}):
                    raise Mismatch(f"{path}: fixture requires a hex placeholder")
                self.bind(expected, actual, PATTERNS[match[1]], path)
        elif rule.startswith("timestamp"):
            if expected in self.bindings:
                if actual != self.bindings[expected]:
                    raise Mismatch(f"{path}: timestamp reference changed")
                return
            try:
                dt = datetime.fromisoformat(actual)
                lo, hi = map(datetime.fromisoformat, self.window)
                if not lo <= dt <= hi + (timedelta(seconds=600) if rule.endswith("600s") else timedelta()):
                    raise ValueError("outside capture window")
            except (ValueError, TypeError):
                raise Mismatch(f"{path}: timestamp outside run window: {actual!r}") from None
            self.bindings[expected] = actual
        elif rule == "json-bound-tokens":
            try:
                self.event(json.loads(expected), json.loads(actual), path)
            except (TypeError, json.JSONDecodeError):
                raise Mismatch(f"{path}: invalid JSON") from None
        elif rule == "implementation-version":
            version, _, digest = expected.partition("+")
            aversion, _, adigest = str(actual).partition("+")
            if version != aversion or not re.fullmatch("[0-9a-f]{16}", adigest):
                raise Mismatch(f"{path}: implementation version differs")
            reference = PREFIX.fullmatch(digest)
            full = self.bindings.get(reference[1] + ">") if reference else None
            if full is None or not full.startswith(adigest):
                raise Mismatch(f"{path}: implementation artifact reference differs")
        elif expected != actual:
            raise Mismatch(f"{path}: expected {expected!r}, got {actual!r}")

    def event(self, expected, actual, path="events"):
        if type(expected) is not type(actual):
            raise Mismatch(f"{path}: different type")
        if isinstance(expected, dict):
            if expected.keys() != actual.keys():
                raise Mismatch(f"{path}: different fields")
            for key in expected:
                self.event(expected[key], actual[key], f"{path}.{key}")
        elif isinstance(expected, list):
            if len(expected) != len(actual):
                raise Mismatch(f"{path}: row/event count differs ({len(actual)} != {len(expected)})")
            for i, (e, a) in enumerate(zip(expected, actual)):
                self.event(e, a, f"{path}[{i}]")
        elif isinstance(expected, str):
            self.text(expected, actual, path)
        elif expected != actual:
            raise Mismatch(f"{path}: expected {expected!r}, got {actual!r}")

    def row(self, table, expected, actual):
        if expected.keys() != actual.keys():
            raise Mismatch(f"{table}: columns differ")
        # Literal columns first narrow the unordered row matching before binding any identifiers.
        keys = sorted(expected, key=lambda c: self.rules[f"{table}.{c}"] != "literal")
        for c in keys:
            rule = effective_rule(table, c, self.rules[f"{table}.{c}"])
            self.value(expected[c], actual[c], f"{table}.{c}", rule)

    def rows(self, table, expected, actual):
        if len(expected) != len(actual):
            raise Mismatch(f"{table}: row count differs ({len(actual)} != {len(expected)})")

        def match(rest, available):
            if not rest:
                return
            first_error = None
            for i, row in enumerate(available):
                saved = self.bindings.copy(), self.reverse.copy()
                try:
                    self.row(table, rest[0], row)
                    match(rest[1:], available[:i] + available[i + 1:])
                    return
                except Mismatch as exc:
                    first_error = first_error or exc
                    self.bindings, self.reverse = saved
            raise first_error or Mismatch(f"{table}: missing row")

        match(expected, actual)

    def books(self, expected, actual):
        if expected.keys() != actual.keys():
            raise Mismatch("books: table set differs")
        for table in expected:
            self.rows(table, expected[table], actual[table])


def compare_capture(fixture, capture):
    """No ignored tables, rows, columns, extra events or intermediate snapshots."""
    if fixture["rules"] != capture["rules"]:
        return "schema/column rules differ"
    matcher = Matcher(fixture["rules"], capture["window"])
    try:
        matcher.books(fixture["before"], capture["before"])
        matcher.window = [capture.get("trial_started", capture["window"][0]), capture["window"][1]]
        if len(fixture["steps"]) != len(capture["steps"]):
            raise Mismatch("steps: owner message/approval count differs")
        for e, a in zip(fixture["steps"], capture["steps"]):
            matcher.event(e["owner"], a["owner"], "owner")
            matcher.event(e["events"], a["events"])
            matcher.books(e["books"], a["books"])
        matcher.books(fixture["after"], capture["after"])
    except Mismatch as exc:
        return str(exc)
    return None


def premature(before, mid, *, fence_checked=False):
    """Permit a new pending card; a fence requires successful per-turn convention checks first."""
    for table in before.keys() | mid.keys():
        old, new = before.get(table, []), mid.get(table, [])
        if sorted(old, key=repr) == sorted(new, key=repr):
            continue
        if table == "conversation_fence" and fence_checked:
            continue
        if table == "approval":
            added = [r for r in new if r not in old]
            if all(r in new for r in old) and len(added) == 1 and added[0].get("status") == "pending":
                continue
        return f"premature write: {table}"
    return None
