# CamPiX repository instructions

## Working agreement

- Follow the shared [engineering standard](docs/ENGINEERING.md). Read the relevant sections before implementation, review, or release work.
- Make focused, production-quality changes within the requested scope. Preserve unrelated user changes and existing upstream history. Do not publish, change repository settings, or alter a deployment unless requested.
- Write source notes, documentation, configuration guidance, and commit messages in English. Keep existing interface localization functional.
- Inspect the affected code and tests before editing. Use a short plan for changes spanning service boundaries, security, persistence, or packaging; a small correction does not need a ceremonial plan.
- For a reported defect, reproduce it and add a regression test before or alongside the fix. Fix causes, not only symptoms. Do not silence failing tests or weaken security to make a check pass.

## CamPiX invariants

- Keep camera capture, motion analysis, recording, exports, and media publishing outside HTTP request handling. Preserve bounded queues, cancellation, backoff, shutdown cleanup, and observable errors.
- Enforce authorization on the server. Preserve roles, CSRF/origin checks, session revocation, private storage, path bounds, parameterized SQL, and restricted media proxy targets.
- Do not pass camera URLs, filenames, or user input to a shell. Do not add arbitrary macro, hook, or GStreamer execution paths without an explicit design and security review.
- Persist media only after successful finalization; preserve interrupted files and transactional recovery. Schema migrations must protect existing data and reject unsupported downgrades.
- Demo sources must remain explicit. Never present synthetic images, a discovered encoder name, an HTTP health check, or an available control as evidence of hardware qualification.
- `auto` prefers an allowlisted hardware encoder only after a successful profile-specific packet probe, with tested software `libx264` fallback. Encoder usability does not certify a physical camera or sustained performance. Keep implemented architecture, research direction, and measured evidence distinct.
- Keep supplied artwork, English alternative text, original copyright notices, and dependency licenses. Do not manufacture compatibility or performance claims.

## Verification

Run commands from the repository root. Use the locked environment; do not regenerate lockfiles for unrelated work.

```sh
uv lock --check
uv run --no-sync pytest -q
npm run check
npm run check:docs
npm run format:check
npm test
```

Select checks proportionate to the change using [verification gates](docs/ENGINEERING.md#verification-gates). Runtime/security changes require the relevant integration suites, not syntax checks alone. Report optional-tool skips and unexecuted checks explicitly.

Managed MediaMTX tests use fixed loopback ports. Run Python and browser suites sequentially when `CAMPIX_TEST_MEDIAMTX_BINARY` is set. Test accounts and media belong in isolated temporary directories, never the user's `.campix/` state.

## Code review rules

- Flag authorization bypasses, secrets in output/exports, path traversal, arbitrary proxy destinations, and unsafe command execution as blocking findings.
- Flag unbounded queues, capture blocked by HTTP or encoders, orphaned subprocesses, duplicate scheduler execution, unsafe migrations, and incomplete media reported as successful.
- Require regression evidence for changed failure/recovery paths. Flag fake readiness, undocumented skips, unsupported hardware claims, and stale operational instructions.
- Keep formatting checks in automation; concentrate review on correctness, security, maintainability, and operational behavior.

## Git and handoff

- Commit or push only when requested. Review the exact diff and staged paths; never include secrets, local state, recordings, downloaded tools, or generated artifacts.
- Use the authorized repository identity. The maintainer is DensenDeluxe. Do not impersonate contributors, rewrite existing authors, or remove license attribution.
- Commit subjects and pull-request descriptions should explain the actual changes. Do not add tool/model credits, generated-by notices, session links, or automatic co-author trailers.
- Never force-push, reset user work, delete remote history, or change branch protection without explicit approval. Fetch and compare histories before synchronizing.
- Finish with what changed, checks actually run, remaining risks, and any unverified deployment or hardware boundary. Do not claim production readiness from instructions or synthetic tests alone.
