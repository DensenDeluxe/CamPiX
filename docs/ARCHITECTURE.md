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
   ├── Per camera: sole capture owner → JPEG preview / small analysis frames
   │    ├── Picamera2: native H.264 main / lores preview / dynamic controls
   │    ├── V4L2: matching H.264/MJPEG capture or OpenCV / v4l2-ctl controls
   │    ├── DirectShow: isolated FFmpeg device input on Windows
   │    ├── Network: isolated FFmpeg decoder, RTSP/HTTP(S)
   │    └── Legacy: existing local JPEG preview (not an MMAL/FIFO driver)
   ├── Per camera: motion worker → slow background, ignore mask → event → recording
   ├── Per camera: optional separate audio capture, off unless the profile enables it
   ├── Notification worker: fixed event catalog → signed webhook or local SMTP
   ├── Per camera: native H.264 or shared FFmpeg encoder → bounded packet hub
   │    ├── Recording subscriber → timestamped TS → copy remux → finalized MP4
   │    └── Live subscriber → timestamped TS → copy remux → MediaMTX
   ├── Scheduler: persistent claim-before-execute rules, no backlog replay
   ├── Export worker: bounded queue, timelapse, explicit job status
   └── Optional: MediaMTX supervisor → RTSP publisher → WebRTC/HLS
