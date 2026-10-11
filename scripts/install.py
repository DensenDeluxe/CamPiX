"""Install a reviewed source checkout without replacing an existing deployment.

The dry run uses only the standard library and read-only host inspection. System
packages are installed by the host package manager; Python tooling and builds
remain unprivileged. Discovery is an inventory, never hardware qualification.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from install_startup import (
    activate_portable,
    check_ports,
    check_startup_available,
    wait_for_listener,
)

ROOT = Path(__file__).resolve().parents[1]
UV_VERSION = "0.13.0"
MEDIAMTX_VERSION = "1.21.2"
DOWNLOAD_LIMIT = 100 * 1024 * 1024
BINARY_LIMIT = 64 * 1024 * 1024
NATIVE_PACKAGES = (
    "python3-venv",
    "python3-dev",
    "python3-numpy",
    "python3-pil",
    "python3-opencv",
    "ffmpeg",
    "v4l-utils",
    "adduser",
    "build-essential",
    "pkg-config",
    "libffi-dev",
)


class InstallError(RuntimeError):
    """An actionable install failure whose message contains no credentials."""


@dataclass(frozen=True)
class Host:
    system: str
    architecture: str
    distribution: str
    distribution_version: str
    board: str
    raspberry_pi: bool
    kernel: str
    python: str
    package_manager: str | None
    video_nodes: tuple[str, ...] = ()
    camera_modules: tuple[str, ...] = ()
    sensors: tuple[str, ...] = ()


def read_text(path: Path, limit: int = 8192) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(limit).decode("utf-8", errors="replace").replace("\0", " ").strip()
    except OSError:
        return ""


def os_release(path: Path) -> dict[str, str]:
    """Parse data without sourcing a shell file from the host."""
    values = {}
    for line in read_text(path).splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z_]+", key):
            try:
                parts = shlex.split(value, comments=False)
            except ValueError:
                continue
            if len(parts) == 1:
                values[key] = parts[0]
    return values


def detect_host() -> Host:
    system = platform.system().lower()
    release = os_release(Path("/etc/os-release")) if system == "linux" else {}
    board = read_text(Path("/sys/firmware/devicetree/base/model"))
    if not board:
        board = read_text(Path("/proc/device-tree/model"))
    compatible = read_text(Path("/sys/firmware/devicetree/base/compatible"))
    if not board:
        board = read_text(Path("/sys/class/dmi/id/product_name")) or platform.machine()
    pi = "raspberry pi" in board.lower() or "raspberrypi," in compatible.lower()
    managers = {
        "linux": ("apt-get", "dnf", "pacman", "zypper"),
        "darwin": ("brew",),
        "windows": ("winget",),
    }.get(system, ())
    manager = next((name for name in managers if shutil.which(name)), None)
    modules = []
    for line in read_text(Path("/proc/modules"), 128 * 1024).splitlines():
        name = line.split()[0] if line.split() else ""
        if any(token in name for token in ("uvc", "video", "unicam", "pisp", "imx", "ov5647")):
            modules.append(name)
    sensors = set()
    for root in (Path("/sys/bus/i2c/devices"), Path("/sys/bus/spi/devices")):
        for item in sorted(root.glob("*/name"))[:256]:
            name = read_text(item, 256).lower()
            if re.fullmatch(r"(?:imx|ov|arducam)[a-z0-9_-]+", name):
                sensors.add(name)
    return Host(
        system=system,
        architecture=platform.machine().lower(),
        distribution=release.get("ID", system),
        distribution_version=release.get("VERSION_ID", platform.release()),
        board=board,
        raspberry_pi=pi,
        kernel=platform.release(),
        python=platform.python_version(),
        package_manager=manager,
        video_nodes=tuple(str(path) for path in sorted(Path("/dev").glob("video[0-9]*"))[:128]),
        camera_modules=tuple(sorted(modules)),
        sensors=tuple(sorted(sensors)),
    )


def uv_asset(host: Host) -> tuple[str, str]:
    arch = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
        "armv7l": "armv7",
        "armv6l": "arm",
        "i386": "i686",
        "i686": "i686",
    }.get(host.architecture)
    if not arch:
        raise InstallError(f"No pinned uv installer for architecture {host.architecture}.")
    if host.system == "linux":
        target = "unknown-linux-gnu"
        if arch == "armv7":
            target = "unknown-linux-gnueabihf"
        elif arch == "arm":
            target = "unknown-linux-musleabihf"
        extension = "tar.gz"
    elif host.system == "darwin" and arch in {"x86_64", "aarch64"}:
        target, extension = "apple-darwin", "tar.gz"
    elif host.system == "windows" and arch in {"x86_64", "aarch64", "i686"}:
        target, extension = "pc-windows-msvc", "zip"
    else:
        raise InstallError(f"No pinned uv installer for {host.system}/{host.architecture}.")
    folder = f"uv-{arch}-{target}"
    return f"{folder}.{extension}", folder


def install_plan(host: Host, without_mediamtx: bool = False) -> dict:
    native = host.system == "linux" and host.package_manager == "apt-get"
    packages = list(NATIVE_PACKAGES) if native else ["ffmpeg"]
    if native and host.raspberry_pi:
        packages.append("python3-picamera2")
        if "imx500" in host.sensors:
            packages.append("imx500-all")
    problems = []
    if host.system not in {"linux", "darwin", "windows"}:
        problems.append("Supported installer hosts are Linux, macOS, and Windows.")
    if not host.package_manager:
        problems.append("No supported package manager found; install host prerequisites manually.")
    if tuple(int(part) for part in host.python.split(".")[:2]) < (3, 11):
        problems.append(
            "Python 3.11 or newer is required; native CSI needs the distribution Python."
        )
    try:
        asset, _ = uv_asset(host)
    except InstallError as exc:
        asset = None
        problems.append(str(exc))
    if host.raspberry_pi and not native:
        problems.append("Raspberry Pi CSI requires the native Debian/Raspberry Pi OS profile.")
    if not without_mediamtx and host.architecture not in {
        "amd64",
        "x86_64",
        "aarch64",
        "arm64",
        "armv7l",
        "armv6l",
    }:
        problems.append(
            "No managed MediaMTX download for this architecture; use --without-mediamtx."
        )
    return {
        "host": asdict(host),
        "profile": "native-debian" if native else "portable-user",
        "packages": packages,
        "uv": {"version": UV_VERSION, "asset": asset},
        "mediamtx": None if without_mediamtx else MEDIAMTX_VERSION,
        "listener": "127.0.0.1:8080",
        "hardware_qualified": False,
        "problems": problems,
        "notes": [
            "Device nodes and loaded drivers are inventory; capture readiness is checked separately.",
            "Vendor CSI drivers and device-tree overlays are not guessed or downloaded.",
            "Camera-related native Python packages stay matched to the operating system.",
            "Encoding auto uses software libx264.",
        ],
    }


def run(
    command: list[str | Path],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
    timeout: int | None = 120,
) -> subprocess.CompletedProcess:
    """Use argument arrays exclusively and never include secrets in diagnostics."""
    argv = [str(item) for item in command]
    try:
        if capture:
            process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            output = bytearray()
            exceeded = threading.Event()

            def collect():
                assert process.stdout is not None
                try:
                    while chunk := process.stdout.read(65536):
                        if len(output) + len(chunk) > 1024 * 1024:
                            exceeded.set()
                            process.kill()
                            break
                        output.extend(chunk)
                finally:
                    process.stdout.close()

            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise
            reader.join(timeout=10)
            if reader.is_alive() or exceeded.is_set():
                raise InstallError(f"{Path(argv[0]).name} exceeded its diagnostic output limit.")
            assert process.stdout is not None
            process.stdout.close()
            result = subprocess.CompletedProcess(
                argv, process.returncode, output.decode(errors="replace"), ""
            )
        else:
            result = subprocess.run(
                argv, cwd=cwd, env=env, check=False, text=True, timeout=timeout, stdout=sys.stderr
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(
            f"Could not finish {Path(argv[0]).name}; check prerequisites and retry."
        ) from exc
    if check and result.returncode:
        raise InstallError(f"{Path(argv[0]).name} failed with exit code {result.returncode}.")
    return result


def download(url: str, limit: int) -> bytes:
    if not url.startswith("https://"):
        raise InstallError("Tool downloads require HTTPS.")
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            if not response.geturl().startswith("https://"):
                raise InstallError("Tool download redirected outside HTTPS.")
            data = response.read(limit + 1)
    except OSError as exc:
        raise InstallError("Tool download failed; check connectivity and retry.") from exc
    if len(data) > limit:
        raise InstallError("Tool download exceeds its size limit.")
    return data


def provision_uv(host: Host, folder: Path, supplied: Path | None = None) -> Path:
    candidate = supplied or (Path(found) if (found := shutil.which("uv")) else None)
    if candidate:
        version = run([candidate, "--version"], capture=True, check=False)
        if version.returncode == 0 and (version.stdout or "").split()[:2] == ["uv", UV_VERSION]:
            return candidate.resolve()
        if supplied:
            raise InstallError(f"--uv must point to uv {UV_VERSION}.")
    asset, prefix = uv_asset(host)
    base = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
    try:
        parts = download(base + asset + ".sha256", 4096).decode("ascii").split()
    except UnicodeError as exc:
        raise InstallError("uv checksum manifest is invalid.") from exc
    checksum = parts[0] if parts else ""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
        raise InstallError("uv checksum manifest is invalid.")
    data = download(base + asset, DOWNLOAD_LIMIT)
    if hashlib.sha256(data).hexdigest() != checksum.lower():
        raise InstallError("uv SHA-256 verification failed.")
    binary = "uv.exe" if host.system == "windows" else "uv"
    try:
        if asset.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                member = archive.getinfo(binary)
                if member.file_size > BINARY_LIMIT:
                    raise InstallError("uv executable exceeds its size limit.")
                executable = archive.read(member)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                member = archive.getmember(f"{prefix}/{binary}")
                if not member.isfile() or member.size > BINARY_LIMIT:
                    raise InstallError("uv executable is invalid or too large.")
                stream = archive.extractfile(member)
                if stream is None:
                    raise InstallError("uv archive contains no executable.")
                with stream:
                    executable = stream.read(BINARY_LIMIT + 1)
    except (KeyError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise InstallError("uv archive does not contain the expected executable.") from exc
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = folder / binary
    with output.open("xb") as stream:
        stream.write(executable)
    output.chmod(0o755)
    version = run([output, "--version"], capture=True).stdout.split()[:2]
    if version != ["uv", UV_VERSION]:
        raise InstallError("Downloaded uv reports an unexpected version.")
    return output


def require_unprivileged(host: Host) -> None:
    elevated = host.system != "windows" and hasattr(os, "geteuid") and os.geteuid() == 0
    if host.system == "windows":
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    if elevated:
        raise InstallError(
            "Run this installer as your normal user; it elevates only package operations."
        )


def source_revision(source: Path) -> str:
    for name in ("uv.lock", "pyproject.toml", "dist/index.html", "scripts/build-deb.py"):
        if not (source / name).is_file():
            raise InstallError(
                "Source checkout is incomplete; use the documented bootstrap command."
            )
    revision = run(["git", "-C", source, "rev-parse", "HEAD"], capture=True).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise InstallError("Source revision must be a complete Git commit.")
    status = run(["git", "-C", source, "status", "--porcelain"], capture=True).stdout.strip()
    if status:
        raise InstallError("Source checkout has local changes; install a clean, reviewed commit.")
    return revision


def ensure_native_fresh() -> None:
    result = run(["dpkg-query", "-W", "-f=${Status}", "campix"], capture=True, check=False)
    if result.returncode == 0 and "installed" in result.stdout:
        raise InstallError(
            "CamPiX is already installed; back up and use the documented update procedure."
        )
    account = run(["getent", "passwd", "campix"], capture=True, check=False)
    if account.returncode == 0:
        raise InstallError(
            "An existing campix system account must be reviewed before installation."
        )
    for path in (Path("/etc/campix"), Path("/var/lib/campix"), Path("/usr/local/lib/campix")):
        if path.exists() or path.is_symlink():
            raise InstallError(
                f"Existing deployment at {path}; back up before a reviewed update or recovery."
            )
    if not Path("/run/systemd/system").is_dir():
        raise InstallError(
            "Native installation requires a running systemd host; use a full Linux host."
        )
    unit = run(
        ["systemctl", "show", "campix.service", "--property=LoadState", "--value"], capture=True
    ).stdout.strip()
    if unit != "not-found":
        raise InstallError(
            "An existing CamPiX system service must be reviewed before installation."
        )
    if not Path("/usr/bin/python3").is_file():
        raise InstallError("The native profile requires /usr/bin/python3 from the distribution.")
    version = run(
        ["/usr/bin/python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture=True,
    ).stdout.strip()
    if tuple(int(part) for part in version.split(".")) < (3, 11):
        raise InstallError(
            "Upgrade the operating system: distribution Python 3.11+ is required for CSI."
        )


def package_candidate(package: str) -> bool:
    result = run(["apt-cache", "policy", package], capture=True, check=False)
    match = re.search(r"^\s*Candidate:\s*(\S+)\s*$", result.stdout or "", re.MULTILINE)
    return result.returncode == 0 and bool(match) and match[1] != "(none)"


def install_host_packages(host: Host, packages: list[str]) -> None:
    manager = host.package_manager
    if manager == "apt-get":
        run(["sudo", "apt-get", "update"], timeout=1800)
        for package in ("python3-picamera2", "imx500-all"):
            if package in packages and not package_candidate(package):
                raise InstallError(
                    f"{package} is unavailable in this OS repositories; use a matching Raspberry Pi OS image."
                )
        run(
            ["sudo", "apt-get", "install", "-y", "--no-install-recommends", *packages], timeout=1800
        )
    elif manager == "dnf":
        run(["sudo", "dnf", "install", "-y", "ffmpeg", "v4l-utils"], timeout=1800)
    elif manager == "pacman":
        # Never refresh package databases without the corresponding system upgrade.
        run(
            ["sudo", "pacman", "-S", "--needed", "--noconfirm", "ffmpeg", "v4l-utils"], timeout=1800
        )
    elif manager == "zypper":
        run(["sudo", "zypper", "--non-interactive", "install", "ffmpeg", "v4l-utils"], timeout=1800)
    elif manager == "brew":
        run(["brew", "install", "ffmpeg"], timeout=1800)
    elif manager == "winget":
        run(
            [
                "winget",
                "install",
                "--exact",
                "--id",
                "Gyan.FFmpeg",
                "--source",
                "winget",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--disable-interactivity",
            ],
            timeout=1800,
        )
    else:
        raise InstallError("No supported host package manager is available.")


def executable(name: str, host: Host) -> Path:
    candidate = shutil.which(name)
    if not candidate and host.system == "windows":
        # WinGet aliases can be installed after this process inherited PATH.
        links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links"
        alias = links / f"{name}.exe"
        if alias.is_file():
            candidate = str(alias)
        else:
            packages = links.parent / "Packages"
            matches = sorted(packages.glob(f"Gyan.FFmpeg_*/**/bin/{name}.exe"))[:16]
            candidate = str(matches[0]) if matches else None
    if not candidate:
        raise InstallError(
            f"{name} is unavailable after package installation; refresh PATH and retry."
        )
    return Path(candidate).resolve()


def validate_encoder(host: Host) -> tuple[Path, Path]:
    ffmpeg, ffprobe = executable("ffmpeg", host), executable("ffprobe", host)
    result = run([ffmpeg, "-hide_banner", "-encoders"], capture=True)
    if not re.search(r"^\s*V\S*\s+libx264\s", result.stdout or "", re.MULTILINE):
        raise InstallError(
            "The installed FFmpeg lacks libx264; use your distribution's approved codec packages."
        )
    run([ffprobe, "-version"], capture=True)
    run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=16x16:rate=1",
            "-frames:v",
            "1",
            "-c:v",
            "libx264",
            "-f",
            "null",
            "-",
        ],
        capture=True,
        timeout=30,
    )
    return ffmpeg, ffprobe


def private_json(path: Path, values: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(values, indent=2) + "\n")
    path.chmod(0o600)


def tool_environment(uv: Path, environment: Path, ffmpeg: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UV_", "CAMPIX_"))
        and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    env.update(
        UV_PROJECT_ENVIRONMENT=str(environment),
        UV_PYTHON_DOWNLOADS="never",
        PATH=str(uv.parent) + os.pathsep + str(ffmpeg.parent) + os.pathsep + env.get("PATH", ""),
    )
    return env


def setup_command(command: list[str | Path], non_interactive: bool, *, env: dict[str, str]) -> None:
    options = ["--non-interactive"] if non_interactive else []
    # Password input is inherited from the terminal and never captured or logged.
    run([*command, "setup", *options], env=env, timeout=None if not non_interactive else 120)


def native_install(
    source: Path, host: Host, plan: dict, scratch: Path, args: argparse.Namespace
) -> dict:
    ensure_native_fresh()
    install_host_packages(host, plan["packages"])
    ffmpeg, _ = validate_encoder(host)
    if host.raspberry_pi:
        run(
            ["/usr/bin/python3", "-c", "import picamera2, libcamera, numpy, PIL, cv2"], capture=True
        )
    uv = provision_uv(host, scratch / "tools", args.uv)
    environment = scratch / "build-env"
    env = tool_environment(uv, environment, ffmpeg)
    run(
        [
            uv,
            "sync",
            "--locked",
            "--extra",
            "test",
            "--python",
            "/usr/bin/python3",
            "--no-install-package",
            "numpy",
            "--no-install-package",
            "pillow",
        ],
        cwd=source,
        env=env,
        timeout=1800,
    )
    python = environment / "bin/python"
    packages = scratch / "packages"
    run([python, source / "scripts/build-deb.py", "--output-dir", packages], env=env, timeout=1800)
    built = list(packages.glob("campix_*.deb"))
    if len(built) != 1:
        raise InstallError("Native package build did not produce exactly one package.")
    media = None
    if not args.without_mediamtx:
        media = scratch / "mediamtx"
        run(
            ["/usr/bin/python3", source / "scripts/fetch-mediamtx.py", "--output", media],
            timeout=180,
        )
    run(["sudo", "apt-get", "install", "-y", str(built[0])], timeout=1800)
    smoke_env = {**env, "PYTHONPATH": "/usr/lib/campix:/usr/lib/campix/vendor"}
    run(["/usr/bin/python3", source / "scripts/package-smoke.py"], env=smoke_env)
    config = json.loads((source / "packaging/campix.json").read_text(encoding="utf-8"))
    config["ffmpeg"] = str(ffmpeg)
    if media:
        installed_media = Path(f"/usr/local/lib/campix/mediamtx-v{MEDIAMTX_VERSION}")
        run(["sudo", "install", "-d", "-m", "0755", installed_media.parent])
        run(["sudo", "install", "-m", "0755", media, installed_media])
        run(
            [
                "sudo",
                "install",
                "-m",
                "0644",
                media.with_suffix(".LICENSE"),
                installed_media.with_suffix(".LICENSE"),
            ]
        )
        config["mediamtx_binary"] = str(installed_media)
    config_file = scratch / "campix.json"
    private_json(config_file, config)
    installed_config = Path("/etc/campix/campix.json")
    run(
        [
            "sudo",
            "install",
            "-m",
            "0640",
            "-o",
            "root",
            "-g",
            "campix",
            config_file,
            installed_config,
        ]
    )
    setup_command(
        ["sudo", "-u", "campix", "/usr/bin/campixd", "--config", installed_config],
        args.non_interactive,
        env=env,
    )
    check_ports(config)
    try:
        run(["sudo", "systemctl", "enable", "--now", "campix"])
        wait_for_listener(
            8080,
            lambda: int(
                run(
                    ["systemctl", "show", "campix", "--property=MainPID", "--value"], capture=True
                ).stdout.strip()
                or "0"
            ),
        )
        run(["systemctl", "is-active", "--quiet", "campix"])
    except (RuntimeError, InstallError):
        try:
            run(["sudo", "systemctl", "disable", "--now", "campix"], check=False)
        except InstallError:
            print(
                "Could not stop the new service; run sudo systemctl disable --now campix.",
                file=sys.stderr,
            )
        raise
    return {
        "profile": "native-debian",
        "config": str(installed_config),
        "data": "/var/lib/campix",
        "service": "campix",
        "start": "sudo systemctl start campix",
        "hardware_qualified": False,
        "reboot_recommended": "imx500-all" in plan["packages"],
    }


def portable_root(host: Host) -> Path:
    if host.system == "windows":
        parent = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        return (parent / "CamPiX").absolute()
    return (Path.home() / ".local/share/campix").absolute()


def ensure_portable_fresh(root: Path) -> None:
    if not root.resolve().is_relative_to(Path.home().resolve()):
        raise InstallError("Portable install paths must remain inside your private user directory.")
    if any(ord(character) < 32 for character in str(root)):
        raise InstallError("Control characters are not supported in install paths.")
    if root.is_symlink():
        raise InstallError("Install directory is a symlink; choose a private, unmanaged directory.")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise InstallError(
            f"Existing files at {root}; back up and follow the documented update or recovery procedure."
        )


def copy_source(source: Path, release: Path) -> None:
    """Copy tracked regular files only; ignored credentials/cache never enter releases."""
    files = run(["git", "-C", source, "ls-files", "-z"], capture=True).stdout.split("\0")
    release.mkdir(mode=0o700)
    for name in filter(None, files):
        relative = Path(name)
        original = source / relative
        if relative.is_absolute() or ".." in relative.parts or original.is_symlink():
            raise InstallError("Source checkout contains an unsafe path or symlink.")
        if not original.resolve().is_relative_to(source.resolve()) or not original.is_file():
            raise InstallError("Source files must remain inside the reviewed checkout.")
        destination = release / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(original, destination)


def portable_install(
    source: Path, revision: str, host: Host, scratch: Path, args: argparse.Namespace
) -> dict:
    root = portable_root(host)
    ensure_portable_fresh(root)
    check_startup_available()
    try:
        executable("ffmpeg", host)
        executable("ffprobe", host)
    except InstallError:
        install_host_packages(host, ["ffmpeg"])
    ffmpeg, _ = validate_encoder(host)
    uv = provision_uv(host, scratch / "tools", args.uv)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = root / "install.json"
    private_json(marker, {"format": 1, "revision": revision, "status": "preparing"})
    release = root / "releases" / revision
    release.parent.mkdir(mode=0o700)
    copy_source(source, release)
    environment = release / ".venv"
    env = tool_environment(uv, environment, ffmpeg)
    run(
        [uv, "sync", "--locked", "--extra", "v4l2", "--no-editable", "--python", sys.executable],
        cwd=release,
        env=env,
        timeout=1800,
    )
    windows = host.system == "windows"
    python = environment / ("Scripts/python.exe" if windows else "bin/python")
    daemon = environment / ("Scripts/campixd.exe" if windows else "bin/campixd")
    run([python, release / "scripts/package-smoke.py"], env=env)
    config = {
        "data_dir": str(root / "data"),
        "host": "127.0.0.1",
        "demo": False,
        "ffmpeg": str(ffmpeg),
    }
    if not args.without_mediamtx:
        media = release / "tools" / ("mediamtx.exe" if windows else "mediamtx")
        run([python, release / "scripts/fetch-mediamtx.py", "--output", media], timeout=180)
        config["mediamtx_binary"] = str(media)
    config_file = root / "campix.json"
    private_json(config_file, config)
    setup_command([daemon, "--config", config_file], args.non_interactive, env=env)
    if windows:
        launcher = root / "start-campix.ps1"

        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"

        launcher.write_text(
            f"& {quote(daemon)} --config {quote(config_file)} serve\n", encoding="utf-8"
        )
    else:
        launcher = root / "start-campix"
        launcher.write_text(
            f"#!/bin/sh\nexec {shlex.quote(str(daemon))} --config {shlex.quote(str(config_file))} serve\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
    startup = activate_portable(daemon, config_file, release)
    # Updating this installer-owned marker is safe; application state is separate.
    marker.write_text(
        json.dumps({"format": 1, "revision": revision, "status": "ready"}, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "profile": "portable-user",
        "config": str(config_file),
        "data": str(root / "data"),
        "release": str(release),
        "start": str(launcher),
        **startup,
        "hardware_qualified": False,
        "reboot_recommended": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Read-only host inventory and install plan"
    )
    parser.add_argument(
        "--non-interactive", action="store_true", help="Fail if secure admin input is needed"
    )
    parser.add_argument(
        "--without-mediamtx", action="store_true", help="Omit optional managed streaming"
    )
    parser.add_argument("--uv", type=Path, help=f"Use an existing uv {UV_VERSION} executable")
    parser.add_argument(
        "--source", type=Path, default=ROOT, help="Clean source checkout to install"
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    args = parser.parse_args(argv)
    host = detect_host()
    plan = install_plan(host, args.without_mediamtx)
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    try:
        if plan["problems"]:
            raise InstallError(" ".join(plan["problems"]))
        require_unprivileged(host)
        if args.non_interactive:
            raise InstallError(
                "A fresh installation requires interactive secure administrator creation; rerun in a terminal."
            )
        source = args.source.resolve()
        revision = source_revision(source)
        check_ports({"port": 8080, "mediamtx_binary": None if args.without_mediamtx else "enabled"})
        os.umask(0o077)
        with tempfile.TemporaryDirectory(prefix="campix-install-") as directory:
            scratch = Path(directory)
            if plan["profile"] == "native-debian":
                result = native_install(source, host, plan, scratch, args)
            else:
                result = portable_install(source, revision, host, scratch, args)
        result.update(revision=revision, url="http://127.0.0.1:8080")
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"CamPiX installed. Configuration: {result['config']}")
            print(f"Service started. Manual launch after stopping the service: {result['start']}")
            print("Open http://127.0.0.1:8080 locally; use an SSH tunnel for another computer.")
            print("Verify physical cameras and performance on this host before production use.")
            if result["reboot_recommended"]:
                print("IMX500 firmware was installed; reboot the host before camera validation.")
        return 0
    except (InstallError, RuntimeError, OSError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, RuntimeError)
            else "Installation failed while handling local files."
        )
        print(f"CamPiX installation stopped: {message}", file=sys.stderr)
        print(
            "Existing accounts/media are preserved. Inspect the retained deployment before recovery; shared packages are not removed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
