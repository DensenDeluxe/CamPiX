---
name: campix-ui
description: Change the buildless CamPiX interface in dist/ and its Playwright workflows, including German and English strings, live view, library, schedules, and settings. Use when editing HTML, CSS, app.js, browser tests, layout, localization, or fullscreen and stream fallback behavior.
---

# Change the web interface

The interface is buildless. Edit `dist/index.html`, `dist/styles.css`, and `dist/app.js` directly. `npm run check` runs `node --check dist/app.js`. `npm run format` formats those files and `tests/browser/workflows.spec.mjs`. There is no TypeScript compile step and no bundler. Packaged installs copy `dist/` through the setuptools `data-files` entries in `pyproject.toml`.

Source comments and documentation stay in English. The interface itself is German and English. Preserve both.

## Strings and views

User-visible phrases live in the `words` object in `dist/app.js`. Each value is `[german, english]`. `t()` uses index 0 when `s.lang === "de"` and index 1 otherwise. The default language is German (`localStorage` key `campix-language`, and `<html lang="de">`). Add both strings for every new key. Elements that use `data-i18n` need a matching key.

The views are `overview`, `media`, `scheduler`, and `settings`. Overview is the live view and can show every camera as the current JPEG. That wall does not open WebRTC or HLS. The live image shows LIVE only after a frame decodes. Failures clear LIVE. The JPEG fallback keeps one fetch and decode in flight, cancels it on navigation, camera change, visibility change, and logout, and releases blob URLs.

Role checks in the page are presentation. Viewers, operators, and administrators still have to be rejected by the API. See [campix-security](../campix-security/SKILL.md) when a control starts calling a new route.

## Browser proof

Add or extend a case in `tests/browser/workflows.spec.mjs` when the behavior is user-visible. Use the file's `signin(page, role)` helper. It reuses a test-only `campix_session` cookie per role so extra cases do not exhaust the login rate limit. Passwords are `test-only-<role>-password`. The server is the Playwright `webServer` on `http://127.0.0.1:4401` with `CAMPIX_E2E=1`, one worker, and tests in series. On this cloud VM export `CAMPIX_TEST_CHROME=/usr/bin/google-chrome`.

Playwright and pytest must not run together when `CAMPIX_TEST_MEDIAMTX_BINARY` is set; they bind the same loopback media ports. Follow [campix-verify](../campix-verify/SKILL.md).

For a layout change, exercise the affected flow at desktop size and at the mobile sizes already used in the spec (`390×844` and, for the live view, `844×390`). Cover the empty, error, and signed-out states the change can reach. Session expiry and leaving the live view close fullscreen and drop pending stream work.

Screenshots of the demo or of generated scenes show the layout. They do not qualify a camera, an encoder, or a sustained frame rate. Say that when you attach them.
