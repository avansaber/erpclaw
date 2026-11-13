"""Offline package-data and availability checks with a deliberately tiny normalizer."""
import copy
import hashlib
import json
import logging
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from erpclaw_assistant import profiles
from erpclaw_core.common import Refusal
from test_profile_admission_pins import MANIFEST, PRIME, install_manifest, profile_text
from test_tokenizer_structural import ByteTokenizer, descriptor, v, v2_settings


def stub_normalize(form, text):
    text.encode("utf-8")
    if form == "NFD":
        return text.replace("é", "e\u0301")
    return text.replace("e\u0301", "é")


@pytest.fixture
def stub(tmp_path, monkeypatch):
    table = v.derive_nfd_table(stub_normalize)
    digest = hashlib.sha256(b"".join(n.to_bytes(4, "big") for n in table)).hexdigest()
    data = tomllib.loads(MANIFEST.read_text())
    data["structural"] = {**v2_settings(), "nfd_table_sha256": digest}
    for pin in data.values():
        if isinstance(pin, dict):
            pin.pop("fixed_segments", None)
    install_manifest(data, tmp_path, monkeypatch)
    calls = []

    def files(package):
        calls.append(package)
        return tmp_path

    monkeypatch.setattr(profiles, "resources", SimpleNamespace(files=files))
    path = tmp_path / "nfd_bytes.json"
    v.emit_nfd_table(table, path)
    return SimpleNamespace(table=table, digest=digest, path=path, manifest=data, calls=calls)


def assert_unavailable(profile, monkeypatch):
    from erpclaw_assistant import model
    from erpclaw_assistant.graph import MODEL_REFUSALS

    assert profile.admission == "unavailable"
    assert profile.nfd_table is None
    with pytest.raises(Refusal) as exc:
        profile.nfd_bytes("owner")
    assert exc.value.code == "model_unavailable"
    assert exc.value.code in MODEL_REFUSALS
    assert str(exc.value) == "The hosted model is unavailable right now. Nothing was saved."

    def forbidden(**_):
        pytest.fail("an unavailable profile must not construct a client or request credentials")

    monkeypatch.setattr(model, "OpenAICompatModel", forbidden)
    monkeypatch.setenv("ERPCLAW_MODEL_PROFILE", PRIME)
    wrapped = profiles.from_env()  # startup succeeds without any model installation
    assert wrapped.describe()["profile"] == PRIME
    assert wrapped.check_ready() == str(exc.value)
    for _ in range(2):
        with pytest.raises(Refusal) as refused:
            wrapped.complete([], [])
        assert refused.value.code == exc.value.code
        assert str(refused.value) == str(exc.value)


def test_stub_emit_load_expand_and_hash_round_trip(stub):
    assert json.loads(stub.path.read_text()) == [[233, 3]]
    p = profiles.load(text=profile_text())[PRIME]
    assert stub.calls == ["erpclaw_assistant"]
    assert p.admission == "bounded"
    assert tuple(p.nfd_table) == tuple(stub.table)
    assert hashlib.sha256(b"".join(n.to_bytes(4, "big") for n in p.nfd_table)).hexdigest() == stub.digest
    assert [p.nfd_table[cp] for cp in (0, 0x7F, 0x80, 0x7FF, 0x800, 0xD7FF,
                                     0xD800, 0xDFFF, 0xE000, 0xFFFF, 0x10000, 0x10FFFF)] == [
                                         1, 1, 2, 2, 3, 3, 0, 0, 3, 3, 4, 4]
    assert p.nfd_bytes("Aé😀") == v.nfd_sum("Aé😀", stub.table) == 8
    for text in ("\ud800", "x\udfff"):
        with pytest.raises(UnicodeEncodeError):
            p.nfd_bytes(text)


def test_tampered_entry_fails_closed_and_logs_identities_once(stub, monkeypatch, caplog):
    stub.path.write_text("[[233,4]]")
    changed = copy.copy(stub.table)
    changed[233] = 4
    digest = hashlib.sha256(b"".join(n.to_bytes(4, "big") for n in changed)).hexdigest()
    with caplog.at_level(logging.WARNING, logger=profiles.__name__):
        p = profiles.load(text=profile_text())[PRIME]
        profiles.load(text=profile_text())
        assert_unavailable(p, monkeypatch)
    entries = [r.getMessage() for r in caplog.records if r.name == profiles.__name__]
    assert len(entries) == 1
    assert stub.digest in entries[0] and digest in entries[0]
    assert "[[233" not in entries[0]


