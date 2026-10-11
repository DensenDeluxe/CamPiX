import pytest
from fastapi.testclient import TestClient

from campix.app import create_app
from campix.config import Settings


@pytest.fixture
def application(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", demo=True, min_free_mb=32)
    app = create_app(settings)
    app.state.auth.create_user("admin", "test-only-admin-password", "admin")
    app.state.auth.create_user("viewer", "test-only-viewer-password", "viewer")
    app.state.auth.create_user("operator", "test-only-operator-password", "operator")
    return app


@pytest.fixture
def client(application):
    with TestClient(application) as client:
        yield client


def signin(client, username="admin"):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": f"test-only-{username}-password"},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return response.json()
