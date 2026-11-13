"""The engine's half of the "no technical text reaches the owner" checker (the owner-facing text rules).

`owner_text_violations(text)` reads tests/owner_text_rules.json (the host's C# helper reads the same file) and, for
the rules that name the engine's registries, the engine as it is loaded now: the plugin registry (contracts, tools,
metrics, voucher types, services, every contract's field names), the SQLAlchemy metadata (tables, columns, named
constraints and indexes) and the engine's source (every Refusal code it constructs and every event code it writes).
Each violation is one string naming the rule, its pattern and the text that broke it. Allow entries may restrict
their mask to one rule and to engine-rendered document line prefixes; other rules still check those spans.

`owner_strings(event)` lists the owner-facing strings of one turn event: its text, a question's options
and choice labels, a card's title, lines, books, warnings and footer; never `code`, ids or other program fields.
"""
from __future__ import annotations

import ast
import json
import re
import typing
import uuid
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = Path(__file__).with_name("owner_text_rules.json")
SOURCE_ROOTS = [ROOT / "packages", ROOT / "plugins"]
FLAGS = {"": 0, "i": re.IGNORECASE}
DOTTED = re.compile(r"(?<![\w.@/:-])[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,2}(?![\w-]|\.[a-z0-9])")
FIELD = re.compile(r"\{([^{}]*)\}")


class DeferredOwnerText(AssertionError):
    """Only a verified, enumerated owner-text failure may satisfy a strict xfail."""


def refusal_location(refusal) -> tuple[str, int]:
    """The deepest engine frame where this refusal was raised."""
    tb = refusal.__traceback__
    assert tb is not None, "the refusal must actually be raised"
    while tb.tb_next:
        tb = tb.tb_next
    return str(Path(tb.tb_frame.f_code.co_filename).relative_to(ROOT)), tb.tb_lineno


RUNTIME_REFUSALS = {}


def expected_refusal_codes():
    from erpclaw_assistant.graph import ASKED
    from test_owner_text_sweep_turns import DIRECT_CODES

    return (declared_refusal_codes() | DIRECT_CODES
            | {f"{part}_choice_missing" for part in ASKED}
            | {f"{what}_near_ambiguous" for what in ("customer", "supplier", "item")})


def runtime_coverage_skip_reason(session):
    """Coverage requires all three complete producer modules in this session."""
    producers = {f"test_owner_text_sweep_{part}.py" for part in ("static", "turns", "routes")}
    missing = producers - {item.path.name for item in session.items}
    option = session.config.option
    filters = [name for name in ("keyword", "markexpr", "deselect", "lf") if getattr(option, name, None)]
    if missing or filters or any("::" in str(arg) for arg in session.config.args):
        return ("runtime coverage skipped: partial owner-text selection; run all three complete sweep files "
                "without selection filters (missing files: " + ", ".join(sorted(missing))
                + "; filters: " + ", ".join(filters) + ")")
    return None


@pytest.fixture(scope="session")
def refusal_code_report(request):
    """Session teardown runs after every producer, regardless of file ordering."""
    reached = RUNTIME_REFUSALS
    yield reached
    print(f"\nRuntime refusal codes reached: {len(reached)}")
    for code, routes in sorted(reached.items()):
        print(f"{code}: {sorted(routes)[0]}")
    reason = runtime_coverage_skip_reason(request.session)
    if reason:
        print(reason)
        return
    expected = expected_refusal_codes()
    missing = expected - reached.keys()
    assert not missing, ("refusal codes without a runtime case", sorted(missing))
    print(f"All {len(expected)} declared codes and concrete family members reached; "
          f"{len(reached)} distinct runtime codes checked")


@pytest.fixture(autouse=True)
def observe_refusal_codes(request, monkeypatch, refusal_code_report):
    """Retain each real construction's code, source and reaching test for the report."""
    import sys

    from erpclaw_core.common import Refusal, is_question

    original = Refusal.__init__

    def record(self, *args, **kwargs):
        original(self, *args, **kwargs)
        caller = sys._getframe(1)
        path = Path(caller.f_code.co_filename)
        if path.is_relative_to(ROOT) and path.parts[len(ROOT.parts)] in ("packages", "plugins"):
            where = f"{path.relative_to(ROOT)}:{caller.f_lineno}"
            route = f"{request.node.nodeid} ({where})"
            refusal_code_report.setdefault(self.code, set()).add(route)
            event = {"type": "ask" if is_question(self.code) else "refused", "code": self.code,
                     "text": self.message, "options": self.options, "data": self.data}
            for text in owner_strings(event):
                request.node.__dict__.setdefault("owner_refusals", []).append({
                    "text": text, "file": str(path.relative_to(ROOT)),
                    "line": caller.f_lineno, "code": self.code, "event": event,
                })

    monkeypatch.setattr(Refusal, "__init__", record)


