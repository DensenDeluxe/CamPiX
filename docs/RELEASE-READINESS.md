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

## Deep debugging and failure/recovery verification — 12 October 2026

This pass examined the existing service, process management, media paths, authentication, persistence, installer, and browser workflows. The initial Windows baseline recorded 188 passes and 2 skips. The final Python suite contains 66 additional cases, including reproduced failure paths and negative security cases; a green baseline alone did not reveal these defects.

Confirmed corrections:

- HTTP validation and configuration exceptions no longer echo credential inputs. Fixed model-validation guidance remains available, and arbitrary mapping keys and validator text are redacted.
- Login atomically rechecks the verified account before inserting a session, including against a separate SQLite connection used for password recovery. Pending logins and rehashes cannot undo password or role changes or session revocation.
- Camera profile replacement/removal refuses to relinquish ownership while an old worker remains alive. Retry succeeds after the driver releases it. Controls cannot overwrite a newer profile, and shutdown discards delayed frames.
- Motion analysis retains its previous frame between polls, including at low capture frame rates. Idle recording workers detect encoder exit without reporting successful media.
- Diagnostic output overruns terminate promptly, and inherited pipe writers cannot prolong cleanup. Windows discovery children end with their abruptly terminated worker; containment refusal prevents enumeration.
- Publisher shutdown clears its online state. IPv6 media origins work, configuration origins and WHEP session locations are validated, and malformed locations produce sanitized errors.
- Media proxy responses have size and total-time bounds, release their streams after cancellation/failure, omit GET bodies, and reject compressed upstream bodies before decoding.

Final local evidence:

| Environment                        | Result                 | Explicit skips                                                                                                     |
| ---------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Windows / Python 3.11.9            | 254 passed, 2 skipped  | Managed MediaMTX integration not configured; POSIX-only containment case.                                          |
| Ubuntu 24.04 / WSL / Python 3.12.3 | 242 passed, 14 skipped | Eight Windows-only cases, five FFmpeg integration cases (Linux FFmpeg unavailable), and one managed MediaMTX case. |
| Real Chromium browser              | 5 passed, 1 skipped    | Optional real WebRTC/HLS MediaMTX integration not configured.                                                      |

The running host deployment owned the fixed MediaMTX loopback ports. It was not stopped or altered to make optional integration tests run; the local binary's presence does not establish a fresh media-plane integration result. Browser checks preserved the existing DirectShow error-guidance assertion while verifying authentication, recording/playback, media management, schedules, mobile layout, localization, and downloads.

`uv lock --check`, JavaScript syntax, documentation links, formatting, and Ruff 0.17.0 passed. A wheel was built, compared against the current Python sources, installed into an isolated environment using hash-verified `requirements.lock`, and passed the packaged-assets/authentication/JPEG smoke check. An amd64 Debian package was built with Python 3.12 in isolated WSL tooling and passed the extracted-package smoke check. The package checks emitted the existing Starlette TestClient deprecation warning; dependency versions were not changed.

There is no schema or dependency migration. Existing media and database state remain compatible. Configurations with media API base paths, embedded credentials, or invalid origins must use a valid origin and separate token settings; external upstreams must honor identity encoding and the documented response limits. Source rollback does not require a database downgrade. No commit, publication, deployed-service replacement, new vulnerability audit, OCI build, target-native Raspberry Pi run, or physical-camera/disconnect/soak qualification was performed in this pass.

## DirectShow live latency correction — 12 October 2026

The reported symptom was a webcam live view that could fall minutes behind. Three software causes were reproduced: synchronous FIFO pipe consumption blocked behind image processing, FFmpeg's `fps` filter/output synchronization duplicated images across timestamp gaps, and the live publisher assigned frame-count timestamps even when actual capture ran slower. Two generated images 120 seconds apart produced 1,200 output JPEGs before the correction. Small timestamp jitter also reduced sampling cadence.

DirectShow now drains raw JPEG bytes independently into one latest-frame slot, samples clock buckets with a 5 ms boundary tolerance, and uses output passthrough/packet flushing. It does not invent higher native FPS or replay timestamp gaps. Cancelled discovery, initial-frame waiting, and mode retries cannot reopen a closed device. The complete-frame watchdog, bounded partial buffer, worker cleanup, overwritten-frame counter, and receipt-based frame age have regression coverage. The live publisher uses wall-clock variable-rate timing and immediate frame flushing; recording/timelapse frame-count timing is unchanged.

The browser's JPEG fallback makes one authenticated fetch/decode at a time, cancels stale generations, and shows connection/recovery status. It clears LIVE before switching from failed WebRTC and until a JPEG decodes. Tests cover stalled network requests, both stalled decoding stages, late image completion, camera changes, hidden tabs, logout/session expiry, and recovery. Browser test contexts reuse validated, test-only sessions per role so additional cases respect the existing login rate limit; production limits are unchanged.

Final checks:

