"""Offline loader checks; no database or tokenizer package is needed.

max_input_tokens (proposed) and context_window (proposed) are profile settings.
The vocabulary pins and measured margins remain manifest data.
"""
import copy
import json
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from erpclaw_assistant import profiles
from erpclaw_assistant.profiles import ProfileError
from test_tokenizer_structural import measured_v2, strict_normalize, v

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(profiles.__file__).with_name("tokenizer_pins.toml")
REFERENCE = ROOT / "tests/fixtures/tokenizer_reference.json"
PRIME = "qwen3_5_9b_openrouter_primeintellect"
LOCAL = "qwen3_5_9b_lmstudio"


@pytest.fixture
def pins(tmp_path, monkeypatch, measured_v2):
    data = copy.deepcopy(measured_v2.manifest)
    # Synthetic data stays in pytest's scratch directory, never in the package.
    v.emit_nfd_table(v.derive_nfd_table(strict_normalize), tmp_path / "nfd_bytes.json")
    monkeypatch.setattr(profiles, "resources", SimpleNamespace(files=lambda _: tmp_path))
    install_manifest(data, tmp_path, monkeypatch)
    return data


def toml_value(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {toml_value(val)}" for key, val in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(val) for val in value) + "]"
    return json.dumps(value)


def profile_text(name=PRIME, **changes):
    values = {"backend": "openrouter", "model": "qwen/qwen3.5-9b",
              "base_url": "https://models.test/v1", "max_input_tokens": 8000,
              "context_window": 12000}  # synthetic window only
    values.update(changes)
    return f"[{name}]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in values.items() if v is not None)


def test_manifest_pins_reach_loader(pins):
    p = profiles.load(text=profile_text())[PRIME]
    assert p.vocab_repo == pins[PRIME]["vocab_repo"]
    assert p.vocab_revision == pins[PRIME]["vocab_revision"]
    assert re.fullmatch("[0-9a-f]{64}", p.files["tokenizer.json"])
    assert p.files == {f["name"]: f["sha256"] for f in pins[PRIME]["file"]}
    assert p.margin_route == pins[PRIME]["margin_route"]
    assert p.margin == pins["route"][p.margin_route]
    assert p.admission == "bounded"
    assert p.max_tokens == 2048  # reply cap keeps its independent default


@pytest.mark.parametrize("bad, says", [
    ("missing_revision", "vocab_revision.*required"),
    ("unknown_revision", "vocab_revision.*match"),
    ("unknown_repo", "vocab_repo.*match"),
    ("flipped_sha", "sha256.*tokenizer.json.*match"),
    ("zero_budget", "max_input_tokens.*positive"),
    ("negative_budget", "max_input_tokens.*positive"),
])
def test_bad_pin_refused_at_load(pins, bad, says):
    values = copy.deepcopy(pins[PRIME])
    if bad == "missing_revision":
        values.pop("vocab_revision")
    elif bad == "unknown_revision":
        values["vocab_revision"] = "0" * 40
    elif bad == "unknown_repo":
        values["vocab_repo"] = "unknown/vocabulary"
    elif bad == "flipped_sha":
        file = next(f for f in values["file"] if f["name"] == "tokenizer.json")
        file["sha256"] = ("0" if file["sha256"][0] != "0" else "1") + file["sha256"][1:]
    else:
        values["max_input_tokens"] = 0 if bad == "zero_budget" else -1
    with pytest.raises(ProfileError, match=says):
        profiles.load(text=profile_text(**values))


def test_unmeasured_and_muse_use_conservative_bound(pins):
    text = profile_text(LOCAL, backend="lmstudio") + profile_text("muse")
    loaded = profiles.load(text=text)
    local, muse = loaded[LOCAL], loaded["muse"]
    assert local.margin_route == "unmeasured"
    assert local.margin is None
    assert local.admission == "conservative_bound"
    assert muse.admission == pins["muse"]["admission"] == "conservative_bound"
    assert muse.vocab_repo is None and muse.vocab_revision is None and muse.files == {}
    assert muse.margin_route is None and muse.margin is None


def test_unset_local_window_stays_unset_and_disallows_bounded_admission(pins):
    local = profiles.load(text=profile_text(LOCAL, backend="lmstudio", context_window=None))[LOCAL]
    assert local.context_window is None
    assert local.margin_route == "unmeasured"
    assert local.margin is None
    assert local.admission == "conservative_bound"
    assert profiles.load()[LOCAL].context_window is None


def test_unset_window_uses_conservative_bound_even_with_a_measured_margin(pins):
    p = profiles.load(text=profile_text(context_window=None))[PRIME]
    assert p.context_window is None
    assert p.admission == "conservative_bound"


def test_unknown_margin_route_refused(pins):
    values = {**pins[PRIME], "margin_route": "no-such-route"}
    with pytest.raises(ProfileError, match="margin_route.*no-such-route"):
        profiles.load(text=profile_text(**values))


def test_row_zero_gate_no_invented_counts(pins, monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "PROFILE_PINS_FILE", tmp_path / "missing.toml", raising=False)
    with pytest.raises(ProfileError, match="manifest.*missing.toml"):
        profiles.load(text=profile_text())


