# Hardware encoding and capture throughput

This implementation addresses the shared capture bottleneck and introduces usable hardware H.264 encoders. A requested resolution/frame rate is a target; camera modes, driver delivery, encoder capacity, and browser playback are measured separately. A synthetic 4K60 encode cannot qualify a physical camera's 4K60 capture or sensor-to-screen latency.

## Implementation boundaries

- Keep one owner of each physical camera. Preserve DirectShow/network JPEG packets when they already match the profile, instead of decoding and encoding every frame again in Python. The independent motion worker decodes only its latest analysis sample.
- Discover allowlisted FFmpeg H.264 encoders, then perform bounded packet-producing probes. Probe the requested profile in a background camera worker before advertising a selected encoder. Listing an encoder is insufficient evidence that its driver/device can run.
- Automatic selection prefers an encoder that successfully initializes on this host/profile, then software `libx264`. Explicit encoder choices fail visibly when unavailable. Report compiled availability, probe usability, selected codec, and fallback reason separately from physical hardware qualification.
- Use centralized low-latency arguments for publishing and recordings. Preserve wall-clock live timestamps, recording frame-count timing, bounded queues, process cancellation, finalization, interrupted files, and existing pre-roll behavior.
- This copied-frame implementation uses separate publisher and recorder sessions. Native CSI encoded callbacks, compressed-camera passthrough, encoded fanout and DMABUF/zero-copy remain separate research work; they must not be claimed by enabling a GPU codec.
- Preserve all server authorization, camera/source/path bounds and subprocess argument lists. Encoder/device selection is controlled by trusted code; no arbitrary command or filter entry points are introduced.

## Acceptance and rollback

Regression coverage must include missing drivers, malformed inventory, bounded probe timeout/output, failed profile initialization, software fallback, strict explicit selection, capture/analysis isolation, dropped/stale frames, cancellation, encoder failure and failed recording finalization. Validate actual FFmpeg output with `ffprobe`, real authenticated WebRTC/HLS browser decoding, and relevant complete runtime/security suites. Record synthetic throughput separately from camera measurements and document unavailable platforms/tools.

Existing software-only profiles retain an explicit `libx264` choice. No schema or dependency migration is required. Before rollback to an older release, save profiles using `auto` or `libx264`, because older validators cannot parse newly introduced encoder names. Preserve recordings and interrupted files, and do not deploy or replace the active installation as part of source verification.

The implementation uses the installed FFmpeg's encoder options and [NVIDIA's FFmpeg guidance](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/ffmpeg-with-nvidia-gpu/index.html). [FFmpeg's hardware-device options](https://ffmpeg.org/ffmpeg.html#Advanced-options) define QSV hardware selection. Native CSI integration remains distinct: [Picamera2's encoder exports](https://github.com/raspberrypi/picamera2/blob/main/picamera2/encoders/__init__.py) select different implementations by platform, so an `H264Encoder` name alone is insufficient evidence of hardware encoding.
