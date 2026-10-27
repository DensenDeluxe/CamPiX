---
name: campix-security
description: Change CamPiX authentication, roles, CSRF, sessions, media paths, exports, notifications, ONVIF, and share links without widening a trust boundary. Use when editing login, permissions, webhooks, proxies, validation errors, or any route that accepts camera URLs, filenames, or secrets.
---

# Change authentication and trust boundaries

Authorization is enforced on the server. Browser controls, hidden buttons, and a successful `/healthz` are not access control. Read [security](../../../docs/SECURITY.md) and the route's existing negative tests before editing. Add a regression that proves the forbidden caller still fails. Follow [campix-verify](../campix-verify/SKILL.md) when you run it.

## Roles

`viewer` can view and download. `operator` can also apply controls, record, lock or delete unlocked media, export, and create share links. `admin` can also manage cameras, schedules, users, storage, notifications, integrations, backup, and ONVIF. The API refuses to remove or demote the last active administrator.

An empty `viewer_users` list leaves the shared library. A non-empty list hides that camera, its media, and its events from other viewers. Operators and administrators still see every camera.

Home Assistant pairing creates at most eight viewer tokens. Only the SHA-256 digest is stored. The raw token is returned once. A bearer token skips cookie CSRF because a browser does not attach it automatically. A browser session still requires CSRF when a session cookie is present, even if a bearer header is also present. Revoking a token stops later requests and an open MJPEG stream.

## Sessions and writes

Passwords use Argon2id. Session ids are stored as hashes. Cookies are HttpOnly and SameSite=Strict. Login rechecks the account inside a write transaction before inserting a session. A password, role, or disabled-state change that lands during login rejects that login. Logout, disable, expiry, and role or password changes revoke access.

State-changing routes, including logout, camera commands, and WHEP session management, require a CSRF token plus Origin and Fetch-Site checks. WebSockets require a valid session and a matching origin. Login is limited by IP and username. Writes are limited by user. The API body limit is 64 KiB (`src/campix/app.py`).

HTTP 422 responses expose error types, vetted field locations, and fixed English messages. They withhold submitted values, validator context, arbitrary mapping keys, and arbitrary exception text. Configuration errors must not echo camera URLs, tokens, or passwords. Keep the existing tests in `tests/test_validation.py` passing when you change a model.

## Files, network, and proxies

`MediaStore.path` accepts only a direct child of the media root and rejects symlinks. SQL stays parameterized. Camera URLs, filenames, and other user input are never passed to a shell. There is no arbitrary macro, hook, or GStreamer pipeline.

Network camera sources are `rtsp`, `rtsps`, `http`, or `https` with a hostname. V4L2 sources match `/dev/videoN`. DirectShow names cannot contain quotes, colons, or control characters. Legacy sources are absolute JPEG paths. The `gstreamer` backend name is rejected.

Notification kinds that may leave the service are `motion.detected`, `camera.stalled`, `storage.reserve`, and `export.failed`. The webhook is HTTPS, or HTTP to loopback, with redirects disabled and link-local or metadata addresses rejected. Mail is SMTP to `127.0.0.1` only. The signing secret and webhook URL stay out of configuration export.

Managed MediaMTX stays on loopback. The HLS proxy limit is `HLS_PROXY_MAX_BYTES` (8 MiB) and the WHEP limit is `WHEP_PROXY_MAX_BYTES` (64 KiB), with a bounded upstream deadline. Proxy targets stay on the fixed media paths. Each camera allows at most four concurrent JPEG or MJPEG readers (`StreamSlots`); further readers get HTTP 429. WebRTC viewers do not take those slots.

Share links and integration tokens store digests. Raw secrets appear once in the response that creates them. Logs, screenshots, configuration exports, and test diagnostics stay redacted. ONVIF stores an Argon2 hash, accepts PasswordText or HTTP Basic, and rejects password digest. PTZ is limited to the host already present in that network camera's source URL.

Update [security](../../../docs/SECURITY.md) when a trust boundary, role, export omission, or proxy rule changes. Say plainly which checks you did not run. This skill does not replace a dependency audit or a review of a new deployment.
