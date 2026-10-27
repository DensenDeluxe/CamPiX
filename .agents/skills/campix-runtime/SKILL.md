---
name: campix-runtime
description: Change CamPiX capture, motion, encoding, recording, live publishing, audio, scheduling, or child processes without blocking HTTP or hiding failure. Use when editing those workers, their tests, encoder selection, MediaMTX publishing, or a camera that stalls, drops frames, or records incorrectly.
---

# Change capture and background workers

HTTP handlers in `src/campix/app.py` schedule work. They do not own capture loops, motion analysis, encoding, recording, or publishing. `src/campix/runtime.py` owns each camera: a capture thread, a motion thread, and an encoder-preparation thread.

Read the worker and the test module that already covers it before editing. Reproduce a reported defect with a regression test that fails on the old behavior. Use `demo=True` or a stand-in backend in a temporary data directory. Follow [campix-verify](../campix-verify/SKILL.md) for the commands and the evidence wording.

## Ownership and failure

One physical device has one capture owner. Replacing or removing a profile waits until the old workers stop. If a driver is still blocked after the shutdown timeout, the API returns a conflict and keeps the profile and the worker. Control changes and profile updates share the manager lock. A frame that arrives after shutdown starts is discarded.

A camera is `online` only after it produces a real frame. Missing cameras, missing encoders, and MediaMTX failures stay visible in status and events. Previously saved synthetic profiles stay disabled unless `settings.demo` is true. The explicit demo camera id is `demo`. A synthetic frame, an encoder name from inventory, or `/healthz` is not hardware qualification. Leave `hardware_verified` false unless a recorded physical qualification says otherwise.

Failed opens use bounded retry backoff. New queues, ring buffers, subscriber buffers, subprocess output, and retry loops need a maximum size, a defined overflow behavior, cancellation, a timeout, and an owner that stops them on shutdown. A service restart must not replay a schedule backlog or leave child processes behind.

## Pictures, packets, and sound

JPEG capture keeps a latest-frame slot so encoder backpressure cannot stall capture. Motion runs on the latest analysis sample in its own worker, and only when motion is enabled. The score removes one global luminance offset, keeps a slow background, and honors a 16 by 9 ignore mask of 144 `0`/`1` characters plus up to eight normalized regions.

Each camera has one H.264 producer and a bounded packet hub. Recording and the live publisher remux with stream copy. `auto` may use an allowlisted hardware encoder only after a successful profile-specific packet probe, then tested software `libx264`. The allowlist is NVENC, QSV, AMF, VideoToolbox, VAAPI, and V4L2 M2M. An explicit encoder choice fails closed. Publishing trouble must not switch the encoder. A codec restart starts a new epoch and ends the recording. Read the current byte, access-unit, and pre-roll limits in the hub before changing them. The contract is [native encoding design](../../../docs/NATIVE-ENCODING-DESIGN.md).

Audio starts only when `audio_enabled` is true, as a second capture process. A recording that requested audio is indexed only after the video remux and the AAC mux both succeed. A device name becomes one validated FFmpeg argument.

Build FFmpeg and MediaMTX commands as argument lists in code. Camera URLs, device names, and filenames never go through a shell. GStreamer vendor pipelines stay rejected in `CameraConfig`. Short diagnostic processes use `src/campix/processes.py` with an output limit and a deadline.

## Scheduler

Schedules are persistent and claim a run before executing it. Pausing, renaming, or editing weekdays keeps the next run when the interval is unchanged. Monday is `0`. A new recording rule must be shorter than its interval. An older overlapping recording is skipped, without an error event, until that take should have finished. There is no solar, cron, or macro engine.

## Docs and tests

Behavior changes update the worker section of [architecture](../../../docs/ARCHITECTURE.md). Encoding changes also update [native encoding design](../../../docs/NATIVE-ENCODING-DESIGN.md). Extend the existing test module for that worker, including `tests/test_runtime.py`, `tests/test_runtime_concurrency.py`, `tests/test_recovery.py`, and `tests/test_service.py` when the failure crosses restart or media indexing. Managed MediaMTX tests use fixed loopback ports; run them apart from `npm test` when `CAMPIX_TEST_MEDIAMTX_BINARY` is set.
