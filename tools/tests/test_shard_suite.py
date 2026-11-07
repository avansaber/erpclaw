"""The suite splitter: shards keep ordinary files whole and cover every test once, durations balance better than counts,
and `check` passes only on an exact, all-passing union of every expected shard. No PostgreSQL: tools/tests has
no conftest of its own, and the root conftest only redacts reports."""
from __future__ import annotations

import importlib.util
import json
import math
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "shard_suite.py"
_spec = importlib.util.spec_from_file_location("shard_suite", _PATH)
ss = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("shard_suite", ss)
_spec.loader.exec_module(ss)


def suite(files: int = 6, per_file: int = 4) -> list[str]:
    """Node ids across nested files, with parameters, classes and brackets in ids."""
    ids = []
    for f in range(files):
        path = f"tests/pkg{f % 2}/test_f{f}.py"
        for t in range(per_file):
            ids.append(f"{path}::test_{t}" if t % 2 else f"{path}::TestK::test_{t}[a-{t}::x]")
    return ids


def write_junit(path: Path, cases: list[tuple[str, str]], seconds: float = 1.0) -> None:
    """A report in pytest's shape: testsuites/testsuite/testcase(classname, name, time), with a failure, error or
    skipped child. An outcome 'failed+errors' writes two elements, as pytest does for a call failure followed
    by a teardown error."""
    s = ET.Element("testsuite", name="pytest", tests=str(len(cases)), time=f"{seconds:.3f}")
    for nodeid, outcome in cases:
        classname, name = ss.junit_key(nodeid)
        for part in outcome.split("+"):
            tc = ET.SubElement(s, "testcase", classname=classname, name=name, time="0.100")
            child = {"failed": "failure", "errors": "error", "skipped": "skipped"}.get(part)
            if child:
                ET.SubElement(tc, child, message="x")
    root = ET.Element("testsuites", name="pytest tests")
    root.append(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode"))


def run_shards(tmp_path: Path, ids: list[str], shards: int, override: dict[str, str] | None = None) -> Path:
    """Every shard's report as if each test ran in its planned shard; `override` sets some tests' outcomes."""
    plan = ss.make_plan(ids, shards)
    out = tmp_path / "junit"
    for s in plan["plan"]:
        write_junit(out / f"shard-{s['index']}.xml", [(n, (override or {}).get(n, "passed")) for n in s["nodeids"]])
    return out


# ---------------------------------------------------------------- collect

def test_parse_collect_output_reads_ids_and_checks_the_count():
    ids = suite(2, 2)
    text = "\n".join(ids) + "\n\n4 tests collected in 0.12s\n"
    assert ss.parse_collect_output(text) == ids
    with pytest.raises(ss.InputError, match="says 5"):
        ss.parse_collect_output("\n".join(ids) + "\n\n5 tests collected in 0.12s\n")
    with pytest.raises(ss.InputError, match="repeats"):
        ss.parse_collect_output(f"{ids[0]}\n{ids[0]}\n\n2 tests collected in 0.1s\n")
    with pytest.raises(ss.InputError, match="deselection"):
        ss.parse_collect_output(f"{ids[0]}\n\n1/3 tests collected (2 deselected) in 0.1s\n")


def test_parse_collect_output_ignores_the_warnings_summary_after_the_ids():
    ids = suite(1, 2)
    text = "\n".join(ids) + f"\n\n=== warnings summary ===\n{ids[0]}\n  a warning\n\n2 tests collected in 0.1s\n"
    assert ss.parse_collect_output(text) == ids


# ---------------------------------------------------------------- split

@pytest.mark.parametrize("shards", [1, 2, 3, 5, 6])
def test_split_keeps_each_file_in_one_shard_and_every_id_in_exactly_one(shards):
    ids = suite()
    plan = ss.make_plan(ids, shards)
    placed = [n for s in plan["plan"] for n in s["nodeids"]]
    assert sorted(placed) == sorted(ids) and len(placed) == len(set(placed))
    owner = {}
    for s in plan["plan"]:
        assert s["files"], "no shard may be empty: pytest with no paths runs the whole suite"
        assert s["nodeids"] == [n for n in ids if ss.file_of(n) in s["files"]]  # collection order kept
        for n in s["nodeids"]:
            assert owner.setdefault(ss.file_of(n), s["index"]) == s["index"]


def test_split_refuses_more_shards_than_files():
    with pytest.raises(ss.InputError, match="at least one"):
        ss.make_plan(suite(3), 4)


def test_balancing_by_durations_beats_balancing_by_count_on_a_skewed_suite():
    # one file of 2 slow tests and five files of 10 fast ones: by count the slow file looks smallest
    ids = [f"tests/test_slow.py::test_{i}" for i in range(2)]
    ids += [f"tests/test_fast{f}.py::test_{i}" for f in range(5) for i in range(10)]
    durations = {n: (120.0 if "slow" in n else 0.5) for n in ids}
    by_count = ss.make_plan(ids, 3)
    by_time = ss.make_plan(ids, 3, durations)
    assert by_time["balance"] == "durations" and by_count["balance"] == "count"
    assert ss.makespan(by_time, durations) < ss.makespan(by_count, durations)
    assert ss.makespan(by_time, durations) == 240.0  # the slow file alone, the best possible


def read_committed_durations() -> dict[str, float]:
    path = Path(__file__).resolve().parents[2] / ".test_durations"
    return json.loads(path.read_text(encoding="utf-8"))


def test_committed_durations_balance_four_shards():
    durations = read_committed_durations()
    ids = sorted(durations)
    plan = ss.make_plan(ids, 4, durations)
    weights = [shard["weight"] for shard in plan["plan"]]
    mean = sum(weights) / len(weights)
    largest = max(weights)
    assert largest <= 1.10 * mean, (
        f"largest predicted shard is {largest / 60:.1f} minutes; "
        f"mean is {mean / 60:.1f} minutes (maximum allowed: 110% of mean)"
    )


def test_that_plan_keeps_every_test_exactly_once():
    durations = read_committed_durations()
    ids = sorted(durations)
    plan = ss.make_plan(ids, 4, durations)
    placed = [nodeid for shard in plan["plan"] for nodeid in shard["nodeids"]]
    assert set(placed) == set(ids)
    assert len(placed) == len(set(placed)), "the plan must not duplicate any test"
    for shard in plan["plan"]:
        assert shard["files"], "every shard must have at least one file"


def assert_duration_shape(durations):
    assert isinstance(durations, dict), "durations must map node ids to seconds"
    for nodeid, seconds in durations.items():
        path, separator, name = nodeid.partition("::")
        assert path and separator and name, f"not a test node id: {nodeid!r}"
        assert (isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                and math.isfinite(seconds) and seconds >= 0), (
            f"{nodeid!r} must have a finite non-negative duration in seconds, got {seconds!r}"
        )
    assert sum(seconds > 0 for seconds in durations.values()) * 100 >= 95 * len(durations), (
        "at least 95% of test durations must be positive; skipped tests may have zero duration in JUnit reports"
    )
    assert sum(durations.values()) > 60 * 60, (
        ".test_durations is recorded from a CI run: use "
        "shard_suite.py check ... --write-durations .test_durations "
        "on the shard reports of a green dispatched run"
    )


def test_committed_durations_are_current_in_shape():
    assert_duration_shape(read_committed_durations())


@pytest.mark.parametrize("positive_count", [19, 20])
def test_duration_shape_allows_up_to_five_percent_zero_values(positive_count):
    durations = {nodeid: (240.0 if i < positive_count else 0.0) for i, nodeid in enumerate(suite(5, 4))}
    assert_duration_shape(durations)


@pytest.mark.parametrize("invalid_value, positive_count, message", [
    pytest.param(0.0, 0, "at least 95%", id="all-zeros"),
    pytest.param(0.0, 18, "at least 95%", id="too-many-zeros"),
    pytest.param(-1.0, 19, "non-negative", id="negative"),
    pytest.param("invalid", 19, "non-negative", id="non-numeric"),
])
def test_duration_shape_rejects_invalid_files(tmp_path, invalid_value, positive_count, message):
    durations = {nodeid: (240.0 if i < positive_count else invalid_value)
                 for i, nodeid in enumerate(suite(5, 4))}
    path = tmp_path / ".test_durations"
    path.write_text(json.dumps(durations), encoding="utf-8")
    with pytest.raises(AssertionError, match=message):
        assert_duration_shape(json.loads(path.read_text(encoding="utf-8")))


def test_a_file_heavier_than_the_mean_is_reported():
    ids = [f"tests/test_slow.py::test_{i}" for i in range(2)]
    ids += [f"tests/test_fast{i}.py::test_one" for i in range(12)]
    durations = {nodeid: (50.0 if "slow" in nodeid else 1.0) for nodeid in ids}
    plan = ss.make_plan(ids, 4, durations)
    assert sum(durations.values()) / 4 < 100.0
    assert ss.makespan(plan, durations) == 100.0  # a whole file bounds the makespan
    assert max(shard["weight"] for shard in plan["plan"]) == 100.0
    placed = [nodeid for shard in plan["plan"] for nodeid in shard["nodeids"]]
    assert set(placed) == set(ids)
    assert len(placed) == len(set(placed))


def test_tests_missing_from_the_durations_file_weigh_the_known_mean():
    groups = ss.group_by_file(["a.py::t1", "a.py::t2", "b.py::t1"])
    assert ss.file_weights(groups, {"a.py::t1": 4.0, "b.py::t1": 2.0}) == {"a.py": 7.0, "b.py": 2.0}


def independent_suite() -> tuple[list[str], dict[str, float]]:
    ids = [f"tests/test_turn_interleaving.py::test_turn[case {i}]" for i in range(8)] + suite(6, 2)
    return ids, {n: (200.0 if ss.file_of(n) == "tests/test_turn_interleaving.py" else 10.0) for n in ids}


@pytest.mark.parametrize("shards", [4, 5, 6])
@pytest.mark.parametrize("by_duration", [False, True])
def test_independent_cases_spread_and_cover_the_suite_once(shards, by_duration, monkeypatch):
    ids, durations = independent_suite()
    durations = durations if by_duration else None
    plan = ss.make_plan(ids, shards, durations)
    heavy = "tests/test_turn_interleaving.py"
    owners = [s for s in plan["plan"] if any(ss.file_of(n) == heavy for n in s["nodeids"])]
    assert len(owners) >= 2
    assert ss.SPLITTABLE_FILES == (heavy,)
    placed = [n for s in plan["plan"] for n in s["nodeids"]]
    assert sorted(placed) == sorted(ids)
    assert len(placed) == len(set(placed))
    for shard in plan["plan"]:
        assert heavy not in shard["files"]
        assert shard["files"] or shard["split"]
        assert shard["split"] == [n for n in ids if n in shard["nodeids"] and ss.file_of(n) == heavy]
        assert shard["nodeids"] == [n for n in ids if n in shard["split"] or ss.file_of(n) in shard["files"]]
    assert plan == ss.make_plan(ids, shards, durations)
    monkeypatch.setattr(ss, "SPLITTABLE_FILES", ())
    whole = ss.make_plan(ids, shards, durations)
    assert max(s["weight"] for s in plan["plan"]) < max(s["weight"] for s in whole["plan"])


def test_unlisted_heavy_file_stays_whole():
    ids = suite()
    durations = {n: (200.0 if ss.file_of(n) == ss.file_of(ids[0]) else 1.0) for n in ids}
    plan = ss.make_plan(ids, 4, durations)
    heavy = ss.file_of(ids[0])
    assert sum(durations[n] for n in ids if ss.file_of(n) == heavy) > sum(durations.values()) / 8
    owners = [s for s in plan["plan"] if heavy in s["files"]]
    assert len(owners) == 1
    assert all(s["split"] == [] for s in plan["plan"])
    assert all(n in owners[0]["nodeids"] for n in ids if ss.file_of(n) == heavy)


@pytest.mark.parametrize("weight, should_split", [(4.0, False), (5.0, False), (6.0, True)])
def test_independent_file_splits_only_above_half_the_mean(weight, should_split):
    heavy = "tests/test_turn_interleaving.py"
    ids = [f"{heavy}::test_turn[{i}]" for i in range(2)] + suite(7, 1)
    durations = {n: (weight / 2 if ss.file_of(n) == heavy else 5.0) for n in ids}
    plan = ss.make_plan(ids, 4, durations)
    assert (weight > sum(durations.values()) / 8) == should_split
    assert any(s["split"] for s in plan["plan"]) == should_split
    assert sum(heavy in s["files"] for s in plan["plan"]) == (0 if should_split else 1)


def test_split_cases_missing_durations_use_the_known_mean():
    ids, _ = independent_suite()
    durations = {ids[0]: 4.0, ids[-1]: 2.0}
    plan = ss.make_plan(ids, 4, durations)
    assert any(s["split"] for s in plan["plan"])
    for shard in plan["plan"]:
        assert shard["weight"] == sum(durations.get(n, 3.0) for n in shard["nodeids"])


@pytest.mark.parametrize("shards", [4, 5, 6])
def test_independent_cases_can_fill_shards_without_whole_files(shards):
    ids, _ = independent_suite()
    ids = ids[:8]
    plan = ss.make_plan(ids, shards)
    for shard in plan["plan"]:
        assert shard["files"] == []
        assert shard["split"] == shard["nodeids"]
        assert shard["split"]
    assert sorted(n for s in plan["plan"] for n in s["nodeids"]) == sorted(ids)


@pytest.mark.parametrize("shards", [4, 5])
def test_committed_collection_balances_and_spreads_independent_cases(shards):
    durations = read_committed_durations()
    ids = sorted(durations)
    plan = ss.make_plan(ids, shards, durations)
    weights = [s["weight"] for s in plan["plan"]]
    mean = sum(weights) / shards
    largest = max(weights)
    heavy = [n for n in ids if ss.file_of(n) == "tests/test_turn_interleaving.py"]
    assert len(heavy) == 8
    assert sum(any(n in heavy for n in s["split"]) for s in plan["plan"]) >= 2
    placed = [n for s in plan["plan"] for n in s["nodeids"]]
    assert sorted(placed) == sorted(ids)
    assert len(placed) == len(set(placed))
    assert plan == ss.make_plan(ids, shards, durations)
    assert largest <= 1.10 * mean, (
        f"largest predicted shard is {largest / 60:.1f} minutes; "
        f"mean is {mean / 60:.1f} minutes (maximum allowed: 110% of mean)"
    )


@pytest.fixture
def imported_test_modules(tmp_path, monkeypatch):
    """Stand in for real test modules collected earlier in the parent process."""
    modules = {}
    for name in ("test_turn_interleaving", *(f"test_fast{i}" for i in range(6)), "test_one", "test_two"):
        path = tmp_path / f"{name}.py"
        path.write_text("def test_existing(): pass\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return modules


def test_split_file_arguments_select_exactly_the_planned_cases(pytester, capsys, imported_test_modules):
    pytester.makepyfile(**{
        "tests/test_turn_interleaving": """
            import pytest
            @pytest.mark.parametrize("value", range(8), ids=[f"case {i} [inner]" for i in range(8)])
            def test_turn(value): pass
        """,
        **{f"tests/test_fast{i}": "def test_one(): pass" for i in range(6)},
    })
    collected = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--collect-only", "-q")
    assert collected.ret == 0
    ids = ss.parse_collect_output("\n".join(collected.outlines))
    durations = {n: (200.0 if "test_turn_interleaving.py::" in n else 10.0) for n in ids}
    nodeids_path = pytester.path / "nodeids.txt"
    nodeids_path.write_text("\n".join(ids) + "\n", encoding="utf-8")
    duration_path = pytester.path / "durations.json"
    duration_path.write_text(json.dumps(durations), encoding="utf-8")
    plan_path = pytester.path / "plan.json"
    assert ss.main(["split", "--collected", str(nodeids_path), "--shards", "4", "--durations",
                    str(duration_path), "--plan-out", str(plan_path)]) == 0
    plan = ss.load_plan(plan_path)
    assert any(s["split"] and s["files"] for s in plan["plan"])
    capsys.readouterr()
    for shard in plan["plan"]:
        assert ss.main(["split", "--plan", str(plan_path), "--index", str(shard["index"]), "--files"]) == 0
        output = capsys.readouterr().out
        arguments = output.splitlines()
        assert output == "".join(f"{n}\n" for n in [*shard["files"], *shard["split"]])
        assert any("case " in n and "[inner]" in n for n in shard["split"])
        selected = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--collect-only", "-q", *arguments)
        assert selected.ret == 0
        assert sorted(ss.parse_collect_output("\n".join(selected.outlines))) == sorted(shard["nodeids"])
        capsys.readouterr()
        assert ss.main(["split", "--plan", str(plan_path), "--index", str(shard["index"])]) == 0
        assert capsys.readouterr().out.splitlines() == shard["nodeids"]
    assert all(sys.modules[name] is module for name, module in imported_test_modules.items())


def test_split_is_deterministic(tmp_path, capsys):
    ids = suite(9, 3)
    durations = {n: float(len(n) % 7) for n in ids}
    shuffled = dict(random.Random(7).sample(sorted(durations.items()), len(durations)))
    assert ss.make_plan(ids, 4, durations) == ss.make_plan(ids, 4, shuffled)
    collected = tmp_path / "nodeids.txt"
    collected.write_text("".join(f"{n}\n" for n in ids))
    (tmp_path / "d.json").write_text(json.dumps(shuffled))
    outputs = []
    for _ in range(2):
        assert ss.main(["split", "--collected", str(collected), "--shards", "4", "--durations",
                        str(tmp_path / "d.json"), "--index", "2"]) == 0
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1] and outputs[0].strip()
    plan_path = tmp_path / "plan.json"
    assert ss.main(["split", "--collected", str(collected), "--shards", "4", "--durations",
                    str(tmp_path / "d.json"), "--plan-out", str(plan_path)]) == 0
    assert ss.main(["split", "--plan", str(plan_path), "--index", "2"]) == 0
    assert capsys.readouterr().out == outputs[0]


# ---------------------------------------------------------------- check

def test_check_cli_accepts_split_reports_and_detects_misplaced_cases(tmp_path, capsys):
    ids, durations = independent_suite()
    plan = ss.make_plan(ids, 4, durations)
    owners = [s for s in plan["plan"] if s["split"]]
    assert len(owners) >= 2
    collected = tmp_path / "nodeids.txt"
    collected.write_text("\n".join(ids) + "\n", encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    junit = tmp_path / "junit"
    for shard in plan["plan"]:
        write_junit(junit / f"shard-{shard['index']}.xml", [(n, "passed") for n in shard["nodeids"]])
    args = ["check", "--collected", str(collected), "--shards", "4", "--junit-dir", str(junit)]
    for extra in ([], ["--plan", str(plan_path)]):
        assert ss.main([*args, *extra]) == 0
        assert "UNION OK" in capsys.readouterr().out
    source, target = owners[:2]
    moved = source["split"][0]
    write_junit(junit / f"shard-{source['index']}.xml",
                [(n, "passed") for n in source["nodeids"] if n != moved])
    write_junit(junit / f"shard-{target['index']}.xml",
                [(n, "passed") for n in [*target["nodeids"], moved]])
    assert ss.main(args) == 0
    assert "UNION OK" in capsys.readouterr().out
    assert ss.main([*args, "--plan", str(plan_path)]) == 1
    assert "misplaced: 1" in capsys.readouterr().out


def test_check_passes_on_an_exact_union(tmp_path):
    ids = suite()
    result = ss.check(ids, run_shards(tmp_path, ids, 3), 3, ss.make_plan(ids, 3))
    assert result["problems"] == {}
    assert result["totals"] == {"passed": len(ids), "failed": 0, "skipped": 0, "errors": 0}
    assert sum(r["tests"] for r in result["per_shard"]) == len(ids)


def test_skips_count_but_do_not_fail_the_union(tmp_path):
    ids = suite()
    result = ss.check(ids, run_shards(tmp_path, ids, 2, {ids[1]: "skipped"}), 2)
    assert result["problems"] == {} and result["totals"]["skipped"] == 1


def test_check_fails_on_a_missing_id(tmp_path):
    ids = suite()
    junit = run_shards(tmp_path, ids[:-1], 2)  # the last test never ran
    result = ss.check(ids, junit, 2)
    assert result["problems"] == {"missing": [ids[-1]]}


def test_check_fails_on_an_id_duplicated_across_shards(tmp_path):
    ids = suite()
    junit = run_shards(tmp_path, ids, 2)
    extra_copy = ss.make_plan(ids, 2)["plan"][0]["nodeids"][0]
    s1 = [(n, "passed") for n in ss.make_plan(ids, 2)["plan"][1]["nodeids"]] + [(extra_copy, "passed")]
    write_junit(junit / "shard-1.xml", s1)
    result = ss.check(ids, junit, 2)
    assert list(result["problems"]) == ["duplicated"]
    assert result["problems"]["duplicated"] == [f"{extra_copy} (shards 0 and 1)"]


def test_check_fails_on_an_id_run_twice_in_one_shard(tmp_path):
    ids = suite()
    junit = run_shards(tmp_path, ids, 1)
    write_junit(junit / "shard-0.xml", [(n, "passed") for n in ids] + [(ids[0], "passed")])
    assert list(ss.check(ids, junit, 1)["problems"]) == ["duplicated"]


def test_a_call_failure_with_a_teardown_error_is_one_failed_test_not_a_duplicate(tmp_path):
    ids = suite()
    result = ss.check(ids, run_shards(tmp_path, ids, 2, {ids[0]: "failed+errors"}), 2)
    assert result["problems"] == {"failed": [ids[0]]}
    assert result["totals"]["failed"] == 1 and result["totals"]["errors"] == 0


def test_check_fails_on_an_extra_id(tmp_path):
    ids = suite()
    junit = run_shards(tmp_path, ids, 1)
    write_junit(junit / "shard-0.xml", [(n, "passed") for n in ids] + [("tests/test_new.py::test_x", "passed")])
    assert ss.check(ids, junit, 1)["problems"] == {"extra": ["tests.test_new::test_x (shard 0)"]}


def test_check_fails_on_a_failed_test_and_on_an_error(tmp_path):
    ids = suite()
    result = ss.check(ids, run_shards(tmp_path, ids, 3, {ids[2]: "failed", ids[5]: "errors"}), 3)
    assert result["problems"] == {"failed": [ids[2]], "errors": [ids[5]]}
    assert result["totals"] == {"passed": len(ids) - 2, "failed": 1, "skipped": 0, "errors": 1}


def test_check_fails_on_a_shard_that_did_not_report(tmp_path):
    ids = suite()
    junit = run_shards(tmp_path, ids, 3)
    lost = ss.make_plan(ids, 3)["plan"][1]["nodeids"]
    (junit / "shard-1.xml").unlink()
    result = ss.check(ids, junit, 3)
    assert result["problems"]["absent shard"] == ["shard 1: no shard-1.xml"]
    assert result["problems"]["missing"] == sorted(lost)
    assert [r["reported"] for r in result["per_shard"]] == [True, False, True]


def test_an_unreadable_report_is_a_shard_that_did_not_report(tmp_path):
    ids = suite()
    junit = run_shards(tmp_path, ids, 2)
    (junit / "shard-0.xml").write_text("<testsuites><testsuite>")  # cut off mid-write
    assert "absent shard" in ss.check(ids, junit, 2)["problems"]


def test_check_fails_on_a_report_from_a_shard_it_did_not_expect(tmp_path):
    ids = suite()
    junit = run_shards(tmp_path, ids, 2)
    write_junit(junit / "shard-2.xml", [])
    assert ss.check(ids, junit, 2)["problems"] == {"unexpected file": ["shard-2.xml"]}


def test_check_fails_on_a_test_run_in_a_shard_its_plan_did_not_give_it(tmp_path):
    ids = suite()
    plan = ss.make_plan(ids, 2)
    first, second = plan["plan"][0]["nodeids"], plan["plan"][1]["nodeids"]
    junit = tmp_path / "junit"
    write_junit(junit / "shard-0.xml", [(n, "passed") for n in first[1:]])
    write_junit(junit / "shard-1.xml", [(n, "passed") for n in [first[0], *second]])
    assert list(ss.check(ids, junit, 2, plan)["problems"]) == ["misplaced"]


def test_check_cli_writes_the_summary_and_exits_nonzero_on_a_problem(tmp_path, capsys):
    ids = suite()
    collected = tmp_path / "nodeids.txt"
    collected.write_text("".join(f"{n}\n" for n in ids))
    junit = run_shards(tmp_path, ids, 2, {ids[3]: "failed"})
    summary = tmp_path / "summary.md"
    report = tmp_path / "report.json"
    code = ss.main(["check", "--collected", str(collected), "--shards", "2", "--junit-dir", str(junit),
                    "--sha", "a" * 40, "--summary", str(summary), "--report", str(report)])
    assert code == 1
    assert "UNION FAILED" in capsys.readouterr().out
    md = summary.read_text()
    assert "a" * 40 in md and f"| {len(ids)} | {len(ids)} | {len(ids) - 1} | 1 | 0 | 0 |" in md
    assert json.loads(report.read_text())["outcomes"][ids[3]] == "failed"


def test_check_output_is_deterministic(tmp_path):
    ids = suite()
    junit = run_shards(tmp_path, ids[2:], 3)
    first = ss.render_text(ss.check(ids, junit, 4))
    assert first == ss.render_text(ss.check(ids, junit, 4)) and "UNION FAILED" in first


def test_the_junit_names_match_what_pytest_writes(pytester, imported_test_modules):
    """junit_key against a real pytest run: parameters with brackets, colons, slashes and a non-ASCII value."""
    pytester.makepyfile(**{
        "sub/test_one": """
            import pytest
            @pytest.mark.parametrize("v", ["a::b", "x[y]", "p/q", "é", 3])
            def test_p(v): pass
            class TestK:
                def test_m(self): pass
        """,
        "test_two": """
            import pytest
            def test_skip(): pytest.skip("no")
            def test_fail(): assert False
        """,
    })
    collected = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--collect-only", "-q")
    assert collected.ret == 0
    ids = ss.parse_collect_output("\n".join(collected.outlines))
    assert len(ids) == 8
    plan = ss.make_plan(ids, 2)
    junit = pytester.path / "junit"
    for s in plan["plan"]:
        report = junit / ss.JUNIT_NAME.format(index=s["index"])
        pytester.runpytest_subprocess("-p", "no:cacheprovider", "-q", f"--junitxml={report}", *s["files"])
    result = ss.check(ids, junit, 2, plan)
    assert result["problems"] == {"failed": ["test_two.py::test_fail"]}
    assert result["totals"] == {"passed": 6, "failed": 1, "skipped": 1, "errors": 0}
    assert all(sys.modules[name] is module for name, module in imported_test_modules.items())
