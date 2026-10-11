# Installation and operations

[Documentation index](README.md) · [Security](SECURITY.md) · [Release readiness](RELEASE-READINESS.md)

## Requirements and installation profiles

Windows is a local development, network-camera, and demo environment, not a Raspberry Pi driver host. Python 3.11 or newer, FFmpeg with `libx264`, and `ffprobe` are required. Linux V4L2 also needs OpenCV and `v4l2-ctl`; CSI needs Picamera2/libcamera packages matched to the operating-system release. Do not run two capture applications against the same device simultaneously.

The fully locked development and OCI environments use `uv.lock` and `requirements.lock`. The native Debian profile keeps libcamera, Picamera2, NumPy, Pillow, and OpenCV from the same distribution. The `.deb` builder vendors locked web/auth dependencies while obtaining camera-related ABI packages from APT. Each package is tied to its build architecture and Python minor version: do not interchange Bookworm/Trixie or amd64/arm64 builds.

## Windows and local development

```powershell
uv sync --locked --extra test --extra v4l2
.venv\Scripts\campixd.exe init-admin
.venv\Scripts\campixd.exe serve --demo
```

Open <http://127.0.0.1:8080>. `serve` without `--demo` does not invent a camera. Data defaults to `.campix/`, or the path specified with `--data-dir` or `CAMPIX_DATA_DIR`. Password prompts do not echo input; do not put passwords in command-line arguments. Global options precede the subcommand, for example:

```powershell
.venv\Scripts\campixd.exe --data-dir C:\CamPiXData serve
```

For Linux development, replace `.venv\Scripts\campixd.exe` with `.venv/bin/campixd`. Use the native profile below for Raspberry Pi CSI capture rather than mixing unrelated pip and distribution camera packages.

## Native Debian package for Raspberry Pi and Linux

Provide `uv` and Python build tools on the target system or a matching build environment. Raspberry Pi OS needs `python3-picamera2`; that package is not necessarily available on other Debian/Linux distributions.

```sh
uv sync --locked --extra test
uv run --no-sync python scripts/build-deb.py
sudo apt install ./packages/campix_0.2.0_$(dpkg --print-architecture).deb
sudo -u campix campixd --config /etc/campix/campix.json init-admin
sudo systemctl enable --now campix
systemctl status campix
journalctl -u campix -f
```

The package does not perform network pip installation as root and does not start with a default account. The service runs as `campix`, with a private state directory, filesystem restrictions, and access through the `video` and, where appropriate, `render` groups. Verify device groups and required CSI device nodes on the actual host. The initial listener is `127.0.0.1:8080`; use an SSH tunnel for a test from another computer:

```sh
ssh -L 8080:127.0.0.1:8080 user@camera-host
```

Installing CSI dependencies directly into the fully locked development virtual environment can override distribution-native NumPy/OpenCV/libcamera ABIs. Use the native package path for CSI. `campixd probe` reports a device/encoder inventory, not hardware certification.

## LAN/WAN and TLS

`packaging/nginx.conf` is a template, not an already deployed TLS configuration. Replace hostnames and certificate paths, provide valid certificates, and run `nginx -t`. Do not publish the CamPiX API port directly to the internet.

Set the following in `/etc/campix/environment`, owned by root with mode `0600`:

```text
CAMPIX_PUBLIC_ORIGIN=https://camera.example.invalid
CAMPIX_SECURE_COOKIES=true
CAMPIX_TRUST_PROXY=true
```

Replace the placeholder with the actual origin and restart the service. Forwarded headers are accepted only from loopback proxies. The CLI rejects public/LAN HTTP origins without secure cookies. The supplied loopback setup uses HTTP only locally. Internet exposure additionally needs a firewall, certificate operations, host patching, and a reviewed media-network configuration.

## Optional MediaMTX

```powershell
.venv\Scripts\python.exe scripts/fetch-mediamtx.py --output .tools\mediamtx.exe
.venv\Scripts\campixd.exe serve --demo --mediamtx-binary .tools\mediamtx.exe
```

