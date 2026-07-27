"""Part A tests for the #7071-class pre-master-key-load environment check
(M36 R-b, Wave F sprint 1 rider S1.6).

Covers: clean environment (no findings, key loads silently), injected
environment (WARN to stderr + continue — the self-hosted default), and
ERPCLAW_STRICT_ENV=1 (refuse before any key material is touched), for each
indicator class: tmp-rooted / world-writable PYTHONPATH entries,
PYTHONSTARTUP, and a sitecustomize import from a suspicious path. Also pins
the wiring: get_or_create_master_key / import_master_key / unwrap_master_key
all run the check first.

NOTE ON IMPORTS: ~/.openclaw/erpclaw/lib may be a symlink to a DIFFERENT
checkout (dev-machine layout), so these tests spec-load THIS worktree's
crypto.py + master_key.py under their real dotted names — the code under
test is always the sibling source, never an installed copy.
"""
import importlib.util
import os
import stat
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_PKG_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "lib", "erpclaw_lib")


def _load_wt(dotted_name, filename):
    """Load a worktree-local erpclaw_lib module under its real dotted name so
    package-relative imports (master_key -> .crypto) resolve to the worktree
    copy via sys.modules."""
    spec = importlib.util.spec_from_file_location(
        dotted_name, os.path.join(_LIB_PKG_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


crypto = _load_wt("erpclaw_lib.crypto", "crypto.py")
master_key = _load_wt("erpclaw_lib.master_key", "master_key.py")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Clean interpreter env + key paths redirected into tmp_path."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONSTARTUP", raising=False)
    monkeypatch.delenv(crypto.ERPCLAW_STRICT_ENV_VAR, raising=False)
    # Neutralize any real sitecustomize/usercustomize for determinism.
    monkeypatch.setitem(sys.modules, "sitecustomize", None)
    monkeypatch.setitem(sys.modules, "usercustomize", None)
    # Key file lives in the test sandbox, never ~/.config/erpclaw.
    cfg = tmp_path / "config"
    monkeypatch.setattr(master_key, "CONFIG_DIR", str(cfg))
    monkeypatch.setattr(master_key, "MASTER_KEY_PATH", str(cfg / "master.key"))
    # Reset the once-per-process warning memo so each test observes emission.
    monkeypatch.setattr(crypto, "_ENV_WARNING_EMITTED", False)
    return tmp_path


@pytest.fixture
def non_tmp_dir():
    """A scratch dir inside the repo checkout — NOT under any tmp root on any
    platform (pytest's tmp_path itself lives under $TMPDIR/(/tmp), which the
    check correctly flags, so it cannot play the 'safe directory' role)."""
    import shutil
    d = os.path.join(_TESTS_DIR, f".envcheck_scratch_{os.getpid()}")
    os.makedirs(d, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ── scan itself ──────────────────────────────────────────────────────────────

def test_clean_environment_no_findings(clean_env, capsys):
    findings = crypto.check_environment_before_key_load()
    assert findings == []
    assert capsys.readouterr().err == ""


def test_tmp_rooted_pythonpath_flagged(clean_env, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil-libs")
    findings = crypto.scan_environment_injection_indicators()
    assert len(findings) == 1
    assert "PYTHONPATH" in findings[0] and "/tmp/evil-libs" in findings[0]


def test_world_writable_pythonpath_flagged(clean_env, monkeypatch, non_tmp_dir):
    ww = os.path.join(non_tmp_dir, "injected")
    os.makedirs(ww, exist_ok=True)
    os.chmod(ww, os.stat(ww).st_mode | stat.S_IWOTH)
    monkeypatch.setenv("PYTHONPATH", ww)
    findings = crypto.scan_environment_injection_indicators()
    assert len(findings) == 1
    assert "world-writable" in findings[0]


def test_normal_pythonpath_not_flagged(clean_env, monkeypatch, non_tmp_dir):
    """A user-owned, non-world-writable, non-tmp dir is legitimate dev usage."""
    safe = os.path.join(non_tmp_dir, "mylibs")
    os.makedirs(safe, exist_ok=True)
    monkeypatch.setenv("PYTHONPATH", safe)
    assert crypto.scan_environment_injection_indicators() == []


def test_pythonstartup_flagged(clean_env, monkeypatch):
    monkeypatch.setenv("PYTHONSTARTUP", "/home/user/.pythonrc")
    findings = crypto.scan_environment_injection_indicators()
    assert len(findings) == 1
    assert "PYTHONSTARTUP" in findings[0]


def test_sitecustomize_from_tmp_flagged(clean_env, monkeypatch, tmp_path):
    fake = type(sys)("sitecustomize")
    fake.__file__ = "/tmp/sneaky/sitecustomize.py"
    monkeypatch.setitem(sys.modules, "sitecustomize", fake)
    findings = crypto.scan_environment_injection_indicators()
    assert len(findings) == 1
    assert "sitecustomize" in findings[0]


# ── default = warn + continue ────────────────────────────────────────────────

def test_injected_env_warns_and_continues(clean_env, monkeypatch, capsys):
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil-libs")
    findings = crypto.check_environment_before_key_load()
    assert len(findings) == 1
    err = capsys.readouterr().err
    assert "WARNING [erpclaw]" in err
    assert "ERPCLAW_STRICT_ENV=1" in err
    # Once per process: a second call scans but does not re-spam stderr.
    crypto.check_environment_before_key_load()
    assert capsys.readouterr().err == ""


def test_injected_env_key_still_loads(clean_env, monkeypatch, capsys):
    """The self-hosted default must not brick key-backed actions."""
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil-libs")
    mk = master_key.get_or_create_master_key()
    assert len(mk) == 32
    assert "WARNING [erpclaw]" in capsys.readouterr().err
    # And the key round-trips normally.
    assert master_key.get_or_create_master_key() == mk


# ── strict = refuse ──────────────────────────────────────────────────────────

def test_strict_refuses_scan(clean_env, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil-libs")
    monkeypatch.setenv(crypto.ERPCLAW_STRICT_ENV_VAR, "1")
    with pytest.raises(RuntimeError, match="Refusing to load"):
        crypto.check_environment_before_key_load()


def test_strict_refuses_master_key_load_before_touching_key(
        clean_env, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil-libs")
    monkeypatch.setenv(crypto.ERPCLAW_STRICT_ENV_VAR, "1")
    with pytest.raises(RuntimeError, match="Refusing to load"):
        master_key.get_or_create_master_key()
    # Refused BEFORE generation: no key file was created.
    assert not os.path.exists(master_key.MASTER_KEY_PATH)


def test_strict_refuses_import_and_unwrap(clean_env, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil-libs")
    monkeypatch.setenv(crypto.ERPCLAW_STRICT_ENV_VAR, "1")
    with pytest.raises(RuntimeError, match="Refusing to load"):
        master_key.import_master_key(b"\x01" * 32)
    wrapped = None
    # Build a valid wrapped key under a clean env, then unwrap under strict+injected.
    monkeypatch.delenv(crypto.ERPCLAW_STRICT_ENV_VAR, raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    wrapped = crypto.wrap_master_key(b"\x02" * 32, "pass")
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil-libs")
    monkeypatch.setenv(crypto.ERPCLAW_STRICT_ENV_VAR, "1")
    with pytest.raises(RuntimeError, match="Refusing to load"):
        crypto.unwrap_master_key(wrapped, "pass")


def test_strict_clean_env_loads_normally(clean_env, monkeypatch):
    """Strict mode with a clean environment is a no-op."""
    monkeypatch.setenv(crypto.ERPCLAW_STRICT_ENV_VAR, "1")
    mk = master_key.get_or_create_master_key()
    assert len(mk) == 32
    wrapped = crypto.wrap_master_key(mk, "pass")
    assert crypto.unwrap_master_key(wrapped, "pass") == mk
