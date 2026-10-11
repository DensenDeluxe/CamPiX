"""Isolated E2E fixture. Never use these public test credentials for a real server."""

import os
import tempfile
from pathlib import Path

import uvicorn

from campix.app import create_app
from campix.config import Settings

if __name__ == "__main__":
    if os.environ.get("CAMPIX_E2E") != "1":
        raise SystemExit("This test fixture requires CAMPIX_E2E=1 and must never be deployed")
    with tempfile.TemporaryDirectory(prefix="campix-e2e-") as directory:
        app = create_app(
            Settings(
                data_dir=Path(directory),
                demo=True,
                min_free_mb=32,
                port=4401,
                mediamtx_binary=os.environ.get("CAMPIX_TEST_MEDIAMTX_BINARY"),
            )
        )
        for role in ("admin", "operator", "viewer"):
            app.state.auth.create_user(role, f"test-only-{role}-password", role)
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=4401,
            loop="campix.loops:windows_selector" if os.name == "nt" else "auto",
            access_log=False,
        )
