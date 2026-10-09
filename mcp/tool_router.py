"""erpclaw_action dispatch: JSON args -> ``db_query.py --action`` subprocess.

m242 containment: binds the checked action to the executed action. Every nested
arg key used to become a separate ``--key value`` option, so
``get-payment`` + ``args.action=submit-payment`` produced two action options
(the router checked the first, the domain parser could consume the second).
This module now refuses reserved routing/environment/identity controls in args,
encodes every scalar as one ``--key=value`` token (values can never become
options), validates inputs strictly, and restricts execution to the exposed
foundation catalog plus router aliases whose resolved target stays in that
catalog. Model-supplied ``user_confirmed=true`` on a non-carved-out action
retains current behavior and is NOT human approval (see CHANGES.md).

Arg mapping (value-safe: option-looking values can never become options):
  - ``{"company_id": "c1"}``  -> ``--company-id c1`` (two tokens; safe: no
    leading hyphen, proved not to split via real-parser tests)
  - ``{"note": "--db-path=x"}`` -> ``--note=--db-path=x`` (one token)
  - ``{"rate": "-1e3"}``      -> ``--rate=-1e3``      (one token)
  - ``{"force": true}``       -> ``--force``          (bool true => flag presence)
  - ``{"force": false}``      -> (omitted)            (bool false => no flag)
  - ``{"items": [ ... ]}``    -> ``--items=<json>``   (one token)
  - ``{"name": "Acme"}``      -> ``--name Acme``      (two tokens when safe)
  - ``None``                  -> (omitted)
  Any scalar whose text starts with "-" uses one ``--key=value`` token; other
  scalars keep the historical two-token shape (proved equivalent: they cannot
  split because argparse only treats leading-hyphen tokens as options).

Error semantics: a non-zero router exit (or non-JSON stdout) is surfaced as a
structured error object -- never swallowed, never narrated. Validation errors
never echo submitted values.
"""
import ast
from functools import lru_cache
import json
import os
import re
import subprocess
import sys

from . import confirm, paths
from .skill_reader import RouterGateUnavailable, dangerous_actions

_FOUNDATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROUTER = os.path.join(_FOUNDATION_DIR, "scripts", "db_query.py")

_RESERVED_CANONICAL = frozenset({
    "action", "action-name", "user-confirmed", "db-path", "db-url",
    "actor", "actor-id", "session-token", "authorization-id",
})

_AUTHORIZATION_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,127}")

_ABBREV_PROTECTED = frozenset({"action", "db-path", "user-confirmed"})

_STORE_TRUE_KNOWN = frozenset({
    "force", "confirm", "active-only", "enabled", "enabled-only",
    "must-be-whole-number", "encrypt", "dry-run", "from-stdin",
    "passphrase-from-stdin", "reset", "include-inactive", "required",
    "is-group", "include-frozen", "reclassify-posted", "auto-submit",
    "is-percentage", "is-default", "ssl", "skip-build", "include-inactive",
})
# Legacy hand-maintained set above is retained for import compatibility only
# and is NOT consulted by validation. Boolean-only options are derived per
# receiver from source AST (see _receiver_boolean_options).

_TRUST_OVERRIDE_CANONICAL = "unsafe-trust-bundled"

_MODULE_MANAGER_PATH = os.path.join(_FOUNDATION_DIR, "scripts", "module_manager.py")
_ONBOARDING_PATH = os.path.join(_FOUNDATION_DIR, "scripts", "onboarding.py")


