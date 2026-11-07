"""Row 0 of token admission: the pin manifest and the reference fixture are well formed, the counted rendering is the
frozen one, the measured bound covers every model-received count, and (when the pinned files are on disk) every
fixture count is reproduced exactly."""
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import verify_tokenizer_pin as v  # noqa: E402

MANIFEST = ROOT / "packages/erpclaw-assistant/src/erpclaw_assistant/tokenizer_pins.toml"
FIXTURE = ROOT / "tests/fixtures/tokenizer_reference.json"
PROFILES = ROOT / "packages/erpclaw-assistant/src/erpclaw_assistant/profiles.toml"


def manifest():
    return tomllib.loads(MANIFEST.read_text())


def fixture():
    return json.loads(FIXTURE.read_text())


def test_every_profile_has_a_pin_table_and_muse_is_a_conservative_bound():
    pins, profiles = manifest(), tomllib.loads(PROFILES.read_text())
    for name in profiles:
        pin = v.profile_pin(pins, name)
        assert len(pin["vocab_revision"]) == 40
        assert "tokenizer.json" in pin["files"]
        for f, h in pin["files"].items():
            assert len(h) == 64 and h == h.lower(), f
    assert pins["muse"] == {"admission": "conservative_bound"}


def test_a_profile_without_a_measured_margin_gets_no_bound():
    pins = manifest()
    assert v.route_margin(pins, v.profile_pin(pins, "qwen3_5_9b_lmstudio")) is None
    assert v.route_margin(pins, v.profile_pin(pins, "qwen3_5_9b_openrouter_primeintellect"))["factor"] > 1


def test_fixture_has_the_twelve_inputs_and_the_count_boundary_is_exact():
    d = fixture()
    ids = [c["id"] for c in d["cases"]]
    assert len(ids) >= 12 and ids == sorted(ids)
    by = {c["id"]: c for c in d["cases"]}
    b = d["reference_budget"]
    assert (by["10_budget_minus_1"]["tokens"], by["11_budget_exact"]["tokens"], by["12_budget_plus_1"]["tokens"]) == (b - 1, b, b + 1)
    assert d["format"] == "ERPCLAW-COUNT-v1" and d["add_special_tokens"] is False


def test_the_stored_rendering_is_the_frozen_rendering():
    for c in fixture()["cases"]:
        assert c["rendered"] == v.render_count_input(c["messages"], c["tools"]), c["id"]
        assert c["rendered"].startswith("ERPCLAW-COUNT-v1\n") and c["rendered"].endswith("\n")


