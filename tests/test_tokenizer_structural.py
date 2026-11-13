"""Offline structural proofs; descriptors and tokenizer doubles never load a vocabulary."""
import ast
import copy
import hashlib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import verify_tokenizer_pin as v


def strict_normalize(form, text):
    """Model the pinned binding's requirement that input strings be valid UTF-8."""
    text.encode("utf-8", errors="strict")
    return unicodedata.normalize(form, text)


BYTE_SYMBOLS = (
    '\u0100\u0101\u0102\u0103\u0104\u0105\u0106\u0107\u0108\u0109\u010a\u010b\u010c\u010d\u010e\u010f\u0110\u0111\u0112\u0113\u0114\u0115\u0116\u0117\u0118\u0119\u011a\u011b\u011c\u011d\u011e\u011f'
    '\u0120!"#$%&\'()*+,-./0123456789:;<=>?'
    '@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_'
    '`abcdefghijklmnopqrstuvwxyz{|}~\u0121'
    '\u0122\u0123\u0124\u0125\u0126\u0127\u0128\u0129\u012a\u012b\u012c\u012d\u012e\u012f\u0130\u0131\u0132\u0133\u0134\u0135\u0136\u0137\u0138\u0139\u013a\u013b\u013c\u013d\u013e\u013f\u0140\u0141'
    '\u0142\xa1\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xab\xac\u0143\xae\xaf\xb0\xb1\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xbb\xbc\xbd\xbe\xbf'
    '\xc0\xc1\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xcb\xcc\xcd\xce\xcf\xd0\xd1\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xdb\xdc\xdd\xde\xdf'
    '\xe0\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xeb\xec\xed\xee\xef\xf0\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xfb\xfc\xfd\xfe\xff'
)


def descriptor():
    # The measured shape, represented as data; synthetic token strings and ids.
    return {
        "model": {"type": "BPE", "byte_fallback": False, "unk_token": None,
                  "fuse_unk": False, "ignore_merges": False,
                  "vocab": {c: i for i, c in enumerate(BYTE_SYMBOLS)}, "merges": []},
        "normalizer": {"type": "NFC"},
        "pre_tokenizer": {"type": "Sequence", "pretokenizers": [
            {"type": "Split", "pattern": {"Regex": ".+"}, "behavior": "Isolated", "invert": False},
            {"type": "ByteLevel", "add_prefix_space": False, "trim_offsets": False, "use_regex": False}]},
        "post_processor": {"type": "ByteLevel", "add_prefix_space": True,
                           "trim_offsets": True, "use_regex": True},
        "decoder": {"type": "ByteLevel", "add_prefix_space": True,
                    "trim_offsets": True, "use_regex": True},
        "added_tokens": [{"id": 256 + i, "content": f"<special_{i}>", "special": i < 14,
                          "normalized": False, "single_word": False, "lstrip": False, "rstrip": False}
                         for i in range(26)],
        "padding": None, "truncation": None,
    }


def test_measured_shape_passes_with_complete_bytes_and_no_fallback():
    d = descriptor()
    assert v.byte_alphabet() == tuple(BYTE_SYMBOLS)
    assert len(d["added_tokens"]) == 26
    assert sum(t["special"] for t in d["added_tokens"]) == 14
    assert v.prove_descriptor(d, unicodedata.normalize) == 0


@pytest.mark.parametrize("section, patch", [
    ("model", {"type": "Unigram"}),
    ("model", {"vocab": {"a": 0}}),
    ("model", {"byte_fallback": True}),
    ("model", {"unk_token": "?"}),
    ("model", {"fuse_unk": True}),
    ("model", {"ignore_merges": True}),
    ("model", {"continuing_subword_prefix": "prefix"}),
    ("model", {"end_of_word_suffix": "suffix"}),
    ("model", {"merges": [["a", "b", "extra"]]}),
    ("normalizer", {}),
    ("normalizer", {"type": "Sequence", "normalizers": []}),
    ("normalizer", {"type": "Sequence", "normalizers": [{"type": "NFC"}, {"type": "NFC"}]}),
    ("normalizer", {"type": "NFD"}),
    ("normalizer", {"type": "NFC", "variant": "other"}),
    ("pre_tokenizer", {"type": "ByteLevel", "add_prefix_space": False}),
    ("post_processor", {"type": "TemplateProcessing", "single": [{"SpecialToken": {"id": "prefix"}}]}),
    ("post_processor", {"type": "TemplateProcessing", "single": [{"Sequence": {}}, {"SpecialToken": {"id": "suffix"}}]}),
    ("decoder", {"type": "Sequence", "decoders": []}),
    ("padding", {"strategy": {"Fixed": 1}, "pad_id": 0}),
])
def test_structural_proof_rejects(section, patch):
    d = descriptor()
    d[section] = {**d[section], **patch} if section == "model" else patch
    with pytest.raises(v.StructuralError):
        v.prove_descriptor(d, unicodedata.normalize)