@lru_cache(maxsize=None)
def rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


# --- the engine's registries, read at test time ------------------------------------------------------------------

def source_files() -> list[Path]:
    return sorted(p for root in SOURCE_ROOTS for p in root.glob("*/src/**/*.py") if "migrations" not in p.parts)


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def refusal_calls() -> list[tuple[Path, ast.Call]]:
    """Every `Refusal(...)` construction in the engine's source, with its file."""
    out = []
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out += [(path, n) for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) == "Refusal"]
    return out


def refusal_code(node: ast.Call) -> ast.expr | None:
    """The code argument of a Refusal construction (the second positional or `code=`), or None for the default."""
    if len(node.args) > 1:
        return node.args[1]
    return next((k.value for k in node.keywords if k.arg == "code"), None)


def _pattern_of(node: ast.JoinedStr) -> re.Pattern:
    """An f-string code as a pattern: its literal parts, each placeholder an identifier's characters."""
    parts = [re.escape(v.value) if isinstance(v, ast.Constant) else "[a-z0-9_]+" for v in node.values]
    return re.compile("".join(parts))


def _model_fields(model, seen: set) -> set[str]:
    """A Pydantic model's field names, its nested models' too."""
    if model in seen:
        return set()
    seen.add(model)
    names = set(model.model_fields)
    for f in model.model_fields.values():
        stack = [f.annotation]
        while stack:
            t = stack.pop()
            if isinstance(t, type) and hasattr(t, "model_fields"):
                names |= _model_fields(t, seen)
            stack += [a for a in typing.get_args(t) if a is not None]
    return names


@lru_cache(maxsize=None)
def registry() -> dict:
    from erpclaw_assistant import graph
    from erpclaw_core import common
    from erpclaw_core.db import metadata
    from erpclaw_core.plugins import REGISTRY, load_plugins

    load_plugins()
    reg: dict = {k: set() for k in ("contracts", "tools", "metrics", "voucher_types", "services", "contract_fields",
                                    "tables", "columns", "constraints", "plugins", "refusal_codes", "event_codes",
                                    "api_error_codes")}
    reg["contracts"] = set(REGISTRY.contracts)
    reg["tools"] = set(REGISTRY.contracts) | {graph.FIND_TOOL["function"]["name"], graph.CHOOSE_TOOL["function"]["name"]}
    reg["metrics"] = set(REGISTRY.metrics)
    reg["services"] = set(REGISTRY.services)
    reg["plugins"] = set(REGISTRY.plugins)
    for p in REGISTRY.plugins.values():
        reg["voucher_types"] |= set(p.manifest.voucher_types)
    seen: set = set()
    for c in REGISTRY.contracts.values():
        reg["contract_fields"] |= _model_fields(c.input_model, seen)
    for t in metadata.tables.values():
        reg["tables"].add(t.name)
        reg["columns"] |= {c.name for c in t.columns}
        reg["constraints"] |= {c.name for c in t.constraints if isinstance(c.name, str)}
        reg["constraints"] |= {i.name for i in t.indexes if isinstance(i.name, str)}
    patterns = [re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*_(?:" + "|".join(s.lstrip("_") for s in
                                                                            common.QUESTION_SUFFIXES) + ")")]
    for _, node in refusal_calls():
        code = refusal_code(node)
        if code is None:
            reg["refusal_codes"].add("refused")  # Refusal's default
        elif isinstance(code, ast.Constant) and isinstance(code.value, str):
            reg["refusal_codes"].add(code.value)
        elif isinstance(code, ast.JoinedStr) and "_" in "".join(
                v.value for v in code.values if isinstance(v, ast.Constant)):  # never a bare placeholder
            patterns.append(_pattern_of(code))
    for path in source_files():  # every event code the engine writes: {"code": "..."} in a dict it builds
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value == "code" and isinstance(v, ast.Constant) \
                            and isinstance(v.value, str):
                        reg["event_codes"].add(v.value)
    rule4 = next(r for r in rules()["rules"] if r["rule"] == 4)
    reg["api_error_codes"] = set(rule4["api_error_codes"])
    reg["code_patterns"] = patterns
    return reg


