"""Compile bounded JSON predicates for a read-only business-rule preview."""
import json
import re
from decimal import Decimal


OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "contains", "in"}
FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)
NUMBER = re.compile(r"-?[0-9]{1,30}(?:\.[0-9]{1,12})?\Z", re.ASCII)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _load(text, label):
    if not isinstance(text, str) or len(text) > 32768:
        raise ValueError(f"{label} must be JSON text of at most 32768 characters")
    def refuse_number(value):
        raise ValueError("Use exact Decimal strings, not JSON fractional numbers")
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_float=refuse_number, parse_constant=refuse_number)
    except (json.JSONDecodeError, RecursionError):
        raise ValueError(f"Invalid {label} JSON") from None


def _scalar(value):
    return value is None or type(value) in (str, int, bool)


def _number(value):
    if type(value) not in (str, int) or not NUMBER.fullmatch(str(value)):
        raise ValueError("Numeric comparisons need exact Decimal strings or integers")
    return Decimal(str(value))


def compile_rule(text):
    """Keep the existing JSON condition shape, with an explicit all/any mode."""
    rule = _load(text, "rule")
    if (not isinstance(rule, dict) or set(rule) - {"conditions", "match"}
            or "conditions" not in rule):
        raise ValueError("Rule must contain conditions and optional match")
    mode = rule.get("match", "all")
    if mode not in ("all", "any"):
        raise ValueError("Rule match must be all or any")
    conditions = rule["conditions"]
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 100:
        raise ValueError("Rule must contain between 1 and 100 conditions")
    compiled = []
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != {"field", "operator", "value"}:
            raise ValueError("Each condition needs only field, operator and value")
        field, operator, expected = (condition[k] for k in ("field", "operator", "value"))
        if not isinstance(field, str) or not FIELD.fullmatch(field):
            raise ValueError("Rule fields must be plain identifiers, not expressions")
        if not isinstance(operator, str) or operator not in OPERATORS:
            raise ValueError("Unsupported rule operator")
        if operator in (">", ">=", "<", "<="):
            expected = _number(expected)
        elif operator == "contains":
            if not isinstance(expected, str) or not expected:
                raise ValueError("contains requires a nonempty text value")
        elif operator == "in":
            if not isinstance(expected, list) or not 1 <= len(expected) <= 100 or not all(_scalar(v) for v in expected):
                raise ValueError("in requires between 1 and 100 scalar values")
        elif not _scalar(expected):
            raise ValueError("Equality requires a scalar value")
        compiled.append((field, operator, expected))
    return mode, tuple(compiled)


def evaluate_rule(rule_text, facts_text):
    """Evaluate every condition without executing actions or reading records."""
    mode, compiled = compile_rule(rule_text)
    facts = _load(facts_text, "facts")
    if (not isinstance(facts, dict) or len(facts) > 100
            or not all(isinstance(k, str) and FIELD.fullmatch(k) and _scalar(v)
                       for k, v in facts.items())):
        raise ValueError("Facts must be a flat object of at most 100 scalar fields")
    results = []
    for field, operator, expected in compiled:
        missing = field not in facts
        actual = facts.get(field)
        matched = False
        if not missing:
            if operator in (">", ">=", "<", "<="):
                actual = _number(actual)
                matched = {">": actual > expected, ">=": actual >= expected,
                           "<": actual < expected, "<=": actual <= expected}[operator]
            elif operator == "contains":
                if not isinstance(actual, str):
                    raise ValueError("contains requires a text fact")
                matched = expected.casefold() in actual.casefold()
            elif operator == "in":
                matched = any(type(actual) is type(v) and actual == v for v in expected)
            else:
                same = type(actual) is type(expected) and actual == expected
                matched = same if operator == "=" else not same
        results.append({"field": field, "operator": operator,
                        "matched": matched, "missing": missing})
    values = [result["matched"] for result in results]
    return {"matched": all(values) if mode == "all" else any(values),
            "match": mode, "conditions": results, "preview_only": True}
