#!/usr/bin/env python3
"""Split the engine's pytest suite across N shards, and check that the shards together ran it all.

Standard library commands, so it adds nothing to uv.lock and runs on Python 3.12.

  collect   run `pytest --collect-only -q` (pytest options after `--`) and write the node ids, one per line;
            `--from-output FILE` parses output captured earlier instead of running pytest
  split     shard by test file, splitting allow-listed independent files by node id when their weight exceeds
            half the mean shard load; balance by a durations file ({node id: seconds}, pytest-split's
            .test_durations format) when given, else by test count; with `--index i` print shard i's node ids
            (`--files` prints whole-file paths then split node ids, one per line); without it print the whole
            plan as JSON (`--plan-out FILE` writes it too)
  check     read every shard's JUnit XML (DIR/shard-<i>.xml for i in 0..N-1) against the collected list: the
            union must equal the collected set exactly (none missing, none duplicated, none extra), every
            expected shard must have reported, and no test may have failed, errored or strictly xpassed;
            print passed, failed, skipped, xfailed, xpassed and error totals separately
  durations refresh .test_durations from shard JUnit reports and the collected list; parameter cases share
            one function key with their mean seconds; ambiguous and unmatched testcases are skipped

The shard's pytest run loads this module with `-p shard_suite` (tools on PYTHONPATH) to record non-strict
xpasses as JUnit properties; pytest otherwise writes them as ordinary passes. The commands need no pytest.

Exit status: 0 when all is well, 1 when a check fails, 2 on a usage or input error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

JUNIT_NAME = "shard-{index}.xml"
_JUNIT_FILE = re.compile(r"^shard-(\d+)\.xml$")
OUTCOMES = ("passed", "failed", "skipped", "xfailed", "xpassed", "errors")
_RANK = {"passed": 0, "skipped": 1, "xfailed": 1, "xpassed": 1,
         "errors": 2, "strict xpass": 3, "failed": 4}  # merging one test's two testcase elements
LIST_LIMIT = 50  # ids printed per kind of problem; the counts are always complete

# Tests normally stay together because fixtures and module state are per file; only listed files have
# been checked to be independent per case, so only those files may be split across shards.
SPLITTABLE_FILES = ("tests/test_turn_interleaving.py",)

# pytest's own bin_xml_escape (pytest 9.1, _pytest/junitxml.py), which it applies to a testcase's name
_ILLEGAL_XML = re.compile("[^\u0009\u000a\u000d -~\u0080-퟿-�\U00010000-\U0010ffff]")


class InputError(Exception):
    """Input this script cannot work from (exit status 2)."""


def pytest_configure(config):
    """Preserve non-strict xpasses in JUnit when loaded as a pytest plugin."""
    import pytest  # only the report hook needs pytest; the commands remain standard library only

    class XpassProperties:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_makereport(self, item, call):
            report = yield  # pytest's xfail handling must have finished before inspecting the outcome
            if report.passed and hasattr(report, "wasxfail"):
                # JUnit takes properties from the teardown report, so store on the item for later phases.
                item.user_properties.append(("pytest.xpass", "true"))
            return report

    config.pluginmanager.register(XpassProperties())


# ---------------------------------------------------------------- collect

def parse_collect_output(text: str) -> list[str]:
    """The node ids `pytest --collect-only -q` printed: one per line up to the first blank line, then the
    'N tests collected' line, whose N must equal the ids' count."""
    lines = text.splitlines()
    ids: list[str] = []
    rest: list[str] = []
    for n, line in enumerate(lines):
        if not line.strip():
            rest = lines[n + 1:]
            break
        if "::" not in line:
            raise InputError(f"not a node id in the collected list: {line!r}")
        ids.append(line)
    else:
        raise InputError("the collect output has no summary after its node ids")
    stated = None
    for line in rest:
        summary = line.strip().strip("=").strip()
        if m := re.match(r"(\d+) tests? collected\b", summary):
            stated = int(m.group(1))
            break
        if summary.startswith("no tests collected"):
            stated = 0
            break
    if stated is None:
        raise InputError("no 'N tests collected' line (a deselection or collection error is not supported)")
    if stated != len(ids):
        raise InputError(f"pytest says {stated} tests were collected, the list has {len(ids)}")
    dups = sorted(k for k, c in Counter(ids).items() if c > 1)
    if dups:
        raise InputError(f"the collected list repeats {len(dups)} node ids, e.g. {dups[0]!r}")
    return ids