def credential_shapes() -> list[tuple[str, re.Pattern]]:
    """The hosted-model guard's CREDENTIAL_SHAPES when the engine defines them (erpclaw_assistant.hosted); else none beyond the
    rule file's copy."""
    try:
        from erpclaw_assistant import hosted  # type: ignore[attr-defined]
    except ImportError:
        return []
    return [(label, shape) for label, shape in getattr(hosted, "CREDENTIAL_SHAPES", [])]


# --- the compiled checker -----------------------------------------------------------------------------------------

def _alternation(words) -> str:
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


def _identifier_patterns(names: set[str]) -> list[re.Pattern]:
    """Rule's registered names by `identifier_form`: snake_case whole, a one-word name only where shown as a name."""
    form = rules()["identifier_form"]
    snake = {n for n in names if "_" in n}
    single = {n for n in names if "_" not in n and "." not in n and n}
    dotted = {n for n in names if "." in n}
    out = []
    if snake | dotted:
        out.append(re.compile(form["whole_word"].replace("{w}", "(?:" + _alternation(snake | dotted) + ")")))
    if single:
        out.append(re.compile(form["presented"].replace("{w}", "(?:" + _alternation(single) + ")")))
    return out


@lru_cache(maxsize=None)
def _compiled() -> dict:
    data, reg = rules(), registry()
    fixed: list[tuple[int, str, re.Pattern]] = []
    for r in data["rules"]:
        for p in r["patterns"]:
            regex = p["regex"].replace("{libraries}", _alternation(r.get("libraries", [])))
            fixed.append((r["rule"], p["id"], re.compile(regex, FLAGS[p["flags"]])))
    fixed += [(7, f"credential_shapes.{label}", shape) for label, shape in credential_shapes()]
    fixed += [(4, "code_pattern", re.compile(r"(?<![A-Za-z0-9_])(?:" + p.pattern + r")(?![A-Za-z0-9_])"))
              for p in reg["code_patterns"][1:]]
    registered = {
        3: ("tables", "constraints"),
        4: ("refusal_codes", "event_codes", "api_error_codes"),
        5: ("contracts", "tools", "metrics", "voucher_types", "services", "contract_fields", "columns"),
    }
    for rule, kinds in registered.items():
        names = set().union(*(reg[k] for k in kinds))
        fixed += [(rule, "registered:" + "+".join(kinds), p) for p in _identifier_patterns(names)]
    identifiers = set().union(*(reg[k] for k in ("contracts", "tools", "metrics", "voucher_types", "contract_fields",
                                                 "tables", "columns", "plugins")))
    allow = [(a, re.compile(a["regex"], FLAGS[a.get("flags", "")]))
             for a in data["allow"] if a.get("regex")]
    return {"fixed": fixed, "identifiers": identifiers, "allow": allow}


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


def _allowed_urls() -> list[str]:
    return [u for a in rules()["allow"] if a["id"] == "allowed_url" for u in a.get("urls", [])]


def owner_text_violations(text: str, *, code: str | None = None, planted=(),
                          card_preview_line: bool = False, invoice_line_segments=(), resend_id_span=()) -> list[str]:
    """Every rule the text breaks, as "rule N (pattern): 'what matched'"; [] for a text the owner may read.
    `code` is the response's own code (rule 4); `planted` are exact secret values a test planted (rule 7).
    `card_preview_line` identifies a card's `lines`; `invoice_line_segments` identifies
    the rendered labels of the specific invoice-line question, never arbitrary prose."""
    c = _compiled()
    spans = []
    per_rule: dict[int, list[tuple[int, int]]] = {}
    if resend_id_span:
        per_rule[5] = list(resend_id_span)
    for entry, pattern in c["allow"]:
        if entry.get("context") == "card_preview_line":
            segments = [(0, len(text))] if card_preview_line else (
                invoice_line_segments if entry.get("question") else [])
            matches = [(start + m.start(), start + m.end()) for start, end in segments
                       for m in pattern.finditer(text[start:end])]
        else:
            matches = [m.span() for m in pattern.finditer(text)]
        if "rules" not in entry:
            spans.extend(matches)
        else:
            for rule in entry["rules"]:
                per_rule.setdefault(rule, []).extend(matches)
    for url in _allowed_urls():
        spans += [m.span(1) for m in re.finditer("(" + re.escape(url) + r")(?=[\s.,;:!?)\]]|$)", text)]
    masked = _mask(text, spans)
    found: list[str] = []
    for rule, pid, pattern in c["fixed"]:
        rule_text = _mask(masked, per_rule[rule]) if rule in per_rule else masked
        found += [f"rule {rule} ({pid}): {m.group(0)!r}" for m in pattern.finditer(rule_text)]
    for m in DOTTED.finditer(masked):  # a dotted name whose every part the engine registers (rule 5)
        if all(part in c["identifiers"] for part in m.group(0).split(".")):
            found.append(f"rule 5 (dotted): {m.group(0)!r}")
    if code:  # the response's own code
        found += [f"rule 4 (own_code): {m.group(0)!r}" for p in _identifier_patterns({code}) for m in p.finditer(masked)]
    found += [f"rule 7 (planted): {v[:4]!r}..." for v in planted if v and v in text]
    return list(dict.fromkeys(found))