def _router_maps():
    try:
        text = open(_ROUTER).read()
    except Exception as exc:
        raise RouterGateUnavailable(_ROUTER, "%s: %s" % (type(exc).__name__, exc))
    try:
        tree = ast.parse(text)
    except Exception as exc:
        raise RouterGateUnavailable(_ROUTER, "%s: %s" % (type(exc).__name__, exc))
    module_actions = None
    onboarding_actions = None
    aliases = None
    action_map = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "MODULE_ACTIONS" and isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
                vals = set()
                for e in node.value.elts:
                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                        vals.add(e.value)
                    else:
                        raise RouterGateUnavailable(_ROUTER, "MODULE_ACTIONS has non-string member")
                module_actions = frozenset(vals)
            elif target.id == "ONBOARDING_ACTIONS" and isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
                vals = set()
                for e in node.value.elts:
                    if isinstance(e, ast.Constant) and isinstance(e.value, str):
                        vals.add(e.value)
                    else:
                        raise RouterGateUnavailable(_ROUTER, "ONBOARDING_ACTIONS has non-string member")
                onboarding_actions = frozenset(vals)
            elif target.id == "ALIASES" and isinstance(node.value, ast.Dict):
                out = {}
                for k, v in zip(node.value.keys, node.value.values):
                    if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                        raise RouterGateUnavailable(_ROUTER, "ALIASES has non-string key")
                    if not (isinstance(v, ast.Tuple) and len(v.elts) == 2):
                        raise RouterGateUnavailable(_ROUTER, "ALIASES entry malformed")
                    dom, tgt = v.elts
                    if not (isinstance(dom, ast.Constant) and isinstance(dom.value, str)
                            and isinstance(tgt, ast.Constant) and isinstance(tgt.value, str)):
                        raise RouterGateUnavailable(_ROUTER, "ALIASES entry non-string members")
                    out[k.value] = (dom.value, tgt.value)
                aliases = dict(out)
            elif target.id == "ACTION_MAP" and isinstance(node.value, ast.Dict):
                out = {}
                for k, v in zip(node.value.keys, node.value.values):
                    if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                        raise RouterGateUnavailable(_ROUTER, "ACTION_MAP has non-string key")
                    if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
                        raise RouterGateUnavailable(_ROUTER, "ACTION_MAP has non-string value")
                    out[k.value] = v.value
                aliases_map = dict(out)
                action_map = aliases_map
    if module_actions is None:
        raise RouterGateUnavailable(_ROUTER, "MODULE_ACTIONS assignment not found")
    if onboarding_actions is None:
        raise RouterGateUnavailable(_ROUTER, "ONBOARDING_ACTIONS assignment not found")
    if aliases is None:
        raise RouterGateUnavailable(_ROUTER, "ALIASES assignment not found")
    if action_map is None:
        raise RouterGateUnavailable(_ROUTER, "ACTION_MAP assignment not found")
    return module_actions, onboarding_actions, aliases, action_map


def _receiver_parser_path(action_name):
    module_actions, onboarding_actions, aliases, action_map = _router_maps()
    if action_name in module_actions:
        return _MODULE_MANAGER_PATH
    if action_name in onboarding_actions:
        return _ONBOARDING_PATH
    if action_name in aliases:
        domain, _target = aliases[action_name]
        return os.path.join(_FOUNDATION_DIR, "scripts", domain, "db_query.py")
    if action_name in action_map:
        domain = action_map[action_name]
        return os.path.join(_FOUNDATION_DIR, "scripts", domain, "db_query.py")
    return None


# Literal argparse action kinds the transport can represent. The implicit
# default (no action keyword) and explicit "store" take a value; "append"
# takes a value and is repeatable (reporting --dimension-key/--dimension-value
# rely on it); "store_true"/"store_false" take no value and are the only
# zero-argument kinds accepted. Every other zero-argument kind
# (store_const/count/append_const/version/help) is refused with
# RouterGateUnavailable: the transport cannot faithfully represent their
# accumulating/constant semantics, and no current receiver declares them.
_SUPPORTED_PARSER_ACTIONS = frozenset({"store", "store_true", "store_false", "append"})


