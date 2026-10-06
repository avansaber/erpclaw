"""Exact predicates, refusals and a routed no-write rule preview."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[2]
LIB = SCRIPTS / "erpclaw-setup" / "lib"
sys.path.insert(0, str(LIB))
from erpclaw_lib.rule_evaluation import evaluate_rule
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, Q, Table, insert_row


def evaluate(operator, expected, actual, mode="all"):
    return evaluate_rule(json.dumps({"conditions": [{"field": "amount", "operator": operator, "value": expected}], "match": mode}), json.dumps({"amount": actual}))


@pytest.mark.parametrize("operator,expected,actual,matched", [
    (">", "500.00", "500.01", True), (">", "500.00", "500.00", False),
    ("<=", "0.10", "0.100000000001", False),
    (">=", "999999999999999999999999999999.01", "999999999999999999999999999999.02", True),
    ("<", "-0.01", "-0.02", True), ("=", "draft", "draft", True),
    ("=", True, 1, False), ("!=", True, 1, True), ("=", "1.00", "1", False),
    ("contains", "RENT", "October rent payment", True),
    ("in", ["draft", "submitted"], "draft", True), ("in", [True], 1, False),
])
def test_exact_predicates(operator, expected, actual, matched):
    result = evaluate(operator, expected, actual)
    assert result["matched"] is matched
    assert result["conditions"][0]["matched"] is matched
    assert result["preview_only"] is True


@pytest.mark.parametrize("mode,expected", [("all", False), ("any", True)])
def test_combination_and_missing(mode, expected):
    rule = {"match": mode, "conditions": [{"field": "present", "operator": "=", "value": "yes"}, {"field": "missing", "operator": "!=", "value": "no"}]}
    result = evaluate_rule(json.dumps(rule), '{"present":"yes"}')
    assert result["matched"] is expected
    assert result["conditions"][1] == {"field": "missing", "operator": "!=", "matched": False, "missing": True}


@pytest.mark.parametrize("rule,facts", [
    ('{"conditions":[]}', '{}'),
    ('{"conditions":[],"conditions":[]}', '{}'),
    ('{"conditions":[{"field":"x.y","operator":"=","value":1}]}', '{}'),
    ('{"conditions":[{"field":"x","operator":"exec","value":"pass"}]}', '{}'),
    ('{"conditions":[{"field":"x","operator":"=","value":0.1}]}', '{}'),
    ('{"conditions":[{"field":"x","operator":">","value":"NaN"}]}', '{}'),
    ('{"conditions":[{"field":"x","operator":">","value":"1e2"}]}', '{}'),
    ('{"conditions":[{"field":"x","operator":">","value":true}]}', '{}'),
    ('{"conditions":[{"field":"x","operator":"in","value":[]}]}', '{}'),
    ('{"conditions":[{"field":"x","operator":"=","value":1,"code":"ignored"}]}', '{}'),
    ('{"conditions":[{"field":"x","operator":"=","value":1}]}', '{"x":1,"x":2}'),
    ('{"conditions":[{"field":"x","operator":">","value":"1"}]}', '{"x":1.2}'),
    ('{"conditions":[{"field":"x","operator":">","value":"1"}]}', '{"x":true}'),
    ('{"conditions":[{"field":"x","operator":"contains","value":"a"}]}', '{"x":1}'),
    ('{"conditions":[{"field":"x","operator":"=","value":1}]}', '{"x":{"nested":1}}'),
])
def test_malformed_inputs_refuse(rule, facts):
    with pytest.raises(ValueError):
        evaluate_rule(rule, facts)


def test_any_still_validates_later_predicate():
    rule = {"match": "any", "conditions": [{"field": "x", "operator": "=", "value": "yes"}, {"field": "y", "operator": ">", "value": "1"}]}
    with pytest.raises(ValueError, match="exact Decimal"):
        evaluate_rule(json.dumps(rule), '{"x":"yes","y":"invalid"}')


def test_limits():
    condition = {"field": "x", "operator": "=", "value": 1}
    with pytest.raises(ValueError):
        evaluate_rule(json.dumps({"conditions": [condition] * 101}), '{}')
    with pytest.raises(ValueError):
        evaluate_rule(" " * 32769, '{}')


@pytest.mark.parametrize("readonly", ["", "1"])
def test_root_preview_preserves_fresh_books(tmp_path, readonly):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "data.sqlite"
    spec = importlib.util.spec_from_file_location("rule_preview_init", SCRIPTS / "erpclaw-setup" / "init_schema.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.init_db(str(path))
    conn = get_connection(str(path))
    sql, _ = insert_row("company", {"id": P(), "name": P(), "abbr": P()})
    conn.execute(sql, ("preview-company", "Rule Preview Company", "RPC"))
    conn.commit()
    def snapshot():
        return {name: [dict(row) for row in conn.execute(Q.from_(Table(name)).select(Table(name).star).get_sql()).fetchall()]
                for name in ("company", "gl_entry", "payment_entry", "audit_log")}
    before = snapshot()
    path.chmod(0o600)
    before_mode = path.stat().st_mode
    env = dict(os.environ, ERPCLAW_HOME=str(home), PYTHONPATH=str(LIB), ERPCLAW_DB_READONLY="1")
    env.pop("ERPCLAW_DB_URL", None)
    env.pop("ERPCLAW_TEST_SESSION", None)
    env["ERPCLAW_DB_PATH"] = str(path)
    env["ERPCLAW_DB_READONLY"] = readonly
    command = [sys.executable, str(SCRIPTS / "db_query.py"), "--action", "evaluate-rule", "--rule-json", '{"conditions":[{"field":"amount","operator":">","value":"500.00"}]}', "--facts-json", '{"amount":"500.01"}']
    run = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["matched"] is True
    assert snapshot() == before
    assert path.stat().st_mode == before_mode
    conn.close()
    if readonly:
        sys.path.insert(0, str(SCRIPTS.parents[2] / "testing"))
        import readonly_sweep
        report = readonly_sweep.run_sweep(str(path), jobs=1, names=["evaluate-rule"])
        assert report["passed"] == ["evaluate-rule"], json.dumps(report["results"], sort_keys=True)
        assert report["failed"] == {} and report["unproven"] == {}
