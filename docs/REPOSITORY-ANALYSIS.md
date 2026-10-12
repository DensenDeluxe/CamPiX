# Reference repository analysis

Snapshot: 11 October 2026 (Europe/Berlin)

Reference: [DensenDeluxe/RPi_Cam_Web_Interface_Rework](https://github.com/DensenDeluxe/RPi_Cam_Web_Interface_Rework)

## Repository topology

At the snapshot date, the remote exposed three branch heads and 765 reachable commits in total. These are historical observations, not a continuously refreshed branch inventory:

| Branch                        | Tip       | Reachable commits | Meaning                                                                     |
| ----------------------------- | --------- | ----------------: | --------------------------------------------------------------------------- |
| `master`                      | `b6d0726` |               764 | Public integration branch; merges the v7 rework and `rpicamd` work          |
| `claude/zealous-fermi-so7x0c` | `4946b4d` |               763 | Feature branch for the `rpicamd` camera daemon; fully contained in `master` |
| `claude/dazzling-cori-goqnno` | `62715a7` |               765 | One-commit Pinhole rewrite based on `master`; not merged                    |

The branch graph is linear at the recent end:

```text
master:                 ... ── 3c23ac0 ── c4f5d1b ── 4946b4d ── b6d0726
                                                                    ▲
zealous-fermi:         ... ── 3c23ac0 ── c4f5d1b ── 4946b4d ────────┘

dazzling-cori:          master ───────────────────────────────────── 62715a7
```

`master...zealous-fermi` has one commit on the `master` side (the merge commit). `master...dazzling-cori` has one commit on the `dazzling-cori` side. This matters: the current feature branch is not an alternative implementation with an independent history; it is a complete replacement commit layered on the existing product.

## Commit history in aggregate

The full history is the accumulated history of the original RPi Cam Web Interface and its community maintenance, not a recent greenfield project.

| Year | Commits |
| ---: | ------: |
| 2014 |      96 |
| 2015 |     345 |
| 2016 |      88 |
| 2017 |      92 |
| 2018 |      47 |
| 2019 |      37 |
| 2020 |      12 |
| 2021 |      26 |
| 2022 |      12 |
| 2024 |       1 |
| 2025 |       4 |
| 2026 |       5 |

The dominant author identities are Robert Tidey (`roberttidey` and `Robert Tidey`, 479 commits combined), Tanel Kurvits (78), Silvan Melchior (68 when the two author spellings are combined), and the current 2026 Claude/DensenDeluxe work. The exact author spelling varies through the original history, so author totals are useful for provenance but not for ownership decisions.

The subject history is strongly maintenance-oriented: installation and platform compatibility, bug fixes, camera daemon behavior, and incremental UI changes. The repository is therefore valuable as a compatibility and workflow reference, but it should not be treated as a clean target architecture.

## Three decisive recent commits

### `3c23ac0` — v7.0.0 web interface rework

This commit modernises the existing PHP/JS surface without breaking the underlying command protocol. It adds:

- responsive live view, gallery, schedule, motion and minimal views;
- light, dark and automatic themes;
- English and German translations;
- settings grouped into tabs with immediate apply and capability-aware hiding;
- keyboard/swipe media lightbox and bulk actions;
- server-side authorization and CSRF/origin protections;
- PHP 8 and Debian/web-server installer fixes.

The lesson for CamPiX is to preserve the proven operator workflow: live view, capture, recording, media, schedules and motion belong to one coherent appliance experience. The lesson not to copy is the coupling between presentation, PHP request handling, FIFO commands and legacy installation assumptions.

### `c4f5d1b` — v7.0.1 hardware compatibility

This commit turns the README and installer into a hardware compatibility layer. It distinguishes 32-bit userland from kernel architecture, handles Pi 400/500, Pi Zero variants, Pi 5, Compute Modules and non-Raspberry-Pi boards, and adds current presets including IMX477.

The important CamPiX design decision is to expose runtime capabilities rather than infer support from a board name. A capability record should answer which capture backend, controls, resolutions, encoders and transports are actually available.

### `4946b4d` — v7.1.0 `rpicamd`

This is the strongest technical bridge to CamPiX. `rpicamd` keeps the old FIFO, config, status, preview and media naming contracts while adding:

- Picamera2/libcamera for Raspberry Pi and compatible CSI cameras;
- V4L2 for USB/UVC and other Linux boards;
- a synthetic test backend;
- stills, video, pre-roll, timelapse, annotations and motion detection;
- autofocus and lens controls where available;
- capability-aware settings in the existing UI.

The daemon is a pragmatic compatibility adapter, not yet the final control plane. It is also explicitly described as new and not tested against every board/camera combination. CamPiX should retain this adapter strategy during migration, then move the contract behind a versioned API.

### `62715a7` — unmerged Pinhole rewrite

The `dazzling-cori` branch replaces the legacy tree in one large commit. It deletes the PHP/FIFO/installer surface, documentation, bundled binaries, screenshots, license file and `rpicamd`, and introduces a Python package with settings, SQLite, event bus, authentication, camera types/controls and media helpers under `src/pinhole/`.

That branch is useful as an architectural signal: the next generation wants a typed settings model, persistent storage, event-driven services and explicit camera abstractions. It is not a safe direct base for CamPiX because it currently removes the working product surface and changes the product name to Pinhole. CamPiX therefore uses the ideas, not the destructive migration shape.

## Architectural conclusions from the upstream analysis

These were migration recommendations, not a claim that every compatibility path already exists in CamPiX:

1. Keep the existing command/FIFO compatibility path available while the new API matures.
2. Make `libcamera/Picamera2` and `V4L2` first-class capture adapters; add network/GStreamer adapters later.
3. Model capabilities as data: sensor, controls, stream sizes, encoder acceleration, DMABUF, transport and multi-camera support.
4. Keep the media plane replaceable. MediaMTX is an integration candidate, not code to fork into the control plane.
5. Keep motion and AI outside the capture loop as plugins or event consumers.
6. Treat Pi 5 encoding separately: its H.264 path is software-oriented, unlike Pi 4 hardware H.264.
7. Preserve the legacy backend as an explicit compatibility mode instead of silently deleting it.
8. Keep security properties from v7: server-side authorization, CSRF protection, safe macro boundaries and secure defaults.

## What the local CamPiX implementation now provides

Version 0.2.0 has moved beyond the earlier static dashboard milestone. It provides an executable Python/FastAPI `campixd` service and an authenticated, buildless JavaScript interface using the supplied CamPiX graphics:

- Picamera2, V4L2, Windows DirectShow, network-source, and local legacy-JPEG adapters with independent capture workers;
- real JPEG capture, FFmpeg H.264/MP4 recording, bounded pre-roll, motion post-roll, and queued timelapse exports;
- persistent camera profiles, schedules, media, users, events, and sessions in SQLite;
- server-enforced roles, Argon2id passwords, CSRF protection, and revocable sessions;
- optional supervised MediaMTX integration with authenticated WHEP/HLS proxying;
- API, recovery, media-server, and browser integration tests, plus native/OCI packaging files.

The interface must be served by `campixd`, not opened directly from `dist/` or hosted by a static file server. Synthetic input is allowed only in explicit demo mode; it does not establish physical camera compatibility.

The network adapter is implemented; vendor GStreamer and AI/plugin paths remain future work. Legacy support is limited to an existing JPEG preview and a restricted authenticated HTTP facade, not complete FIFO/MMAL or macro migration. Python/FastAPI and vanilla JavaScript deliberately differ from the research report's proposed Rust/Go and TypeScript/PWA stack.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the current component boundaries and [RELEASE-READINESS.md](RELEASE-READINESS.md) for dated evidence and remaining production gates. This analysis records branch reachability, aggregate commit history, and decisive changes; it is not a claim that every line of every historical patch received a security audit.
