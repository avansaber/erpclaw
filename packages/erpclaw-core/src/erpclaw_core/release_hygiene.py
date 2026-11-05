"""Pure file hygiene for the future publisher-verification path.

This is not admission, signature verification, a binary parser or confinement.
The caller must enumerate the complete release/dependency closure and validate
canonical relative POSIX paths before calling this function for each file.
Nothing here opens a file, loads a plugin or grants permission to run it.
"""
from typing import Literal


_NATIVE_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")
_NATIVE_HEADERS = (
    b"\x7fELF",
    b"MZ",  # Conservatively refuse the DOS/PE family, including truncated files.
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",  # Mach-O 32, both byte orders.
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",  # Mach-O 64, both byte orders.
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",  # Fat/universal 32.
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",  # Fat/universal 64.
)


def file_hygiene_issue(relative_path: str, content: bytes) -> Literal["bytecode_cache", "native_code"] | None:
    """Return a bounded issue code, or None if this check finds no issue.

    Inputs are exact str and immutable bytes values supplied by the caller;
    None never establishes that their path, contents or publisher are safe.
    Cache components and suffixes are case-insensitive for Windows compatibility.
    Native headers are recognised only at offset zero. Complete leading magic
    is refused even if the rest is truncated: MZ is deliberately conservative,
    as is CAFEBABE (also used by Java class files). Embedded magic is not scanned.
    """
    if type(relative_path) is not str or type(content) is not bytes:
        raise TypeError("file hygiene requires a str path and bytes content")
    parts = relative_path.casefold().split("/")
    if "__pycache__" in parts or parts[-1].endswith((".pyc", ".pyo")):
        return "bytecode_cache"
    if parts[-1].endswith(_NATIVE_SUFFIXES) or content.startswith(_NATIVE_HEADERS):
        return "native_code"
    return None
