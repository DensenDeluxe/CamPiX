---
name: campix-verify
description: Select and run CamPiX checks from the repository root, including pytest, docs, formatting, and Playwright, and report skips honestly. Use when verifying a change, reproducing a failure, running the test suite, or writing a handoff about what was and was not proven.
---

# Verify a CamPiX change

Run every command from the repository root. Use the locked environment. Leave `uv.lock`, `package-lock.json`, and `requirements.lock` unchanged unless the task itself changes dependencies.

Read the matching row in [Verification gates](../../../docs/ENGINEERING.md#verification-gates) before choosing commands. Also follow the skill for the area you changed: [runtime](../campix-runtime/SKILL.md), [security](../campix-security/SKILL.md), [interface](../campix-ui/SKILL.md), [persistence](../campix-persistence/SKILL.md), or [installer](../campix-installer/SKILL.md).

## What a passing check proves

A lint pass is not a security review. A demo or mock pass is not hardware qualification. A wheel build is not an installed-package check. `/healthz` shows that a process answered; it does not show that a camera or encoder works. Record physical results only with board, OS, sensor, backend, encoder, tool versions, and the date. Outstanding hardware gates are in [release readiness](../../../docs/RELEASE-READINESS.md#remaining-production-gates).

## Commands

Start with the narrowest test that exercises the change, then run the gate that the diff requires.

```sh
uv lock --check
uv run --no-sync pytest -q tests/test_relevant.py
uv run --no-sync pytest -q
npm run check
npm run check:docs
npm run format:check
npm test
```

`npm run format` rewrites only the paths listed in `package.json`. Python lint, when the change needs it, is `uvx ruff check src tests scripts`. The settings live in `pyproject.toml`. Record the resolved Ruff version in the handoff.

## This cloud environment

Export `CAMPIX_TEST_CHROME=/usr/bin/google-chrome` before `npm test`. Optional MediaMTX cases need `CAMPIX_TEST_MEDIAMTX_BINARY=/opt/campix/mediamtx`, which is the verified v1.21.2 binary. Python and browser suites bind the same loopback MediaMTX ports, so run `uv run --no-sync pytest -q` and `npm test` one after the other when that variable is set.

The boot demo listens at `http://127.0.0.1:8080` and stores data in `/var/tmp/campix-dev`. That directory is not a user's `.campix/` state. Leave its admin password out of commits, logs, test output, and replies. Playwright starts a separate server at `http://127.0.0.1:4401` through `scripts/browser-test-server.py`. Browser tests use that server.

## Isolated test state

Use `tmp_path` or the `application` fixture in `tests/conftest.py`. The fixture sets `demo=True` and creates `admin`, `operator`, and `viewer` with passwords `test-only-<role>-password`. `signin(client)` stores the CSRF header. The synthetic camera id is `demo`. Tests must not open `/var/tmp/campix-dev` or a real `.campix/` directory.

## Report

State the exact commands, pass and skip counts, and the reason for every skip. Name checks that did not run. Keep these apart: behavior the test exercised, a missing tool, a synthetic or demo source, and a measurement on a named device. A handoff also names compatibility or migration impact, backup or rollback needs, and any hardware or deployment gate that is still open.
