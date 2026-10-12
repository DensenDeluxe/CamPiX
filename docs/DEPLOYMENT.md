# Installation and operations

[Documentation index](README.md) · [Security](SECURITY.md) · [Release readiness](RELEASE-READINESS.md)

## One-command installation

The all-in-one installer identifies the operating system, CPU architecture, Raspberry Pi board information where exposed by Linux, and visible camera devices. It chooses an installation profile, prepares missing platform prerequisites, installs CamPiX and FFmpeg, downloads checksum-verified MediaMTX by default, and runs terminal setup for an administrator and discovered cameras. Runtime capture and camera controls remain in `campixd`; installation never manufactures a working camera from a board name.

These remote commands require this installer revision to be published first. No publication or deployment is implied by their presence in this checkout.

The Unix one-liner needs Bash and curl. The bootstrap prepares Git and Python when their supported host package manager can supply them. macOS requires Homebrew to be installed first; Windows requires WinGet through Microsoft App Installer. The installer reports these missing bootstrap requirements before continuing.

Linux and macOS:

```sh
curl -fsSL https://raw.githubusercontent.com/DensenDeluxe/CamPiX/main/install.sh | bash
```

Windows PowerShell:

```powershell
& ([scriptblock]::Create((Invoke-RestMethod https://raw.githubusercontent.com/DensenDeluxe/CamPiX/main/install.ps1)))
```

Run from your normal user account in an interactive terminal; root/elevated administrator sessions are rejected. The installer elevates only operations that need system privileges. Administrator passwords are entered privately; never place them in command arguments or shared shell history. After setup, it starts the installed service and verifies its managed process state and an HTTP response belonging to that process at <http://127.0.0.1:8080>. This startup check does not certify camera capture or MediaMTX publishing.

For a review before execution, download and inspect the entrypoint:

```sh
curl -fL https://raw.githubusercontent.com/DensenDeluxe/CamPiX/main/install.sh -o campix-install.sh
less campix-install.sh
bash campix-install.sh
```

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/DensenDeluxe/CamPiX/main/install.ps1 -OutFile campix-install.ps1
Get-Content campix-install.ps1
& ([scriptblock]::Create((Get-Content -Raw .\campix-install.ps1)))
```

The entrypoints resolve `CAMPIX_REF` once, check out that exact application commit in a fresh directory, and never overwrite your current checkout. The default is `main`; use an explicit release tag or commit for a reproducible application version. To pin the bootstrap code as well, download it from the same reviewed immutable commit URL instead of `/main/`. Git reference pinning is separate from dependency-download checksum verification.

```sh
CAMPIX_REF='REPLACE_WITH_RELEASE_TAG_OR_COMMIT' bash campix-install.sh
```

```powershell
$env:CAMPIX_REF = 'REPLACE_WITH_RELEASE_TAG_OR_COMMIT'
& ([scriptblock]::Create((Get-Content -Raw .\campix-install.ps1)))
```

For this checkout, Python 3.11 or newer can print a read-only plan without downloads, package installation, elevation, or changes to application state:

```sh
python scripts/install.py --dry-run
```

To install a reviewed clean committed checkout, run `python scripts/install.py`. Uncommitted source changes are rejected. Use `python3` where that is the platform's executable name. The shell/PowerShell entrypoints prepare a separate checkout of `CAMPIX_REF`; the Python dry run can inspect a locally edited checkout.

Installer options:

| Option               | Behavior                                                                                                                    |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `--dry-run`          | Print a read-only host inventory and install plan. Detected problems are included in the plan.                              |
| `--without-mediamtx` | Skip managed MediaMTX; authenticated MJPEG remains available.                                                               |
| `--uv PATH`          | Use an existing executable matching pinned uv 0.13.0. Otherwise download a SHA-256-verified pinned executable.              |
| `--source PATH`      | Install a different clean committed checkout.                                                                               |
| `--json`             | Print the final installation summary as JSON.                                                                               |
| `--non-interactive`  | Fail before a fresh installation because secure administrator creation requires a terminal. It supplies no default account. |

The Unix entrypoint forwards these options. The PowerShell entrypoint exposes `-DryRun`, `-WithoutMediaMTX`, `-Uv PATH`, and `-NonInteractive`; use the Python installer for `--source` and `--json`. A downloaded bootstrap's dry run prints its bootstrap plan without fetching source; when invoked from this checkout with Python available, it runs the local Python dry run.

## Platform and camera profiles

| Host profile                                                                 | Installation path                                                                 | Camera boundary                                                                                                                                |
| ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Debian, Ubuntu, Raspberry Pi OS with system Python 3.11+ and running systemd | Target-native `.deb`, distribution camera dependencies, non-root systemd service  | Linux V4L2; Raspberry Pi CSI when its distribution supplies the required Picamera2/libcamera packages and the sensor is supported by the host. |
| Linux with DNF, pacman, or Zypper and an active systemd user manager         | System prerequisites plus a private locked user environment and user service      | Linux V4L2 where device access and host drivers are available; Raspberry Pi CSI requires the native Debian profile.                            |
| macOS with Homebrew and a GUI login session                                  | System prerequisites plus a private locked user environment and LaunchAgent       | Network RTSP/HTTP(S) sources; local Picamera2/V4L2 is unavailable.                                                                             |
| Windows with winget and Task Scheduler                                       | System prerequisites plus a private locked user environment and limited user task | USB/integrated webcams through FFmpeg DirectShow and network RTSP/HTTP(S) sources; Picamera2/V4L2 are unavailable.                             |

Profile support is implemented installation logic, not evidence that every distribution, CPU, board, sensor, and package-manager repository has been tested. Unsupported hosts and native systems with unsuitable Python versions fail with a clear requirement rather than silently using an incompatible camera ABI.

FFmpeg must supply `libx264` and `ffprobe`; the installer performs a small software encoding check. Windows webcams also need FFmpeg's DirectShow input, which the WinGet FFmpeg build provides. Linux V4L2 requires OpenCV and `v4l2-ctl`; Raspberry Pi CSI requires distribution-matched Picamera2/libcamera. Kernel USB/UVC drivers generally belong to the operating system. The installer uses known distribution package sources and reports unavailable CSI prerequisites; it does not guess device-tree overlays, execute vendor install scripts, or perform a distribution upgrade. If Linux identifies an IMX500 sensor on the native Pi profile, the package plan includes the distribution's `imx500-all` firmware package and recommends a reboot. That firmware package does not enable AI inference in CamPiX. Follow the sensor manufacturer's supported instructions for drivers not supplied by the host distribution.

Terminal setup registers only observed capture devices with conservative starting profiles and preserves existing camera profiles. An empty inventory remains empty. Network URLs cannot be discovered from a board inventory: add those sources through the administrator's **Cameras** view. Stop other camera applications before starting CamPiX so that only one capture owner accesses a device. The `probe` inventory and available controls do not certify sensor operation or recording performance; `auto` encoding remains software `libx264`.

On Windows, setup and the **Scan** button ask the configured FFmpeg for its DirectShow device list. The cameras are not opened. Only Plug and Play video devices are registered automatically; virtual cameras and other software sources are reported and can be added manually. A unique device name becomes the source, so a USB camera keeps working on another port. Identical models fall back to their port-specific alternative names. Setup starts DirectShow cameras at 1280×720 and 10 fps. Each time a camera opens, CamPiX lists its native capture sizes without streaming. The settings view then offers only those sizes, and the API rejects other sizes with a list of supported ones, without interrupting the running capture. FFmpeg reduces the frame rate before scaling, because many webcams offer only fixed rates. Older profiles that match no native size are captured from the smallest covering mode, or from the largest one, and padded instead of distorted. A DirectShow stream that delivers no data for 10 seconds is stopped, so the capture loop reconnects instead of staying stalled. Decoded DirectShow and network frames cross a near-lossless local MJPEG pipe before CamPiX encodes them at the profile quality. CamPiX exposes no image controls for DirectShow cameras. If a camera stays offline, close other camera applications and allow desktop apps to use the camera in the Windows privacy settings.

The fully locked development and OCI environments use `uv.lock` and `requirements.lock`. The native Debian profile keeps libcamera, Picamera2, NumPy, Pillow, and OpenCV from the same distribution. The `.deb` builder vendors locked web/auth dependencies while obtaining camera-related ABI packages from APT. Each package is tied to its build architecture and Python minor version: do not interchange Bookworm/Trixie or amd64/arm64 builds.

## Installed paths and startup

| Profile        | Configuration and state                                                                                 | Startup                                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Native Debian  | `/etc/campix/campix.json`, `/var/lib/campix/`; optional binary `/usr/local/lib/campix/mediamtx-v1.21.2` | `campix.service`, starts now and at system boot under the `campix` account.                               |
| Portable Linux | `~/.local/share/campix/campix.json`, `~/.local/share/campix/data/`                                      | `~/.config/systemd/user/campix.service`, starts now and at user login. No automatic linger configuration. |
| macOS          | `~/.local/share/campix/campix.json`, `~/.local/share/campix/data/`                                      | `~/Library/LaunchAgents/org.campix.service.plist`, starts now and at GUI user login.                      |
| Windows        | `%LOCALAPPDATA%\CamPiX\campix.json`, `%LOCALAPPDATA%\CamPiX\data\`                                      | Task Scheduler task `CamPiX`, starts now and at login as the installing limited user.                     |

Portable releases live under `releases/<full-commit>/` inside the installation directory, with their own `.venv` and optional `tools/mediamtx` executable. Stable configuration and data remain outside the release directory. Portable services write `data/service.log`, rotating at 1 MiB with three retained backups. On Linux also inspect `journalctl --user -u campix`; native logs use `journalctl -u campix`.

Portable background startup ignores ambient `CAMPIX_` environment variables. Change the installed `campix.json` and restart the managed service for configuration changes. The native systemd service continues to support its explicit `/etc/campix/environment` file as described below.

The Windows task runs the windowless `pythonw.exe`. MediaMTX and FFmpeg children start without console windows and belong to a kill-on-close job, so they end with the service process, including after an abnormal exit. If Windows refuses that job, `service.log` records a warning and the service continues. Installer revision `c396dd0` lacked this behavior: it showed an empty console window titled with the managed `mediamtx.exe` path, and closing that window only restarted MediaMTX. Replace such an installation with a corrected revision instead of closing the window.

Short-lived Windows discovery workers also contain their native listing children. Discovery reports failure if Windows refuses this protection; it does not enumerate devices without child cleanup. Correct the host's process/job restrictions before retrying automatic discovery.

The portable installation also supplies `start-campix` or Windows `start-campix.ps1` for foreground diagnosis. Stop the background service first; two processes must not use the same data directory or capture device. Native services use `sudo systemctl stop campix`, portable Linux uses `systemctl --user stop campix`, macOS uses `launchctl bootout gui/$(id -u)/org.campix.service`, and Windows uses `Stop-ScheduledTask -TaskName CamPiX`. Task cancellation can interrupt recordings; preserve any `.partial` files and follow recovery guidance below.

## Existing installations and failed attempts

The all-in-one entrypoint is for a new installation. It preserves existing accounts, configuration, recordings, and legacy installations. An occupied installation/state target or existing native deployment is rejected with update guidance; rerunning it is not an unattended upgrade or migration. Keep the reported installation paths and failure stage when diagnosing an interrupted attempt.

Failures return nonzero, including dependency downloads, checksums, package operations, administrator setup, and startup checks. Newly installed system prerequisites are retained, since other applications may use them. Bootstrap source staging and partially prepared portable deployments can remain for inspection; the Python installer's temporary build directory is removed. No cleanup operation recursively deletes an arbitrary user-provided path. Account setup must succeed before a new service is activated; a foreign owner of an expected port is not treated as a working CamPiX service.

For an existing deployment, use the backup and update procedure below. Keep a private consistent database/media snapshot, configuration, and the previous application package before changing versions. No automated legacy migration, system-package rollback, or driver replacement is included. After an interrupted setup, inspect the installed configuration and retained state before using the matching installed executable's `setup` command. If a startup definition already exists, review and restart that definition rather than registering a second one. An unfinished portable `install.json` is diagnostic metadata, not a record of successful installation.

## Development and explicit demo mode

Use the [contributor setup](../CONTRIBUTING.md) for a locked development environment. `campixd init-admin` creates the first account; `campixd serve --demo` deliberately selects synthetic images. Omit `--demo` for real sources. Neither the installer nor normal service startup enables synthetic input automatically.

Direct development defaults to `.campix/`, or the path specified with `--data-dir` or `CAMPIX_DATA_DIR`. Use isolated temporary directories for tests. Global CLI options precede the subcommand, for example:

```powershell
.venv\Scripts\campixd.exe --data-dir C:\CamPiXData serve
```

On Linux, use `.venv/bin/campixd`. Use the native profile for Raspberry Pi CSI rather than mixing unrelated pip and distribution camera packages.

## Native Debian package for Raspberry Pi and Linux

The all-in-one installer performs the native package preparation and setup on suitable Debian-family hosts. For a manual build instead, provide `uv` and Python build tools on the target system or a matching build environment. Raspberry Pi OS needs `python3-picamera2`; that package is not necessarily available on other Debian/Linux distributions.

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

For a portable installation, add the equivalent fields to the existing `campix.json`, preserving its generated data and executable paths:

```json
{
  "public_origin": "https://camera.example.invalid",
  "secure_cookies": true,
  "trust_proxy": true
}
```

Replace the placeholder with the actual origin and restart the managed service. Forwarded headers are accepted only from loopback proxies. The CLI rejects public/LAN HTTP origins without secure cookies. The supplied loopback setup uses HTTP only locally. Internet exposure additionally needs a firewall, certificate operations, host patching, and a reviewed media-network configuration.

## Optional MediaMTX

```powershell
.venv\Scripts\python.exe scripts/fetch-mediamtx.py --output .tools\mediamtx.exe
.venv\Scripts\campixd.exe serve --demo --mediamtx-binary .tools\mediamtx.exe
```

The downloader verifies the SHA-256 checksum of pinned release v1.21.2, preserves its license, and refuses to overwrite an existing file. Managed MediaMTX uses loopback ports 8554, 8888, 8889, 9997, 9998, and ICE/TCP 8189. Those ports must be free. The CamPiX supervisor restarts an exited child process.

For services, use an absolute binary path or a configuration path resolvable from the service's working directory. LAN/WAN requires external MediaMTX with reachable ICE addresses and TURN where necessary. To switch an installer-managed setup to an external server, stop CamPiX and set `mediamtx_binary` to `null` in its installed JSON configuration before adding the external settings. These native service environment settings connect an external deployment:

```text
CAMPIX_MEDIAMTX_API=http://127.0.0.1:9997
CAMPIX_MEDIAMTX_RTSP=rtsp://127.0.0.1:8554
CAMPIX_MEDIAMTX_TOKEN=<private API/WHEP token, if required>
CAMPIX_MEDIAMTX_HLS_TOKEN=<private secret matching hlsCDNSecret>
```

For portable background startup, set `mediamtx_api`, `mediamtx_rtsp`, `mediamtx_token`, and `mediamtx_hls_token` in the installed `campix.json` instead of environment variables, and restart the managed service.

`mediamtx_api` must be an HTTP(S) origin, for example `http://127.0.0.1:9997` or `http://[::1]:9997`. Embedded credentials, API base paths, query strings, fragments, and invalid ports are rejected; keep authentication secrets in the token settings. An empty value disables the external API. The proxy uses the origin's host on port 8888 for HLS and 8889 for WHEP.

