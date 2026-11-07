"""The interactive documentation routes are not part of the product: they answer the frozen 404 everywhere."""
import pytest
from fastapi.testclient import TestClient

from erpclaw_api import pipe
from erpclaw_api.app import create_app
from erpclaw_assistant.model import ScriptedModel

PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]


def _app(engine):
    return create_app(engine, ScriptedModel([]), embedded_owner=True)


def test_the_app_registers_no_documentation_route(engine):
    registered = {getattr(route, "path", None) for route in _app(engine).routes}
    assert not registered & set(PATHS)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_documentation_paths_answer_the_frozen_not_found_in_process(engine, path, method):
    client = TestClient(_app(engine))
    response = client.request(method, path)
    assert response.status_code == 404
    if method == "GET":
        assert response.json() == {"detail": "There is no such route.", "code": "not_found"}


@pytest.mark.parametrize("listener", ["host", "cli", "loopback"])
@pytest.mark.parametrize("path", PATHS)
def test_documentation_paths_answer_the_frozen_not_found_on_every_listener(engine, listener, path):
    client = TestClient(pipe.ListenerApp(_app(engine), listener))
    for headers in ({}, {"Authorization": "Bearer anything"}, {"Authorization": "Session anything"}):
        response = client.get(path, headers=headers)
        assert response.status_code == 404
        assert response.json() == {"detail": "There is no such route.", "code": "not_found"}


def test_the_real_routes_are_still_served(engine):
    assert TestClient(_app(engine)).get("/v1/health").json() == {"ok": True}