@pytest.mark.parametrize("index, patch", [
    (0, {"behavior": "Removed"}), (0, {"invert": True}),
    (0, {"pattern": {"String": "x"}}),
    (1, {"add_prefix_space": True}), (1, {"trim_offsets": True}), (1, {"use_regex": True}),
])
def test_pre_tokenizer_parameters(index, patch):
    d = descriptor()
    d["pre_tokenizer"]["pretokenizers"][index].update(patch)
    with pytest.raises(v.StructuralError):
        v.prove_descriptor(d, unicodedata.normalize)


def test_extra_inserting_step_rejected():
    d = descriptor()
    d["pre_tokenizer"]["pretokenizers"].append({"type": "Metaspace", "prepend_scheme": "always"})
    with pytest.raises(v.StructuralError):
        v.prove_descriptor(d, unicodedata.normalize)


@pytest.mark.parametrize("patch", [
    {"content": ""}, {"id": [1, 2]}, {"content": "x", "emits": 2},
    {"normalized": "false"}, {"content": "vanishes", "normalized": True},
])
def test_added_tokens_effective_matching(patch):
    d = descriptor()
    d["added_tokens"][0].update(patch)
    def normalize(form, text):
        return "" if text == "vanishes" else unicodedata.normalize(form, text)
    with pytest.raises(v.StructuralError):
        v.prove_descriptor(d, normalize)


@pytest.fixture(scope="module")
def table():
    start = time.monotonic()
    result = v.derive_nfd_table(unicodedata.normalize)
    assert time.monotonic() - start < 60
    return result


def test_nfd_table_pinned_version(table):
    assert len(table) == 0x110000
    start = time.monotonic()
    v.check_nfd_table(table, unicodedata.normalize)
    assert time.monotonic() - start < 60
    wrong = list(table)
    wrong[0x10FFFF] += 1
    with pytest.raises(v.StructuralError, match="10FFFF"):
        v.check_nfd_table(wrong, unicodedata.normalize)
    with pytest.raises(v.StructuralError):
        v.check_nfd_table(table[:-1], unicodedata.normalize)


def test_nfd_table_derivation_with_strict_normalizer():
    result = v.derive_nfd_table(strict_normalize)
    assert len(result) == 0x110000
    assert all(value == 0 for value in result[0xD800:0xE000])
    for cp in (0, 0xD7FF, 0xE000, 0x10FFFF):
        assert result[cp] == len(strict_normalize("NFD", chr(cp)).encode("utf-8"))


def test_nfd_table_check_with_strict_normalizer(table):
    v.check_nfd_table(table, strict_normalize)
    wrong = list(table)
    wrong[0xD800] = 1
    with pytest.raises(v.StructuralError, match="D800"):
        v.check_nfd_table(wrong, strict_normalize)


@pytest.mark.parametrize("text", ["\ud800", "before\udfffafter"])
def test_byte_measurement_refuses_surrogates(text):
    with pytest.raises(UnicodeEncodeError):
        v._utf8_length(text)
    # No table lookup or bound can count text that cannot be rendered as UTF-8.
    with pytest.raises(UnicodeEncodeError):
        v.nfd_sum(text, None)
    with pytest.raises(UnicodeEncodeError):
        v.structural_bound(12, text, None, 0)


@pytest.mark.parametrize("text", ["\u00e9\u0323", "\u01d5\u0323", "\u0344", "\u0958"])
def test_named_nfd_bounds(table, text):
    assert len(unicodedata.normalize("NFC", text).encode()) <= v.nfd_sum(text, table)


@pytest.mark.parametrize("text, shortfall", [("\u00e9\u0323", 1), ("\u01d5\u0323", 3)])
def test_per_cp_max_bound_rejected(table, text, shortfall):
    withdrawn = sum(max(len(c.encode()), len(unicodedata.normalize("NFC", c).encode())) for c in text)
    actual = len(unicodedata.normalize("NFC", text).encode())
    assert actual - withdrawn == shortfall
    assert v.nfd_sum(text, table) >= actual


def test_composition_never_lengthens():
    start = time.monotonic()
    pairs = v.prove_compositions(unicodedata.normalize)
    assert time.monotonic() - start < 60
    assert ("e", "\u0301", "\u00e9") in pairs
    assert ("\u1100", "\u1161", "\uac00") in pairs
    assert len(pairs) == 12113
    for text in ("\u00e9\u0323", "\u01d5\u0323"):
        assert len(unicodedata.normalize("NFC", text).encode()) <= len(unicodedata.normalize("NFD", text).encode())
    def lengthening(form, text):
        if form == "NFC" and text == "e\u0301":
            return "\U00010000"
        return unicodedata.normalize(form, text)
    with pytest.raises(v.StructuralError):
        v.prove_compositions(lengthening)


def test_compositions_with_strict_normalizer():
    pairs = v.prove_compositions(strict_normalize)
    assert ("e", "\u0301", "\u00e9") in pairs
    assert ("\u1100", "\u1161", "\uac00") in pairs
    assert len(pairs) == 12113


def test_original_byte_units(table):
    text = v.MULTIBYTE
    escaped = "".join(v.byte_alphabet()[b] for b in text.encode())
    assert len(escaped.encode()) != len(text.encode())
    assert len(text.encode()) == 89  # variable text, excluding framing
    assert v.nfd_sum(text, table) == len(unicodedata.normalize("NFD", text).encode())
    assert v.nfd_sum(text, table) != len(escaped.encode())