def test_rendering_rules():
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"b":1,"a":2}'}}
    msgs = [{"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "c1", "content": "R"}]
    out = v.render_count_input(msgs, [{"type": "function", "function": {"name": "f"}}])
    assert out == ('ERPCLAW-COUNT-v1\nmsg=0\nrole=assistant\ncontent=\ncall=c1\nname=f\narguments={"b":1,"a":2}\n'
                   'msg=1\nrole=tool\ntool_call_id=c1\ncontent=R\ntools=1\ntool={"function":{"name":"f"},"type":"function"}\n')
    other = [dict(msgs[0], tool_calls=[dict(call, function={"name": "f", "arguments": '{"a":2,"b":1}'})]), msgs[1]]
    assert v.render_count_input(other, []) != v.render_count_input(msgs, [])  # the exact wire string, never re-serialised


def test_the_bound_covers_every_received_count_of_every_measured_route():
    pins, d = manifest(), fixture()
    assert set(d["received"]) >= {"openrouter_primeintellect", "openrouter_deepinfra", "ollama_gateway_q8_0"}
    for route, margin in pins["route"].items():
        assert v.check_margin(d, route, margin) == [], route
        assert sum(1 for x in d["received"][route].values() if x is not None) == margin["fixture_cases"]


def test_an_underestimating_margin_fails():  # the mutation: a factor of 1.0 and no constant must be caught
    d = fixture()
    assert v.check_margin(d, "openrouter_primeintellect", {"factor": 1.0, "constant": 0}) != []
    assert v.check_margin(d, "openrouter_primeintellect", {"factor": 1.2, "constant": 100}) != []


def test_the_manifest_margin_is_the_envelope_derived_from_the_received_counts():
    pins, d = manifest(), fixture()
    for route, margin in pins["route"].items():
        got = v.derive_margin(d, route)
        assert (margin["factor"], margin["constant"]) == (got["factor"], got["constant"]), route
        assert margin["method"] == "envelope-v1"


def test_the_one_factor_method_is_not_required_and_would_be_far_looser():
    # the old method: 1.1 x the largest ratio over ALL cases (set by the one-tool request) -> about 2.5; the envelope
    # takes the slope from the cases with at least 10 tools and carries the small request in the constant
    pins, d = manifest(), fixture()
    cnt = {c["id"]: c["tokens"] for c in d["cases"]}
    for route, margin in pins["route"].items():
        old = max(x / cnt[k] for k, x in d["received"][route].items() if x is not None) * 1.1
        assert margin["factor"] < old - 0.8, route
        assert margin["factor"] < 1.6 and margin["constant"] < 200, route


def test_raising_the_largest_case_by_one_percent_is_still_covered():
    # a 1 percent larger request that the model receives 1 percent more tokens for stays inside the bound
    pins, d = manifest(), fixture()
    big = max(d["cases"], key=lambda c: c["tokens"])
    for route, margin in pins["route"].items():
        got = d["received"][route][big["id"]]
        assert got is not None
        assert v.bounded_estimate(big["tokens"] * 1.01, margin) >= got * 1.01, route
    # and the mutation: a margin without its constant, or with the factor of a one-tool-sized ratio removed, is caught
    assert v.check_margin(d, "openrouter_primeintellect", {"factor": 1.47, "constant": 0}) != []
    assert v.check_margin(d, "openrouter_primeintellect", {"factor": 1.3, "constant": 155}) != []


def test_a_measured_route_without_its_received_counts_is_refused():
    pins, d = manifest(), fixture()
    route = "openrouter_primeintellect"
    margin = pins["route"][route]
    assert v.missing_measurements(d, route, margin) is None
    no_received = {k: val for k, val in d.items() if k != "received"}
    assert "no received counts" in v.missing_measurements(no_received, route, margin)
    empty = dict(d, received={route: {}})
    assert "no received counts" in v.missing_measurements(empty, route, margin)
    short = json.loads(json.dumps(d))
    short["received"][route]["06_tools_30"] = None
    assert "received counts, the manifest states" in v.missing_measurements(short, route, margin)
    bad = json.loads(json.dumps(d))
    bad["received"][route]["06_tools_30"] = -5
    assert "not a whole number" in v.missing_measurements(bad, route, margin)
    assert "states no fixture_cases" in v.missing_measurements(d, route, {"factor": 1.5, "constant": 1})


def test_the_manifest_pins_the_counting_package_and_a_hash_per_platform():
    tool = manifest()["tool"]
    assert tool["package"] == "tokenizers" and tool["version"] == "0.22.1"
    plats = {w["platform"]: w for w in tool["wheel"]}
    assert set(plats) == {"linux_x86_64", "win_amd64", "macos_arm64"}
    for w in plats.values():
        assert len(w["sha256"]) == 64 and w["name"].startswith("tokenizers-0.22.1-")


def test_the_install_line_is_hash_enforced_and_names_the_platforms_hash():
    for plat in ("linux_x86_64", "win_amd64", "macos_arm64"):
        line = v.install_lines(manifest(), plat)
        w = v.wheel_for(manifest(), plat)
        assert "--require-hashes" in line and "--no-deps" in line and f"--hash=sha256:{w['sha256']}" in line, plat
        assert "tokenizers==0.22.1" in line


def test_a_platform_without_a_pinned_hash_gets_no_install_line():
    with pytest.raises(SystemExit) as e:
        v.install_lines(manifest(), "linux_arm64")
    assert "pins no tokenizers wheel for this platform" in str(e.value)


def test_a_wheel_with_the_wrong_hash_is_rejected_before_install(tmp_path):
    wrong = tmp_path / "tokenizers-0.22.1-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
    wrong.write_bytes(b"not the pinned wheel")
    with pytest.raises(SystemExit) as e:
        v.verify_wheel(wrong, manifest())
    assert "not a pinned tokenizers wheel" in str(e.value)
    with pytest.raises(SystemExit):
        v.main(["wheel", "--file", str(wrong)])


@pytest.mark.skipif("ERPCLAW_TOKENIZER_WHEEL" not in os.environ, reason="the pinned wheel is not on disk")
def test_the_real_pinned_wheel_is_accepted():
    assert v.verify_wheel(Path(os.environ["ERPCLAW_TOKENIZER_WHEEL"]), manifest()) in ("linux_x86_64", "win_amd64", "macos_arm64")


def test_a_missing_counting_package_is_a_one_line_refusal_with_the_hash_enforced_install_line(monkeypatch):
    monkeypatch.setitem(sys.modules, "tokenizers", None)  # makes `import tokenizers` raise ImportError
    monkeypatch.setattr(v, "this_platform", lambda: "linux_x86_64")
    with pytest.raises(SystemExit) as e:
        v.require_tokenizers(manifest())
    msg = str(e.value)
    assert msg.startswith("refused: the tokenizers package is not installed") and "\n" not in msg
    assert "--require-hashes" in msg and "--hash=sha256:" in msg


def test_a_wrong_version_of_the_counting_package_is_refused_with_the_install_line(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(__version__="0.21.0"))
    monkeypatch.setattr(v, "this_platform", lambda: "linux_x86_64")
    with pytest.raises(SystemExit) as e:
        v.require_tokenizers(manifest())
    msg = str(e.value)
    assert "0.21.0 is installed" in msg and "pins 0.22.1" in msg and "\n" not in msg
    assert "--require-hashes" in msg


def test_a_manifest_without_the_package_pin_is_refused():
    with pytest.raises(SystemExit) as e:
        v.require_tokenizers({"tool": {"package": "tokenizers"}})
    assert "does not pin the tokenizers package" in str(e.value)


@pytest.mark.skipif("ERPCLAW_TOKENIZER_FILES" not in os.environ, reason="the pinned vocabulary files are not on disk")
def test_check_refuses_a_fixture_with_no_measurements(tmp_path):
    pytest.importorskip("tokenizers")
    d = fixture()
    d.pop("received")
    bare = tmp_path / "fixture.json"
    bare.write_text(json.dumps(d))
    out = v.main(["check", "--profile", "qwen3_5_9b_openrouter_primeintellect",
                  "--files", os.environ["ERPCLAW_TOKENIZER_FILES"], "--fixture", str(bare)])
    assert out == 1


@pytest.mark.skipif("ERPCLAW_TOKENIZER_FILES" not in os.environ, reason="the pinned vocabulary files are not on disk")
def test_the_pinned_files_reproduce_every_count_and_the_bound():
    pytest.importorskip("tokenizers")
    folder = Path(os.environ["ERPCLAW_TOKENIZER_FILES"])
    for profile in ("qwen3_5_9b_lmstudio", "qwen3_5_9b_openrouter_primeintellect", "qwen3_5_9b_openrouter_deepinfra"):
        assert v.main(["check", "--profile", profile, "--files", str(folder), "--fixture", str(FIXTURE)]) == 0