def _extract_parser_options(path):
    try:
        text = open(path).read()
    except Exception as exc:
        raise RouterGateUnavailable(path, "%s: %s" % (type(exc).__name__, exc))
    try:
        tree = ast.parse(text)
    except Exception as exc:
        raise RouterGateUnavailable(path, "%s: %s" % (type(exc).__name__, exc))
    options = {}
    found_parser = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        for a in node.args:
            if isinstance(a, ast.Starred):
                raise RouterGateUnavailable(path, "add_argument with starred argument is unresolvable")
            if not (isinstance(a, ast.Constant) and isinstance(a.value, str)):
                raise RouterGateUnavailable(path, "add_argument with nonliteral argument name is unresolvable")
        for kw in node.keywords:
            if kw.arg is None:
                raise RouterGateUnavailable(path, "add_argument with expanded keywords is unresolvable")
        found_parser = True
        opt_strings = []
        for a in node.args:
            if a.value.startswith("-"):
                opt_strings.append(a.value)
        action_kind = None
        for kw in node.keywords:
            if kw.arg == "action":
                if isinstance(kw.value, ast.Constant) and (kw.value.value is None or (isinstance(kw.value.value, str) and kw.value.value in _SUPPORTED_PARSER_ACTIONS)):
                    action_kind = kw.value.value
                else:
                    raise RouterGateUnavailable(path, "add_argument with unresolvable action kind is unresolvable")
        longs = [o for o in opt_strings if o.startswith("--")]
        for flag in longs:
            name = flag[2:]
            norm = _normalize_key(name)
            if norm not in options:
                options[norm] = {"action": action_kind, "flag": flag}
            else:
                prev = options[norm].get("action")
                if prev in ("store_true", "store_false") or action_kind in ("store_true", "store_false"):
                    options[norm] = {"action": action_kind if action_kind in ("store_true", "store_false") else prev, "flag": flag}
    if not found_parser:
        raise RouterGateUnavailable(path, "no add_argument calls found")
    if "help" not in options:
        options["help"] = {"action": "store_true", "flag": "--help"}
    return dict(options)


@lru_cache(maxsize=None)
def _receiver_tree(path):
    try:
        text = open(path).read()
    except Exception as exc:
        raise RouterGateUnavailable(path, "%s: %s" % (type(exc).__name__, exc))
    try:
        return ast.parse(text)
    except Exception as exc:
        raise RouterGateUnavailable(path, "%s: %s" % (type(exc).__name__, exc))


@lru_cache(maxsize=None)
def _receiver_handlers(path):
    handlers = {}
    for node in ast.walk(_receiver_tree(path)):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "ACTIONS"
                   for target in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if isinstance(value, ast.Name):
                handlers[key.value] = value.id
            elif isinstance(value, ast.Constant) and value.value is None:
                handlers[key.value] = None
    return handlers


@lru_cache(maxsize=None)
def _receiver_imported_action_paths(path):
    """Return local modules whose literal ``ACTIONS`` maps feed a receiver."""
    paths_out = []
    parent = os.path.dirname(path)
    for node in _receiver_tree(path).body:
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        if not any(alias.name == "ACTIONS" for alias in node.names):
            continue
        if not node.module:
            continue
        candidate = os.path.join(parent, *node.module.split(".")) + ".py"
        if os.path.isfile(candidate):
            paths_out.append(candidate)
    return tuple(paths_out)


@lru_cache(maxsize=None)
def _receiver_imported_functions(path):
    """Map local imported function names to their source module and name."""
    functions = {}
    parent = os.path.dirname(path)
    for node in _receiver_tree(path).body:
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        candidate = os.path.join(parent, *node.module.split(".")) + ".py"
        if not os.path.isfile(candidate):
            continue
        for alias in node.names:
            if alias.name != "ACTIONS":
                functions[alias.asname or alias.name] = (candidate, alias.name)
    return functions


@lru_cache(maxsize=None)
def _receiver_functions(path):
    return {
        node.name: node for node in _receiver_tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _action_handler_source(path, action_name):
    handlers = _receiver_handlers(path)
    if action_name in handlers and handlers[action_name] is not None:
        return path, handlers[action_name]
    for imported_path in _receiver_imported_action_paths(path):
        handlers = _receiver_handlers(imported_path)
        if action_name in handlers and handlers[action_name] is not None:
            return imported_path, handlers[action_name]
    return None


@lru_cache(maxsize=None)
def _dispatch_branch_contract(path, action_name):
    """Resolve a literal ``if action == ...`` branch in receiver ``main``."""
    functions = _receiver_functions(path)
    imported_functions = _receiver_imported_functions(path)
    main = functions.get("main")
    if main is None:
        return None
    for node in ast.walk(main):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if not (isinstance(test.left, ast.Name) and test.left.id == "action"
                and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == action_name):
            continue
        argument_names = set()
        requirements = set()
        handler_source = None
        for statement in node.body:
            for child in ast.walk(statement):
                if (isinstance(child, ast.Attribute)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == "args"):
                    argument_names.add(child.attr)
                if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and any(isinstance(item, ast.Name) and item.id == "args"
                                for item in child.args)):
                    local_name = child.func.id
                    if local_name in functions:
                        handler_source = (path, local_name)
                    elif local_name in imported_functions:
                        handler_source = imported_functions[local_name]
                text_value = _joined_text(child)
                if "required" in text_value.lower() and "--" in text_value:
                    requirements.add(text_value)
        if handler_source is not None:
            source_path, handler_name = handler_source
            return source_path, handler_name, argument_names, requirements
    return None