The downloader verifies the SHA-256 checksum of pinned release v1.21.2, preserves its license, and refuses to overwrite an existing file. Managed MediaMTX uses loopback ports 8554, 8888, 8889, 9997, 9998, and ICE/TCP 8189. Those ports must be free. The CamPiX supervisor restarts an exited child process.

For services, use an absolute binary path or a configuration path resolvable from the service's working directory. LAN/WAN requires external MediaMTX with reachable ICE addresses and TURN where necessary. These settings connect an external deployment:

```text
CAMPIX_MEDIAMTX_API=http://127.0.0.1:9997
CAMPIX_MEDIAMTX_RTSP=rtsp://127.0.0.1:8554
CAMPIX_MEDIAMTX_TOKEN=<private API/WHEP token, if required>
CAMPIX_MEDIAMTX_HLS_TOKEN=<private secret matching hlsCDNSecret>
```

The external server must be in the same host/proxy network; HLS/WHEP HTTP ports are currently 8888/8889. Do not place secrets in Git, shared URLs, or configuration exports. External RTSP publishing also requires matching permissions, such as private network binding and credentials in the server-side RTSP origin. Managed mode does not expose anonymous network streams.

The Linux package installs the downloader at `/usr/share/campix/tools/fetch-mediamtx.py`. When enabling managed mode, download as an unprivileged user, verify the result, and provide an executable path accessible to `campix`.

## OCI and Docker Compose

```sh
docker compose build
docker compose run --rm campix init-admin
docker compose up -d
```

The local Compose template publishes only `127.0.0.1:8080` and uses `http://localhost:8080` as its origin. Data lives in `campix-data`. Configure the public origin, secure cookies, and proxy trust for a TLS proxy. Do not use `--privileged` containers. Grant specific USB devices and the host's numeric video-group ID. CSI/Picamera2 is deliberately not a supported default container path.

The Dockerfile's default `PYTHON_IMAGE` is digest-pinned. If overriding it, use a reviewed immutable digest for the release build. Container build and operation still need validation on the target host.

## Storage, backups, recovery, and updates

- **Storage reserve:** insufficient free space prevents new recordings and interrupts running encoders with an error event. Hourly retention removes expired, unlocked media; locked recordings remain. The reserve is not a quota against other processes.
- **Interrupted recording:** a clean service shutdown finalizes recordings. Power loss may leave `.partial` MP4 files; they are not presented as completed recordings and are not automatically deleted. Make a separate copy before attempting repair.
- **Database backup:** `campixd --config /etc/campix/campix.json backup /secure/path/campix.sqlite3` produces a consistent SQLite backup without active sessions. It still contains password hashes and potentially private camera URLs. Use mode `0600`, external encryption, and restricted access. Back up media separately; stop the service during the database/media backup for a matching snapshot.
- **Restore:** stop the service and restore matching database and media backups into a new private data directory. Verify `campix` ownership and `0700`/`0600` permissions before starting. Never copy an old database over an active WAL database.
- **Account recovery:** run `sudo -u campix campixd --config /etc/campix/campix.json reset-password admin`. All sessions for that account are revoked. This requires existing access to the private data directory.
- **Updates:** back up first. Schema 1 migrates to 2; downgrading an unknown newer schema is blocked. Package removal retains media and accounts. Roll back with a matching older database backup and package version.
- **Monitoring:** `/healthz` checks HTTP liveness. Use authenticated `/api/v1/system`, camera frame age, `scheduler_alive`, MediaMTX readiness, and `/metrics` for operational monitoring. A green HTTP check does not qualify an offline camera.

## Before production use

Complete the [release-readiness checklist](RELEASE-READINESS.md#remaining-production-gates) on the actual combination: CSI/UVC controls, resolution/FPS, thermal soak, disk-full behavior, disconnect/reconnect, service restart, TLS/origin, WebRTC ICE, backup restore, and permissions. Software tests do not replace this qualification.