| Environment                                              | Result                 | Explicit skips                                                                                                            |
| -------------------------------------------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Windows / Python 3.11.9 / FFmpeg 8.1.1 / MediaMTX 1.21.2 | 278 passed, 1 skipped  | POSIX-only containment case.                                                                                              |
| Ubuntu 24.04 / WSL / Python 3.12.3                       | 256 passed, 23 skipped | Eight Windows-only cases, fourteen FFmpeg cases because Linux FFmpeg was unavailable, and one unconfigured MediaMTX case. |
| Installed Chrome / real MediaMTX                         | 12 passed              | None; actual WebRTC frames and authenticated HLS were tested.                                                             |

Python and browser managed-media checks ran sequentially. Lock consistency, Ruff 0.17.0, JavaScript syntax, documentation links, and formatting passed. A wheel containing the current Python/web sources passed isolated installed-package assets/authentication/JPEG checks with hash-verified runtime dependencies. An amd64 Debian build and extracted-package smoke check also passed in WSL. No dependency locks or database schema changed.

A separate short physical check used the ACER FHD User Facing webcam on Windows build 10.0.26300, Python 3.11.9, and FFmpeg 8.1.1. Frames remained only in memory. The consumer intentionally paused 350 ms between images; pipe-reader skipping kept its handoff current:

| Profile                 | Duration | Observed pipe FPS | Consumer FPS | Overwritten JPEGs | Receipt-to-decode age, median / maximum | FFmpeg CPU share of host |
| ----------------------- | -------- | ----------------- | ------------ | ----------------- | --------------------------------------- | ------------------------ |
| 1280×720, target 10 FPS | 4.01 s   | 9.96              | 2.74         | 28                | 62 / 109 ms                             | 0.79%                    |
| 1280×720, target 30 FPS | 4.30 s   | 16.76             | 2.79         | 59                | 31.5 / 63 ms                            | 0.50%                    |

These ages start when a complete JPEG reaches CamPiX, not when the sensor exposes an image; they are not sensor-to-screen measurements. The 30 FPS profile did not deliver 30 FPS in this short check. Owned FFmpeg processes and reader/watch workers ended after each profile. No disconnect, long soak, other-camera, target Raspberry Pi, deployed browser latency, or hardware encoder qualification is inferred.

The installed release was not replaced, and no commit or publication was performed. The checkout correction and packaged artifact still require an explicit deployment update to affect that separate installation. No data migration is needed; rollback restores the previous source release and its former latency behavior.

## Live fullscreen controls — 12 October 2026

The existing fullscreen trigger was an ambiguous icon outside the fullscreen element, so it disappeared on entry. The live view now has localized entry and in-view exit buttons, Escape support, focus/state synchronization, and an expanded-window fallback when native fullscreen is unavailable or denied. Session expiry and view changes close fullscreen; late fullscreen requests cannot reopen an expired session's live view. Fullscreen transitions preserve the active stream.

The installed Chrome browser suite recorded **17 passes and no skips**, including five fullscreen regression cases and real MediaMTX v1.21.2 WebRTC/HLS integration. The WebRTC-to-JPEG fallback retains fullscreen. Media testing used a separate temporary loopback instance and ICE listener because the installed service occupied the managed ports; that deployment was not stopped or changed, and all test processes were cleaned up. Sources were explicitly synthetic. Mobile portrait/landscape layouts and unavailable/denied APIs were exercised in desktop Chrome, rather than on physical mobile devices.

Lock consistency, JavaScript syntax, documentation links, formatting, and diff checks passed. Python/service sources are unchanged; the Python suite and package builds were not rerun for this UI correction. Safari/native HLS fullscreen, physical mobile browsers, and an installed-release update remain unverified. No dependency, schema, commit, publication, or deployment change was made.

## Remaining production gates

1. Test at least Pi 4/CM4 and Pi 5/CM5 on their target operating systems with official and relevant Arducam/third-party sensors. Qualify UVC devices and at least one RTSP camera separately. Device discovery is not full functional certification.
2. Prioritize native encoding/passthrough, or deliberately limit profiles to measured portable software performance. Recorders and media publishers currently encode separately; there is no universal 1080p30, 4K, or Pi Zero performance promise.
3. Run 24–72-hour multi-client/multi-camera soak tests, measuring CPU, RAM, temperature, recording drops, and live latency. Force USB removal, network loss, and driver hangs; verify recovery.
4. Exercise the all-in-one installer on fresh target hosts for each supported profile, including package-source/codec availability, private state, secure setup, occupied ports, failed startup, reboot/login lifecycle, and recovery. Install `.deb` packages on matching OS/Python/architecture combinations and check services, device groups, and TLS. Validate OCI builds, data volumes, non-privileged USB access, and restore.
5. Verify the real TLS/origin/ICE/TURN configuration and protection of direct media ports. Define host/dependency patching and read-stream limits in the proxy.
6. Qualify backup restore on a clean device, power-loss and disk-full behavior, and private access to data and camera credentials.
7. Define the first production release's feature scope. Long-term AI, vendor, zero-copy, and legacy migration proposals are not completed by the status of this foundation.

This release must not be described as “all report ideas implemented” or “production-tested on every Raspberry Pi.”
