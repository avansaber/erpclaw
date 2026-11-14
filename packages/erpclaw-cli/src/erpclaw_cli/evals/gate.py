"""Public five-case qualification gate and deterministic capture through the same trial path.

Qualification is deliberately separate from capture: capture never constructs a hosted client.
Timeout and connection failures share model_unavailable/statuses [504]; they cannot be distinguished.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import re
import subprocess
import time
import traceback
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import run as runner
from .gate_oracle import (Matcher, Mismatch, column_rules, compare_capture, placeholder_fixture,
                          premature, take_snapshot, validate_fixture)
from .gate_public import CASES, CONVENTIONS, PUBLIC_FILES, card_requirements, check_public, check_turns, public_revision
from .gate_seed import seed_gate_books

TODAY = "2026-10-26"
PROFILE = "qwen3_5_9b_openrouter_primeintellect"
SCOPE = ("create_invoice", "create_sales_order", "create_credit_note", "record_payment", "question")
PINNED = {"profile": PROFILE, "provider": "openrouter", "model": "qwen/qwen3.5-9b",
          "base_url": "https://openrouter.ai/api/v1", "hosted": True, "reasoning": {"effort": "low"},
          "max_tokens": 4096, "temperature": 0, "max_parallel": 8,
          "pins": {"order": ["primeintellect"], "allow_fallbacks": False, "data_collection": "deny", "zdr": True,
                   "quantizations": ["bf16"], "require_parameters": True}}
MODEL_FAILURES = {"model_unavailable", "model_refused", "model_rate_limited", "credential_refused",
                  "hosted_books_refused", "model_input_invalid", "answer_interrupted", "busy", "turn_lapsed",
                  "reconciliation_incomplete"}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def enforce_clock():
    os.environ["ERPCLAW_TODAY"] = TODAY


def candidate_sha(root):
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def load_fixture(folder, case):
    if case not in CASES:
        raise ValueError("unknown public case")
    path = folder / f"{case.id}.json"
    if not path.is_file():
        return None
    fixture = json.loads(path.read_text())
    validate_fixture(fixture)
    return fixture


def public_keys(root):
    """Keep exact review inputs without embedding revision digests in fixtures."""
    return {name: (root / "tests/expected_books" / name).read_text() for name in PUBLIC_FILES}


def planned(case):
    from erpclaw_core.plugins import REGISTRY, load_plugins

    load_plugins()
    contract = REGISTRY.contracts.get(case.operation)
    return contract.planned if contract else None


@contextmanager
def capture_operations():
    """Exercise implemented operations only in scripted capture; qualification keeps release flags."""
    from erpclaw_core.plugins import REGISTRY, load_plugins

    load_plugins()
    contracts = [REGISTRY.contracts[c.operation] for c in CASES if c.operation]
    previous = [c.planned for c in contracts]
    try:
        for contract in contracts:
            contract.planned = None
        yield
    finally:
        for contract, value in zip(contracts, previous):
            contract.planned = value


def scripted_model(case):
    from erpclaw_assistant.model import Reply, ScriptedModel, call

    if case.operation == "create_invoice":
        reply = Reply("", [call("create_invoice", customer="Acme", items=[{"item": "Copper Wire", "quantity": 5}],
                                submit=True)])
    elif not case.operation:
        reply = Reply("", [call("create_invoice", customer="ABC", items=[])])
    elif case.operation == "create_sales_order":
        reply = Reply("", [call("create_sales_order", customer="Wayne",
                                items=[{"item": "Circuit Board", "quantity": 500}], delivery_date="2026-10-27")])
    else:
        class AnswerScript(ScriptedModel):
            def complete(self, messages, tools):
                owner = [m.get("content") for m in messages if m["role"] == "user"]
                if case.operation == "create_credit_note":
                    args = {"everything": True}
                    if "INV-2026-00005" in owner:
                        args["invoice"] = "INV-2026-00005"
                else:
                    args = {"customer": "Pied Piper Inc" if "Pied Piper Inc" in owner else "Pied Piper",
                            "method": "bank transfer"}
                    if "2235" in owner:
                        args["amount"] = "2235"
                    if "Operating Checking Account" in owner:
                        args["account"] = "Operating Checking Account"
                self.replies = [Reply("", [call(case.operation, **args)])]
                return super().complete(messages, tools)
        return AnswerScript([])
    return ScriptedModel([reply] * (len(case.answers) + 1))


@dataclass
class Trial:
    verdict: str = "PASS"
    error: str | None = None
    trace: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    turns: int = 0
    seconds: float = 0
    capture: dict | None = field(default=None, repr=False)

    def fail(self, error):
        if self.verdict != "FAIL":
            self.verdict, self.error = "FAIL", str(error)

    def inconclusive(self, error):
        if self.verdict != "FAIL":
            self.verdict, self.error = "INCONCLUSIVE", str(error)


def inspect_events(result, events):
    if claim := runner.false_claim(events):
        result.fail(f"false write claim: {claim}")
    visible = "\n".join(e.get("text", "") + (json.dumps(e["card"]) if "card" in e else "") for e in events)
    if runner.UUID.search(visible):
        result.fail("reply shows an internal id")
    for event in events:
        code = str(event.get("code", "")).lower()
        if code in MODEL_FAILURES:
            result.inconclusive(f"{code}: {event}")


def compare_step(fixture, step, index, matcher):
    if index >= len(fixture["steps"]):
        raise Mismatch("unexpected extra owner turn/card")
    expected = fixture["steps"][index]
    matcher.event(expected["owner"], step["owner"], "owner")
    matcher.event(expected["events"], step["events"])
    matcher.books(expected["books"], step["books"])


def run_trial(case, books, model, fixture=None, *, seed_started=None, conventions=CONVENTIONS):
    """Single request, engine questions only, one approval, no retries and no chosen trial."""
    from fastapi.testclient import TestClient
    from erpclaw_api.app import create_app

    enforce_clock()
    result = Trial()
    engine = client = None
    start = time.monotonic()
    usage_start = len(getattr(model, "usage", []))
    cap = {"case": case.id, "window": [seed_started or utcnow(), utcnow()], "steps": []}
    matcher = None
    try:
        engine = books.trial()
        cap["rules"], cap["before"] = column_rules(engine), take_snapshot(engine)
        if fixture:
            # The upper bound is refreshed at every comparison, never taken from the fixture.
            matcher = Matcher(fixture["rules"], [cap["window"][0], utcnow()])
            if cap["rules"] != fixture["rules"]:
                result.fail("schema/column rules differ")
            else:
                try:
                    matcher.books(fixture["before"], cap["before"])
                except Mismatch as exc:
                    result.fail(f"seed mismatch: {exc}")
        if result.verdict == "FAIL":
            return result
        trial_started = utcnow()
        cap["trial_started"] = trial_started
        if matcher:
            matcher.window = [trial_started, utcnow()]
        client = TestClient(create_app(engine, model, embedded_owner=True))
        cid = client.post("/v1/conversations").json()["id"]
        answers = fixture["answers"] if fixture else list(case.answers)
        owner = case.says
        approved = False
        while True:
            result.turns += 1
            response = client.post(f"/v1/conversations/{cid}/messages", json={"text": owner})
            response.raise_for_status()
            events = response.json()["events"]
            result.trace.append({"owner": owner, "events": events})
            inspect_events(result, events)
            mid = take_snapshot(engine)
            step = {"owner": owner, "events": events, "books": mid}
            cap["steps"].append(step)
            cap["window"][1] = utcnow()
            turn_findings = check_turns(cap, conventions, conversation_id=cid)
            if turn_findings:
                result.fail(f"S1 FINDING: {json.dumps(turn_findings)}")
            if error := premature(cap["before"], mid, fence_checked=not turn_findings):
                result.fail(error)
            if matcher and result.verdict != "INCONCLUSIVE":
                matcher.window[1] = utcnow()
                try:
                    compare_step(fixture, step, len(cap["steps"]) - 1, matcher)
                except Mismatch as exc:
                    result.fail(exc)
            cards = [e for e in events if e["type"] == "card"]
            asks = [e for e in events if e["type"] == "ask"]
            if result.verdict != "PASS":
                for card in cards:
                    client.post(f"/v1/cards/{card['approval_id']}/decline")
                break
            if asks:
                if len(asks) != 1 or cards:
                    result.fail("question/card sequence differs")
                    break
                if not case.operation:
                    break
                if result.turns > len(answers):
                    result.fail("unexpected question; no scripted answer")
                    break
                owner = answers[result.turns - 1]
                continue
            if len(cards) != 1 or not case.operation:
                result.fail("expected exactly one approval card")
                break
            card = cards[0]
            # Capture also reads the card against the public request before approving.
            if not fixture:
                text = runner.card_text(card["card"])
                missing = [s for s in card_requirements(case) if s.lower() not in text.lower()]
                if missing:
                    result.fail(f"public card mismatch: {missing}")
                    client.post(f"/v1/cards/{card['approval_id']}/decline")
                    break
            response = client.post(f"/v1/cards/{card['approval_id']}/approve", params={"conversation_id": cid},
                                   json={"digest": card["digest"]})
            response.raise_for_status()
            events = response.json()["events"]
            result.trace.append({"approval": True, "events": events})
            cap["steps"].append({"owner": None, "events": events, "books": take_snapshot(engine)})
            if not any(e.get("status") == "committed" for e in events):
                result.fail(f"approval not committed: {events}")
            approved = True
            break
        cap["after"], cap["window"][1] = take_snapshot(engine), utcnow()
        cap["answers"] = answers
        if fixture and result.verdict == "PASS":
            if error := compare_capture(fixture, cap):
                result.fail(error)
        if result.verdict == "PASS":
            findings = check_public(case, cap, conventions, conversation_id=cid)
            if findings:
                result.fail(f"S1 FINDING: {json.dumps(findings)}")
        if result.verdict == "PASS" and case.operation and not approved:
            result.fail("write trial ended without approval")
    except Exception as exc:
        result.trace.append({"exception": traceback.format_exc()})
        result.inconclusive(f"{type(exc).__name__}: {exc}")
    finally:
        for usage in getattr(model, "usage", [])[usage_start:]:
            result.trace.append({"model_usage": {k: str(v) if k == "cost" and v is not None else v
                                                for k, v in usage.items()}})
        for label, close in (("client", client.close if client else None),
                             ("trial books", (lambda: books.finish(engine)) if engine is not None else None)):
            if close:
                try:
                    close()
                except Exception as exc:
                    result.warnings.append(f"cleanup {label}: {type(exc).__name__}: {exc}")
        result.seconds = round(time.monotonic() - start, 3)
        result.capture = cap
    return result


def aggregate(results):
    trials = [r for rows in results.values() for r in rows]
    # S3 applies to the whole run, even when a different trial failed.
    if any(r.verdict == "INCONCLUSIVE" for r in trials):
        return "INCONCLUSIVE"
    if set(results) != {c.id for c in CASES} or any(len(rs) != 1 for rs in results.values()):
        return "FAIL"
    return "PASS" if all(r.verdict == "PASS" for r in trials) else "FAIL"


@dataclass(frozen=True)
class Config:
    k: int = 1
    today: str = TODAY
    scope: tuple = SCOPE
    profile: str = PROFILE


def config_problem(config, model):
    if config != Config():
        return "qualification configuration deviates from the frozen profile/k/scope/date"
    if model.describe() != PINNED or getattr(getattr(model, "profile", None), "timeout_s", None) != 120:
        return "model route/profile pins differ from the frozen configuration"
    return None


RELEASE_SURFACES = (
    "plugins/erpclaw-sales/src/erpclaw_sales/__init__.py",
    "plugins/erpclaw-sales/src/erpclaw_sales/service.py",
    "plugins/erpclaw-sales/src/erpclaw_sales/stock_sales.py",
    "plugins/erpclaw-sales/src/erpclaw_sales/tables.py",
)


def release_problem(root, release):
    """Require released candidates AND unchanged operation sources; planned flags alone are insufficient."""
    for case in CASES:
        if reason := planned(case):
            return f"{case.operation} is planned ({reason}); released operations required"
    if not release or set(release) != {"orders_credits", "payments"}:
        return "released candidate records for orders/credits and payments are required"
    for label, record in release.items():
        if set(record) != {"candidate", "dispatcher_link"} or not record["dispatcher_link"]:
            return f"{label}: release evidence missing"
        sha = record["candidate"]
        if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{40}", sha):
            return f"{label}: invalid released candidate SHA"
        for path in RELEASE_SURFACES:
            try:
                released = subprocess.check_output(["git", "show", f"{sha}:{path}"], cwd=root, stderr=subprocess.DEVNULL)
            except subprocess.CalledProcessError:
                return f"{label}: released candidate not available"
            if released != (root / path).read_bytes():
                return f"{label}: released operation source modified: {path}"
    return None


def write_report(folder, report):
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{secrets.token_hex(16)}"
        path = folder / f"gate-{run_id}.json"
        try:
            with path.open("x") as stream:
                json.dump({"run_id": run_id, **report}, stream, indent=2)
            return path
        except FileExistsError:
            continue


def qualify(root, fixtures, reports, model, release, dispatcher_link, *, config=Config()):
    """No model creation, readiness request or trial before all release/config/oracle checks pass."""
    enforce_clock()
    identity = {"candidate_sha": candidate_sha(root), "oracle_revision": public_revision(root / "tests/expected_books"),
                "spec_revision": "public-five-case-gate-v1", "dispatcher_link": dispatcher_link,
                "release": release, "configuration": asdict(config)}
    report = {**identity, "model_line": runner.model_line(model), "trials": {}}
    print(report["model_line"], flush=True)
    problem = config_problem(config, model) or release_problem(root, release)
    frozen = {c.id: load_fixture(fixtures, c) for c in CASES}
    problem = problem or next((f"reviewed fixture absent: {c.id}" for c in CASES if not frozen[c.id]), None)
    problem = problem or ("dispatcher link is required" if not dispatcher_link else None)
    if not problem:
        for fixture in frozen.values():
            if fixture["public_keys"] != public_keys(root):
                problem = "public keys changed since fixture review"
                break
    results, books = {}, None
    started = utcnow()
    try:
        if problem:
            report.update(verdict="REFUSED", refusal=problem)
        else:
            for case in CASES:
                books = runner.EvalBooks(runner.server_url())
                books.seed(lambda engine: seed_gate_books(engine, case))
                trial = run_trial(case, books, model, frozen[case.id], seed_started=started,
                                  conventions=root / "tests/expected_books/CONVENTIONS.md")
                results[case.id] = [trial]
                print(f"{case.id} trial 1: {trial.verdict} ({trial.seconds}s, {trial.turns} owner messages)", flush=True)
                try:
                    books.close()
                except Exception as exc:
                    trial.warnings.append(f"cleanup seed: {type(exc).__name__}: {exc}")
                books = None
            report.update(verdict=aggregate(results), trials={key: [
                {**{k: v for k, v in asdict(r).items() if k != "capture"},
                 "failure_line": f"Failure: {r.error}" if r.error else None}
                for r in rs] for key, rs in results.items()})
    except Exception as exc:
        report.update(verdict="INCONCLUSIVE", failure=f"{type(exc).__name__}: {exc}", exception=traceback.format_exc())
    finally:
        if books:
            try:
                books.close()
            except Exception as exc:
                report.setdefault("warnings", []).append(f"cleanup seed: {exc}")
    passed = sum(len(rs) == 1 and rs[0].verdict == "PASS" for rs in results.values())
    report["pass_table"] = {c.id: {"passed": sum(r.verdict == "PASS" for r in results.get(c.id, [])),
                                   "trials": len(results.get(c.id, [])),
                                   "pass^1": len(results.get(c.id, [])) == 1 and all(
                                       r.verdict == "PASS" for r in results[c.id])} for c in CASES}
    report["summary"] = f"pass^1: {passed} of 5 cases"
    report["tokens_line"] = runner.tokens_line(getattr(model, "usage", []))
    report["served_line"] = runner.served_line(model)
    report["transport_limit"] = "Timeout and connection errors both arrive as model_unavailable/statuses [504]."
    path = write_report(reports, report)
    print(f"{report['summary']}. {report['verdict']}. Report: {path}", flush=True)
    if report["served_line"]:
        print(report["served_line"], flush=True)
    print(report["tokens_line"], flush=True)
    return (0 if report["verdict"] == "PASS" else 2 if report["verdict"] in {"REFUSED", "INCONCLUSIVE"} else 1), path


def capture_fixtures(root, folder, reports, *, cases=CASES):
    """One deterministic trial per selected public case. No overwrite or fixture on a finding."""
    enforce_clock()
    if not cases or any(case not in CASES for case in cases) or len(set(cases)) != len(cases):
        raise ValueError("capture requires distinct public cases")
    revisions = public_revision(root / "tests/expected_books")
    if any((folder / f"{case.id}.json").exists() for case in cases):
        raise FileExistsError("Capture is immutable: use a fresh output directory, never overwrite a reviewed fixture")
    report = {"mode": "scripted capture", "candidate_sha": candidate_sha(root), "public_revision": revisions,
              "cases": {}}
    books = None
    started = utcnow()
    try:
        with capture_operations():
            for case in cases:
                if reason := planned(case):
                    report["cases"][case.id] = {"status": "UNAVAILABLE", "reason": f"{case.operation}: planned ({reason})"}
                    continue
                books = runner.EvalBooks(runner.server_url())
                books.seed(lambda engine: seed_gate_books(engine, case))
                result = run_trial(case, books, scripted_model(case), seed_started=started,
                                   conventions=root / "tests/expected_books/CONVENTIONS.md")
                try:
                    books.close()
                except Exception as exc:
                    result.warnings.append(f"cleanup seed: {type(exc).__name__}: {exc}")
                books = None
                record = {k: v for k, v in asdict(result).items() if k != "capture"}
                report["cases"][case.id] = record
                if result.verdict != "PASS":
                    continue
                fixture = placeholder_fixture({**result.capture, "public_keys": public_keys(root)})
                validate_fixture(fixture)
                folder.mkdir(parents=True, exist_ok=True)
                with (folder / f"{case.id}.json").open("x") as stream:
                    json.dump(fixture, stream, indent=2)
    finally:
        if books:
            books.close()
    return write_report(reports, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true", required=True,
                        help="scripted capture only; qualification uses qualify() with explicit release evidence")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--case", choices=[c.id for c in CASES], action="append",
                        help="public cases to capture; qualification always requires all five")
    parser.add_argument("--fixtures", type=Path, help="fresh destination for the selected fixtures")
    parser.add_argument("--reports", type=Path, help="external directory for capture reports")
    args = parser.parse_args()
    cases = tuple(c for c in CASES if not args.case or c.id in args.case)
    with tempfile.TemporaryDirectory(prefix="gate-reports-") as reports:
        path = capture_fixtures(args.root, args.fixtures or args.root / "tests/gate_m4",
                                args.reports or Path(reports), cases=cases)
        rows = json.loads(path.read_text())["cases"].values()
        print(json.dumps({c: {k: r[k] for k in ("verdict", "status", "error", "turns", "warnings", "reason") if k in r}
                          for c, r in json.loads(path.read_text())["cases"].items()}))
        rows = list(rows)
        return 2 if any(r.get("verdict") == "INCONCLUSIVE" or r.get("status") == "UNAVAILABLE" for r in rows) else (
            1 if any(r.get("verdict") == "FAIL" for r in rows) else 0)


if __name__ == "__main__":
    raise SystemExit(main())
