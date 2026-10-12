import pytest
from pydantic import ValidationError

from campix.config import Settings


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:9997",
        "https://media.example.invalid:9997",
        "http://[::1]:9997",
        "http://127.0.0.1",
    ],
)
def test_media_api_accepts_http_origins(value):
    assert Settings(mediamtx_api=value).mediamtx_api == value
    assert Settings(mediamtx_api=value + "/").mediamtx_api == value


@pytest.mark.parametrize(
    "value",
    [
        "http:///",
        "https://",
        "http://127.0.0.1:invalid",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:0",
        "http://[invalid]:9997",
        "http://::1:9997",
        "http://private:credential@127.0.0.1:9997",
        "http://private@127.0.0.1:9997",
        "http://127.0.0.1:9997/api",
        "http://127.0.0.1:9997?token=private",
        "http://127.0.0.1:9997#fragment",
        "http://127.0.0.1:9997\n",
        "http://127.0.0.1\\other:9997",
        "rtsp://127.0.0.1:8554",
    ],
)
def test_media_api_rejects_invalid_or_non_origin_targets(value):
    with pytest.raises(ValidationError, match=r"mediamtx_api must be an HTTP\(S\) origin"):
        Settings(mediamtx_api=value)


def test_media_api_can_be_disabled():
    assert Settings(mediamtx_api=None).mediamtx_api is None
    assert Settings(mediamtx_api="").mediamtx_api is None


@pytest.mark.parametrize(
    "values",
    [
        {"mediamtx_api": "http://u:TEST_SECRET@a"},
        {"mediamtx_token": {"private": "TEST_SECRET"}},
    ],
)
def test_configuration_errors_hide_credential_inputs(values):
    with pytest.raises(ValidationError) as failure:
        Settings(**values)
    assert "TEST_SECRET" not in str(failure.value)
    assert "input_value" not in str(failure.value)
