"""Prove a resolver split against the immutable source blob in the starting tree.

Run with the workspace Python and bytecode disabled. --base accepts another git
revision for an explicit comparison. No checkout or generated file is needed.
"""
from __future__ import annotations

import argparse
import ast
import importlib
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("packages/erpclaw-assistant/src/erpclaw_assistant")
# The original resolve.py blob, retained by git even after the split is committed.
BASE_BLOB = "59d5df63468cdf1025929d1532f34e3649fe56ba"
BASE_TREE = "679b3acdbae99247900c2a9c2d85fa3dc46be789"


# Starting-tree path, line, enclosing test/fixture, operation, alias, name, chosen bindings.
PATCH_SITES = [
    ('tests/test_ask_controls.py', 105, 'test_every_control_choice_listed_is_one_the_engine_accepts_for_that_question', 'setattr', 'resolve', 'match_answer', ('resolve', 'resolve_state')),
    ('tests/test_ask_controls.py', 130, 'test_the_control_labels_come_from_the_language_table', 'setitem', 'resolve.TABLES', 'zz', ('resolve.TABLES',)),
    ('tests/test_ask_controls.py', 131, 'test_the_control_labels_come_from_the_language_table', 'setattr', 'resolve', 'LANGUAGE', ('resolve', 'resolve_text')),
    ('tests/test_message_retry.py', 180, 'test_retry_keeps_its_answered_question_between_consecutive_confirmations', 'setattr', 'resolve.Session', 'answer', ('resolve.Session',)),
    ('tests/test_money_spans.py', 742, 'test_completed_messages_are_never_sent_back_through_name_resolution', 'setattr', 'rv', '_resolve_record', ('rv', 'resolve_policy', 'resolve_records')),
    ('tests/test_outgoing_account.py', 381, 'test_still_owed_skips_outgoing_reader_in_occurrence_accounting', 'setattr', 'rv', 'outgoing_account_phrases', ('rv',)),
    ('tests/test_payment_phrase_misc.py', 37, 'test_non_payment_resolution_and_inventory_do_not_read_accounts', 'setattr', 'rv', 'customers', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_records')),
    ('tests/test_payment_phrase_misc.py', 38, 'test_non_payment_resolution_and_inventory_do_not_read_accounts', 'setattr', 'rv', '_items', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_policy', 'resolve_types')),
    ('tests/test_payment_phrase_misc.py', 157, 'test_consumed_method_span_excludes_inner_dates', 'setattr', 'rv', 'method_spans', ('rv', 'resolve_inventory', 'resolve_payment')),
    ('tests/test_payment_phrase_misc.py', 166, 'test_consumed_method_span_excludes_inner_dates', 'setattr', 'rv', 'resolve_date', ('rv', 'resolve_args', 'resolve_dates')),
    ('tests/test_payment_phrase_misc.py', 202, 'test_payment_date_source_uses_the_real_phrase_outside_consumed_names', 'setattr', 'rv', 'inventory', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_policy')),
    ('tests/test_payment_phrase_misc.py', 203, 'test_payment_date_source_uses_the_real_phrase_outside_consumed_names', 'setattr', 'rv', '_items', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_policy', 'resolve_types')),
    ('tests/test_payment_phrase_misc.py', 423, 'test_marker_guard_carries_weight_without_leftover_item_asking', 'setattr', 'rv', '_marks_at', ('rv', 'resolve_amounts', 'resolve_payment', 'resolve_records')),
    ('tests/test_payment_phrase_misc.py', 576, 'test_failed_owned_name_projection_asks_instead_of_unmasking', 'setattr', 'rv', 'consumed_ranges', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_policy', 'resolve_words')),
    ('tests/test_payment_phrase_misc.py', 617, 'test_marker_mutation_makes_card_when_item_is_claimed', 'setattr', 'rv', '_marks_at', ('rv', 'resolve_amounts', 'resolve_payment', 'resolve_records')),
    ('tests/test_payment_phrase_misc.py', 841, 'test_corrections_of_other_kinds_preserve_attached_amount_markers', 'setattr', 'rv', 'inventory', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_policy')),
    ('tests/test_payment_phrase_misc.py', 851, 'test_corrections_of_other_kinds_preserve_attached_amount_markers', 'setattr', 'rv', 'inventory', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_policy')),
    ('tests/test_resolve_turns.py', 797, 'test_the_accounting_carries_the_weight', 'setattr', 'resolve', '_unclaimed', ('resolve', 'resolve_inventory')),
    ('tests/test_turn_deadline.py', 439, 'test_cancel_declines_a_pending_card_even_past_the_deadline', 'setattr', 'graph.resolve', 'match_answer', ('graph.resolve',)),
    ('tests/test_turn_not_done_questions.py', 243, 'test_memo_qualifier_wording_comes_from_the_active_language_table', 'setitem', 'resolve.TABLES', 'toy', ('resolve.TABLES',)),
    ('tests/test_turn_not_done_questions.py', 244, 'test_memo_qualifier_wording_comes_from_the_active_language_table', 'setattr', 'resolve', 'LANGUAGE', ('resolve', 'resolve_text')),
    ('tests/test_turn_steps.py', 236, 'test_turn_reads_only_typed_summary_and_single_canonical_scope_lookup', 'setattr', 'resolve', 'metric_scope_name', ('resolve', 'resolve_text')),
    ('tests/test_turn_steps.py', 251, 'test_turn_reads_only_typed_summary_and_single_canonical_scope_lookup', 'setattr', 'resolve', 'metric_scope_name', ('resolve', 'resolve_text')),
    ('tests/test_turn_steps.py', 271, 'test_summaryless_read_and_missing_pair_are_omitted_with_dense_run_order', 'delitem', "resolve.TABLES['en']['text']", 'step_list_invoices_returned', ("resolve.TABLES['en']['text']",)),
    ('tests/test_turn_steps.py', 322, 'test_card_steps_and_stored_replay_are_exact_without_a_new_model_call', 'setattr', 'resolve', 'step_text', ('resolve', 'resolve_text')),
    ('tests/test_turn_steps.py', 340, 'test_aggregate_bound_is_applied_before_finalise_and_survives_reopen', 'setattr', 'resolve', 'step_text', ('resolve', 'resolve_text')),
    ('tests/test_warehouse_names.py', 50, 'use_state', 'setattr', 'rv', 'customers', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_records')),
    ('tests/test_warehouse_names.py', 51, 'use_state', 'setattr', 'rv', '_items', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_policy', 'resolve_types')),
    ('tests/test_warehouse_names.py', 52, 'use_state', 'setattr', 'rv', '_company_warehouse_names', ('rv', 'resolve_warehouse')),
    ('tests/test_warehouse_names.py', 53, 'use_state', 'setattr', 'rv', 'call_service', ('rv', 'resolve_args', 'resolve_policy', 'resolve_warehouse')),
    ('tests/test_warehouse_names.py', 384, 'test_disabled_stock_plugin_reads_as_no_known_warehouses', 'setattr', 'rv', 'call_service', ('rv', 'resolve_args', 'resolve_policy', 'resolve_warehouse')),
    ('tests/test_warehouse_names.py', 397, 'test_unrelated_plugin_refusal_still_raises', 'setattr', 'rv', 'call_service', ('rv', 'resolve_args', 'resolve_policy', 'resolve_warehouse')),
    ('tests/test_warehouse_names.py', 546, 'test_other_name_readers_keep_the_break_after_month_period', 'setattr', 'rv', 'bounded_names', ('rv', 'resolve_amounts', 'resolve_args', 'resolve_inventory', 'resolve_payment', 'resolve_policy', 'resolve_records', 'resolve_warehouse', 'resolve_words')),
    ('tests/test_warehouse_reader_unit.py', 35, 'memory', 'setattr', 'rv', 'customers', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_records')),
    ('tests/test_warehouse_reader_unit.py', 36, 'memory', 'setattr', 'rv', '_items', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_policy', 'resolve_types')),
    ('tests/test_warehouse_reader_unit.py', 38, 'memory', 'setattr', 'rv', '_company_warehouse_names', ('rv', 'resolve_warehouse')),
    ('tests/test_warehouse_reader_unit.py', 39, 'memory', 'setattr', 'rv', 'call_service', ('rv', 'resolve_args', 'resolve_policy', 'resolve_warehouse')),
    ('tests/test_warehouse_reader_unit.py', 136, 'test_a_service_item_cannot_silently_consume_a_warehouse', 'setattr', 'rv', '_items', ('rv', 'resolve_args', 'resolve_inventory', 'resolve_policy', 'resolve_types')),
    ('tests/test_warehouse_reader_unit.py', 137, 'test_a_service_item_cannot_silently_consume_a_warehouse', 'setattr', 'rv', '_company_warehouse_names', ('rv', 'resolve_warehouse')),
    ('tests/unit/test_stock_onboarding.py', 98, 'resolved', 'setattr', 'resolve', 'planned_instead', ('resolve', 'resolve_args')),
]


def source_at(revision: str, path: Path) -> str:
    return subprocess.check_output(
        ["git", "show", f"{revision}:{path}"], cwd=ROOT, text=True)


def original_source(base: str | None) -> str:
    if base:
        return source_at(base, PACKAGE / "resolve.py")
    return subprocess.check_output(
        ["git", "cat-file", "blob", BASE_BLOB], cwd=ROOT, text=True)


def definitions(tree: ast.Module) -> dict[str, ast.AST]:
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            key = node.name
        elif isinstance(node, ast.Assign):
            key = ast.unparse(node.targets[0])
        elif isinstance(node, ast.For):
            key = "for " + ast.unparse(node.target)
        elif isinstance(node, (ast.Import, ast.ImportFrom)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        else:
            raise AssertionError(f"Unclassified top-level statement: {ast.dump(node)}")
        assert key not in out, f"Duplicate definition: {key}"
        out[key] = node
    return out


def prove(base: str | None = None) -> None:
    before = ast.parse(original_source(base))
    expected = definitions(before)
    actual, owners, dependencies = {}, {}, {}
    shim_sites = {("resolve_args", "outgoing_account_phrases"),
                  ("resolve_inventory", "outgoing_account_phrases")}
    shim = ast.parse('''def outgoing_account_phrases(*args, **kwargs):
    from . import resolve
    return resolve.outgoing_account_phrases(*args, **kwargs)
''').body[0]
    seen_shims = set()
    paths = sorted((ROOT / PACKAGE).glob("resolve_*.py"))
    assert paths, "No domain modules"
    for path in paths:
        tree = ast.parse(path.read_text())
        dependencies[path.stem] = set()
        for name, node in definitions(tree).items():
            site = (path.stem, name)
            if site in shim_sites:
                assert ast.dump(node, include_attributes=False) == ast.dump(
                    shim, include_attributes=False), f"Forwarding shim changed: {site}"
                seen_shims.add(site)
                continue
            assert name not in actual, f"Definition duplicated: {name}"
            actual[name], owners[name] = node, path.stem
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = (node.module or "").rsplit(".", 1)[-1]
                if module == "resolve" or module.startswith("resolve_"):
                    dependencies[path.stem].add(module)
    assert seen_shims == shim_sites, ("Missing forwarding shims", shim_sites - seen_shims)
    assert actual.keys() == expected.keys(), (
        "Missing", expected.keys() - actual.keys(), "Added", actual.keys() - expected.keys())
    for name in expected:
        assert ast.dump(expected[name], include_attributes=False) == ast.dump(
            actual[name], include_attributes=False), f"AST changed: {name} in {owners[name]}"

    done, visiting = set(), []

    def visit(module):
        assert module != "resolve", "A domain imports the facade"
        assert module not in visiting, f"Import cycle: {visiting + [module]}"
        if module in done:
            return
        visiting.append(module)
        for dependency in dependencies[module]:
            visit(dependency)
        visiting.pop()
        done.add(module)

    for module in dependencies:
        visit(module)

    facade = importlib.import_module("erpclaw_assistant.resolve")
    assert not definitions(ast.parse(Path(facade.__file__).read_text())), "Facade contains implementation"
    for name, module in owners.items():
        if name.startswith("for "):
            continue
        provider = importlib.import_module("erpclaw_assistant." + module)
        assert getattr(facade, name) is getattr(provider, name), f"Facade identity: {name}"
    # Imported dependencies were also patchable members of the original surface.
    for node in before.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and getattr(node, "module", None) != "__future__":
            for alias in node.names:
                assert hasattr(facade, alias.asname or alias.name.split(".")[0])
    count = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                for n in expected.values())
    print(f"PASS: {count} definitions and {len(expected) - count} data/initialisation statements "
          f"have identical ASTs; {len(paths)} acyclic domain modules; facade identities preserved")


def patch_calls(tree):
    """Find patches through imports, attribute aliases, and dotted string targets."""
    aliases = {"graph.resolve"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "erpclaw_assistant":
            aliases.update(a.asname or a.name for a in node.names if a.name == "resolve")
        elif isinstance(node, ast.Import):
            aliases.update(a.asname or a.name for a in node.names if a.name == "erpclaw_assistant.resolve")
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and ast.unparse(node.value) in aliases:
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        changed = True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not node.args:
            continue
        if node.func.attr not in {"setattr", "setitem", "delitem", "object"}:
            continue
        target = ast.unparse(node.args[0])
        if (any(target == a or target.startswith(a + ".") for a in aliases)
                or "erpclaw_assistant.resolve." in target):
            yield node


def enclosing_function(tree, node):
    return min((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.lineno <= node.lineno <= n.end_lineno), key=lambda n: n.end_lineno - n.lineno).name


def current_call(site):
    path, line, function, operation, alias, name, bindings = site
    tree = ast.parse((ROOT / path).read_text())
    ordinal = [s for s in PATCH_SITES if s[:1] == site[:1] and s[2:4] == site[2:4]
               and s[5] == name].index(site)
    calls = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == operation
                    and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
                    and n.args[1].value == name and enclosing_function(tree, n) == function),
                   key=lambda n: n.lineno)
    return tree, calls[ordinal]


def prove_patch_sites(base: str) -> None:
    discovered = set()
    for path in sorted((ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(ROOT)
        before = ast.parse(source_at(base, relative))
        for node in patch_calls(before):
            discovered.add((str(relative), node.lineno))
    assert discovered == {(s[0], s[1]) for s in PATCH_SITES}, (
        "Unclassified patch sites", discovered ^ {(s[0], s[1]) for s in PATCH_SITES})
    for site in PATCH_SITES:
        tree, call = current_call(site)
        target = ast.unparse(call.args[0])
        if target == "_module":
            loop = min((n for n in ast.walk(tree) if isinstance(n, ast.For)
                        and n.lineno < call.lineno <= n.end_lineno), key=lambda n: n.end_lineno - n.lineno)
            bindings = tuple(ast.unparse(n) for n in loop.iter.elts)
        else:
            bindings = (target,)
        assert bindings == site[6], (site, bindings)
        print(f"{site[0]}:{site[1]} {site[3]} {site[4]}.{site[5]} -> {', '.join(bindings)}")
    for path in {s[0] for s in PATCH_SITES}:
        before, after = ast.parse(source_at(base, Path(path))), ast.parse((ROOT / path).read_text())
        from collections import Counter
        assertions = lambda t: Counter(ast.dump(n, include_attributes=False) for n in ast.walk(t)
                                       if isinstance(n, ast.Assert))
        assert not assertions(before) - assertions(after), f"Assertion changed: {path}"
    print(f"PASS: all {len(PATCH_SITES)} patch sites in {len({s[0] for s in PATCH_SITES})} files "
          "are classified; every original assertion is unchanged")


def replace_node(source: str, node: ast.AST, replacement: str) -> str:
    raw = source.encode()
    lines = raw.splitlines(keepends=True)
    start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
    return (raw[:start] + replacement.encode() + raw[end:]).decode()


def mutation(site):
    """One killed mutant per original site, including the three negative guards.

    Positive call sites: poison that exact replacement, requiring its consumer
    to execute it. Table/language sites: omit that exact patch.
    Negative guards: introduce the forbidden call in its real consumer. Omitting
    a negative guard's patch cannot change the successful path it protects.
    All mutations are restored in finally blocks, including on interruption.
    """
    path, line, function, operation, alias, name, bindings = site
    if path == "tests/test_money_spans.py" and line == 742:
        production = ROOT / PACKAGE / "resolve_policy.py"
        original = production.read_text()
        assert original.count('if msg.get("completed"):') == 1
        return production, original.replace('if msg.get("completed"):', 'if False:'), "completed consumption was resolved again"
    if path == "tests/test_outgoing_account.py" and line == 381:
        production = ROOT / PACKAGE / "resolve_inventory.py"
        original = production.read_text()
        inventory = next(n for n in ast.parse(original).body if isinstance(n, ast.FunctionDef) and n.name == "inventory")
        guard = next(n for n in ast.walk(inventory) if isinstance(n, ast.If)
                     and isinstance(n.test, ast.Name) and n.test.id == "outgoing")
        return production, replace_node(original, guard.test, "True"), "must not run the outgoing account reader"
    if path == "tests/test_turn_steps.py" and line == 322:
        production = ROOT / PACKAGE / "store.py"
        original = production.read_text()
        node = next(n for n in ast.parse(original).body if isinstance(n, ast.FunctionDef) and n.name == "events")
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            first = node.body[1]
        lines = original.splitlines(keepends=True)
        lines.insert(first.lineno - 1, '    from . import resolve\n'
                     '    resolve.step_text("write", operation="Draft an invoice", '
                     'title="the invoice card", names=[], parts=[])\n')
        return production, "".join(lines), "recomputed on replay"
    tree, call = current_call(site)
    production = ROOT / path
    if operation == "setattr" and name != "LANGUAGE":
        diagnostic = f"resolver patch probe {line} intercepted"
        poison = f"lambda *a, **k: (_ for _ in ()).throw(AssertionError({diagnostic!r}))"
        return production, replace_node(production.read_text(), call.args[2], poison), diagnostic
    return production, replace_node(production.read_text(), call, "None"), None


def mutation_selector(site):
    fixtures = {
        "use_state": "test_month_day_tail_leaves_the_known_name_whole",
        "memory": "test_owner_named_warehouse_is_read_and_accounted_without_model_argument",
        "resolved": "test_price_stays_grounded_in_owner_digits_with_new_stock_choices",
    }
    return site[0] + "::" + fixtures.get(site[2], site[2])


def prove_mutations(selected: int | None, start: int = 1) -> None:
    sites = PATCH_SITES[start - 1:] if selected is None else [PATCH_SITES[selected - 1]]
    selectors = list(dict.fromkeys(mutation_selector(site) for site in sites))
    baseline = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-ra", *selectors],
                              cwd=ROOT, text=True, capture_output=True)
    assert baseline.returncode == 0, ("Unmutated patch tests must pass", baseline.stdout, baseline.stderr)
    print("Unmutated patch tests: " + baseline.stdout.strip().splitlines()[-1], flush=True)
    for site in sites:
        path, mutated, diagnostic = mutation(site)
        original = path.read_text()
        try:
            path.write_text(mutated)
            result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-ra", "-x",
                                     mutation_selector(site)], cwd=ROOT, text=True, capture_output=True)
        finally:
            path.write_text(original)
        assert result.returncode == 1 and ("FAILED " in result.stdout or
                                         diagnostic is not None and "ERROR " in result.stdout), (
            "Mutant survived or pytest did not execute", site, result.stdout, result.stderr)
        assert diagnostic is None or diagnostic in result.stdout, (site, result.stdout)
        print(f"KILLED {PATCH_SITES.index(site) + 1:02d}: {site[0]}:{site[1]} ({site[5]})", flush=True)
    print(f"PASS: {len(sites)} of {len(sites)} site mutations killed")


def owner_snapshot():
    """Run in a fresh interpreter rooted in either tree; never write a snapshot."""
    import contextlib
    import hashlib
    import io
    import json
    from pathlib import Path
    import sys

    root = Path.cwd()
    sys.path[:0] = [str(p) for group in ("packages", "plugins")
                   for p in sorted((root / group).glob("*/src"))] + [str(root / "tests")]
    import pytest

    class Collect:
        def pytest_collection_finish(self, session):
            self.marks = sorted((item.nodeid, [
                (list(mark.args), {k: v.__module__ + "." + v.__name__ if isinstance(v, type) else v
                                   for k, v in mark.kwargs.items()})
                for mark in item.iter_markers("xfail")])
                for item in session.items if list(item.iter_markers("xfail")))

    collector, output = Collect(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider",
                              "tests/test_owner_text_checker.py", "tests/test_owner_text_sweep_static.py",
                              "tests/test_owner_text_sweep_turns.py", "tests/test_owner_text_sweep_routes.py"],
                             plugins=[collector])
        import test_owner_text_sweep_static as static
        import owner_text
    assert result == 0, output.getvalue()

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def original_id(key):
        return "resolve.py:" + key.split(":", 1)[1] if key.startswith("resolve_") else key

    refusals = {original_id(k): v[:2] for k, v in static.REFUSALS.items()}
    assert len(refusals) == len(static.REFUSALS), "Relocation cannot merge construction IDs"
    tables = {k.replace("erpclaw_assistant.resolve_text.", "erpclaw_assistant.resolve."): v
              for k, v in static.TABLE.items()}
    print(json.dumps({
        "marks": collector.marks, "refusal_hash": digest(refusals), "refusal_count": len(refusals),
        "literal_count": sum(v[0] is not None for v in refusals.values()),
        "table_hash": digest(tables), "table_count": len(tables),
        "kinds": sorted(owner_text.resolver_occurrence_kinds()),
        "declared": digest(sorted(owner_text.declared_refusal_codes())),
        "moved": {original_id(k): v[2] for k, v in static.REFUSALS.items() if k.startswith("resolve")},
        "table_ids": list(static.TABLE),
    }))


def prove_owner_text(base_directory: Path):
    import inspect
    import json
    from collections import Counter

    base_directory = base_directory.resolve()
    assert not base_directory.is_relative_to(ROOT), "The comparison checkout must be outside the working tree"
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=base_directory, text=True).strip()
    assert tree == BASE_TREE, f"Wrong comparison tree: {tree}"
    script = inspect.getsource(owner_snapshot) + "\nowner_snapshot()\n"
    snapshots = []
    for directory in (base_directory, ROOT):
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=directory,
                                text=True, capture_output=True)
        assert result.returncode == 0, (result.stdout, result.stderr)
        snapshots.append(json.loads(result.stdout))
    before, after = snapshots
    for key in before.keys() - {"moved", "table_ids"}:
        assert before[key] == after[key], ("Owner-text inventory changed", key, before[key], after[key])
    for key, where in before["moved"].items():
        print(f"{key}: {where} -> {after['moved'][key]}")
    for old, new in zip(before["table_ids"], after["table_ids"], strict=True):
        if old != new:
            print(f"Language entry: {old} -> {new}")
    for name in ("owner_text.py", "test_owner_text_sweep_static.py", "test_owner_text_checker.py"):
        old = ast.parse(source_at(BASE_TREE, Path("tests") / name))
        new = ast.parse((ROOT / "tests" / name).read_text())
        functions = {n.name: n for n in ast.walk(new) if isinstance(n, ast.FunctionDef)}
        for function in (n for n in ast.walk(old) if isinstance(n, ast.FunctionDef)):
            assertions = lambda n: Counter(ast.dump(a, include_attributes=False)
                                           for a in ast.walk(n) if isinstance(a, ast.Assert))
            assert not assertions(function) - assertions(functions[function.name]), (
                "Original owner-text assertion changed", name, function.name)
    print(f"PASS: owner-text inventories unchanged ({before['refusal_count']} constructions, "
          f"{before['literal_count']} readable texts, {before['table_count']} entries, "
          f"{len(before['kinds'])} occurrence kinds); identical {len(before['marks'])} strict xfail IDs/marks; "
          "every original assertion retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="git revision containing the original resolver")
    parser.add_argument("--patch-sites", action="store_true", help="audit and print every patch choice")
    parser.add_argument("--mutations", action="store_true", help="run one mutation per patch site (requires test DB env)")
    parser.add_argument("--site", type=int, choices=range(1, len(PATCH_SITES) + 1), help="run only this mutation")
    parser.add_argument("--start-site", type=int, default=1, choices=range(1, len(PATCH_SITES) + 1),
                        help="resume mutation checks at this site")
    parser.add_argument("--owner-base", type=Path, help="audit owner-text against an external starting-tree checkout")
    args = parser.parse_args()
    prove(args.base)
    if args.patch_sites:
        prove_patch_sites(args.base or BASE_TREE)
    if args.mutations:
        prove_mutations(args.site, args.start_site)
    if args.owner_base:
        prove_owner_text(args.owner_base)


if __name__ == "__main__":
    main()