def read_nodeids(path: Path) -> list[str]:
    ids = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    dups = sorted(k for k, c in Counter(ids).items() if c > 1)
    if dups:
        raise InputError(f"{path} repeats {len(dups)} node ids, e.g. {dups[0]!r}")
    return ids


def nodeids_digest(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- split

def file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def function_key(nodeid: str) -> str:
    """A stable duration key without parameter text, including any nested brackets or colons."""
    return nodeid.partition("[")[0]


def group_by_file(ids: list[str]) -> dict[str, list[str]]:
    """{file: its node ids}, both in collection order."""
    groups: dict[str, list[str]] = {}
    for nodeid in ids:
        groups.setdefault(file_of(nodeid), []).append(nodeid)
    return groups


def load_durations(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise InputError(f"{path}: a durations file is a JSON object of node id -> seconds")
    out: dict[str, float] = {}
    for key, value in data.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise InputError(f"{path}: {key!r} has no duration in seconds")
        out[str(key)] = float(value)
    return out


def file_weights(groups: dict[str, list[str]], durations: dict[str, float] | None) -> dict[str, float]:
    """A file's weight: its test count, or the sum of its tests' durations; a test the durations file does not
    know by exact id or function key counts as the known collected tests' mean."""
    if durations is None:
        return {f: float(len(ids)) for f, ids in groups.items()}
    seconds = {n: durations.get(n, durations.get(function_key(n))) for ids in groups.values() for n in ids}
    known = [value for value in seconds.values() if value is not None]
    default = sum(known) / len(known) if known else 1.0
    return {f: sum(seconds[n] if seconds[n] is not None else default for n in ids) for f, ids in groups.items()}


def make_plan(ids: list[str], shards: int, durations: dict[str, float] | None = None) -> dict:
    """Longest-weight-first onto the least loaded shard (ties: the path or node id, then the lower index).
    Allow-listed files above half the mean shard load become individual test items. Within a shard, files
    and ids keep collection order, so the same input always gives the same plan."""
    groups = group_by_file(ids)
    if shards < 1:
        raise InputError("--shards must be at least 1")
    weights = file_weights(groups, durations)
    threshold = sum(weights.values()) / shards / 2
    items: dict[str, list[str]] = {}
    for f, nodeids in groups.items():
        if f in SPLITTABLE_FILES and weights[f] > threshold:
            items.update({n: [n] for n in nodeids})
        else:
            items[f] = nodeids
    if shards > len(items):
        # an empty shard would run pytest with no paths, which is the whole suite
        raise InputError(f"{shards} shards but only {len(items)} test items: every shard needs at least one")
    weights = file_weights(items, durations)
    loads = [0.0] * shards
    assigned: list[list[str]] = [[] for _ in range(shards)]
    for item in sorted(items, key=lambda item: (-weights[item], item)):
        i = min(range(shards), key=lambda k: (loads[k], k))
        assigned[i].append(item)
        loads[i] += weights[item]
    position = {f: n for n, f in enumerate(groups)}
    node_position = {n: i for i, n in enumerate(ids)}
    plan_shards = []
    for i, assigned_items in enumerate(assigned):
        files = sorted((item for item in assigned_items if item in groups), key=position.__getitem__)
        split = sorted((item for item in assigned_items if item not in groups), key=node_position.__getitem__)
        nodeids = sorted((n for item in assigned_items for n in items[item]), key=node_position.__getitem__)
        plan_shards.append({"index": i, "files": files, "split": split, "tests": len(nodeids),
                            "weight": round(loads[i], 3), "nodeids": nodeids})
    return {"format": 1, "shards": shards, "balance": "count" if durations is None else "durations",
            "collected": len(ids), "collected_sha256": nodeids_digest(ids), "plan": plan_shards}


def makespan(plan: dict, durations: dict[str, float]) -> float:
    """The slowest shard's total, timed by `durations` (to compare plans)."""
    return max(sum(durations.get(n, 0.0) for n in s["nodeids"]) for s in plan["plan"])


def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or plan.get("format") != 1 or not isinstance(plan.get("plan"), list):
        raise InputError(f"{path} is not a shard plan of this script's format 1")
    return plan


# ---------------------------------------------------------------- check

def junit_key(nodeid: str) -> tuple[str, str]:
    """The (classname, name) pytest's JUnit XML gives a node id: pytest's mangle_test_address, no junit_prefix
    (pyproject sets none), and bin_xml_escape on the name."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params

    def repl(m: re.Match[str]) -> str:
        i = ord(m.group())
        return f"#x{i:02X}" if i <= 0xFF else f"#x{i:04X}"

    return ".".join(names[:-1]), _ILLEGAL_XML.sub(repl, names[-1])


def junit_index(ids: list[str]) -> dict[tuple[str, str], list[str]]:
    """Keep every candidate for each JUnit name so collisions cannot silently choose a test."""
    by_key: dict[tuple[str, str], list[str]] = {}
    for nodeid in ids:
        by_key.setdefault(junit_key(nodeid), []).append(nodeid)
    return by_key


def _outcome(testcase: ET.Element) -> str:
    tags = {child.tag for child in testcase}
    if "failure" in tags:
        if any(f.get("message", "").startswith("[XPASS(strict)]") for f in testcase.findall("failure")):
            return "strict xpass"
        return "failed"
    if "error" in tags:
        return "errors"
    if "skipped" in tags:
        if any(s.get("type") == "pytest.xfail" for s in testcase.findall("skipped")):
            return "xfailed"
        return "skipped"
    if any(p.get("name") == "pytest.xpass" and p.get("value") == "true"
           for p in testcase.findall("properties/property")):
        return "xpassed"
    return "passed"


def read_junit(path: Path) -> tuple[list[tuple[tuple[str, str], str, float]], float]:
    """([(key, outcome, seconds) per testcase element, in file order], pytest's session seconds)."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise InputError(f"{path}: not well-formed XML ({exc})") from exc
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise InputError(f"{path}: no <testsuite> element")
    cases = []
    for suite in suites:
        for tc in suite.iter("testcase"):
            key = (tc.get("classname", ""), tc.get("name", ""))
            cases.append((key, _outcome(tc), float(tc.get("time", "0") or 0)))
    return cases, sum(float(s.get("time", "0") or 0) for s in suites)


def check(ids: list[str], junit_dir: Path, shards: int, plan: dict | None = None) -> dict:
    problems: dict[str, list[str]] = {k: [] for k in
                                      ("ambiguous", "absent shard", "unexpected file", "duplicated", "extra",
                                       "missing", "misplaced", "failed", "errors", "strict xpass")}
    candidates = junit_index(ids)
    by_key = {key: matches[0] for key, matches in candidates.items() if len(matches) == 1}
    problems["ambiguous"] = [f"{matches[0]} and {nodeid} share a JUnit name"
                             for matches in candidates.values() for nodeid in matches[1:]]

    planned: dict[str, int] = {}
    if plan is not None:
        if plan.get("shards") != shards:
            raise InputError(f"the plan has {plan.get('shards')} shards, --shards says {shards}")
        if plan.get("collected_sha256") != nodeids_digest(ids):
            raise InputError("the plan was made from a different collected list")
        planned = {n: s["index"] for s in plan["plan"] for n in s["nodeids"]}

    expected = {JUNIT_NAME.format(index=i) for i in range(shards)}
    present = {p.name for p in junit_dir.iterdir() if _JUNIT_FILE.match(p.name)} if junit_dir.is_dir() else set()
    problems["unexpected file"] = sorted(present - expected)

    outcomes: dict[str, str] = {}
    seen_in: dict[str, int] = {}
    durations: dict[str, float] = {}
    per_shard = []
    for i in range(shards):
        path = junit_dir / JUNIT_NAME.format(index=i)
        row = {"index": i, "reported": path.is_file(), "tests": 0, "seconds": 0.0, **{k: 0 for k in OUTCOMES}}
        per_shard.append(row)
        if not row["reported"]:
            problems["absent shard"].append(f"shard {i}: no {path.name}")
            continue
        try:
            cases, row["seconds"] = read_junit(path)
        except InputError as exc:  # an unreadable report is a shard that did not report, and still summarised
            row["reported"] = False
            problems["absent shard"].append(f"shard {i}: {exc}")
            continue
        here: dict[tuple[str, str], list[str]] = {}
        for key, outcome, seconds in cases:
            here.setdefault(key, []).append(outcome)
            nodeid = by_key.get(key)
            if nodeid is not None:
                durations[nodeid] = durations.get(nodeid, 0.0) + seconds
        for key, found in here.items():
            label = by_key.get(key) or f"{key[0]}::{key[1]}"
            # pytest writes a second testcase element for a failed call (including a strict xpass)
            # followed by a teardown error; other repeats mean a test ran twice.
            double_report = "errors" in found and ("failed" in found or "strict xpass" in found)
            if len(found) > 1 and not double_report:
                problems["duplicated"].append(f"{label} ({len(found)} times in shard {i})")
            outcome = max(found, key=_RANK.__getitem__)
            nodeid = by_key.get(key)
            if nodeid is None:
                problems["extra"].append(f"{label} (shard {i})")
                continue
            if nodeid in seen_in:
                problems["duplicated"].append(f"{nodeid} (shards {seen_in[nodeid]} and {i})")
                continue
            seen_in[nodeid] = i
            if "strict xpass" in found:
                problems["strict xpass"].append(nodeid)
            if outcome == "strict xpass":
                outcome = "xpassed"
            outcomes[nodeid] = outcome
            row["tests"] += 1
            row[outcome] += 1
            if planned and planned.get(nodeid) != i:
                problems["misplaced"].append(f"{nodeid} (planned for shard {planned.get(nodeid)}, ran in {i})")
            if outcome in ("failed", "errors"):
                problems[outcome].append(nodeid)

    problems["missing"] = [n for n in ids if n not in seen_in]
    for kind in problems:
        problems[kind] = sorted(problems[kind])
    totals = {k: sum(1 for o in outcomes.values() if o == k) for k in OUTCOMES}
    return {"shards": shards, "collected": len(ids), "reported": len(outcomes), "totals": totals,
            "per_shard": per_shard, "problems": {k: v for k, v in problems.items() if v},
            "outcomes": dict(sorted(outcomes.items())), "durations": dict(sorted(durations.items()))}


def render_text(result: dict) -> str:
    t = result["totals"]
    lines = [f"collected {result['collected']}, reported {result['reported']} across {result['shards']} shards: "
             + ", ".join(f"{t[k]} {k}" for k in OUTCOMES)]
    for row in result["per_shard"]:
        state = f"{row['tests']} tests, {row['seconds']:.1f}s" if row["reported"] else "DID NOT REPORT"
        lines.append(f"  shard {row['index']}: {state}")
    for kind, items in result["problems"].items():
        lines.append(f"{kind}: {len(items)}")
        lines += [f"  {item}" for item in items[:LIST_LIMIT]]
        if len(items) > LIST_LIMIT:
            lines.append(f"  ... and {len(items) - LIST_LIMIT} more")
    lines.append("UNION OK" if not result["problems"] else "UNION FAILED")
    return "\n".join(lines)


def render_markdown(result: dict, sha: str | None) -> str:
    t = result["totals"]
    ok = not result["problems"]
    out = [f"## Sharded suite: {'union OK' if ok else 'union FAILED'}", ""]
    if sha:
        out.append(f"Candidate `{sha}`")
        out.append("")
    columns = " | ".join(OUTCOMES)
    out += [f"| collected | reported | {columns} |", "|" + "---|" * (2 + len(OUTCOMES)),
            f"| {result['collected']} | {result['reported']} | " + " | ".join(str(t[k]) for k in OUTCOMES) + " |", "",
            f"| shard | tests | {columns} | pytest seconds |", "|" + "---|" * (3 + len(OUTCOMES))]
    for r in result["per_shard"]:
        if r["reported"]:
            out.append(f"| {r['index']} | {r['tests']} | " + " | ".join(str(r[k]) for k in OUTCOMES)
                       + f" | {r['seconds']:.1f} |")
        else:
            out.append(f"| {r['index']} | did not report |" + " |" * (1 + len(OUTCOMES)))
    for kind, items in result["problems"].items():
        out += ["", f"**{kind}: {len(items)}**", ""]
        out += [f"- `{item}`" for item in items[:LIST_LIMIT]]
        if len(items) > LIST_LIMIT:
            out.append(f"- ... and {len(items) - LIST_LIMIT} more")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- command line

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def cmd_collect(args: argparse.Namespace) -> int:
    if args.from_output:
        text = Path(args.from_output).read_text(encoding="utf-8")
    else:
        extra = [a for a in args.pytest_args if a != "--"]
        # the environment's pytest script, as ci.yml's `uv run pytest` runs it (`python -m` would also put the
        # working directory on sys.path)
        script = Path(sys.executable).with_name("pytest")
        launcher = [str(script)] if script.is_file() else [sys.executable, "-m", "pytest"]
        proc = subprocess.run([*launcher, "--collect-only", "-q", *extra], capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout + proc.stderr)
            print(f"pytest --collect-only exited {proc.returncode}", file=sys.stderr)
            return 2
        text = proc.stdout
    ids = parse_collect_output(text)
    _write(Path(args.out), "".join(f"{n}\n" for n in ids))
    print(f"collected {len(ids)} tests in {len(group_by_file(ids))} files, sha256 {nodeids_digest(ids)}")
    return 0


def cmd_split(args: argparse.Namespace) -> int:
    if args.plan:
        plan = load_plan(Path(args.plan))
    else:
        if not args.collected or args.shards is None:
            raise InputError("split needs --collected and --shards, or --plan")
        durations = load_durations(Path(args.durations)) if args.durations else None
        plan = make_plan(read_nodeids(Path(args.collected)), args.shards, durations)
    text = json.dumps(plan, indent=1, sort_keys=True) + "\n"
    if args.plan_out:
        _write(Path(args.plan_out), text)
    if args.index is None:
        if not args.plan_out:
            sys.stdout.write(text)
        return 0
    if not 0 <= args.index < plan["shards"]:
        raise InputError(f"--index {args.index} is outside 0..{plan['shards'] - 1}")
    shard = plan["plan"][args.index]
    lines = [*shard["files"], *shard.get("split", [])] if args.files else shard["nodeids"]
    for line in lines:
        print(line)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    ids = read_nodeids(Path(args.collected))
    plan = load_plan(Path(args.plan)) if args.plan else None
    result = check(ids, Path(args.junit_dir), args.shards, plan)
    print(render_text(result))
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(render_markdown(result, args.sha))
    if args.report:
        _write(Path(args.report), json.dumps(
            {"sha": args.sha, "collected": result["collected"], "totals": result["totals"],
             "outcomes": result["outcomes"]}, indent=1, sort_keys=True) + "\n")
    if args.write_durations:
        _write(Path(args.write_durations),
               json.dumps({k: round(v, 3) for k, v in result["durations"].items()}, indent=1, sort_keys=True) + "\n")
    return 1 if result["problems"] else 0


def cmd_durations(args: argparse.Namespace) -> int:
    candidates = junit_index(read_nodeids(Path(args.collected)))
    seconds: dict[str, list[float]] = {}
    total = ambiguous = unmatched = 0
    for path in sorted(Path(args.junit_dir).iterdir()):
        if not _JUNIT_FILE.match(path.name):
            continue
        cases, _ = read_junit(path)
        for key, _, duration in cases:
            total += 1
            matches = candidates.get(key, [])
            if not matches:
                unmatched += 1
            elif len(matches) > 1:
                ambiguous += 1
            else:
                seconds.setdefault(function_key(matches[0]), []).append(duration)
    durations = {key: round(sum(values) / len(values), 3) for key, values in seconds.items()}
    _write(Path(args.out), json.dumps(durations, indent=2, sort_keys=True) + "\n")
    print(f"durations: {len(durations)} keys from {total} testcases "
          f"({ambiguous + unmatched} skipped: ambiguous {ambiguous}, unmatched {unmatched})", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shard_suite.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="collect the suite's node ids")
    c.add_argument("--out", required=True, help="where to write the node ids, one per line")
    c.add_argument("--from-output", help="parse this saved `pytest --collect-only -q` output instead of running it")
    c.add_argument("pytest_args", nargs=argparse.REMAINDER, help="after --: more pytest options")
    c.set_defaults(func=cmd_collect)

    s = sub.add_parser("split", help="print shard arguments or the whole plan; split heavy independent files",
                       description="Balance whole files, splitting allow-listed independent files by node id "
                                   "when their weight exceeds half the mean shard load.")
    s.add_argument("--collected", help="the node-id list `collect` wrote")
    s.add_argument("--shards", type=int)
    s.add_argument("--index", type=int, help="the shard to print (0-based); omit for the whole plan as JSON")
    s.add_argument("--durations", help="JSON {node id: seconds} to balance by; else balance by test count")
    s.add_argument("--plan", help="read this plan instead of making one")
    s.add_argument("--plan-out", help="also write the whole plan as JSON here")
    s.add_argument("--files", action="store_true",
                   help="print whole-file paths then split test node ids, one per line, as pytest arguments")
    s.set_defaults(func=cmd_split)

    k = sub.add_parser("check", help="check the shards' JUnit XML against the collected set")
    k.add_argument("--collected", required=True)
    k.add_argument("--shards", type=int, required=True, help="how many reports to expect")
    k.add_argument("--junit-dir", required=True, help=f"holds {JUNIT_NAME.format(index='<i>')} for every shard")
    k.add_argument("--plan", help="also check each test ran in the shard this plan gave it")
    k.add_argument("--sha", help="the candidate commit, for the summary")
    k.add_argument("--summary", help="append a Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    k.add_argument("--report", help="write {node id: outcome} and the totals as JSON here (for comparing runs)")
    k.add_argument("--write-durations", help="write {node id: seconds} here, a durations file for `split`")
    k.set_defaults(func=cmd_check)

    d = sub.add_parser("durations", help="refresh function durations from the collected tests' JUnit reports")
    d.add_argument("--collected", required=True, help="the node-id list `collect` wrote")
    d.add_argument("--junit-dir", required=True, help="holds shard-<i>.xml reports")
    d.add_argument("--out", default=".test_durations", help="output JSON (default: .test_durations)")
    d.set_defaults(func=cmd_durations)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (InputError, OSError, json.JSONDecodeError) as exc:
        print(f"shard_suite: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