# --- what of an event the owner reads ----------------------------------------------------------------------------

def _owner_text_parts(event: dict) -> list[tuple[str, str]]:
    """The owner-facing strings and the event field each came from."""
    out: list[tuple[str, str]] = []
    if isinstance(event.get("text"), str):
        out.append((event["text"], "text"))
    out += [(o, "choice_label") for o in event.get("options") or [] if isinstance(o, str)]
    for ch in [*(event.get("choices") or []), *((event.get("data") or {}).get("choices") or [])]:
        if isinstance(ch, dict) and isinstance(ch.get("label"), str):
            out.append((ch["label"], "choice_label"))
    card = event.get("card") or {}
    if card:
        out += [(s, "other") for s in (card.get("title"), card.get("footer")) if isinstance(s, str) and s]
        allowance = next(a for a in rules()["allow"] if a["id"] == "resend_message_id")
        message_id_lines = sum(len(line) == 2 and line[0] == allowance["exact"]
                               for line in card.get("lines") or [])
        for line in card.get("lines") or []:
            for index, s in enumerate(line):
                field = "card_preview_line"
                if (event.get("type") == "card" and card.get("title") == allowance["card_title"]
                        and message_id_lines == 1 and len(line) == 2
                        and line[0] == allowance["exact"] and index == 1):
                    field = "resend_message_id"
                if isinstance(s, str):
                    out.append((s, field))
        out += [(s, "other") for key in ("books", "warnings") for s in card.get(key) or [] if isinstance(s, str)]
        for detail in card.get("details") or []:
            out += [(s, "other") for s in (detail if isinstance(detail, (list, tuple)) else [detail]) if isinstance(s, str)]
    for step in event.get("steps") or []:  # the engine-written words of a turn's steps
        out += [(step[k], "other") for k in ("asked", "returned") if isinstance(step, dict) and isinstance(step.get(k), str)]
    return out


def owner_texts(event: dict) -> list[tuple[str, bool]]:
    """The owner-facing strings, paired with whether each is a card preview line."""
    return [(s, field in ("card_preview_line", "resend_message_id")) for s, field in _owner_text_parts(event)]


def owner_strings(event: dict) -> list[str]:
    """The owner-facing strings of a turn event."""
    return [s for s, _ in owner_texts(event)]


def event_text_violations(event: dict) -> list[tuple[str, list[str]]]:
    """Check whole strings, allowing only the rendered prefixes in their ruled contexts."""
    question = next(a["question"] for a in rules()["allow"] if a["id"] == "card_preview_line_number")
    invoice_question = all(event.get(key) == value for key, value in question.items())
    parts = _owner_text_parts(event)
    labels = {s for s, field in parts if field == "choice_label"}
    out = []
    for text, field in parts:
        resend_span = []
        if field == "resend_message_id" and text.startswith("<") and text.endswith(">"):
            local, sep, domain = text[1:-1].partition("@")
            try:
                canonical = str(uuid.UUID(local)) == local
            except ValueError:
                canonical = False
            if canonical and sep and domain:
                resend_span = [(1, 1 + len(local))]
        segments = []
        if invoice_question:
            if field == "choice_label":
                segments = [(0, len(text))]
            elif field == "text":
                for label in labels:
                    # The renderer joins these exact labels after ': ' and '; '.
                    segments += [(m.start(), m.end()) for m in re.finditer(re.escape(label), text)
                                 if text[max(0, m.start() - 2):m.start()] in (": ", "; ")]
        out.append((text, owner_text_violations(
            text, code=event.get("code"), invoice_line_segments=segments,
            card_preview_line=event.get("type") == "card" and field == "card_preview_line",
            resend_id_span=resend_span)))
    return out