class ByteTokenizer:
    """One id per original byte, except non-overlapping added-token matches."""
    def __init__(self, tokens=()):
        self.tokens = sorted(tokens, key=len, reverse=True)

    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is False
        n, i = 0, 0
        while i < len(text):
            match = next((t for t in self.tokens if text.startswith(t, i)), None)
            if match:
                n += 1
                i += len(match)
            else:
                n += len(text[i].encode())
                i += 1
        return SimpleNamespace(ids=[0] * n)


def test_special_delimiting_proved():
    added = [{"content": "<s>", "special": True, "normalized": False,
              "single_word": False, "lstrip": False, "rstrip": False, "id": 1}]
    tok = ByteTokenizer(["<s>"])
    parts = [("fixed", "rules<s>"), ("variable", "owner")]
    assert v.delimited_parts(parts, added) == parts
    assert v.count_tokens(tok, "rules<s>owner") == sum(v.count_tokens(tok, s) for _, s in parts)
    # An apparent delimiter inside a longer non-special match is ineffective.
    shadowed = added + [dict(added[0], content="rules<s>", special=False, id=2)]
    assert v.delimited_parts([("fixed", "rules"), ("variable", "<s>owner")], shadowed) == [("variable", "rules<s>owner")]
    assert v.delimited_parts([("fixed", "rules"), ("variable", "owner")], added) == [("variable", "rulesowner")]
    conditional = [dict(added[0], single_word=True)]
    assert v.delimited_parts(parts, conditional) == [("variable", "rules<s>owner")]


def test_structural_constant_zero_proved(table):
    d = descriptor()
    tok = ByteTokenizer()
    assert v.prove_descriptor(d, unicodedata.normalize) == 0
    assert v.count_tokens(tok, "") == 0
    assert v.structural_bound(12, "\u00e9\u0323", table, 0) == 12 + v.nfd_sum("\u00e9\u0323", table)
    with pytest.raises(v.StructuralError):
        v.structural_bound(12, "", table, 1)
    d["post_processor"] = {"type": "TemplateProcessing", "single": [{"SpecialToken": {"id": "one"}}]}
    with pytest.raises(v.StructuralError):
        v.prove_descriptor(d, unicodedata.normalize)


@pytest.mark.parametrize("normalize", [unicodedata.normalize, strict_normalize], ids=["unicodedata", "strict"])
def test_structural_command_hashes_first_and_uses_injected_tokenizer(tmp_path, monkeypatch, capsys, normalize):
    folder = tmp_path / "files"
    folder.mkdir()
    raw = json.dumps(descriptor()).encode()
    (folder / "tokenizer.json").write_bytes(raw)
    manifest = {"synthetic": {"admission": "bounded", "vocab_repo": "synthetic/data", "vocab_revision": "a" * 40,
                             "file": [{"name": "tokenizer.json", "sha256": hashlib.sha256(raw).hexdigest()}]},
                "structural": {"method": "structural-v1", "constant": 0, "renderer": "ERPCLAW-COUNT-v1",
                               "fixed_counts": {}, "fixed_counts_status": "unmeasured"}}
    monkeypatch.setattr(v, "load_manifest", lambda _: manifest)
    cases = json.loads((ROOT / "tests/fixtures/tokenizer_reference.json").read_text())["cases"]
    monkeypatch.setattr(v, "fixture_inputs", lambda *_: cases)
    assert v.main(["structural", "--profile", "synthetic", "--files", str(folder)],
                  tokenizer=ByteTokenizer(), normalize=normalize) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["structural"]["constant"] == 0
    assert result["structural"]["fixed_counts_status"] == "measured"
    assert len(result["cases"]) == 12
    assert all(c["additive"] for c in result["cases"])
    assert all(c["tokens"] <= c["bound"] for c in result["cases"])
    assert (result["cases"][8]["tokens"], result["cases"][8]["bound"]) == (139, 143)
    assert not result["cases"][5]["admits"]  # undelimited schemas use the whole-span byte bound
    (folder / "tokenizer.json").write_text("{}")
    with pytest.raises(SystemExit, match="hashes"):
        v.main(["structural", "--profile", "synthetic", "--files", str(folder)],
               tokenizer=ByteTokenizer(), normalize=normalize)


