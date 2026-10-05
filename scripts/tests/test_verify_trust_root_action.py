"""Trust-root reporting and signature acceptance with disposable signing keys."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SCRIPTS = Path(__file__).resolve().parents[1]
LIB = SCRIPTS / "erpclaw-setup" / "lib"
sys.path.insert(0, str(LIB))

from erpclaw_lib import signing


@pytest.fixture
def signed_registry():
    # Private keys exist only in memory during this disposable fixture.
    private_key = Ed25519PrivateKey.generate()
    public_hex = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    trusted = signing.TrustedKey(public_hex, None, "disposable-fixture-signer")
    payload = json.dumps(
        {"registry_version": 1, "modules": {}}, sort_keys=True
    ).encode("utf-8")
    signature = private_key.sign(payload).hex()
    return payload, signature, trusted


def _reported_root(monkeypatch, capsys, trusted):
    monkeypatch.setattr(signing, "TRUSTED_KEYS", (trusted,))
    spec = importlib.util.spec_from_file_location(
        "trust_root_action_test_module", SCRIPTS / "module_manager.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.verify_trust_root_action(argparse.Namespace())
    return json.loads(capsys.readouterr().out)


def test_verify_trust_root_router_reports_embedded_keys_without_database(tmp_path):
    """The public action is a pure report, with no supplied-root argument."""
    install = tmp_path / "install"
    install.mkdir()
    env = {
        **os.environ,
        "ERPCLAW_HOME": str(install),
        "PYTHONPATH": str(LIB),
    }
    env.pop("ERPCLAW_ACTOR_CONTEXT", None)
    env.pop("ERPCLAW_TEST_SESSION", None)
    env.pop("ERPCLAW_DB_READONLY", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "db_query.py"),
         "--action", "verify-trust-root"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reported = json.loads(result.stdout)
    assert reported["status"] == "ok"
    assert reported["trusted_keys"] == [
        {"label": root.label,
         "fingerprint": signing.fingerprint(root.public_key_hex),
         "valid_until": root.valid_until}
        for root in signing.TRUSTED_KEYS
    ]
    assert list(install.iterdir()) == []


def test_reported_trust_root_verifies_real_signed_fixture(
    signed_registry, monkeypatch, capsys
):
    payload, signature, trusted = signed_registry
    reported = _reported_root(monkeypatch, capsys, trusted)
    assert reported["trusted_keys"] == [{
        "label": trusted.label,
        "fingerprint": signing.fingerprint(trusted.public_key_hex),
        "valid_until": None,
    }]
    assert signing.verify_registry_signature(
        payload, signature, accepted_keys=(trusted,), today_iso="2026-10-04"
    ) == trusted


def test_incorrect_reported_root_refuses_real_signed_fixture(
    signed_registry, monkeypatch, capsys
):
    payload, signature, correct_root = signed_registry
    other_public = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    incorrect_root = signing.TrustedKey(other_public, None, "other-fixture-root")
    reported = _reported_root(monkeypatch, capsys, incorrect_root)
    assert reported["trusted_keys"][0]["fingerprint"] == signing.fingerprint(
        incorrect_root.public_key_hex
    )
    assert reported["trusted_keys"][0]["fingerprint"] != signing.fingerprint(
        correct_root.public_key_hex
    )
    # The report takes no signature input. Reconciliation's real verifier
    # supplies the refusal, rather than inventing a CLI refusal branch.
    with pytest.raises(InvalidSignature, match="no trusted key verified"):
        signing.verify_registry_signature(
            payload, signature, accepted_keys=(incorrect_root,),
            today_iso="2026-10-04",
        )
