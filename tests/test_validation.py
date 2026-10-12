import pytest
from fastapi.exceptions import RequestValidationError

from .conftest import signin


def test_invalid_login_password_is_not_returned_in_validation_errors(client):
    password = "test-only-private-password-marker" * 40
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": password}
    )

    assert response.status_code == 422
    assert password not in response.text
    error = response.json()["detail"][0]
    assert error["loc"] == ["body", "password"]
    assert error["type"] == "string_too_long"
    assert "input" not in error and "ctx" not in error
    assert client.get("/api/v1/auth/me").status_code == 401


def test_invalid_camera_source_credentials_are_not_returned(client):
    signin(client)
    source = "ftp://test-only-private-user:test-only-private-password@example.invalid/video"
    response = client.post(
        "/api/v1/cameras",
        json={"id": "invalid", "name": "Invalid source", "backend": "network", "source": source},
    )

    assert response.status_code == 422
    assert source not in response.text
    assert "test-only-private-user" not in response.text
    assert "test-only-private-password" not in response.text
    assert response.json()["detail"][0]["type"] == "value_error"
    assert client.get("/api/v1/cameras").json()[0]["id"] == "demo"


def test_invalid_directshow_source_preserves_static_validation_guidance(client):
    signin(client)
    source = "Audit camera:audio=test-only-private-device"
    response = client.post(
        "/api/v1/cameras",
        json={"id": "invalid", "name": "Invalid camera", "backend": "dshow", "source": source},
    )

    assert response.status_code == 422
    assert source not in response.text
    assert "test-only-private-device" not in response.text
    assert response.json()["detail"][0]["msg"] == (
        "Value error, DirectShow source must be a device name without quotes, colons, "
        "or control characters"
    )


def test_validation_error_locations_do_not_return_private_mapping_keys(client):
    signin(client)
    private_key = "rtsp://test-only-private-user:test-only-private-password@example.invalid/video"
    response = client.post(
        "/api/v1/cameras",
        json={
            "id": "invalid",
            "name": "Invalid controls",
            "backend": "synthetic",
            "controls": {private_key: []},
        },
    )

    assert response.status_code == 422
    assert private_key not in response.text
    assert "test-only-private-password" not in response.text
    for error in response.json()["detail"]:
        assert error["loc"][:3] == ["body", "controls", "[redacted]"]


def test_validation_error_locations_do_not_return_private_extra_field_names(client):
    private_key = "test-only-private-extra-field-marker"
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-only-admin-password", private_key: True},
    )

    assert response.status_code == 422
    assert private_key not in response.text
    assert response.json()["detail"][0]["loc"] == ["body", "[redacted]"]


def test_validation_error_locations_preserve_snapshot_list_indices(client):
    signin(client)
    response = client.post(
        "/api/v1/timelapse", json={"media_ids": ["a" * 32, "invalid-media-id"]}
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "media_ids", 1]


@pytest.mark.parametrize(
    "message_prefix",
    [
        "Value error, ",
        "Value error, DirectShow source must be a device name without quotes, colons, "
        "or control characters: ",
    ],
)
def test_validation_error_custom_message_does_not_echo_secrets(client, application, message_prefix):
    secret = "test-only-private-validator-marker"

    @application.get("/validation-error-test")
    def fail_validation():
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body", "source"),
                    "msg": message_prefix + secret,
                    "input": secret,
                    "ctx": {"error": ValueError(secret)},
                }
            ]
        )

    application.router.routes.insert(0, application.router.routes.pop())
    response = client.get("/validation-error-test")
    assert response.status_code == 422
    assert secret not in response.text
    assert response.json()["detail"][0]["loc"] == ["body", "source"]


def test_invalid_camera_without_authentication_remains_unauthorized(client):
    response = client.post(
        "/api/v1/cameras",
        json={"id": "invalid", "name": "Invalid source", "backend": "network", "source": "bad"},
    )
    assert response.status_code == 401
