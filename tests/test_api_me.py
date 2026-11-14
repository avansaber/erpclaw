"""The caller's identity, company permissions and available API features."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import drop_everything
from erpclaw_api.app import create_app
from erpclaw_assistant.model import ScriptedModel
from erpclaw_core import administration as admin
from erpclaw_core import models as m, principals as pr, setup
from erpclaw_core.common import company_ctx


def _client(engine, *, embedded=False, model_name="configured-model"):
    model = ScriptedModel([])
    model.name = model_name
    return TestClient(create_app(engine, model, embedded_owner=embedded))


def _person(engine, name, permissions):
    with engine.begin() as conn:
        token = admin.add_principal(conn, name, permissions, [company_ctx(conn).company_id])
        return pr.authenticate(conn, token), {"Authorization": f"Bearer {token}"}


def _me(client, headers=None):
    response = client.get("/v1/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_owner_and_viewer_see_only_their_own_identity_and_company_permissions(engine):
    viewer, viewer_headers = _person(engine, "reader@example.test", ["view"])
    stranger, _ = _person(engine, "another@example.test", ["approve", "post"])
    with engine.begin() as conn:
        token = admin.issue_token(conn, pr.OWNER)
        owner = pr.authenticate(conn, token)
        company = conn.execute(select(m.company)).mappings().one()
    client = _client(engine)
    for who, headers, permissions in [
        (owner, {"Authorization": f"Bearer {token}"}, sorted(m.PERMISSIONS)),
        (viewer, viewer_headers, ["view"]),
    ]:
        body = _me(client, headers)
        assert body == {
            "name": who["name"], "kind": who["kind"], "principal_id": who["id"],
            "books": {"company": company["name"], "currency": company["functional_currency"]},
            "permissions": permissions, "model": "configured-model", "features": ["decision_events", "steps", "turn_failure_codes"],
        }
        for other in (owner, viewer, stranger):
            if other["id"] != who["id"]:
                assert other["id"] not in json.dumps(body)
                assert other["name"] not in body.values()
        assert who["token_sha256"] not in json.dumps(body)


def test_embedded_owner_principal_id_is_the_stored_id(engine):
    with engine.connect() as conn:
        owner = pr.owner(conn)
    body = _me(_client(engine, embedded=True))
    assert body["principal_id"] == owner["id"]
    assert body["name"] == owner["name"]


def test_permissions_follow_current_grants_including_no_grants(engine):
    _, headers = _person(engine, "reader", ["view"])
    client = _client(engine)
    assert _me(client, headers)["permissions"] == ["view"]
    with engine.begin() as conn:
        admin.grant(conn, "reader", ["draft"], company_ctx(conn).company_id)
    assert _me(client, headers)["permissions"] == ["draft", "view"]
    with engine.begin() as conn:
        admin.revoke(conn, "reader", ["draft", "view"], company_ctx(conn).company_id)
    assert _me(client, headers)["permissions"] == []


@pytest.mark.parametrize("model_name", ["local-model", "another-configured-model"])
def test_model_is_the_configured_models_name(engine, model_name):
    assert _me(_client(engine, embedded=True, model_name=model_name))["model"] == model_name


def test_pending_books_have_null_company_and_currency(engine):
    drop_everything(engine)
    setup.create_own_books(engine)
    body = _me(_client(engine, embedded=True))
    assert body["books"] == {"company": None, "currency": None}
    assert body["permissions"] == sorted(m.PERMISSIONS)


@pytest.mark.parametrize("credential", [None, " ".join(('Bearer', 'invalid')), "Basic invalid", "disabled"])
def test_missing_invalid_or_disabled_credentials_use_the_frozen_401_body(engine, credential):
    if credential == "disabled":
        who, headers = _person(engine, "disabled-reader", ["view"])
        with engine.begin() as conn:
            conn.execute(m.principal.update().where(m.principal.c.id == who["id"]).values(disabled=True))
    else:
        headers = {"Authorization": credential} if credential else {}
    response = _client(engine).get("/v1/me", headers=headers)
    assert response.status_code == 401, response.text
    assert response.json() == {
        "detail": "No API token was sent." if credential is None or credential.startswith("Basic ")
        else "That API token is not valid.",
        "code": "unauthenticated",
    }


def test_features_are_exactly_the_sorted_registered_names_and_start_with_engine_registered_names(engine):
    client = _client(engine, embedded=True)
    assert _me(client)["features"] == ["decision_events", "steps", "turn_failure_codes"]
    from erpclaw_api import me

    assert me.FEATURES == ["decision_events", "steps", "turn_failure_codes"]
    try:
        me.add_feature("fake_z")
        me.add_feature("fake_a")
        me.add_feature("fake_z")
        assert me.FEATURES == ["decision_events", "fake_a", "fake_z", "steps", "turn_failure_codes"]
        assert _me(client)["features"] == me.FEATURES
        assert _me(_client(engine, embedded=True))["features"] == me.FEATURES
    finally:
        me.FEATURES[:] = ["decision_events", "steps", "turn_failure_codes"]
    assert _me(client)["features"] == ["decision_events", "steps", "turn_failure_codes"]


def test_live_response_matches_example_keys_and_types(engine):
    example = json.loads((Path(__file__).parent / "contracts" / "api" / "me.json").read_text())
    body = _me(_client(engine, embedded=True))
    assert body.keys() == example.keys()
    for key, value in example.items():
        assert type(body[key]) is type(value), key
    assert body["books"].keys() == example["books"].keys()
    for key, value in example["books"].items():
        assert type(body["books"][key]) is type(value), key
    for key in ("permissions", "features"):
        assert all(isinstance(value, str) for value in body[key]), key
