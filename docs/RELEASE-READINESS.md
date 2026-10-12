# CamPiX 0.2.0 — release readiness

[Documentation index](README.md) · [Architecture](ARCHITECTURE.md) · [Deployment](DEPLOYMENT.md)

Evidence snapshot: 11 October 2026. **Integrated software release candidate; no blanket hardware production certification.** Results below are dated observations, not promises that every environment or future dependency version has been tested.

## Research-to-implementation matrix

| Area                                                          | Status                                                                                                                                                                                |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Standalone `campixd`, REST/OpenAPI, WebSocket, SQLite         | Implemented; API, authentication, and restart tests.                                                                                                                                  |
| Roles, Argon2, CSRF, session expiry, rate limits              | Implemented; negative tests cover permissions, origins, and path boundaries.                                                                                                          |
| Picamera2 and V4L2                                            | Adapters implemented; no physical CSI/UVC combination locally certified.                                                                                                              |
| Windows DirectShow webcams                                    | FFmpeg adapter and discovery implemented; one integrated USB webcam measured on one Windows 11 host, without disconnect or soak tests.                                                |
| RTSP/HTTP network sources                                     | Decoder and reconnection implemented; individual IP camera models still need qualification.                                                                                           |
| Multiple cameras and dynamic controls                         | Implemented; physical pipeline and bandwidth limits not measured.                                                                                                                     |
| JPEG, H.264/MP4, pre-/post-roll, timelapse                    | Real files and FFmpeg encoding; no native zero-copy pipeline.                                                                                                                         |
| Motion                                                        | Independent frame-difference worker; no motion vectors, AI detection, masks, or ROI.                                                                                                  |
| Scheduler                                                     | Persistent intervals, time windows, weekdays, and time zones; no solar, cron, or macro engine.                                                                                        |
| MediaMTX                                                      | Pinned supervised process, RTSP publisher, WHEP/HLS proxy; external configuration required for LAN/WAN ICE.                                                                           |
| Media management                                              | Persistent index, playback/download, locking, retention, storage protection, and export jobs.                                                                                         |
| Branding and modern web UI                                    | Supplied graphics, English/German localization, responsive layout, dark/light themes; vanilla JavaScript, not TypeScript/PWA/Web Components.                                          |
| Debian/OCI/CI/systemd                                         | Build and operations files available; evidence below, no automatic publication.                                                                                                       |
| All-in-one installation                                       | Unix/PowerShell bootstraps, host inventory, native/portable profiles, secure terminal setup, and managed startup implemented; target-host and hardware qualification remain separate. |
| Hardware encoding, UVC passthrough, DMABUF                    | Outstanding; `auto` uses software encoding and `hardware_verified` remains `false`.                                                                                                   |
| AI/TFLite/Hailo/IMX500, vendor GStreamer                      | Not implemented; no simulated detections or arbitrary executable pipelines.                                                                                                           |
| ONVIF and full external hook/plugin system                    | Not implemented.                                                                                                                                                                      |
| MMAL/FIFO compatibility                                       | Authenticated JPEG/status/`im`/`ca` HTTP facade only; no complete legacy daemon or macro import.                                                                                      |
| Migration wizard, hardware test catalog, native Pi soak tests | Outstanding; an existing upstream installation is not overwritten in place.                                                                                                           |

## Verification evidence

`tests/test_service.py`, `tests/test_recovery.py`, and `tests/test_mediamtx.py` exercise the actual service with explicitly synthetic input images. Recordings are encoded with real FFmpeg and validated with `ffprobe`; timelapse, restart persistence, backup, retention, and storage failures are not UI mocks. Optional MediaMTX integration requires the pinned real binary.

`tests/browser/workflows.spec.mjs` exercises authentication, JPEG live view, capture, MP4 playback, the library, schedules, saved camera profiles, viewer permissions, logout, mobile layout, language/theme switching, and downloads in a real Chromium browser. Test accounts exist only in an isolated temporary data directory. Screenshots and traces are generated under `test-results/`.

Recorded on Windows/Python 3.11: **29 API/recovery/FFmpeg/MediaMTX tests passed**, including timed recording completion, actual pre-roll, motion/post-roll, encoder failure without false media entries, stale camera frames without false capture success, foreign owners of media ports, and bootstrap validation without exposing secrets. The complete browser suite with optional MediaMTX recorded **5 passing tests**, including actual WebRTC video frames and HLS. Managed MediaMTX tests use fixed loopback ports: run Python and browser suites sequentially.

The `pip-audit` check of fully pinned `requirements.lock` reported **no known vulnerabilities at the time of the audit**. Unknown vulnerabilities remain possible. Python linting and JavaScript syntax checks passed.

