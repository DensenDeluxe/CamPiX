# CamPiX 0.2.0 — release readiness

[Documentation index](README.md) · [Architecture](ARCHITECTURE.md) · [Deployment](DEPLOYMENT.md)

Evidence snapshot: 11 October 2026. **Integrated software release candidate; no blanket hardware production certification.** Results below are dated observations, not promises that every environment or future dependency version has been tested.

## Research-to-implementation matrix

| Area                                                          | Status                                                                                                                                       |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Standalone `campixd`, REST/OpenAPI, WebSocket, SQLite         | Implemented; API, authentication, and restart tests.                                                                                         |
| Roles, Argon2, CSRF, session expiry, rate limits              | Implemented; negative tests cover permissions, origins, and path boundaries.                                                                 |
| Picamera2 and V4L2                                            | Adapters implemented; no physical CSI/UVC combination locally certified.                                                                     |
| RTSP/HTTP network sources                                     | Decoder and reconnection implemented; individual IP camera models still need qualification.                                                  |
| Multiple cameras and dynamic controls                         | Implemented; physical pipeline and bandwidth limits not measured.                                                                            |
| JPEG, H.264/MP4, pre-/post-roll, timelapse                    | Real files and FFmpeg encoding; no native zero-copy pipeline.                                                                                |
| Motion                                                        | Independent frame-difference worker; no motion vectors, AI detection, masks, or ROI.                                                         |
| Scheduler                                                     | Persistent intervals, time windows, weekdays, and time zones; no solar, cron, or macro engine.                                               |
| MediaMTX                                                      | Pinned supervised process, RTSP publisher, WHEP/HLS proxy; external configuration required for LAN/WAN ICE.                                  |
| Media management                                              | Persistent index, playback/download, locking, retention, storage protection, and export jobs.                                                |
| Branding and modern web UI                                    | Supplied graphics, English/German localization, responsive layout, dark/light themes; vanilla JavaScript, not TypeScript/PWA/Web Components. |
| Debian/OCI/CI/systemd                                         | Build and operations files available; evidence below, no automatic publication.                                                              |
| Hardware encoding, UVC passthrough, DMABUF                    | Outstanding; `auto` uses software encoding and `hardware_verified` remains `false`.                                                          |
| AI/TFLite/Hailo/IMX500, vendor GStreamer                      | Not implemented; no simulated detections or arbitrary executable pipelines.                                                                  |
| ONVIF and full external hook/plugin system                    | Not implemented.                                                                                                                             |
| MMAL/FIFO compatibility                                       | Authenticated JPEG/status/`im`/`ca` HTTP facade only; no complete legacy daemon or macro import.                                             |
| Migration wizard, hardware test catalog, native Pi soak tests | Outstanding; an existing upstream installation is not overwritten in place.                                                                  |

## Verification evidence

`tests/test_service.py`, `tests/test_recovery.py`, and `tests/test_mediamtx.py` exercise the actual service with explicitly synthetic input images. Recordings are encoded with real FFmpeg and validated with `ffprobe`; timelapse, restart persistence, backup, retention, and storage failures are not UI mocks. Optional MediaMTX integration requires the pinned real binary.

`tests/browser/workflows.spec.mjs` exercises authentication, JPEG live view, capture, MP4 playback, the library, schedules, saved camera profiles, viewer permissions, logout, mobile layout, language/theme switching, and downloads in a real Chromium browser. Test accounts exist only in an isolated temporary data directory. Screenshots and traces are generated under `test-results/`.

Recorded on Windows/Python 3.11: **29 API/recovery/FFmpeg/MediaMTX tests passed**, including timed recording completion, actual pre-roll, motion/post-roll, encoder failure without false media entries, stale camera frames without false capture success, foreign owners of media ports, and bootstrap validation without exposing secrets. The complete browser suite with optional MediaMTX recorded **5 passing tests**, including actual WebRTC video frames and HLS. Managed MediaMTX tests use fixed loopback ports: run Python and browser suites sequentially.

The `pip-audit` check of fully pinned `requirements.lock` reported **no known vulnerabilities at the time of the audit**. Unknown vulnerabilities remain possible. Python linting and JavaScript syntax checks passed.

An amd64 Debian package was built in Ubuntu 24.04/WSL with **Python 3.12**. This is not Raspberry Pi arm64 certification. The Linux suite recorded **23 passes and 6 explicit FFmpeg/MediaMTX skips** because those tools were absent. Package smoke checks cover bundled web assets, authentication, and JPEG capture. Docker/OCI builds, deployed systemd/Nginx operation, and physical hardware remain unqualified on this Windows host. CI uses pinned actions and a digest-pinned default OCI base image. Remote verification is tracked in [GitHub Actions](https://github.com/DensenDeluxe/CamPiX/actions); assess the run for the exact commit rather than treating this local evidence as a remote result.

## Remaining production gates

1. Test at least Pi 4/CM4 and Pi 5/CM5 on their target operating systems with official and relevant Arducam/third-party sensors. Qualify UVC devices and at least one RTSP camera separately. Device discovery is not full functional certification.
2. Prioritize native encoding/passthrough, or deliberately limit profiles to measured portable software performance. Recorders and media publishers currently encode separately; there is no universal 1080p30, 4K, or Pi Zero performance promise.
3. Run 24–72-hour multi-client/multi-camera soak tests, measuring CPU, RAM, temperature, recording drops, and live latency. Force USB removal, network loss, and driver hangs; verify recovery.
4. Install `.deb` packages on matching OS/Python/architecture combinations and check services, device groups, and TLS. Validate OCI builds, data volumes, non-privileged USB access, and restore.
5. Verify the real TLS/origin/ICE/TURN configuration and protection of direct media ports. Define host/dependency patching and read-stream limits in the proxy.
6. Qualify backup restore on a clean device, power-loss and disk-full behavior, and private access to data and camera credentials.
7. Define the first production release's feature scope. Long-term AI, vendor, zero-copy, and legacy migration proposals are not completed by the status of this foundation.

This release must not be described as “all report ideas implemented” or “production-tested on every Raspberry Pi.”
