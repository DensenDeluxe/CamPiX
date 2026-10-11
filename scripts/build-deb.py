"""Build for the current Linux architecture/Python; never pip-install into /usr."""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*command):
    subprocess.run(command, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "packages")
    args = parser.parse_args()
    if sys.platform != "linux" or not shutil.which("dpkg-deb") or not shutil.which("uv"):
        parser.error("Build on the target Linux architecture with dpkg-deb and uv installed")
    from campix import __version__

    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"campix_{__version__}_{arch}.deb"
    if destination.exists():
        parser.error("Package exists; use a new output directory")
    with tempfile.TemporaryDirectory(prefix="campix-deb-") as directory:
        stage = Path(directory) / "root"
        lib = stage / "usr/lib/campix"
        lib.mkdir(parents=True)
        requirements = Path(directory) / "native-requirements.txt"
        run(
            "uv",
            "export",
            "--quiet",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-emit-package",
            "numpy",
            "--no-emit-package",
            "pillow",
            "--output-file",
            str(requirements),
        )
        # Distro NumPy/Pillow/OpenCV/libcamera retain ABI compatibility with Picamera2.
        run(
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(lib / "vendor"),
            "--require-hashes",
            "--no-deps",
            "-r",
            str(requirements),
        )
        shutil.copytree(
            ROOT / "src/campix",
            lib / "campix",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        web = stage / "usr/share/campix/web"
        shutil.copytree(ROOT / "dist", web)
        tools = stage / "usr/share/campix/tools"
        tools.mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/fetch-mediamtx.py", tools)
        doc = stage / "usr/share/doc/campix"
        doc.mkdir(parents=True)
        for name in ("DEPLOYMENT.md", "SECURITY.md", "RELEASE-READINESS.md"):
            shutil.copy2(ROOT / "docs" / name, doc)
        shutil.copy2(ROOT / "LICENSE", doc / "copyright")
        bin_dir = stage / "usr/bin"
        bin_dir.mkdir(parents=True)
        entry = bin_dir / "campixd"
        entry.write_text(
            "#!/usr/bin/python3\nimport sys\nsys.path.insert(0, '/usr/lib/campix/vendor')\nsys.path.insert(0, '/usr/lib/campix')\nfrom campix.cli import main\nmain()\n"
        )
        entry.chmod(0o755)
        etc = stage / "etc/campix"
        etc.mkdir(parents=True)
        shutil.copy2(ROOT / "packaging/campix.json", etc / "campix.json")
        service = stage / "usr/lib/systemd/system"
        service.mkdir(parents=True)
        shutil.copy2(ROOT / "packaging/campix.service", service)
        control = stage / "DEBIAN"
        control.mkdir()
        py = f"{sys.version_info.major}.{sys.version_info.minor}"
        next_py = f"{sys.version_info.major}.{sys.version_info.minor + 1}"
        (control / "control").write_text(
            f"Package: campix\nVersion: {__version__}\nArchitecture: {arch}\nSection: video\nPriority: optional\nMaintainer: CamPiX contributors\nDepends: python3 (>= {py}), python3 (<< {next_py}), python3-numpy (>= 1.24), python3-pil (>= 9.4), python3-opencv, ffmpeg, v4l-utils, adduser\nRecommends: python3-picamera2\nDescription: CamPiX camera control, recording, and streaming\n Authenticated multi-camera API and web interface for Raspberry Pi and Linux.\n"
        )
        (control / "conffiles").write_text("/etc/campix/campix.json\n")
        for name in ("postinst", "prerm"):
            shutil.copy2(ROOT / "packaging" / name, control / name)
            (control / name).chmod(0o755)
        run("dpkg-deb", "--root-owner-group", "--build", str(stage), str(destination))
    print(destination)


if __name__ == "__main__":
    main()