An amd64 Debian package was built in Ubuntu 24.04/WSL with **Python 3.12**. This is not Raspberry Pi arm64 certification. The Linux suite recorded **23 passes and 6 explicit FFmpeg/MediaMTX skips** because those tools were absent. Package smoke checks cover bundled web assets, authentication, and JPEG capture. Docker/OCI builds, deployed systemd/Nginx operation, and physical hardware remain unqualified on this Windows host. CI uses pinned actions and a digest-pinned default OCI base image. Remote verification is tracked in [GitHub Actions](https://github.com/DensenDeluxe/CamPiX/actions); assess the run for the exact commit rather than treating this local evidence as a remote result.

## All-in-one installer verification — 12 October 2026

This installer evidence adds to the historical 11 October snapshot above. The Windows/Python 3.11 suite recorded **90 passing tests**, with real FFmpeg and checksum-verified MediaMTX v1.21.2. The Ubuntu 24.04/WSL Python 3.12 suite recorded **82 passes and 8 explicit skips**: six FFmpeg/MediaMTX integration cases because those tools were unavailable there, and two Windows-only PowerShell cases.

`tests/test_install.py`, `tests/test_install_startup.py`, and `tests/test_provision.py` cover host/profile selection, pinned download and archive rejection, failed commands, existing-state protection, secure administrator setup, multi-camera deduplication, metadata/non-capture node exclusion, bounded registration, occupied ports, startup definitions, and service ownership checks. Package-manager and startup registration tests use controlled fixtures; they do not install drivers or activate the host's systemd, LaunchAgent, or Task Scheduler deployment. An actual portable service subprocess check verifies private log output, installed configuration taking precedence over ambient `CAMPIX_` variables, and HTTP liveness tied to its service process. Windows-specific PowerShell checks exercise literal argument handling and ownership of the virtual environment's Python child process without changing the host execution policy.

A target-native amd64 `.deb` was built in WSL and its extracted-package smoke check passed for bundled web assets, authentication, and JPEG capture. A wheel was built and installed into an isolated temporary environment using hash-verified `requirements.lock`; its installed-package assets/authentication/JPEG smoke check passed. The real Chromium browser suite recorded **5 passing tests**, including WebRTC video frames and HLS with the verified MediaMTX binary. Python and browser MediaMTX checks ran sequentially.

Lock consistency, JavaScript syntax, local documentation links, and formatting checks passed. Python lint passed with Ruff 0.17.0. The dependency locks were not regenerated for this installer change; these checks are not a new vulnerability audit.

These are build, installed-package, and software integration results, rather than an installed systemd service or Raspberry Pi qualification. The new entrypoints are local repository changes; no publication, target-host installer rollout, or physical-camera qualification is recorded here.

## Windows service and webcam verification — 12 October 2026

The host was Windows 11 Home 10.0.26300 on a 24-thread Intel CPU, with Windows Terminal as the default terminal and FFmpeg 8.1.1 from WinGet. It ran a portable installation of revision `c396dd0`, which showed two problems.

- **Console window.** An empty console window was titled with the managed `mediamtx.exe` path. Each time the window was closed, MediaMTX terminated and a `media_plane.restart` event followed. The windowless `pythonw.exe` service had started its console children without suppressing their windows.
- **Orphaned children.** A controlled check showed that a child process outlived an abruptly terminated Python parent, both through the virtual environment's launcher and through the base interpreter. A hidden MediaMTX could therefore keep the managed ports after a crash and block the next start.

That revision also ignored the built-in webcam, because local discovery and capture existed only for Linux. A manually added V4L2 profile could only fail on Windows.

The service now starts children without console windows and puts them in a kill-on-close job with the service. The `httpx` logger writes only warnings, instead of one MediaMTX poll line about every two seconds. Windows discovery reads FFmpeg's DirectShow list, and a DirectShow adapter captures through FFmpeg.

The Windows/Python 3.11.9 suite recorded **123 passes and 2 explicit skips**:

- The managed MediaMTX integration case was not run, because the host's own CamPiX installation occupied its fixed ports.
- The other skip is the POSIX-only containment case.

Against the previous sources, 32 of the new tests failed. The two that passed exercise Windows behavior of the new process module itself. An isolated windowless `pythonw.exe` service using the explicit synthetic source started FFmpeg for the encoder probe and for a recording. A 100 ms scan found no new console window. After the service interpreter was terminated abruptly, no FFmpeg process remained.

The physical camera was an integrated ACER FHD User Facing webcam (USB 04F2:B831). DirectShow offered NV12 and YUY2 at 640×360, 640×480, 1280×720, and 1920×1080, all at a fixed 30 fps; the default mode was raw 1920×1080. Discovery listed exactly this camera without opening it; the IR sensor used for face sign-in is not exposed through DirectShow. Capture results:

| Profile  | Mode                                      | Frame rate                            | FFmpeg CPU (share of host) |
| -------- | ----------------------------------------- | ------------------------------------- | -------------------------- |
| 640×480  | Native                                    | 10.2 fps                              | 0.1 %                      |
| 1280×720 | Native                                    | 10.3 fps                              | 0.4 %                      |
| 800×600  | Fallback: padded default mode, not native | 7.4 fps, window included stream start | 1.1 %                      |

In a deployment-shaped check, `setup` registered the camera as a 640×480 `dshow` profile. The windowless service brought it online at 9.3 fps against a 10 fps target. It produced a 640×480 snapshot and a 5-second H.264 640×480 recording, verified with `ffprobe`. No console window appeared. After an abrupt service termination, both FFmpeg processes ended: capture and recording. The test media was deleted afterwards.

The Chromium suite recorded **5 passes**, including DirectShow device-name validation through the dialog. Its optional MediaMTX case was skipped for the same port reason. Lock consistency, JavaScript syntax, local documentation links, and formatting checks passed. Ruff 0.17.0 reported no findings.

These results cover one webcam on one host. They are not recorded here:

- Disconnect and reconnect, soak, and privacy-denied behavior
- Other webcams and virtual cameras
- MediaMTX publishing of the webcam
- Linux/macOS runs of the new tests
- A replacement of the installed `c396dd0` deployment

## Windows webcam resolution correction — 12 October 2026

On the same host, the `ed2f01a` installation replaced `c396dd0`. Stopping the old task with `Stop-ScheduledTask` had left its managed `mediamtx.exe` running, holding the managed ports. The new task left no CamPiX or FFmpeg process behind.

The webcam came online with the 640×480 setup profile. The settings view offered sizes up to 3840×2160 regardless of the camera. After 3840×2160 was selected for this 1080p camera, two failures followed:

- **Upscaling and stall.** The backend upscaled every raw 1920×1080 frame at 30 fps to 4K before reducing the rate. Frame delivery stopped once the browser opened the 4K stream, and a `camera.stalled` event followed.
- **No recovery.** After a switch to 1280×720, the new FFmpeg process waited without CPU use for almost five minutes. It delivered frames only then, because nothing restarted the stalled DirectShow input.

The MJPEG pipe between FFmpeg and CamPiX also used FFmpeg's default rate control: about q=25 at 10 fps, compared with q=3 at `-q:v 3`.

Isolated runs at 4K, with and without managed MediaMTX publishing, delivered about 9 fps without a stall. The stall therefore was not reproduced on demand; the watchdog addresses the missing recovery rather than one specific trigger.

The changes:

- CamPiX now lists native capture sizes when a camera opens. The settings view offers only those sizes, and the API rejects other sizes with HTTP 409 without touching the running capture.
- FFmpeg reduces the frame rate before scaling.
- A stream that delivers no data for 10 seconds is stopped, so the existing reconnect applies.
- The pipe keeps frames near-lossless.
- Setup starts DirectShow cameras at 1280×720.

The Windows/Python 3.11.9 suite recorded **134 passes and 2 explicit skips**. With the host's service stopped and MediaMTX v1.21.2 configured, it recorded **135 passes and 1 POSIX-only skip**. The Chromium suite then recorded **6 passes**, including WebRTC video and HLS. Against `ed2f01a`, 15 of the new or changed DirectShow tests failed.

On the ACER FHD User Facing webcam, in a windowless service:

| Check                         | Result                                                                                       |
| ----------------------------- | -------------------------------------------------------------------------------------------- |
| Setup profile                 | 1280×720 at 10 fps                                                                           |
| Reported native sizes         | 640×360, 640×480, 1280×720, 1920×1080                                                        |
| Chromium resolution menu      | Exactly those four sizes                                                                     |
| 3840×2160 requested           | HTTP 409 with the supported list; the camera stayed online with frame ages of 0.11 s or less |
| 1920×1080 requested           | Online at 9.5 fps; FFmpeg used 1.3 % of host CPU                                             |
| Console windows and processes | No new visible window; no CamPiX or FFmpeg process remained                                  |

A forced driver stall, disconnect recovery on real hardware, and the rebuilt task installation are not recorded here.

## Remaining production gates

1. Test at least Pi 4/CM4 and Pi 5/CM5 on their target operating systems with official and relevant Arducam/third-party sensors. Qualify UVC devices and at least one RTSP camera separately. Device discovery is not full functional certification.
2. Prioritize native encoding/passthrough, or deliberately limit profiles to measured portable software performance. Recorders and media publishers currently encode separately; there is no universal 1080p30, 4K, or Pi Zero performance promise.
3. Run 24–72-hour multi-client/multi-camera soak tests, measuring CPU, RAM, temperature, recording drops, and live latency. Force USB removal, network loss, and driver hangs; verify recovery.
4. Exercise the all-in-one installer on fresh target hosts for each supported profile, including package-source/codec availability, private state, secure setup, occupied ports, failed startup, reboot/login lifecycle, and recovery. Install `.deb` packages on matching OS/Python/architecture combinations and check services, device groups, and TLS. Validate OCI builds, data volumes, non-privileged USB access, and restore.
5. Verify the real TLS/origin/ICE/TURN configuration and protection of direct media ports. Define host/dependency patching and read-stream limits in the proxy.
6. Qualify backup restore on a clean device, power-loss and disk-full behavior, and private access to data and camera credentials.
7. Define the first production release's feature scope. Long-term AI, vendor, zero-copy, and legacy migration proposals are not completed by the status of this foundation.

This release must not be described as “all report ideas implemented” or “production-tested on every Raspberry Pi.”
