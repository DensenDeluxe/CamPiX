"""Read-only host setup: all Python tooling lives in a fresh temporary directory."""

import argparse
import hashlib
import io
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "packages/linux")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("Run in Linux/WSL: python3 scripts/linux-package-check.py")
    folder = Path(tempfile.mkdtemp(prefix="campix-linux-check-"))
    arch = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(platform.machine())
    if not arch:
        raise SystemExit("This convenience check supports amd64/arm64 only")
    asset = f"uv-{arch}-unknown-linux-gnu.tar.gz"
    base = "https://github.com/astral-sh/uv/releases/download/0.13.0/"
    with urllib.request.urlopen(base + asset, timeout=60) as response:
        data = response.read(100 * 1024 * 1024)
    with urllib.request.urlopen(base + asset + ".sha256", timeout=30) as response:
        checksum = response.read().decode().split()[0]
    if hashlib.sha256(data).hexdigest() != checksum:
        raise RuntimeError("uv release checksum verification failed")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        binary = archive.extractfile(f"uv-{arch}-unknown-linux-gnu/uv").read()
    uv_path = folder / "uv"
    uv_path.write_bytes(binary)
    uv_path.chmod(0o755)
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(folder / "project-env")}
    uv = str(uv_path)
    env["PATH"] = str(folder) + os.pathsep + env["PATH"]
    subprocess.run([uv, "sync", "--locked", "--extra", "test"], cwd=ROOT, env=env, check=True)
    python = str(folder / "project-env/bin/python")
    subprocess.run([python, "-m", "pytest", "-q"], cwd=ROOT, env=env, check=True)
    output = args.output_dir.resolve()
    subprocess.run(
        [python, "scripts/build-deb.py", "--output-dir", str(output)], cwd=ROOT, env=env, check=True
    )
    deb = next(output.glob("campix_*.deb"))
    extracted = folder / "extracted"
    subprocess.run(["dpkg-deb", "--extract", str(deb), str(extracted)], check=True)
    package_env = {
        **env,
        "PYTHONPATH": str(extracted / "usr/lib/campix")
        + os.pathsep
        + str(extracted / "usr/lib/campix/vendor"),
    }
    subprocess.run(
        [python, "scripts/package-smoke.py", "--root", str(extracted)],
        cwd=ROOT,
        env=package_env,
        check=True,
    )
    print(f"Isolated Linux check tooling retained in {folder}")


if __name__ == "__main__":
    main()