@pytest.mark.parametrize("setting, value", [
    (None, None),
    ("max_input_tokens", 0), ("max_input_tokens", -1), ("max_input_tokens", None),
    ("context_window", 0), ("context_window", -1), ("context_window", None),
    ("max_input_tokens", True), ("context_window", 1.5),
])
def test_budget_and_window_positive(pins, setting, value):
    if setting is None:
        p = profiles.load(text=profile_text())[PRIME]
        assert (p.max_input_tokens, p.context_window) == (8000, 12000)
    elif setting == "context_window" and value is None:
        p = profiles.load(text=profile_text(context_window=None))[PRIME]
        assert p.context_window is None
        assert p.admission == "conservative_bound"
    else:
        with pytest.raises(ProfileError, match=f"{setting}.*positive"):
            profiles.load(text=profile_text(**{setting: value}))


def install_manifest(data, tmp_path, monkeypatch):
    path = tmp_path / "pins.toml"
    path.write_text("".join(f"{key} = {toml_value(value)}\n" for key, value in data.items()))
    monkeypatch.setattr(profiles, "PROFILE_PINS_FILE", path)


def test_structural_manifest_resolved_without_inventing_fixed_counts(pins):
    p = profiles.load(text=profile_text())[PRIME]
    assert p.structural_method == "structural-v1"
    assert p.structural_constant == pins["structural"]["constant"] == 0
    # the loader resolves exactly the manifest's measured values and invents none
    assert p.fixed_counts == pins["structural"]["fixed_counts"]
    assert p.fixed_segments == tuple(pins[PRIME]["fixed_segments"])


@pytest.mark.parametrize("patch", [
    None, {}, {"method": "structural-v1"},
    {"method": "other", "constant": 0},
    {"method": "structural-v1", "constant": True},
    {"method": "structural-v1", "constant": 1},
    {"method": "structural-v1", "constant": -1},
    {"method": "structural-v1", "constant": "0"},
])
def test_missing_or_ill_formed_structural_constant_refused(pins, tmp_path, monkeypatch, patch):
    if patch is None:
        pins.pop("structural", None)
    else:
        pins["structural"] = patch
    install_manifest(pins, tmp_path, monkeypatch)
    with pytest.raises(ProfileError, match="structural"):
        profiles.load(text=profile_text())


@pytest.mark.parametrize("counts, status", [({}, "unmeasured"), ({"rules": -1}, "measured"),
                                           ({"rules": True}, "measured"), ({"rules": 10}, "unmeasured")])
def test_required_fixed_count_must_be_measured(pins, tmp_path, monkeypatch, counts, status):
    pins[PRIME]["fixed_segments"] = ["rules"]
    pins["structural"].update(fixed_counts=counts, fixed_counts_status=status)
    install_manifest(pins, tmp_path, monkeypatch)
    with pytest.raises(ProfileError, match="fixed"):
        profiles.load(text=profile_text())


def test_measured_fixed_count_resolved(pins, tmp_path, monkeypatch):
    pins[PRIME]["fixed_segments"] = ["rules"]
    pins["structural"].update(fixed_counts={"rules": 12}, fixed_counts_status="measured")
    install_manifest(pins, tmp_path, monkeypatch)
    p = profiles.load(text=profile_text())[PRIME]
    assert p.fixed_counts == {"rules": 12}
    assert p.fixed_segments == ("rules",)


def test_loader_reads_v2_delimiters_exactly(pins):
    p = profiles.load(text=profile_text())[PRIME]
    assert p.renderer == "ERPCLAW-COUNT-v2"
    assert p.segment_open == pins["structural"]["segment_open"]
    assert p.segment_close == pins["structural"]["segment_close"]


@pytest.mark.parametrize("key, value", [("segment_open", "unmeasured"), ("segment_close", "unmeasured"),
                                       ("segment_open", ""), ("segment_close", None),
                                       ("segment_open", 1)])
def test_loader_refuses_v2_placeholder_delimiters(pins, tmp_path, monkeypatch, key, value):
    if value is None:
        pins["structural"].pop(key)
    else:
        pins["structural"][key] = value
    install_manifest(pins, tmp_path, monkeypatch)
    with pytest.raises(ProfileError, match="delimiter.*unmeasured"):
        profiles.load(text=profile_text())


def test_shipped_profile_follows_current_measurement_state(monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_PINS_FILE", MANIFEST)
    data = tomllib.loads(MANIFEST.read_text())
    reference = json.loads(REFERENCE.read_text())
    assert reference["vocab_revision"] == data[PRIME]["vocab_revision"]
    proof = data["structural"]
    if any(proof.get(key) in (None, "", "unmeasured") for key in ("segment_open", "segment_close")):
        with pytest.raises(ProfileError, match="delimiter.*unmeasured"):
            profiles.load(text=profile_text())
    else:
        if not MANIFEST.with_name("nfd_bytes.json").is_file():
            pytest.skip("the pinned NFD table file is written by the dispatcher's structural run")
        p = profiles.load(text=profile_text())[PRIME]
        assert p.renderer == reference["format"] == proof["renderer"]
        assert p.segment_open == proof["segment_open"]
        assert p.segment_close == proof["segment_close"]
        assert p.fixed_counts == reference["structural"]["fixed_counts"] == proof["fixed_counts"]
        assert p.fixed_segments == tuple(data[PRIME]["fixed_segments"])


def test_loader_still_reads_v1_without_guessing_delimiters(pins, tmp_path, monkeypatch):
    pins["structural"]["renderer"] = "ERPCLAW-COUNT-v1"
    pins["structural"].pop("segment_open")
    pins["structural"].pop("segment_close")
    install_manifest(pins, tmp_path, monkeypatch)
    p = profiles.load(text=profile_text())[PRIME]
    assert p.renderer == "ERPCLAW-COUNT-v1"
    assert p.segment_open is None and p.segment_close is None