The authenticated proxy accepts at most 8 MiB per HLS response and 64 KiB per WHEP response, and limits the entire upstream exchange to 20 seconds. Configure external HLS segment sizes within that bound; the managed segment limit already stays within it. Upstreams must honor `Accept-Encoding: identity`; compressed responses are rejected before decoding to keep the memory bound effective. Oversized responses fail with a conflict and timeouts report media unavailability. Proxy streams are closed after success, failure, or cancellation.

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
- **Database backup:** `campixd --config /etc/campix/campix.json backup /secure/path/campix.sqlite3` produces a consistent SQLite backup without active sessions. For portable profiles, use the installed executable and configuration path listed above. The backup still contains password hashes and potentially private camera URLs. Use mode `0600`, external encryption, and restricted access. Back up media separately; stop the service during the database/media backup for a matching snapshot.
- **Restore:** stop the service and restore matching database and media backups into a new private data directory. Verify `campix` ownership and `0700`/`0600` permissions before starting. Never copy an old database over an active WAL database.
- **Account recovery:** run `sudo -u campix campixd --config /etc/campix/campix.json reset-password admin`. All sessions for that account are revoked. This requires existing access to the private data directory.
- **Updates:** back up first. Schema 1 migrates to 2; downgrading an unknown newer schema is blocked. The one-command installer does not update an existing deployment. Native updates use a reviewed matching `.deb` package; portable updates require review of the release environment and its startup paths. Package removal retains media and accounts. Roll back with a matching older database backup and application version.
- **Monitoring:** `/healthz` checks HTTP liveness. Use authenticated `/api/v1/system`, camera frame age, `scheduler_alive`, MediaMTX readiness, and `/metrics` for operational monitoring. A green HTTP check does not qualify an offline camera.

## Before production use

Complete the [release-readiness checklist](RELEASE-READINESS.md#remaining-production-gates) on the actual combination: CSI/UVC controls, resolution/FPS, thermal soak, disk-full behavior, disconnect/reconnect, service restart, TLS/origin, WebRTC ICE, backup restore, and permissions. Software tests do not replace this qualification.