def _literal_value(node):
    """Return a JSON-safe literal from an AST node, or ``None`` if dynamic."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)) and all(
            item is None or isinstance(item, (str, int, float, bool))
            for item in value):
        return list(value)
    return None


@lru_cache(maxsize=None)
def _extract_parser_argument_specs(path):
    """Return receiver argument metadata keyed by argparse destination.

    This is presentation metadata for ``erpclaw_describe_action``. Execution
    validation continues to use ``_extract_parser_options`` so extending the
    model-facing contract cannot relax the transport gate.
    """
    tree = _receiver_tree(path)
    specs = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        option_strings = [
            item.value for item in node.args
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
            and item.value.startswith("--")
        ]
        if not option_strings:
            continue
        flag = option_strings[0]
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        dest_node = keywords.get("dest")
        dest = (_literal_value(dest_node) if dest_node is not None
                else flag[2:].replace("-", "_"))
        if not isinstance(dest, str):
            continue
        action = _literal_value(keywords.get("action")) if "action" in keywords else None
        kind = ("boolean" if action in ("store_true", "store_false")
                else "repeatable" if action == "append" else "value")
        spec = {"name": dest, "flag": flag, "kind": kind}
        if "choices" in keywords:
            choices = _literal_value(keywords["choices"])
            if isinstance(choices, list):
                spec["choices"] = choices
        if "default" in keywords:
            default = _literal_value(keywords["default"])
            if default is not None:
                spec["default"] = default
        help_text = _literal_value(keywords.get("help")) if "help" in keywords else None
        if isinstance(help_text, str) and help_text:
            spec["description"] = help_text
        specs[dest] = spec
    if not specs:
        raise RouterGateUnavailable(path, "no literal long-form parser arguments found")
    return specs


def _joined_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            elif isinstance(item, ast.FormattedValue):
                parts.append("<value>")
        return "".join(parts)
    return ""


@lru_cache(maxsize=None)
def _action_function_contract(path, action_name):
    """Statically bind one ACTIONS entry to the args its handler reads."""
    handler_source = _action_handler_source(path, action_name)
    argument_names = set()
    requirements = set()
    if handler_source is None:
        branch = _dispatch_branch_contract(path, action_name)
        if branch is None:
            raise RouterGateUnavailable(
                path, "ACTIONS handler for %r is not statically resolvable" % action_name)
        source_path, handler_name, branch_names, branch_requirements = branch
        argument_names.update(branch_names)
        requirements.update(branch_requirements)
    else:
        source_path, handler_name = handler_source

    functions = _receiver_functions(source_path)
    if handler_name not in functions:
        raise RouterGateUnavailable(source_path, "handler %r not found" % handler_name)

    pending = [handler_name]
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited or name not in functions:
            continue
        visited.add(name)
        function = functions[name]
        for node in ast.walk(function):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "args"):
                argument_names.add(node.attr)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr" and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "args"
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                argument_names.add(node.args[1].value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if any(isinstance(item, ast.Name) and item.id == "args"
                       for item in node.args):
                    pending.append(node.func.id)
            text_value = _joined_text(node)
            if "required" in text_value.lower() and "--" in text_value:
                requirements.add(text_value)
    ordered_requirements = sorted(requirements)
    requirements = [
        item for item in ordered_requirements
        if not any(other != item and other.startswith(item)
                   for other in ordered_requirements)
    ]
    return argument_names, requirements


def action_argument_contract(action_name):
    """Return exact model-facing argument metadata for one exposed action."""
    module_actions, onboarding_actions, aliases, _action_map = _router_maps()
    if action_name in module_actions or action_name in onboarding_actions:
        return {
            "arguments": [],
            "requirements": [],
            "argument_contract_available": False,
            "args_hint": (
                "This orchestration action has no static action-specific "
                "argument contract; pass documented router arguments only."
            ),
        }
    effective_action = aliases.get(action_name, (None, action_name))[1]
    receiver_path = _receiver_parser_path(action_name)
    if receiver_path is None:
        raise RouterGateUnavailable(_ROUTER, "no static receiver for %r" % action_name)
    specs = _extract_parser_argument_specs(receiver_path)
    names, requirements = _action_function_contract(receiver_path, effective_action)
    arguments = [
        specs[name] for name in sorted(names)
        if name in specs and name not in {"action", "db_path"}
    ]
    return {
        "arguments": arguments,
        "requirements": requirements,
        "argument_contract_available": True,
        "args_hint": (
            "Pass only the listed argument names in erpclaw_action.args. "
            "Use JSON values; repeatable arguments accept an array."
        ),
    }


def _resolve_against_receiver(norm, declared):
    if norm in declared:
        return (declared[norm], False)
    cands = sorted([d for d in declared if d.startswith(norm)])
    if len(cands) == 1:
        return (declared[cands[0]], False)
    if len(cands) > 1:
        return (None, True)
    return (None, False)

_ACTION_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _json_arg(value) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _normalize_key(key: str) -> str:
    return str(key).lower().replace("_", "-")


def _is_prefix_of_protected(norm: str) -> str | None:
    for reserved in _ABBREV_PROTECTED:
        if reserved.startswith(norm) and norm != reserved:
            return reserved
    return None


def _validation_error(action_name, error: str, detail: str = "") -> dict:
    payload = {"status": "error", "error": error}
    if isinstance(action_name, str) and action_name:
        payload["action"] = action_name
    elif action_name is not None and not isinstance(action_name, str):
        pass
    if detail:
        payload["detail"] = detail
    return payload


_SCRUBBED_AUTHORIZATION = "[authorization-id]"


def _scrub_authorization(value, authorization_id):
    if authorization_id is None:
        return value
    if isinstance(value, str):
        return value.replace(authorization_id, _SCRUBBED_AUTHORIZATION)
    if isinstance(value, dict):
        return {
            _scrub_authorization(key, authorization_id):
                _scrub_authorization(item, authorization_id)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _scrub_authorization(item, authorization_id) for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _scrub_authorization(item, authorization_id) for item in value)
    return value


def validate_inputs(action_name, args, user_confirmed=False):
    if not isinstance(action_name, str) or not action_name.strip():
        return _validation_error(
            None, "invalid_action",
            "action_name must be a nonempty string.")
    name = action_name.strip()
    if "\x00" in action_name or "\n" in action_name or "\r" in action_name:
        return _validation_error(None, "invalid_action",
                                 "action_name contains a control character.")
    if len(name) > 128 or not _ACTION_RE.match(name):
        return _validation_error(None, "invalid_action",
                                 "action_name must be canonical kebab-case.")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _validation_error(name, "invalid_args",
                                 "args must be a JSON object.")
    if not isinstance(user_confirmed, bool):
        return _validation_error(name, "invalid_confirmation",
                                 "user_confirmed must be a literal boolean.")
    for key in args.keys():
        if not isinstance(key, str):
            return _validation_error(name, "invalid_arg_key",
                                     "argument keys must be strings.")
        if key == "":
            return _validation_error(name, "invalid_arg_key",
                                     "argument key must not be empty.")
        if chr(0) in key:
            return _validation_error(name, "invalid_arg_key",
                                     "argument key contains NUL.")
    return None


def _check_arg_keys(action_name: str, args: dict):
    seen_norms: dict = {}
    for raw_key, value in args.items():
        if not isinstance(raw_key, str):
            return _validation_error(action_name, "invalid_arg_key",
                                     "argument keys must be strings.")
        if raw_key == "":
            return _validation_error(action_name, "invalid_arg_key",
                                     "argument key must not be empty.")
        if chr(0) in raw_key:
            return _validation_error(action_name, "invalid_arg_key",
                                     "argument key contains NUL.")
        if raw_key[0] == "-":
            return _validation_error(action_name, "reserved_arg",
                                     "argument key must not start with '-': %r." % (raw_key[:32],))
        if "=" in raw_key:
            return _validation_error(action_name, "invalid_arg_key",
                                     "argument key must not contain '='.")
        if not _KEY_RE.match(raw_key):
            return _validation_error(action_name, "invalid_arg_key",
                                     "malformed argument key: %r." % (raw_key[:32],))
        bad_ctrl = False
        for ch in raw_key:
            o = ord(ch)
            if o < 32 or o == 127:
                bad_ctrl = True
                break
            if ch.isspace():
                bad_ctrl = True
                break
        if bad_ctrl:
            return _validation_error(action_name, "invalid_arg_key",
                                     "argument key contains whitespace/control: %r." % (raw_key[:32],))
        norm = _normalize_key(raw_key)
        if norm in seen_norms and seen_norms[norm] != raw_key:
            return _validation_error(
                action_name, "duplicate_arg",
                "normalization collision: %r and %r map to %r."
                % (seen_norms[norm][:32], raw_key[:32], norm[:64],))
        if norm not in seen_norms:
            seen_norms[norm] = raw_key
        else:
            if seen_norms[norm] == raw_key:
                pass
        if norm in _RESERVED_CANONICAL:
            return _validation_error(
                action_name, "reserved_arg",
                "reserved routing control refused: %r." % (norm[:64],))
        if norm == _TRUST_OVERRIDE_CANONICAL:
            return _validation_error(
                action_name, "reserved_arg",
                "reserved trust override refused.")
        hit = _is_prefix_of_protected(norm)
        if hit is not None:
            return _validation_error(
                action_name, "reserved_arg",
                "abbreviation of reserved control refused: %r (-> %r)."
                % (norm[:64], hit,))
        if isinstance(value, str) and chr(0) in value:
            return _validation_error(action_name, "invalid_arg_value",
                                     "argument value contains NUL.")
    try:
        receiver_path = _receiver_parser_path(action_name)
    except Exception:
        return _validation_error(
            action_name, "parser_metadata_unavailable",
            "receiving parser metadata unavailable.")
    if receiver_path is None:
        return _validation_error(
            action_name, "unknown_action",
            "no-static-receiver for this catalog action.")
    try:
        declared = _extract_parser_options(receiver_path)
    except Exception:
        return _validation_error(
            action_name, "parser_metadata_unavailable",
            "receiving parser metadata unavailable.")
    for raw_key, value in args.items():
        norm = _normalize_key(raw_key)
        if norm == _TRUST_OVERRIDE_CANONICAL:
            continue
        resolved, ambiguous = _resolve_against_receiver(norm, declared)
        if ambiguous:
            return _validation_error(
                action_name, "ambiguous_arg",
                "ambiguous argument prefix refused: %r." % (norm[:64],))
        if resolved is None:
            continue
        if norm != _TRUST_OVERRIDE_CANONICAL and resolved.get("flag", "")[2:] is not None:
            target_norm = _normalize_key(resolved.get("flag", "")[2:] if resolved.get("flag", "").startswith("--") else norm)
            if target_norm == _TRUST_OVERRIDE_CANONICAL and norm != _TRUST_OVERRIDE_CANONICAL:
                return _validation_error(
                    action_name, "reserved_arg",
                    "reserved trust override prefix refused.")
        is_bool_only = resolved.get("action") in ("store_true", "store_false")
        if is_bool_only:
            if value is None:
                continue
            if isinstance(value, bool):
                continue
            return _validation_error(
                action_name, "invalid_arg_value",
                "boolean flag takes only true/false/null: %r." % (norm[:64],))
    return None


def _exposed_allowance():
    from . import skill_reader as _sr
    try:
        dangerous = dangerous_actions()
    except RouterGateUnavailable as exc:
        return None, None, confirm.gate_unavailable_response(exc, None)
    try:
        names = _sr._foundation_action_names()
    except Exception as exc:
        return None, None, {
            "status": "error", "error": "destructive_gate_unavailable",
            "detail": "catalog discovery failed: %s: %s" % (type(exc).__name__, exc),
            "path": _sr._ROUTER_PATH, "action": None,
        }
    problems = _sr.discovery_problems()
    if problems:
        first = problems[0]
        if isinstance(first, (tuple, list)) and len(first) == 2:
            detail = "incomplete catalog discovery: %s: %s" % (first[0], first[1])
        else:
            detail = "incomplete catalog discovery: %s" % (first,)
        return None, None, {
            "status": "error", "error": "destructive_gate_unavailable",
            "detail": detail, "path": _sr._ROUTER_PATH, "action": None,
        }
    if not names:
        return None, None, {
            "status": "error", "error": "destructive_gate_unavailable",
            "detail": "catalog discovery returned an empty set; refusing.",
            "path": _sr._ROUTER_PATH, "action": None,
        }
    try:
        aliases = _sr.router_aliases()
    except RouterGateUnavailable as exc:
        return None, None, confirm.gate_unavailable_response(exc, None)
    problems2 = _sr.discovery_problems()
    if problems2:
        first = problems2[0]
        if isinstance(first, (tuple, list)) and len(first) == 2:
            detail = "incomplete catalog discovery: %s: %s" % (first[0], first[1])
        else:
            detail = "incomplete catalog discovery: %s" % (first,)
        return None, None, {
            "status": "error", "error": "destructive_gate_unavailable",
            "detail": detail, "path": _sr._ROUTER_PATH, "action": None,
        }
    return names, aliases, None


def _allowance_check(action_name: str, names: frozenset, aliases: dict):
    from . import skill_reader as _sr
    target = action_name
    is_alias = action_name in aliases
    if is_alias:
        try:
            target = _sr.resolve_alias_target(action_name, aliases)
        except RouterGateUnavailable as exc:
            err = confirm.gate_unavailable_response(exc, action_name)
            return None, err
        if target in aliases and target != action_name:
            return None, {
                "status": "error", "error": "unknown_action",
                "action": action_name,
                "detail": "alias target is itself an alias: %r." % (target[:64],),
            }
    else:
        if action_name not in names:
            return None, {
                "status": "error", "error": "unknown_action",
                "action": action_name,
                "detail": "not in the exposed foundation catalog.",
            }
    if target not in names:
        return None, {
            "status": "error", "error": "unknown_action",
            "action": action_name,
            "detail": "alias target is not in the foundation catalog.",
        }
    if confirm.is_credential_carved_out(action_name):
        return None, confirm.credential_refusal(action_name)
    if confirm.is_credential_carved_out(target):
        return None, confirm.credential_refusal(action_name)
    try:
        alias_destructive = confirm.is_destructive(action_name) if is_alias else False
    except RouterGateUnavailable as exc:
        return None, confirm.gate_unavailable_response(exc, action_name)
    try:
        target_destructive = confirm.is_destructive(target)
    except RouterGateUnavailable as exc:
        return None, confirm.gate_unavailable_response(exc, action_name)
    destructive = bool(alias_destructive or target_destructive)
    if is_alias and action_name not in names:
        pass
    return {"target": target, "is_alias": is_alias, "destructive": destructive}, None


def build_argv(action_name: str, args: dict, user_confirmed: bool,
               authorization_id=None) -> list:
    argv = [sys.executable, _ROUTER, "--action", action_name]
    for key, value in (args or {}).items():
        flag = "--" + _normalize_key(str(key))
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        elif isinstance(value, (list, dict)):
            argv.append("%s=%s" % (flag, _json_arg(value)))
        elif value is None:
            continue
        else:
            text = value if isinstance(value, str) else str(value)
            if chr(0) in text:
                raise ValueError("NUL in argument value")
            if text.startswith("-"):
                argv.append("%s=%s" % (flag, text))
            else:
                argv.extend([flag, text])
    if authorization_id is not None:
        argv.append("--authorization-id=%s" % (authorization_id,))
    try:
        destructive = confirm.is_destructive(action_name)
    except RouterGateUnavailable:
        raise
    if user_confirmed and destructive:
        argv.append("--user-confirmed")
    return argv


# Every router child runs with the server's own actor context, so a
# value forged into the caller's environment never reaches the domain path.
_MCP_ACTOR_CONTEXT = '{"channel":"mcp","hop":[],"principal":null,"v":1}'


def _resolve_env() -> dict:
    env = dict(os.environ)
    env["ERPCLAW_HOME"] = paths.erpclaw_home()
    env["ERPCLAW_ACTOR_CONTEXT"] = _MCP_ACTOR_CONTEXT
    try:
        readonly = confirm.session_readonly()
    except confirm.ReadonlyModeInvalid:
        # dispatch refuses an invalid mode before any child starts; should the
        # value change between the two reads, the child still gets read-only
        # storage, never a writable one.
        readonly = True
    if readonly:
        env["ERPCLAW_DB_READONLY"] = "1"
        env.pop("ERPCLAW_TEST_SESSION", None)
    return env


def dispatch(action_name: str, args: dict, user_confirmed: bool = False,
           authorization_id=None) -> dict:
    try:
        readonly = confirm.session_readonly()
    except confirm.ReadonlyModeInvalid:
        return confirm.invalid_mode()
    if args is None:
        args = {}
    verr = validate_inputs(action_name, args, user_confirmed)
    if verr is not None:
        return verr
    name = action_name.strip()
    if authorization_id is not None:
        if (not isinstance(authorization_id, str)
                or _AUTHORIZATION_RE.fullmatch(authorization_id) is None
                or len(authorization_id) < 16):
            return _validation_error(
                name, "invalid_authorization",
                "authorization_id must be an id string.")
    kerr = _check_arg_keys(name, args)
    if kerr is not None:
        return kerr
    names, aliases, derr = _exposed_allowance()
    if derr is not None:
        if derr.get("action") is None:
            derr = dict(derr)
            derr["action"] = name
        return derr
    allowed, aerr = _allowance_check(name, names, aliases)
    if aerr is not None:
        return aerr
    effective = allowed["target"]
    if readonly:
        # A read-only session has nothing to confirm and runs reads only. An
        # alias counts as a read exactly when its resolved target does; the
        # alias's own name is never tested against the read rule. Any failure
        # computing the read gate refuses fail-closed. No router process
        # starts for any refusal below; each refusal names the action the
        # caller asked for, never the alias target.
        probe = allowed["target"] if allowed["is_alias"] else name
        if user_confirmed is True:
            return confirm.refusal(name)
        try:
            from . import skill_reader as _sr
            dangerous = _sr.dangerous_actions()
            module_actions, onboarding_actions, _ro_aliases, _ro_map = (
                _router_maps())
        except Exception:
            return confirm.refusal(name)
        if not confirm.is_session_read(
                probe, dangerous=dangerous,
                module_actions=module_actions,
                onboarding_actions=onboarding_actions):
            return confirm.refusal(name)
    try:
        needs_confirmation = allowed["destructive"] and not user_confirmed
    except RouterGateUnavailable as exc:
        return confirm.gate_unavailable_response(exc, name)
    if needs_confirmation:
        try:
            dangerous_actions()
        except RouterGateUnavailable as exc:
            return confirm.gate_unavailable_response(exc, name)
        return confirm.confirmation_request(name)
    try:
        argv = build_argv(name, args, user_confirmed,
                          authorization_id=authorization_id)
    except ValueError as exc:
        return _scrub_authorization(
            {"status": "error", "action": name, "error": "invalid_arg_value",
             "detail": str(exc)[:200]}, authorization_id)
    except RouterGateUnavailable as exc:
        return confirm.gate_unavailable_response(exc, name)
    try:
        if allowed["is_alias"]:
            try:
                alias_is_carved = confirm.is_credential_carved_out(name)
                target_is_carved = confirm.is_credential_carved_out(effective)
                if alias_is_carved or target_is_carved:
                    return confirm.credential_refusal(name)
            except Exception:
                pass
        action_opts = [t for t in argv[3:] if t == "--action" or t.startswith("--action=")]
        _ = action_opts
    except Exception:
        pass
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=_resolve_env(),
        )
    except OSError as e:
        return _scrub_authorization(
            {"status": "error", "action": name,
             "error": f"Failed to spawn router subprocess: {e}"},
            authorization_id)

    stdout = (proc.stdout or "").strip()
    parsed = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None

    if parsed is not None and isinstance(parsed, dict):
        if proc.returncode != 0:
            parsed["router_status"] = parsed.get("status")
            parsed["status"] = "error"
            parsed["returncode"] = proc.returncode
        return _scrub_authorization(parsed, authorization_id)

    return _scrub_authorization(
        {
            "status": "error",
            "action": name,
            "error": "Router produced no parseable JSON output.",
            "returncode": proc.returncode,
            "stdout": stdout[:2000],
            "stderr": (proc.stderr or "").strip()[:2000],
        },
        authorization_id,
    )