def event_violations(events: list[dict]) -> list[str]:
    """Each owner-facing string of each event that breaks a rule, with the event's type and the rules it broke."""
    bad = []
    for e in events:
        for s, v in event_text_violations(e):
            if v:
                bad.append(f"{e.get('type')}: {s!r} -> {v}")
    return bad


def fill(template: str, value: str) -> str:
    """A language-table entry with every placeholder filled with one representative value."""
    return FIELD.sub(lambda m: value, template)


PENDING_PATH = Path(__file__).with_name("owner_text_pending.json")
PENDING_REASON = "pending a ruling: see tests/owner_text_pending.json"


def pending_entries():
    return json.loads(PENDING_PATH.read_text(encoding="utf-8"))


def pending_mark(case):
    if any(entry["case"] == case for entry in pending_entries()):
        return [pytest.mark.xfail(strict=True, raises=DeferredOwnerText, reason=PENDING_REASON, case=case)]
    return []


def violation_rules(text, code=None):
    return sorted({int(v.split()[1]) for v in owner_text_violations(text, code=code)})


def defer_owner_text(texts, expected, *, code=None):
    """Defer only the exact enumerated strings and rule sets, after checking both."""
    actual = [(text, violation_rules(text, code)) for text in texts]
    assert actual == expected, ("unclassified or changed owner text", actual, expected)
    assert any(rules for _, rules in actual), "plain words replaced the enumerated failure; remove its strict xfail"
    raise DeferredOwnerText(actual)


def check_inventory(entries, case, *, expected=None):
    """Only the pinned owner-text failures can satisfy a strict xfail."""
    bad = []
    for entry in entries:
        event = entry.get("event", {"text": entry["text"], "code": entry["code"]})
        violations = [v for text, found in event_text_violations(event)
                      if text == entry["text"] for v in found]
        if violations:
            bad.append({**{k: v for k, v in entry.items() if k != "event"},
                        "rules": sorted({int(v.split()[1]) for v in violations}), "case": case})
    if expected is None:
        expected = [entry for entry in pending_entries() if entry["case"] == case]
    assert bad == expected, ("unclassified or changed owner text", bad, expected)
    if bad:
        raise DeferredOwnerText(bad)


def declared_refusal_codes() -> set[str]:
    """Literal codes, module constants and conditional arms, including session wrappers.

    Forwarded codes and formatted families are verified against their concrete
    runtime domains by the sweep's coverage guard.
    """
    out = set()
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                     for target in node.targets if isinstance(target, ast.Name)}

        def values(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str): return {node.value}
            if isinstance(node, ast.IfExp): return values(node.body) | values(node.orelse)
            if isinstance(node, ast.Name) and node.id in constants: return values(constants[node.id])
            return set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call): continue
            if _call_name(node) == "Refusal":
                code = refusal_code(node)
                out |= {"refused"} if code is None else values(code)
            elif path.parent.name == "erpclaw_assistant" and _call_name(node) == "ask" and len(node.args) >= 3:
                out |= values(node.args[2])
            elif path.name == "session.py" and _call_name(node) == "refused" and len(node.args) == 3:
                out |= values(node.args[2])
    return out


def resolver_occurrence_kinds() -> set[str]:
    """The resolver's finite, source-defined domains for its formatted question codes."""
    import inspect
    from erpclaw_assistant import resolve

    owner = Path(inspect.getsourcefile(resolve.inventory))
    trees = {path: ast.parse(path.read_text(encoding="utf-8"))
             for path in source_files() if path.parent == owner.parent
             and (path.name == "resolve.py" or path.name.startswith("resolve_"))}
    inventories = [node for tree in trees.values() for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "inventory"]
    assert len(inventories) == 1 and inventories[0] in trees[owner].body
    inventory = inventories[0]

    def strings(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str): return {node.value}
        if isinstance(node, ast.IfExp): return strings(node.body) | strings(node.orelse)
        return set()

    kinds = set()
    for node in ast.walk(inventory):
        if isinstance(node, ast.Call) and _call_name(node) in ("add", "chars") and node.args:
            kinds |= strings(node.args[0])
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "Occurrence" and node.args:
                kinds |= strings(node.args[0])
    return kinds
