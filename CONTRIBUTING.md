# Contributing to CamPiX

[Project overview](README.md) · [Documentation](docs/README.md) · [Release readiness](docs/RELEASE-READINESS.md)

## Development setup

Use Python 3.11 or newer, `uv`, and FFmpeg/ffprobe. Install Node.js and npm for browser tests and frontend formatting.

```sh
uv sync --locked --extra test --extra v4l2
npm ci --ignore-scripts
npx playwright install chromium
```

Create your own administrator with `campixd init-admin` and run `campixd serve --demo` using the executable in `.venv/bin/` on Linux or `.venv\Scripts\` on Windows. Demo input is synthetic; generated media files are real. Do not reuse browser-test credentials for a deployment.

Use distribution-matched native camera packages for physical CSI tests. Do not overwrite the user's data directory, run an additional capture process on an active camera, or mark an untested device as compatible.

## Verification

Run from the project directory:

```sh
uv run --no-sync pytest -q
npm run check
npm run check:docs
npm run format:check
npm test
uv run --no-sync python -m build --wheel --outdir packages
```

Use `npm run format` to apply frontend and documentation formatting. `npm run check:docs` checks local document links, heading anchors, image paths, and unavailable research references without requesting external URLs. Set `CAMPIX_TEST_CHROME` to an existing Chrome executable if needed. Optional MediaMTX tests require `CAMPIX_TEST_MEDIAMTX_BINARY` pointing to the verified v1.21.2 executable. Keep Python and browser media-server suites sequential because managed MediaMTX ports are fixed. Report skips explicitly.

The [CI workflow](.github/workflows/ci.yml) also builds a native package and Docker image on Linux. Native Debian packages must be built for the target architecture and Python minor version. A successful build or synthetic capture test is not physical hardware qualification.

## Documentation and source notes

- Write project descriptions, documentation, comments, docstrings, configuration notes, and test descriptions in English. Existing user-facing localization dictionaries are not source commentary.
- Update the relevant operational documentation when behavior or configuration changes. Keep examples executable; label placeholders and future installation channels explicitly.
- Separate implemented behavior, measured results, expected compatibility, and proposed features. Include board, OS, sensor, backend, encoder, tool versions, and test date when reporting physical-device results.
- Use relative Markdown links for repository documents and existing graphics. Give images descriptive English alternative text. Design iterations must not be presented as application screenshots.
- Keep supplied branding assets intact and follow the [asset guide](pictures/README.md). Never use a compatibility badge as a substitute for hardware evidence.
- Do not commit credentials, camera URLs containing secrets, private recordings, local state databases, or generated test/build directories. Keep provenance and license notices intact.

## Change scope

Keep capture, motion, recording, and media transport independent from HTTP request handling. Preserve bounded queues, real failure states, authentication, and restart behavior. Add regression tests for changes to those boundaries.

CamPiX currently has no published contribution or private security-reporting channel in this local workspace. Coordinate repository access and sensitive reports with the maintainer; see the [security model](docs/SECURITY.md).
