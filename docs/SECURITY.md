# Security model

[Documentation index](README.md) · [Deployment](DEPLOYMENT.md) · [Release readiness](RELEASE-READINESS.md)

CamPiX does not replace a hardened operating system. Administrators define camera sources and installed adapters; untrusted internet users must not be allowed to supply camera or file paths.

## Implemented controls

- No default accounts or passwords; local bootstrap requires a password of at least twelve characters.
- Argon2id password hashes, random HttpOnly/SameSite=Strict session cookies, and hashed session IDs in SQLite. Expiry, logout, account disabling, and role/password changes revoke access.
- Login rechecks the verified account inside a write transaction before creating a session. Concurrent password, role, or disabled-state changes reject the pending login; stale rehashes cannot restore a replaced password.
- Viewers can view and download. Operators can also apply controls, record, lock/delete media, and export. Administrators can additionally manage cameras, schedules, users, and storage rules. The API prevents removing or demoting the last active administrator.
- CSRF tokens plus Origin/Fetch-Site checks protect writes, including logout, camera commands, and WHEP session management. WebSockets require a valid session and matching origin.
- Login limits by IP and username, write limits by user, bounded input models, and a 64 KiB maximum request body. API, OpenAPI, media, and metrics are not anonymously accessible.
- HTTP 422 validation responses expose only error types, vetted field locations, and fixed English messages. Submitted values, validator context, arbitrary mapping keys, and arbitrary exception text are withheld because they can contain credentials. Known fixed model-validation guidance remains available.
- Configuration validation hides submitted values in exception text, including rejected media origins and token settings, so terminal errors do not echo those credentials.
- A Content Security Policy without inline JavaScript/CSS, framing restrictions, `nosniff`, a restrictive referrer policy, and secure cookies/HSTS in the TLS profile. Forwarded headers are accepted only from explicitly trusted loopback proxies.
- Parameterized SQL, normalized media paths, no shell execution of camera sources or macros, no arbitrary GStreamer pipelines, fixed media-proxy targets, and restricted protocol paths.
- Media proxy responses are limited to 8 MiB for HLS and 64 KiB for WHEP, with a 20-second total upstream deadline. WHEP session locations must match the same restricted session paths accepted for subsequent requests.
- Private data files and secrets excluded from configuration exports. Network-source credentials still necessarily exist in the private database and local FFmpeg decoder process arguments.
- One active service per data directory, child-process cleanup on shutdown, bounded recording/pre-roll/export/event queues, and a storage reserve.

## Trust boundaries and remaining hardening

All roles currently share one media library. There is no tenant isolation or per-user private media ownership. Operators can permanently delete unlocked media. Audit events are operational records, not tamper-proof compliance logs.

Administrator-defined camera sources can contact private networks or open an existing local legacy JPEG file. Do not grant these rights to untrusted users. Operators must keep FFmpeg, libcamera, and camera drivers patched. Host users with data-directory access can read password hashes and camera credentials and reset an account locally; that access is outside the web authentication boundary.

Loopback MediaMTX trusts local processes. Do not forward RTSP, API, ICE, or HLS ports into an untrusted network without additional controls. WAN/TURN configuration and external protocol ACLs belong to deployment qualification.

CamPiX currently provides no MFA, SSO, untrusted-plugin sandbox, antivirus/media-parser sandbox, LUKS key management, or guaranteed denial-of-service protection against unlimited read streams. Use a reverse proxy with appropriate limits or a VPN where applicable.

## Release review and reporting

Re-audit dependencies and review security before publishing or exposing a deployment. The dependency audit recorded in [release readiness](RELEASE-READINESS.md#verification-evidence) found no known vulnerabilities at that time; it is not an evergreen guarantee. CI actions and the default OCI base image use immutable references. A workflow file alone is not evidence of a completed remote CI run.

There is no published dedicated CamPiX security-reporting address in this workspace. Arrange a private reporting channel with the project maintainer before publishing the repository. Do not post credentials, private camera URLs, authentication bypass details, or sensitive recordings in public issues.