def test_receipts_bound_and_triggered():
    identity = {"pins": {"file": [{"name": "synthetic.json", "sha256": "hash"}]},
                "renderer": "render-v1", "proof": "proof-v1",
                "model": "synthetic", "server": "synthetic server", "provider": "synthetic provider"}
    request = {"messages": [{"role": "user", "content": "one"}], "tools": []}
    receipt = v.make_receipt(request, identity, 1, 4)
    assert v.check_receipt(receipt, request, identity, 1, 5) is None
    for key in identity:
        assert v.check_receipt(receipt, request, {**identity, key: "changed"}, 1, 5)
    assert v.check_receipt(receipt, request, identity, 2, 5)
    assert v.check_receipt(receipt, {**request, "messages": []}, identity, 1, 5)
    assert v.check_receipt(receipt, request, identity, 1, 3)
    for key in identity:
        old = {"identity": identity, "received": {"route": {"case": 4}}, "received_note": "old"}
        rebuilt = v.rebuild_receipts({"identity": {**identity, key: "changed"}}, old)
        assert "received" not in rebuilt and "received_note" not in rebuilt
    route = {"server": "synthetic server"}
    case = {"id": "case", **request, "rendered": v.render_count_input_v2(**request, structural=v2_settings()), "bound": 5}
    reference_request = {"case": "case", "format": v.RENDERER_V2, **request, "rendered": case["rendered"]}
    reference_identity = {**identity, "route": "route", "serving_route": route}
    reference = {"identity": identity, "format": v.RENDERER_V2, "cases": [case],
                 "receipts": {"route": {"case": v.make_receipt(reference_request, reference_identity, 1, 4)}}}
    assert v.check_fixture_receipts(reference, {"route": {"route": route}}) is None
    # Actual experiment requests cannot substitute a receipt of the static case.
    assert v.check_receipt(reference["receipts"]["route"]["case"], request, reference_identity, 1, 5)
    reference["cases"][0]["bound"] = 3
    assert "above structural bound" in v.check_fixture_receipts(reference, {"route": {"route": route}})


def test_build_drops_receipts_and_check_requires_fresh_bound_receipts(tmp_path, monkeypatch, capsys):
    pins = {"synthetic": {"admission": "bounded", "vocab_repo": "synthetic/data", "vocab_revision": "a" * 40,
                          "margin_route": "unmeasured", "file": [{"name": "tokenizer.json", "sha256": "a" * 64}]}}
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"received": {"route": {"case": 4}}}))
    built = {"identity": {"model": "changed"}, "cases": []}
    monkeypatch.setattr(v, "load_manifest", lambda _: pins)
    monkeypatch.setattr(v, "verify_files", lambda *_: None)
    monkeypatch.setattr(v, "build_fixture", lambda *_: copy.deepcopy(built))
    args = ["--profile", "synthetic", "--files", str(tmp_path), "--fixture", str(fixture)]
    assert v.main(["build", *args]) == 0
    assert "received" not in json.loads(fixture.read_text())
    assert v.main(["check", *args]) == 1
    assert "receipt" in capsys.readouterr().out.lower()


def runtime_violations(source):
    """AST scan follows local assignments used by file-reading calls."""
    tree = ast.parse(source)
    values = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
    def vocabulary(node, seen=frozenset()):
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return node.id in values and node.id not in seen and vocabulary(values[node.id], seen | {node.id})
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return any(x in node.value for x in ("tokenizer.json", "vocab.json", "merges.txt"))
        return any(vocabulary(n, seen) for n in ast.iter_child_nodes(node))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "tokenizers" for a in node.names):
            bad.append(node.lineno)
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "tokenizers":
            bad.append(node.lineno)
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in ("open", "read_text", "read_bytes", "from_file") and vocabulary(node):
                bad.append(node.lineno)
    return bad


def test_no_runtime_tokenizer_in_engine():
    violations = {str(p.relative_to(ROOT)): runtime_violations(p.read_text())
                  for p in (ROOT / "packages").rglob("*.py")}
    assert not {p: lines for p, lines in violations.items() if lines}
    assert runtime_violations("import tokenizers")
    assert runtime_violations("from tokenizers import Tokenizer")
    assert runtime_violations("p = Path('tokenizer.json')\np.read_text()")
    assert runtime_violations("p = Path('vocab.json')\nopen(p)")


