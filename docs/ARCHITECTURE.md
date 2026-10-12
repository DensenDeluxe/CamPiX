# CamPiX: implemented architecture

[Documentation index](README.md) · [Deployment](DEPLOYMENT.md) · [Release readiness](RELEASE-READINESS.md)

This document describes the implemented 0.2.0 service. The [research report](deep-research-report.md) describes a broader target architecture; it must not be read as a statement that every proposed component exists.

```text
Browser — REST / WebSocket / current JPEG / WHEP / HLS
   │ Session + CSRF + roles
   ▼
campixd (one process per data directory)
   ├── FastAPI: versioned API, static interface, authenticated media proxy
   ├── SQLite/WAL: users, sessions, cameras, schedules, media, events, export jobs
   ├── Per camera: capture thread → JPEG / bounded pre-roll / small analysis frames
   │    ├── Picamera2: distribution libcamera and dynamic sensor controls
   │    ├── V4L2: OpenCV capture and v4l2-ctl controls
   │    ├── DirectShow: isolated FFmpeg device input on Windows
   │    ├── Network: isolated FFmpeg decoder, RTSP/HTTP(S)
   │    └── Legacy: existing local JPEG preview (not an MMAL/FIFO driver)
   ├── Per camera: motion worker → event → recording / renewed post-roll
   ├── Per recording: bounded JPEG queue → FFmpeg → atomically finalized MP4
   ├── Scheduler: persistent claim-before-execute rules, no backlog replay
   ├── Export worker: bounded queue, timelapse, explicit job status
   └── Optional: MediaMTX supervisor → RTSP publisher → WebRTC/HLS
```

## Worker and encoding boundaries

Capture, motion analysis, and recording run outside HTTP request handling. A failed camera is reopened with bounded retry backoff. Recorders and publishers have independent FFmpeg processes; a slow encoder does not block the capture thread. The recorder drops and reports excess frames instead of consuming unbounded memory. Pre-roll and recording queues are each limited to 32 MiB per camera.

DirectShow has a separate raw-JPEG pipe reader and one pending complete frame. It continuously drains FFmpeg while image processing takes the latest frame, discarding overwritten images rather than replaying a FIFO backlog. Timestamp-based sampling reduces work before scaling; output passthrough prevents FFmpeg from inventing frames across clock gaps or above the native source rate. A watchdog requires complete frames, and permanent cancellation prevents mode retries from reopening a closed device. Capture frame age retains the pipe receipt time through processing; `capture_dropped_frames` counts overwritten JPEGs separately from recording drops. Pipe receipt age is not a measurement of sensor-to-screen latency.

The live RTSP publisher bounds known-codec probing, flushes each input frame, and assigns wall-clock timestamps with variable frame-rate output. It requests keyframes at one-second clock intervals when frames are available. Recording and timelapse timing remain separate and retain their frame-count behavior.

Motion analysis retains the last analyzed image between polls, including when capture delivers fewer than five frames per second. Recording workers check encoder exit even when no new frames arrive, so an encoder failure reaches a terminal error instead of leaving an idle recording active.

Profile replacement and removal wait for the old camera workers to stop. If a driver remains blocked after the shutdown timeout, the API reports a conflict and retains the profile and worker ownership; retry once the driver releases the device. Control changes and profile updates share the manager lock so an old control operation cannot overwrite a newer saved profile. A frame returned after shutdown begins is discarded.

Short-lived diagnostic processes read only available pipe bytes and enforce an output limit and deadline. An inherited pipe writer cannot keep cleanup waiting after the direct child exits. Windows discovery workers contain their native listing children so an outer discovery timeout also terminates those children.

The capture worker preserves matching DirectShow/network JPEG bytes without a Pillow decode/re-encode cycle. Motion decoding/resizing runs on the latest sample in the independent analysis worker, only when motion is enabled. DirectShow negotiates reported size/rate/format combinations and copies matching native MJPEG; it does not request invented sensor rates. The JPEG parser scans incoming bytes once, retaining marker-boundary overlap rather than repeatedly scanning a growing frame.

