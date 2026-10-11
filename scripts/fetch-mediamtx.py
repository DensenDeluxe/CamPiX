"""Download a pinned release and verify its published SHA-256; never overwrite."""

import argparse
import hashlib
import io
import platform
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.21.2"


def fetch(url, limit):
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("Download exceeds its size limit")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or output.with_suffix(".LICENSE").exists():
        parser.error("Destination already exists; install updates to a new path")
    system = {"win32": "windows", "linux": "linux", "darwin": "darwin"}.get(sys.platform)
    arch = {
        "AMD64": "amd64",
        "x86_64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armv7",
        "armv6l": "armv6",
    }.get(platform.machine())
    if not system or not arch:
        parser.error("No supported MediaMTX release for this platform")
    extension = "zip" if system == "windows" else "tar.gz"
    asset = f"mediamtx_v{VERSION}_{system}_{arch}.{extension}"
    base = f"https://github.com/bluenviron/mediamtx/releases/download/v{VERSION}/"
    sums = fetch(base + "checksums.sha256", 256 * 1024).decode()
    hashes = {
        parts[1].lstrip("*"): parts[0]
        for line in sums.splitlines()
        if len(parts := line.split()) == 2
    }
    data = fetch(base + asset, 256 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != hashes.get(asset):
        raise RuntimeError("MediaMTX SHA-256 verification failed")
    binary = "mediamtx.exe" if system == "windows" else "mediamtx"
    if extension == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            executable = archive.read(binary)
            license_text = archive.read("LICENSE")
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            executable = archive.extractfile(binary).read()
            license_text = archive.extractfile("LICENSE").read()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(executable)
    output.chmod(0o755)
    output.with_suffix(".LICENSE").write_bytes(license_text)
    print(f"Verified MediaMTX v{VERSION}: {output}")


if __name__ == "__main__":
    main()
