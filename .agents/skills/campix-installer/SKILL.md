---
name: campix-installer
description: Change the CamPiX installer, host profiles, package build, and first-run setup without touching an existing deployment or treating discovery as hardware qualification. Use when editing install.sh, install.ps1, packaging, provision, or installer tests.
---

# Change installation and first-run setup

Read [installer design](../../../docs/INSTALLER-DESIGN.md) and the affected section of [deployment](../../../docs/DEPLOYMENT.md) before editing. The contract there is the behavior to preserve. Update both documents when the contract changes. Follow [campix-verify](../campix-verify/SKILL.md) for commands that go beyond the installer tests.

## Entry points

`install.sh` and `install.ps1` prepare missing bootstrap tools and resolve `CAMPIX_REF` to one detached commit. They use a fresh source directory and do not replace an existing checkout. `install.sh` stays LF-only (`.gitattributes`). The Python plan is `scripts/install.py` and `scripts/install_startup.py`. First-run camera registration is `src/campix/provision.py`. Package metadata is under `packaging/`.

`--dry-run` performs no installation, download, privilege escalation, or application-state change. Root or elevated invocation is rejected. Only an explicit system-change command may elevate, and the service still runs unprivileged. The initial listener is loopback. There is no default account. `--non-interactive` cannot create a fresh deployment. Passwords are read without echo and are at least 12 characters. `campixd setup` and `init-admin` must not print a password.

## What setup is allowed to claim

Discovery lists devices. It does not open a capture stream and it does not qualify a camera. Registration uses a conservative profile at 10 fps: 1280×720 for DirectShow webcams and 640×480 for board-attached cameras. Existing profiles stay in place. Synthetic sources require explicit demo mode.

Unknown CSI sensors and vendor drivers are reported. The installer does not run vendor scripts, guess device-tree overlays, upgrade the distribution, or install GPU drivers. On the native Pi profile, an identified IMX500 may add the distribution package `imx500-all` and a reboot recommendation. That package does not enable AI inference. Native CSI keeps the distribution's libcamera, Picamera2, NumPy, Pillow, and OpenCV. Do not replace those with unrelated pip wheels. An amd64 package build is not an arm64 or Raspberry Pi release.

uv stays on the pinned version used by the installer, and optional MediaMTX stays on checksum-verified v1.21.2. Downloads that fail the checksum are rejected.

## Host state and tests

Tests in `tests/test_install.py`, `tests/test_install_startup.py`, and `tests/test_provision.py` use fixtures and temporary directories. They must not install drivers or activate the machine's systemd, LaunchAgent, or Task Scheduler. A portable service test may start a subprocess it owns and must stop it. Occupied ports fail the install. HTTP liveness has to belong to that service process. Existing accounts, configuration, media, and startup definitions are left intact. The installer does not recursively delete a user-selected path.

Wheel and `.deb` smoke checks, when the task includes packaging, use an isolated prefix. Report them separately from a target-host install and from any physical camera result. Rollback of an existing deployment needs the previous package plus a private database and media backup, and a schema backup if the schema changed. See [campix-persistence](../campix-persistence/SKILL.md).