`auto` prefers a profile-probed FFmpeg hardware H.264 encoder and then tested software `libx264`. Allowlisted paths cover NVENC, QSV, AMF, VideoToolbox, VAAPI and V4L2 M2M. Inventory and profile preparation use bounded processes, output limits, cancellation, a finite cache and serialized probe slots. A dedicated encoder-preparation worker keeps this work outside HTTP requests and capture; JPEG remains available while preparation is pending or fails. Recorder and publisher consume immutable selections with centralized codec/device/pixel-format/low-delay settings. QSV and VideoToolbox explicitly prohibit hidden software implementations. Encoder usability is distinct from physical-camera qualification; `hardware_verified` is not awarded by synthetic probes.

Publishing uses an observable automatic retry with another encoder after a hardware session fails. Explicit choices retain their strict failure behavior. Recording failure retains the partial and reports an error; the encoder is not switched midway through a file. Direct Picamera2 H.264, compressed H.264 UVC passthrough, DMABUF/zero-copy, and a shared live-view/recording encoder remain unimplemented. See the [encoding design and acceptance contract](NATIVE-ENCODING-DESIGN.md).

## Capabilities, not assumed compatibility

A camera becomes `online` only after producing a real frame. Controls are exposed only when detected by the adapter. Missing cameras, missing encoders, and MediaMTX failures produce visible errors. The API reports measured FPS, frame age, and dropped recording frames. An installed encoder name is not proof of tested acceleration; `hardware_verified` remains `false` without qualification.

Synthetic capture is permitted only in explicit demo mode. Previously saved synthetic profiles are disabled outside that mode. A demo test never awards a physical board a tested status.

## Storage and restart behavior

JPEG/MP4 files are written to private temporary paths and enter the media index only after successful finalization. Deletion first renames a file as a journal step; a database failure restores it. Startup removes missing indexed entries while retaining interrupted or unindexed files. Automatic MP4 repair after power loss is not claimed.

SQLite access is protected by transactions and thread synchronization. Schema 1 migrates to schema 2; unknown newer schemas are rejected rather than downgraded. An operating-system lock prevents two active services from using the same data directory. Database backups exclude active sessions. Media files must be backed up separately.

## Media plane and trust boundary

Optional managed MediaMTX is pinned to v1.21.2 and binds API, RTSP, HLS, WebRTC, and ICE to loopback only. RTMP, SRT, and MoQ are disabled. CamPiX proxies authenticated reads; publishing stays internal. A private HLS CDN secret prevents browser media access from bypassing CamPiX authentication. Secrets and network-camera URLs are excluded from configuration exports.

LAN/WAN WebRTC requires an external, secured MediaMTX installation with reachable ICE/TURN addresses. The managed loopback server alone is not a WAN configuration. The frontend currently plays HLS only when the browser supports it natively; other browsers fall back to authenticated current-JPEG requests. The fallback has one fetch/decode in flight, a 2.5-second overall deadline, and a retry delay. Navigation, camera changes, visibility changes, and logout cancel pending work and release Blob URLs. LIVE appears only after decoding an image; failures clear it. The multipart MJPEG endpoint remains available for compatibility clients.

## Deliberate differences from the research report

The report proposes a long-term universal successor. Version 0.2.0 uses Python/FastAPI instead of Rust/Go and buildless JavaScript instead of TypeScript, Web Components, and a PWA. This shortens the Picamera2 integration path; it does not fulfill those specific language and frontend recommendations.

AI/Hailo/IMX500, vendor GStreamer, ONVIF, complete historical macros/FIFOs, modern external event hooks, and native zero-copy pipelines are not hidden behind simulated success indicators. They are explicitly listed as outstanding in [RELEASE-READINESS.md](RELEASE-READINESS.md).