def test_missing_file_fails_closed(stub, monkeypatch, caplog):
    stub.path.unlink()
    p = profiles.load(text=profile_text())[PRIME]
    assert_unavailable(p, monkeypatch)
    assert any(stub.digest in r.getMessage() and "computed=unavailable" in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("content", [
    '[[233,3],[234,"0x04"]]', '[[233,3],["0xea",4]]', '[[233,3],[234,4.0]]',
    '[[233,3],[234,true]]', '[[233,3],[234,null]]', '[[233,3,"extra"]]',
    '[[233,3],[233,4]]', '[[234,4],[233,3]]', '[[55296,4]]', '[[-1,4]]',
    '[[1114112,4]]', '[[234,0]]', '[[234,-1]]', '[[234,4294967296]]',
    '[[234,2]]', '{}', '{"hash":"ignored"}', '[', 'null',
])
def test_malformed_sparse_data_is_refused(stub, monkeypatch, content):
    stub.path.write_text(content)
    assert_unavailable(profiles.load(text=profile_text())[PRIME], monkeypatch)


@pytest.mark.parametrize("expected", [None, "unmeasured", "bad", 42, True])
def test_missing_or_malformed_manifest_hash_fails_closed(stub, monkeypatch, tmp_path, expected):
    stub.manifest["structural"]["nfd_table_sha256"] = expected
    if expected is None:
        stub.manifest["structural"].pop("nfd_table_sha256")
    install_manifest(stub.manifest, tmp_path, monkeypatch)
    assert_unavailable(profiles.load(text=profile_text())[PRIME], monkeypatch)


def test_v1_needs_no_nfd_resource(stub, monkeypatch, tmp_path):
    stub.path.unlink()
    stub.manifest["structural"]["renderer"] = v.RENDERER
    install_manifest(stub.manifest, tmp_path, monkeypatch)
    assert profiles.load(text=profile_text())[PRIME].admission == "bounded"
    assert stub.calls == []


def test_profile_wrapper_refuses_before_inner_completion(stub, monkeypatch):
    stub.path.unlink()
    p = profiles.load(text=profile_text())[PRIME]

    def forbidden(*_):
        pytest.fail("unavailable profile reached the inner model")

    wrapped = profiles.ProfileModel(SimpleNamespace(profile=p, complete=forbidden))
    with pytest.raises(Refusal) as exc:
        wrapped.complete([], [])
    assert exc.value.code == "model_unavailable"


@pytest.mark.parametrize("explicit_path", [True, False])
def test_structural_dispatch_emits_the_same_proved_table(tmp_path, monkeypatch, capsys, explicit_path):
    data, settings = descriptor(), v2_settings()
    raw = json.dumps(data).encode()
    (tmp_path / "tokenizer.json").write_bytes(raw)
    manifest = {"synthetic": {"admission": "bounded", "vocab_repo": "synthetic/data",
                             "vocab_revision": "a" * 40, "file": [
                                 {"name": "tokenizer.json", "sha256": hashlib.sha256(raw).hexdigest()}]},
                "structural": settings}
    monkeypatch.setattr(v, "load_manifest", lambda _: manifest)
    monkeypatch.setattr(v, "pinned_normalize", lambda mf: stub_normalize)
    cases = [{"id": str(i), "messages": [{"role": "system", "content": "rules"},
                                           {"role": "user", "content": "é"}], "tools": []}
             for i in range(12)]
    monkeypatch.setattr(v, "fixture_inputs", lambda *_: cases)
    output = tmp_path / "nfd_bytes.json"
    monkeypatch.setattr(v, "DEFAULT_NFD_TABLE", output)
    args = ["structural", "--profile", "synthetic", "--files", str(tmp_path), "--emit-nfd-table"]
    if explicit_path:
        args.append(str(output))
    tok = ByteTokenizer(t["content"] for t in data["added_tokens"])
    assert v.main(args, tokenizer=tok) == 0
    report = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text()) == [[233, 3]]
    assert report["structural"]["nfd_table_sha256"] == hashlib.sha256(
        b"".join(n.to_bytes(4, "big") for n in v.derive_nfd_table(stub_normalize))).hexdigest()
    # A failed structural proof must never replace package data.
    before = output.read_bytes()
    monkeypatch.setattr(v, "prove_compositions", lambda *_: (_ for _ in ()).throw(v.StructuralError("bad proof")))
    with pytest.raises(v.StructuralError, match="bad proof"):
        v.main(args, tokenizer=tok)
    assert output.read_bytes() == before
    (tmp_path / "tokenizer.json").write_text("{}")
    with pytest.raises(SystemExit, match="hashes"):
        v.main(args, tokenizer=tok)
    assert output.read_bytes() == before


def test_shipped_nfd_resource_matches_manifest():
    path = Path(profiles.__file__).with_name("nfd_bytes.json")
    if not path.is_file():
        pytest.skip("the pinned NFD table file is written by the dispatcher's structural run")
    proof = tomllib.loads(MANIFEST.read_text())["structural"]
    if proof["renderer"] != v.RENDERER_V2:
        pytest.skip("the shipped profile uses the historical renderer")
    loaded = profiles.load()
    assert all(p.admission != "unavailable" and p.nfd_table is not None for p in loaded.values())