def runtime_unicode_violations(source, *, admission=False):
    """Allow character queries and literal compatibility keys outside admission."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [a for a in node.names if a.name.split(".")[0] == "unicodedata"]
            if imports and (admission or any(a.name != "unicodedata" or a.asname for a in imports)):
                bad.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "unicodedata":
                bad.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == "unicodedata" and isinstance(node.ctx, ast.Load):
            attribute = parents.get(node)
            call = parents.get(attribute)
            if (not admission and isinstance(attribute, ast.Attribute)
                    and isinstance(call, ast.Call) and call.func is attribute):
                # Combining classes are queried when stripping accents from search keys.
                if attribute.attr in {"category", "combining"}:
                    continue
                if (attribute.attr == "normalize" and call.args
                        and isinstance(call.args[0], ast.Constant)
                        and call.args[0].value in ("NFKC", "NFKD")):
                    continue
            bad.append(node.lineno)
    return bad


def assert_no_runtime_unicode(sources):
    # Comparison/search helpers may move freely; admission may never use Unicode data.
    admission = "packages/erpclaw-assistant/src/erpclaw_assistant/profiles.py"
    violations = {path: runtime_unicode_violations(source, admission=path == admission)
                  for path, source in sources.items()}
    assert not {path: lines for path, lines in violations.items() if lines}


def test_no_runtime_unicode_normalizer_in_engine():
    sources = {str(p.relative_to(ROOT)): p.read_text() for p in (ROOT / "packages").rglob("*.py")}
    assert_no_runtime_unicode(sources)
    # Mutate a real engine source; the very same repository assertion must fail.
    path = "packages/erpclaw-assistant/src/erpclaw_assistant/profiles.py"
    sources[path] += '\ndef admission_mutant(text):\n    return unicodedata.normalize("NFD", text)\n'
    with pytest.raises(AssertionError, match="profiles.py"):
        assert_no_runtime_unicode(sources)


@pytest.mark.parametrize("path", [
    "packages/erpclaw-assistant/src/erpclaw_assistant/resolve.py",
    "packages/erpclaw-assistant/src/erpclaw_assistant/character_keys.py",
    "packages/erpclaw-core/src/erpclaw_core/character_keys.py",
])
@pytest.mark.parametrize("operation", [
    'unicodedata.normalize("NFD", x)',
    'unicodedata.normalize("NFC", x)',
    'form = "NFKC"\nunicodedata.normalize(form, x)',
    'unicodedata.normalize("NFK" + "C", x)',
    'unicodedata.normalize(*args)',
    'unicodedata.decomposition(c)',
    'normalize = unicodedata.normalize',
    'import unicodedata as ud',
    'from unicodedata import normalize',
    'from unicodedata import normalize as nfd',
], ids=["nfd", "nfc", "variable", "expression", "unpacked", "decomposition",
        "function-alias", "module-alias", "from-import", "aliased-from-import"])
def test_runtime_unicode_rejects_forbidden_operations_in_any_module(path, operation):
    source = "import unicodedata\n" + operation
    with pytest.raises(AssertionError, match=re.escape(path)):
        assert_no_runtime_unicode({path: source})


@pytest.mark.parametrize("package", ["assistant", "core"])
@pytest.mark.parametrize("operation", [
    "",
    'unicodedata.category(c)',
    'unicodedata.normalize("NFKC", x)',
    'unicodedata.normalize("NFKD", x)',
    'unicodedata.combining(c)',
], ids=["import", "category", "nfkc", "nfkd", "combining"])
def test_runtime_unicode_allows_helpers_moved_to_new_modules(package, operation):
    path = f"packages/erpclaw-{package}/src/erpclaw_{package}/character_keys.py"
    assert not (ROOT / path).exists()
    # Repeating an operation is allowed too; helper extraction can duplicate it.
    source = "import unicodedata\n" + "\n".join([operation, operation])
    assert_no_runtime_unicode({path: source})


@pytest.mark.parametrize("operation", [
    'import unicodedata',
    'unicodedata.category(c)',
    'unicodedata.combining(c)',
    'unicodedata.normalize("NFKC", x)',
    'unicodedata.normalize("NFKD", x)',
    'from unicodedata import normalize',
    'data = unicodedata',
], ids=["import", "category", "combining", "nfkc", "nfkd", "from-import", "module-reference"])
def test_runtime_unicode_rejects_every_use_in_admission(operation):
    path = "packages/erpclaw-assistant/src/erpclaw_assistant/profiles.py"
    with pytest.raises(AssertionError, match="profiles.py"):
        assert_no_runtime_unicode({path: operation})


def v2_settings():
    tokens = descriptor()["added_tokens"]
    return {"method": "structural-v1", "constant": 0, "renderer": "ERPCLAW-COUNT-v2",
            "segment_open": tokens[0]["content"], "segment_close": tokens[1]["content"],
            "fixed_counts": {}, "fixed_counts_status": "unmeasured"}


def test_v2_fixed_segments_and_additivity():
    d, settings = descriptor(), v2_settings()
    assert v.prove_v2_delimiters(d, settings) is None
    messages = [{"role": "system", "content": "rules", "tool_calls": [v.CALL]},
                {"role": "user", "content": "owner"},
                {"role": "tool", "content": "result", "tool_call_id": "call"}]
    tools = [v._tool("one", 1), v._tool("two", 1)]
    parts = v.rendering_parts_v2(messages, tools, settings)
    opened, closed = settings["segment_open"], settings["segment_close"]
    fixed = [s for kind, s in parts if kind == "fixed"]
    assert fixed == [opened + "msg=0\nrole=system\ncontent=rules\n" + closed,
                     *(opened + "tool=" + v.J(t) + "\n" + closed for t in tools)]
    variable = "".join(s for kind, s in parts if kind == "variable")
    assert variable.startswith("ERPCLAW-COUNT-v2\n")
    assert "call=" in variable and "tools=2\n" in variable
    assert "role=user\ncontent=owner\n" in variable
    assert "tool_call_id=call\ncontent=result\n" in variable
    rendered = v.render_count_input_v2(messages, tools, settings)
    assert rendered == "".join(s for _, s in parts)
    assert v.proved_parts_v2(messages, tools, settings, d) == parts
    tok = ByteTokenizer(t["content"] for t in d["added_tokens"])
    assert v.count_tokens(tok, rendered) == sum(v.count_tokens(tok, s) for _, s in parts)


@pytest.mark.parametrize("key", ["segment_open", "segment_close"])
@pytest.mark.parametrize("mutation", ["ordinary", "missing", "normalized", "single_word", "lstrip", "rstrip"])
def test_v2_delimiter_mutations_fail_proof_and_use_whole_byte_bound(table, key, mutation):
    d, settings = descriptor(), v2_settings()
    index = 0 if key == "segment_open" else 1
    if mutation == "missing":
        d["added_tokens"].pop(index)
    else:
        d["added_tokens"][index]["special" if mutation == "ordinary" else mutation] = mutation != "ordinary"
    with pytest.raises(v.StructuralError, match="delimiter"):
        v.prove_v2_delimiters(d, settings)
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "owner"}]
    rendered = v.render_count_input_v2(messages, [], settings)
    parts = v.proved_parts_v2(messages, [], settings, d)
    assert parts == [("variable", rendered)]
    assert v.structural_bound(0, rendered, table, 0) == v.nfd_sum(rendered, table)


def test_v2_shadowed_delimiter_uses_whole_byte_bound():
    d, settings = descriptor(), v2_settings()
    d["added_tokens"].append(dict(d["added_tokens"][0], id=400,
                                  content="owner\n" + settings["segment_open"], special=False))
    messages = [{"role": "user", "content": "owner"}, {"role": "system", "content": "rules"}]
    assert v.proved_parts_v2(messages, [], settings, d) == [
        ("variable", v.render_count_input_v2(messages, [], settings))]


def test_v2_structural_command_prints_exact_hash_keyed_segments(tmp_path, monkeypatch, capsys, table):
    d, settings = descriptor(), v2_settings()
    raw = json.dumps(d).encode()
    (tmp_path / "tokenizer.json").write_bytes(raw)
    manifest = {"synthetic": {"admission": "bounded", "vocab_repo": "synthetic/data",
                             "vocab_revision": "a" * 40, "file": [
                                 {"name": "tokenizer.json", "sha256": hashlib.sha256(raw).hexdigest()}]},
                "structural": settings}
    cases = json.loads((ROOT / "tests/fixtures/tokenizer_reference.json").read_text())["cases"]
    monkeypatch.setattr(v, "load_manifest", lambda _: manifest)
    monkeypatch.setattr(v, "fixture_inputs", lambda *_: cases)
    tok = ByteTokenizer(t["content"] for t in d["added_tokens"])
    assert v.main(["structural", "--profile", "synthetic", "--files", str(tmp_path)],
                  tokenizer=tok, normalize=strict_normalize) == 0
    report = json.loads(capsys.readouterr().out)
    assert_list_only_pins(report, {"tokenizer.json"})
    expected = {}
    for case, evidence in zip(cases, report["cases"]):
        parts = v.rendering_parts_v2(case["messages"], case["tools"], settings)
        fixed = [s for kind, s in parts if kind == "fixed"]
        expected.update({hashlib.sha256(s.encode()).hexdigest(): v.count_tokens(tok, s) for s in fixed})
        assert evidence["fixed_exact"] == sum(v.count_tokens(tok, s) for s in fixed)
        assert evidence["variable_nfd"] == v.nfd_sum("".join(s for k, s in parts if k == "variable"), table)
        assert evidence["additive"] and evidence["tokens"] <= evidence["bound"]
    assert report["structural"]["fixed_counts"] == expected
    assert len(expected) == 31  # system block and each schema, without coalescing schemas
    assert report["structural"]["segment_open"] == settings["segment_open"]
    assert report["structural"]["segment_close"] == settings["segment_close"]
    assert report["profiles"]["synthetic"]["fixed_segments"] == sorted(expected)


def test_build_fixture_v2_never_reuses_old_rendering_or_counts(tmp_path, monkeypatch):
    d, settings = descriptor(), v2_settings()
    (tmp_path / "tokenizer.json").write_text(json.dumps(d))
    cases = json.loads((ROOT / "tests/fixtures/tokenizer_reference.json").read_text())["cases"]
    tok = ByteTokenizer(t["content"] for t in d["added_tokens"])
    monkeypatch.setattr(v, "load_tokenizer", lambda *_: tok)
    monkeypatch.setattr(v, "pinned_normalize", lambda *_: strict_normalize)
    monkeypatch.setattr(v, "fixture_inputs", lambda *_: cases)
    manifest = {"structural": settings, "tool": {"version": "synthetic"}}
    pin = {"vocab_repo": "synthetic/data", "vocab_revision": "a" * 40}
    built = v.build_fixture(tmp_path, pin, "synthetic", manifest)
    assert built["format"] == built["identity"]["renderer"] == "ERPCLAW-COUNT-v2"
    for case in built["cases"]:
        assert case["rendered"] == v.render_count_input_v2(case["messages"], case["tools"], settings)
        assert case["tokens"] == v.count_tokens(tok, case["rendered"])
    assert not {"received", "received_note", "receipts"} & built.keys()


def test_v2_padding_targets_the_new_rendering():
    settings = v2_settings()
    tok = ByteTokenizer([settings["segment_open"], settings["segment_close"], " invoice"])
    for target in (7999, 8000, 8001):
        messages = v._padded(tok, target, settings)
        assert v.count_tokens(tok, v.render_count_input_v2(messages, [], settings)) == target


class NormalizingByteTokenizer(ByteTokenizer):
    """Extract literal tokens first, then NFC and count each residual byte span."""
    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is False
        n, start, i = 0, 0, 0
        while i < len(text):
            match = next((t for t in self.tokens if text.startswith(t, i)), None)
            if match:
                n += len(strict_normalize("NFC", text[start:i]).encode()) + 1
                i += len(match)
                start = i
            else:
                i += 1
        n += len(strict_normalize("NFC", text[start:]).encode())
        return SimpleNamespace(ids=[0] * n)


def test_v2_special_extraction_precedes_normalization():
    d = descriptor()
    # Synthetic decomposed literals would change under NFC if not extracted first.
    d["added_tokens"][0]["content"] = "e\u0301"
    d["added_tokens"][1]["content"] = "o\u0308"
    settings = dict(v2_settings(), segment_open=d["added_tokens"][0]["content"],
                    segment_close=d["added_tokens"][1]["content"])
    v.prove_v2_delimiters(d, settings)
    messages = [{"role": "system", "content": "rules e\u0301"}, {"role": "user", "content": "\u0323owner"}]
    parts = v.proved_parts_v2(messages, [v._tool("one", 1)], settings, d)
    tok = NormalizingByteTokenizer(t["content"] for t in d["added_tokens"])
    assert v.count_tokens(tok, settings["segment_open"]) == 1
    assert v.count_tokens(tok, strict_normalize("NFC", settings["segment_open"])) == 2
    assert any(kind == "fixed" for kind, _ in parts)
    assert v.count_tokens(tok, "".join(s for _, s in parts)) == sum(v.count_tokens(tok, s) for _, s in parts)


@pytest.mark.parametrize("key, mutation", [("segment_open", "ordinary"), ("segment_close", "missing")])
def test_v2_report_cannot_claim_fixed_credit_after_failed_proof(key, mutation):
    d, settings = descriptor(), v2_settings()
    index = 0 if key == "segment_open" else 1
    if mutation == "ordinary":
        d["added_tokens"][index]["special"] = False
    else:
        d["added_tokens"].pop(index)
    cases = [{"id": str(i), "messages": [{"role": "system", "content": "rules"}],
              "tools": [v._tool("one", 1)]} for i in range(12)]
    tok = ByteTokenizer(t["content"] for t in d["added_tokens"])
    report = v.structural_report(d, tok, strict_normalize, cases, settings)
    assert report["structural"]["fixed_counts"] == {}
    for evidence, case in zip(report["cases"], cases):
        rendered = v.render_count_input_v2(case["messages"], case["tools"], settings)
        assert evidence["fixed_exact"] == 0 and evidence["fixed_segments"] == []
        assert evidence["bound"] == len(strict_normalize("NFD", rendered).encode())


def assert_list_only_pins(value, names):
    """Check all serialized objects, including nested identity and report data."""
    if isinstance(value, dict):
        assert not names.intersection(value), "a pinned file name is a dictionary key"
        for key, child in value.items():
            assert not ("token" in key.lower() and isinstance(child, str)
                        and re.fullmatch(r"[0-9a-fA-F]{32,}", child)), "token-named hex value"
            assert_list_only_pins(child, names)
    elif isinstance(value, list):
        for child in value:
            assert_list_only_pins(child, names)


@pytest.fixture(scope="module")
def measured_v2(tmp_path_factory):
    """Measure synthetic data only, with the complete proof and a tokenizer double."""
    folder = tmp_path_factory.mktemp("synthetic-v2")
    data = descriptor()
    # A one-id padding word allows the real input builder to hit every boundary.
    data["added_tokens"][-1]["content"] = " invoice"
    raw = json.dumps(data).encode()
    (folder / "tokenizer.json").write_bytes(raw)
    (folder / "tokenizer_config.json").write_text("{}")
    files = [{"name": name, "sha256": v.sha256_file(folder / name)}
             for name in ("tokenizer_config.json", "tokenizer.json")]
    manifest = v.load_manifest(v.DEFAULT_MANIFEST)
    manifest["structural"] = v2_settings()
    for pin in manifest.values():
        if isinstance(pin, dict) and pin.get("admission") == "bounded":
            pin.update(vocab_repo="synthetic/data", vocab_revision="a" * 40, file=copy.deepcopy(files))
    tok = NormalizingByteTokenizer(t["content"] for t in data["added_tokens"])
    cases = v.fixture_inputs(tok, manifest["structural"])
    proof = v.structural_report(data, tok, strict_normalize, cases, manifest["structural"])
    manifest["structural"] = proof["structural"]
    for pin in manifest.values():
        if isinstance(pin, dict) and pin.get("admission") == "bounded":
            pin["fixed_segments"] = sorted(proof["structural"]["fixed_counts"])
    return SimpleNamespace(folder=folder, manifest=manifest, tokenizer=tok, cases=cases)


def build_synthetic(measurement, monkeypatch, profile="qwen3_5_9b_openrouter_primeintellect"):
    monkeypatch.setattr(v, "load_tokenizer", lambda *_: measurement.tokenizer)
    monkeypatch.setattr(v, "pinned_normalize", lambda *_: strict_normalize)
    pin = v.profile_pin(measurement.manifest, profile)
    return v.build_fixture(measurement.folder, pin, profile, measurement.manifest)


def test_build_writes_list_only_pins_and_identity(measured_v2, tmp_path, monkeypatch, capsys):
    profile = "qwen3_5_9b_openrouter_primeintellect"
    monkeypatch.setattr(v, "load_manifest", lambda *_: measured_v2.manifest)
    monkeypatch.setattr(v, "load_tokenizer", lambda *_: measured_v2.tokenizer)
    monkeypatch.setattr(v, "pinned_normalize", lambda *_: strict_normalize)
    output = tmp_path / "fixture.json"
    assert v.main(["build", "--profile", profile, "--files", str(measured_v2.folder),
                   "--fixture", str(output)]) == 0
    built = json.loads(output.read_text())
    pin = v.profile_pin(measured_v2.manifest, profile)
    original = copy.deepcopy(pin)
    identity = v.measurement_identity(pin, profile, measured_v2.manifest)
    names = {f["name"] for f in pin["file"]}
    assert_list_only_pins(built, names)
    assert_list_only_pins(json.loads(v.J(identity)), names)
    expected = sorted(pin["file"], key=lambda entry: entry["name"])
    assert built["identity"]["pins"]["file"] == expected
    assert "files" not in built["identity"]["pins"]
    assert pin == original  # sanitizing the output must not mutate the lookup
    reversed_pin = {**pin, "file": list(reversed(pin["file"]))}
    assert v.measurement_identity(reversed_pin, profile, measured_v2.manifest) == identity
    assert v.main(["structural", "--profile", profile, "--files", str(measured_v2.folder)],
                  tokenizer=measured_v2.tokenizer, normalize=strict_normalize) == 0
    report = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert_list_only_pins(report, names)
    assert report["identity"] == built["identity"] == identity


def test_built_output_passes_repository_secret_guard(measured_v2, monkeypatch):
    import push_scan

    built = build_synthetic(measured_v2, monkeypatch)
    try:
        findings = push_scan.gitleaks_scan([json.dumps(built, indent=1, sort_keys=True).encode()])
    except push_scan.ScanInvalid as exc:
        if "gitleaks is not installed" in str(exc):
            pytest.skip("repository secret guard requires gitleaks, which is not installed")
        raise
    assert not findings


@pytest.mark.parametrize("bad", ["token_hex", "filename_key"])
def test_list_only_pin_walker_catches_regressions(bad):
    probe = "ab" * 32
    value = {"ToKeN_data": probe} if bad == "token_hex" else {"tokenizer.json": probe}
    with pytest.raises(AssertionError):
        assert_list_only_pins({"nested": [value]}, {"tokenizer.json"})


def test_synthetic_measured_v2_fixture_and_structural_check(measured_v2, monkeypatch):
    built = build_synthetic(measured_v2, monkeypatch)
    declared = measured_v2.manifest["structural"]
    assert built["format"] == built["identity"]["renderer"] == declared["renderer"]
    assert built["structural"] == declared
    assert v.check_structural_manifest(built["structural"], declared) is None
    assert declared["fixed_counts"]
    for case in built["cases"]:
        parts = v.rendering_parts_v2(case["messages"], case["tools"], declared)
        fixed = [text for kind, text in parts if kind == "fixed"]
        hashes = [hashlib.sha256(text.encode()).hexdigest() for text in fixed]
        assert case["fixed_segments"] == hashes
        assert case["fixed_exact"] == sum(declared["fixed_counts"][key] for key in hashes)
        assert case["fixed_exact"] == sum(v.count_tokens(measured_v2.tokenizer, text) for text in fixed)
        assert case["rendered"] == "".join(text for _, text in parts)
        assert case["tokens"] == v.count_tokens(measured_v2.tokenizer, case["rendered"])
        assert case["tokens"] <= case["bound"] and case["additive"]
    for key in declared:
        changed = {**declared, key: None}
        assert v.check_structural_manifest(built["structural"], changed)
    assert v.check_fixture_receipts(built, measured_v2.manifest) == "fresh dispatcher receipts required"


def test_v2_report_rejects_nonadditive_encoding(measured_v2):
    class Nonadditive(NormalizingByteTokenizer):
        def encode(self, text, *, add_special_tokens):
            result = super().encode(text, add_special_tokens=add_special_tokens)
            if text.startswith(v.RENDERER_V2) and "role=system" in text:
                result.ids = result.ids[:-1]
            return result

    tok = Nonadditive(measured_v2.tokenizer.tokens)
    data = json.loads((measured_v2.folder / "tokenizer.json").read_text())
    with pytest.raises(v.StructuralError, match="nonadditive special delimiting"):
        v.structural_report(data, tok, strict_normalize, measured_v2.cases, measured_v2.manifest["structural"])


@pytest.mark.parametrize("key", ["segment_open", "segment_close"])
@pytest.mark.parametrize("value", ["unmeasured", "", None, 1])
def test_renderer_settings_refuse_synthetic_placeholders(key, value):
    settings = {**v2_settings(), key: value}
    with pytest.raises(v.StructuralError, match="delimiter.*unmeasured"):
        v.renderer_settings(settings)
