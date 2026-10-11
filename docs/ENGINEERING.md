# CamPiX engineering standard

[Documentation index](README.md) · [Contributing](../CONTRIBUTING.md) · [Architecture](ARCHITECTURE.md) · [Release readiness](RELEASE-READINESS.md)

These requirements govern implementation, review, and release work. They define a verifiable quality bar; instructions alone cannot certify software security or physical hardware. The repository-wide contract is in [AGENTS.md](../AGENTS.md), with a shared import entry point in [CLAUDE.md](../CLAUDE.md).

## Change discipline

- Establish the requested outcome and affected boundaries before editing. Investigate existing implementation and relevant regression tests, not just an isolated symptom.
- Keep changes cohesive and reviewable. Avoid unrelated refactors, unnecessary dependencies, speculative abstractions, or changes to the user's environment.
- For cross-cutting changes, record the intended behavior, compatibility impact, failure cases, acceptance tests, and rollback before implementation. Escalate decisions that require new authority or materially expand scope.
- Use explicit names, small cohesive functions, typed public interfaces, strict input models, and narrow exceptions. Add comments for non-obvious invariants and trade-offs, not restatements of code.
- Avoid magic success values, hidden global state, duplicated business rules, and silent fallback that conceals failure. Validate configuration at its boundary and expose actionable errors without revealing secrets.

## Architecture and concurrency

- Keep the control plane separate from capture, analysis, encoding, and media transport. HTTP requests must not own camera capture loops or block on unlimited background work.
- Bound every queue, ring buffer, exported frame set, subprocess output, and retry loop. Define overload behavior, cancellation, timeouts, shutdown, and resource ownership.
- Keep motion/AI analysis independent of capture. Optional backends must not make the base application depend on unavailable host hardware or proprietary libraries.
- Do not create concurrent capture owners for one physical device. Measure multi-camera bandwidth, memory, thermal limits, frame drops, and live latency before assigning supported profiles.
- Service restart must not replay a schedule backlog or leak child processes. Background jobs must have truthful terminal states and restart behavior.

## Persistence and media integrity

- Use parameterized SQL and explicit transactions; preserve thread-safety and a single active service per data directory.
- Write files to private temporary paths and index them only after successful finalization. Protect deletion/rollback, locked media, storage reserves, and interrupted recordings.
- Supply migration tests for schema changes, including upgrade, future-schema rejection, and recovery. Never overwrite user data or perform destructive migration without a backup and explicit approval.
- Backups must be consistent, private, and restorable. Distinguish sanitized configuration exports from credential-containing recovery backups. Media and database snapshots need a matching restore procedure.

## Security and privacy

- Enforce least privilege and role checks at every protected API, WebSocket, media, export, and compatibility endpoint. Browser controls are not authorization.
- Preserve Argon2id hashes, revocable/expiring sessions, CSRF/origin validation, secure cookie/TLS profiles, rate limits, request bounds, and restrictive browser headers.
- Treat camera URLs, filesystem paths, input media, and proxy requests as trust boundaries. Restrict protocols and destinations; avoid shell interpolation, arbitrary pipelines, or user-supplied executable hooks.
- Redact credentials from logs, errors, screenshots, shared URLs, configuration exports, and test diagnostics. Never print authentication tokens or copy deployment secrets into commands or commits.
- Keep production accounts, recordings, and `.campix/` outside tests. Use private temporary directories and clearly test-only credentials.
- Review dependency and container risks before release. Known-vulnerability audits are dated evidence, not guarantees against unknown flaws. Use immutable action/image references and checksum-verified tool downloads.

## Dependency and platform policy

- Treat `uv.lock` and `package-lock.json` as the development dependency sources of truth. Use `uv sync --locked` and `npm ci --ignore-scripts`; do not edit generated locks by hand.
- Keep `requirements.lock` hash-verified and consistent with the runtime/OCI profile. A dependency change must update the affected locks and include a reason, license/security assessment, and compatibility tests.
- Preserve the native CSI profile's distribution-matched libcamera, Picamera2, NumPy, Pillow, and OpenCV ABIs. Do not replace those packages with unrelated pip wheels.
- Keep native `.deb` architecture/Python constraints, non-root runtime behavior, restricted device access, and private data permissions. Never solve device access by adding a blanket privileged container.
- Do not claim Windows CSI support, a Pi arm64 release from an amd64 build, or hardware acceleration merely because an encoder appears in an inventory.