```

## Worker and encoding boundaries

Capture, motion analysis, encoding and recording run outside HTTP request handling. A failed camera is reopened with bounded retry backoff. One H.264 producer per camera feeds a bounded packet hub; recording and publishing use independent copy-only FFmpeg remux processes. JPEG capture has a latest-frame slot, so encoder backpressure cannot stall capture. Encoded pre-roll and each recording subscriber are bounded to 32 MiB, 600 access units and the configured pre-roll duration. A recording queue overrun fails visibly and retains its partial file; a live queue drops its queued GOP and rejoins a configured IDR. Consumers share immutable encoded bytes, not additional encode sessions.

DirectShow has a separate raw-JPEG pipe reader and one pending complete frame. It continuously drains FFmpeg while image processing takes the latest frame, discarding overwritten images rather than replaying a FIFO backlog. Timestamp-based sampling reduces work before scaling; output passthrough prevents FFmpeg from inventing frames across clock gaps or above the native source rate. A watchdog requires complete frames, and permanent cancellation prevents mode retries from reopening a closed device. Short Windows pacing waits use high-resolution sleep with bounded cancellation instead of rounding every frame to an Event wait tick. Capture frame age retains the pipe receipt time through processing; `capture_dropped_frames` counts overwritten JPEGs separately from recording drops. Pipe receipt age is not a measurement of sensor-to-screen latency.

The shared JPEG encoder assigns each output access unit its actual input receipt timestamp. Picamera2 supplies relative encoder timestamps anchored to the local monotonic clock; UVC copy capture carries the original FFmpeg PTS through a bounded MPEG-TS demuxer and anchors its relative clock locally. Neither anchor proves sensor-to-screen latency. A bounded MPEG-TS mux preserves those intervals, keyframe-aligned pre-roll and clock gaps when publishing or recording with stream copy. Slow source delivery no longer shortens a recording to the requested encoder-header rate. Timelapse retains its explicitly requested frame-count timing. Reordered B frames and changing decoder configuration are rejected; codec restarts create a new epoch and terminate recordings rather than joining incompatible streams.

Motion analysis keeps a slow background between polls, including when capture delivers fewer than five frames per second. Each score removes one global luminance offset before comparing pixels, so a full-frame exposure or IR change is not motion. Changed pixels are held back from that background; stable pixels adapt. A fixed 16×9 mask marks cells to ignore, and the existing normalized regions still limit the score. The background is rebuilt when the mask, regions, or pixel delta change, and it is not stored. Recording workers check encoder exit even when no new frames arrive, so an encoder failure reaches a terminal error instead of leaving an idle recording active.

Profile replacement and removal wait for the old camera workers to stop. If a driver remains blocked after the shutdown timeout, the API reports a conflict and retains the profile and worker ownership; retry once the driver releases the device. Control changes and profile updates share the manager lock so an old control operation cannot overwrite a newer saved profile. A frame returned after shutdown begins is discarded.

Short-lived diagnostic processes read only available pipe bytes and enforce an output limit and deadline. An inherited pipe writer cannot keep cleanup waiting after the direct child exits. Windows discovery workers contain their native listing children so an outer discovery timeout also terminates those children.

The capture worker preserves matching DirectShow/network JPEG bytes without a Pillow decode/re-encode cycle. Motion decoding/resizing runs on the latest sample in the independent analysis worker, only when motion is enabled. Up to eight normalized regions limit that score; with none set, the whole frame is scored except cells set in the ignore mask. DirectShow negotiates reported size/rate/format combinations and copies matching native MJPEG; it does not request invented sensor rates. The JPEG parser scans incoming bytes once, retaining marker-boundary overlap rather than repeatedly scanning a growing frame.

`auto` prefers a profile-probed FFmpeg hardware H.264 encoder and then tested software `libx264`. Allowlisted paths cover NVENC, QSV, AMF, VideoToolbox, VAAPI and V4L2 M2M. Inventory and profile preparation use bounded processes, output limits, cancellation, a finite cache and serialized probe slots. A dedicated encoder-preparation worker keeps this work outside HTTP requests and capture; JPEG remains available while preparation is pending or fails. The sole shared encoder consumes the immutable selection with centralized codec/device/pixel-format/low-delay settings; recorders and publishers copy its packets. QSV and VideoToolbox explicitly prohibit hidden software implementations. Encoder usability is distinct from physical-camera qualification; `hardware_verified` is not awarded by synthetic probes.

Once the encoded hub has published a keyframe, the JPEG pre-roll keeps only the newest preview frame. The encoded hub remains the recording pre-roll. A network source whose H.264 matches the profile is copied into that hub; other network codecs stay on the JPEG decoder. Host memory, a 512 MB or 1 GB ceiling, and a Pi 5 model string can reject a profile before capture starts. Those guards are budgets, not hardware certification. Temperature at 75 °C and 82 °C, or a non-demo load average of three and four times the CPU count, slows motion and, at the higher step, drops every other software-encoder input frame.

The shared encoder performs observable automatic fallback after a hardware decoder or encoder session fails. Explicit choices retain strict failure behavior. Publishing transport failures cannot change the encoder. A restart invalidates old subscribers and recording fails with its partial retained. Native Picamera2 uses one camera with YUV main video and a small preview/analysis stream; full-resolution photos execute on the capture worker through a bounded request queue. Reported, matching H.264 DirectShow/V4L2 modes (including nominal 24/30/60 FPS paired with 23.976/29.97/59.94 FPS delivery) use one compressed capture owner and a separate bounded preview decoder. DMABUF is reported only for Picamera2's actual V4L2 encoder. Probed NVDEC/NVENC, QSV or VAAPI compressed-JPEG paths can retain decoded frames in hardware; strict pixel formats prohibit hidden hardware downloads or software conversion. Compressed packets, previews, photos and container buffers remain outside that raw-frame zero-copy scope. See the [encoding design and acceptance contract](NATIVE-ENCODING-DESIGN.md).

## Capabilities, not assumed compatibility

A camera becomes `online` only after producing a real frame. Controls are exposed only when detected by the adapter. Missing cameras, missing encoders, and MediaMTX failures produce visible errors. The API reports measured FPS, frame age, and dropped recording frames. An installed encoder name is not proof of tested acceleration; `hardware_verified` remains `false` without qualification.

Synthetic capture is permitted only in explicit demo mode. Previously saved synthetic profiles are disabled outside that mode. A demo test never awards a physical board a tested status.

## Storage and restart behavior

JPEG/MP4 files are written to private temporary paths and enter the media index only after successful finalization. Deletion first renames a file as a journal step; a database failure restores it. Startup removes missing indexed entries while retaining interrupted or unindexed files. Automatic MP4 repair after power loss is not claimed.

The media index is listed newest first and can be filtered by kind, camera, and a literal filename substring. `%`, `_`, and `\` in that substring match themselves. The library view searches the whole index; pagination does not hide older matches. The overview keeps the three newest items independent of the library page, kind, or search. A library timelapse exports every matching snapshot, at most 500, in capture order.

SQLite access is protected by transactions and thread synchronization. Schema 1 and 2 migrate to schema 3, which adds the notification outbox. Unknown newer schemas are rejected rather than downgraded. Roll back this release with a database backup taken before the migration; an older release will not open a schema 3 file. An operating-system lock prevents two active services from using the same data directory. Database backups exclude active sessions. Media files must be backed up separately.

Audio is a second capture process, started only when `audio_enabled` is true. Synthetic profiles use a generated tone. DirectShow and ALSA use a validated device name as one argument. Network profiles read audio from the same camera URL with a fixed argument list. The live view can play short WAV chunks from that PCM stream. A recording that requested audio is indexed only after the video remux and the AAC mux both succeed; a failed audio take stays unindexed. Profiles with audio off do not start the process and stay video-only.

The overview can show every camera as a current JPEG tile. That wall does not open WebRTC or HLS. Choosing a tile returns to the single live view.

Notifications deliver only `motion.detected`, `camera.stalled`, `storage.reserve`, and `export.failed`. The payload is a fixed JSON document signed with HMAC-SHA256. The webhook is HTTPS, or HTTP to loopback, with redirects disabled and link-local or metadata addresses rejected. Mail is SMTP to `127.0.0.1` only. The signing secret and webhook URL are omitted from configuration export. There is no shell, macro, or caller-supplied command.

## Media plane and trust boundary

Optional managed MediaMTX is pinned to v1.21.2 and binds API, RTSP, HLS, WebRTC, and ICE to loopback only. RTMP, SRT, and MoQ are disabled. CamPiX proxies authenticated reads; publishing stays internal. A private HLS CDN secret prevents browser media access from bypassing CamPiX authentication. Secrets and network-camera URLs are excluded from configuration exports.

LAN/WAN WebRTC requires an external, secured MediaMTX installation with reachable ICE/TURN addresses. The managed loopback server alone is not a WAN configuration. The frontend currently plays HLS only when the browser supports it natively; other browsers fall back to authenticated current-JPEG requests. The fallback has one fetch/decode in flight, a 2.5-second overall deadline, and a retry delay. Navigation, camera changes, visibility changes, and logout cancel pending work and release Blob URLs. LIVE appears only after decoding an image; failures clear it. The multipart MJPEG endpoint remains available for compatibility clients.

## Deliberate differences from the research report

The report proposes a long-term universal successor. Version 0.2.0 uses Python/FastAPI instead of Rust/Go and buildless JavaScript instead of TypeScript, Web Components, and a PWA. This shortens the Picamera2 integration path; it does not fulfill those specific language and frontend recommendations.

AI/Hailo/IMX500, vendor GStreamer, ONVIF, complete historical macros/FIFOs, modern external event hooks, and additional vendor-specific native pipelines are not hidden behind simulated success indicators. They are explicitly listed as outstanding in [RELEASE-READINESS.md](RELEASE-READINESS.md).
