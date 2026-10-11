"""Validate installed/extracted distribution assets and authenticated capture."""

import argparse
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import campix
from campix.app import create_app
from campix.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, help="Extracted Debian root (omit for installed wheel)"
    )
    args = parser.parse_args()
    if args.root:
        assert Path(campix.__file__).is_relative_to(args.root.resolve()), campix.__file__
        web = args.root / "usr/share/campix/web"
    else:
        assert not Path(campix.__file__).is_relative_to(Path(__file__).resolve().parents[1] / "src")
        web = None
    with tempfile.TemporaryDirectory(prefix="campix-package-smoke-") as folder:
        app = create_app(Settings(data_dir=Path(folder), demo=True, min_free_mb=32, web_root=web))
        app.state.auth.create_user("admin", "test-only-package-password")
        with TestClient(app) as client:
            assert "CamPiX" in client.get("/").text
            assert client.get("/app.js").status_code == 200
            assert client.get("/styles.css").status_code == 200
            assert client.get("/assets/campix-wordmark.svg").status_code == 200
            assert client.get("/api/v1/cameras").status_code == 401
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "test-only-package-password"},
            )
            assert login.status_code == 200
            client.headers["X-CSRF-Token"] = login.json()["csrf"]
            assert client.get("/api/v1/cameras/demo/preview.jpg").content.startswith(b"\xff\xd8")
            result = client.post("/api/v1/cameras/demo/snapshot")
            assert result.status_code == 201, result.text
    print(f"Packaged assets, auth and JPEG capture verified: {campix.__file__}")


if __name__ == "__main__":
    main()
