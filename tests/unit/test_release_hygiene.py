"""In-memory publisher-file hygiene only; no admission or process tests."""
import pytest

from erpclaw_core.release_hygiene import file_hygiene_issue


@pytest.mark.parametrize("header", [
    bytes.fromhex("7f454c46"), b"MZ",
    bytes.fromhex("feedface"), bytes.fromhex("cefaedfe"),
    bytes.fromhex("feedfacf"), bytes.fromhex("cffaedfe"),
    bytes.fromhex("cafebabe"), bytes.fromhex("bebafeca"),
    bytes.fromhex("cafebabf"), bytes.fromhex("bfbafeca"),
])
@pytest.mark.parametrize("tail", [b"", b"synthetic trailing data"])
def test_binary_header_is_refused_even_with_text_extension(header, tail):
    assert file_hygiene_issue("plugin/readme.txt", header + tail) == "native_code"


@pytest.mark.parametrize("path", [
    "plugin/lib.so", "dependency/ext.PYD", "lib.DlL", "lib.dylib",
    "plugin/ext.cpython-313-x86_64-linux-gnu.so",
])
def test_declared_native_suffix_is_refused_even_without_a_header(path):
    assert file_hygiene_issue(path, b"not a valid binary") == "native_code"


@pytest.mark.parametrize("path", [
    "plugin/code.pyc", "plugin/code.PYO", "plugin/__pycache__/any.txt",
    "dependency/__PYCACHE__/empty", "__pycache__",
])
def test_bytecode_cache_is_reported_before_native_content(path):
    assert file_hygiene_issue(path, b"\x7fELF") == "bytecode_cache"


@pytest.mark.parametrize("path, content", [
    ("plugin/code.py", b"print('plain source')\n"),
    ("plugin/data.json", b'{"name":"sample"}'),
    ("plugin/readme.txt", b"ordinary text"),
    ("plugin/__pycache__.txt", b"cache component lookalike"),
    ("plugin/__pycache__extra/code.py", b"pass"),
    ("plugin/lib.so.txt", b"filename suffix lookalike"),
    ("plugin/empty.txt", b""),
    ("plugin/short.txt", b"M"),
    ("plugin/short.txt", b"\x7fEL"),
    ("plugin/short.txt", bytes.fromhex("feedfa")),
    ("plugin/short.txt", bytes.fromhex("cafeba")),
    ("plugin/data.txt", b"text: \x7fELF MZ \xca\xfe\xba\xbe"),
])
def test_plain_or_nonleading_data_has_no_hygiene_issue(path, content):
    assert file_hygiene_issue(path, content) is None


@pytest.mark.parametrize("path, content", [
    (None, b""), (b"name.txt", b""), (1, b""),
    ("name.txt", None), ("name.txt", "text"),
    ("name.txt", bytearray(b"text")), ("name.txt", memoryview(b"text")),
])
def test_wrong_input_types_are_rejected(path, content):
    with pytest.raises(TypeError, match="str path and bytes content"):
        file_hygiene_issue(path, content)