## Verification gates

Install the documented prerequisites and synchronize the locked development environment before running checks. Use the actual tools; do not report a command as passing if it was unavailable or skipped.

| Change                                        | Required evidence                                                                                                                             |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Documentation, metadata, or instruction files | Local link/image/import checks, formatting, accurate examples, and a reviewed diff.                                                           |
| Python/service behavior                       | Relevant regression tests, complete Python suite where supported, linting, failure/recovery checks, and updated operational docs.             |
| Browser/UI behavior                           | JavaScript syntax, formatting, real-browser workflows, mobile layout, keyboard/accessibility checks, and authenticated media behavior.        |
| Authentication, storage, or concurrency       | Negative/adversarial tests, permission/origin/path bounds, race/overload behavior, restart and rollback evidence.                             |
| Dependencies, packaging, or deployment        | Lock consistency, security/license review, wheel build and installed-package smoke test, target-native `.deb`/OCI checks where available.     |
| Physical camera or encoder support            | Recorded board/OS/sensor/backend/encoder versions, actual hardware results, FPS/drops/CPU/RAM/temperature/latency, and disconnect/soak tests. |

Baseline commands:

```sh
uv lock --check
uv run --no-sync pytest -q
npm run check
npm run check:docs
npm run format:check
npm test
uv run --no-sync python -m build --wheel --outdir packages
```

Python lint configuration lives in `pyproject.toml`. The currently validated standalone lint command is `uvx ruff check src tests scripts`; record the resolved tool version when reporting that check. Moving linting into CI requires a reviewed pinned tool and corresponding environment changes; this standard does not claim that the existing workflow already enforces every gate.

Set `CAMPIX_TEST_MEDIAMTX_BINARY` to the verified v1.21.2 executable for optional media integration, and `CAMPIX_TEST_CHROME` when using an installed Chrome. Run Python and browser suites sequentially with managed MediaMTX. Explicitly distinguish missing-tool skips from tested behavior.

A lint pass is not a security review; a mock/demo pass is not hardware qualification; a wheel build is not installed-package verification; HTTP liveness is not camera readiness.

## Documentation, branding, and evidence

- Keep project prose, source comments/docstrings, configuration notes, and release messages in English. Preserve existing localization functionality.
- Update API/configuration behavior, operational examples, security assumptions, migration notes, and release evidence together with the relevant code change.
- Separate implemented features, historical observations, proposed targets, and measured results. Use the [research report](deep-research-report.md) for direction and [release readiness](RELEASE-READINESS.md) for actual coverage.
- Preserve supplied artwork and its rights/attribution boundaries. Give images English alternative text; label synthetic demonstrations and design concepts truthfully. Never replace a compatibility matrix with a marketing badge.
- Keep root instruction entry points concise and their shared requirements consistent. Changes to imports, checks, or required commands must update the corresponding validation and contributor guidance.

## Source control and release handoff

- Review `git status`, the complete diff, ignored paths, and staged content before a requested commit. Commit only scoped, intentional changes; exclude local credentials, caches, state, recordings, and build artifacts.
- Use the authorized Git identity. New maintainer commits use DensenDeluxe when configured by the maintainer; preserve original contributors and historical authors. Keep commit/PR text focused on changes without automatic tool credits or session trailers.
- Fetch and compare remote/local history before a requested push. Preserve remote commits, resolve conflicts deliberately, and never force-push or silently replace a branch's history.
- Verify the remote commit after a push and report the resulting branch/hash. Observe CI for that exact commit when publication is requested; report failures or unavailable runners instead of implying a successful release.
- A release handoff must state changed behavior, actual test results and skips, compatibility/migration implications, backup/rollback requirements, and unresolved hardware or operational gates.

## Instruction entry-point references

The file conventions follow the [official OpenAI documentation](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [project memory documentation](https://code.claude.com/docs/en/memory), and [project settings reference](https://code.claude.com/docs/en/settings-reference). The instructions guide work; existing organizational settings and explicit user directions may add constraints.
