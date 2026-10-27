---
name: campix-persistence
description: Change CamPiX SQLite schema, media indexing, deletion, backup, and recovery so files become library entries only after a successful finalize. Use when editing the database, media store, export jobs, retention, partial recordings, or a migration.
---

# Change storage and recovery

`src/campix/db.py` is the schema. `src/campix/media.py` is the media index. One service holds the data directory, through `ProcessLock`. Access uses `Database.transaction` and parameterized SQL. Follow [campix-verify](../campix-verify/SKILL.md) after the regression tests below.

## Schema

`SCHEMA_VERSION` is 4. Schema 3 added the notification outbox. Schema 4 added `share_links`. Opening a database whose `PRAGMA user_version` is newer raises `Database schema is newer than this CamPiX release; downgrade refused` and must not rewrite that file. `tests/test_recovery.py::test_newer_database_is_not_downgraded` locks this behavior.

The setup script creates missing tables and then sets `user_version` to 4. The notification outbox and share links arrived as new tables that way. `CREATE TABLE IF NOT EXISTS` does not add a column to a table that already exists. A change to existing rows or columns needs an explicit upgrade from the schemas this release still opens, plus tests for upgrade, rejection of a future schema, and recovery of a failed upgrade. Do not drop, overwrite, or rebuild a user's database as the migration. An older release cannot open a newer schema. Rollback uses a private backup taken before the migration. Say that in the handoff and in [architecture](../../../docs/ARCHITECTURE.md).

## Media files

`MediaStore.path` resolves the filename under the media root, requires that parent directory, and rejects symlinks. Snapshots write a sibling `*.partial` file and replace the final name before `register()` inserts the index row. A recording is indexed only after successful finalization. A recording that requested audio is indexed only after the video remux and the AAC mux both succeed. Export and timelapse jobs keep an explicit status. The library timelapse uses at most 500 matching snapshots.

`delete()` refuses a locked item, renames the file to `*<suffix>.deleted`, deletes the row, and puts the file back if the database delete fails. Startup reconciliation removes index rows whose files are gone and keeps interrupted or unindexed files, including `*.mp4.partial`. It does not repair an interrupted MP4. An administrator remux may index a partial only after ffprobe reports a duration.

Filename search escapes `\`, `%`, and `_` so the substring matches itself. The library search covers the whole index. The overview's three newest items stay independent of the library page.

## Backup and export

A database backup is a consistent SQLite snapshot, mode-restricted, and it omits active sessions. It still contains password hashes, token digests, and private camera fields. Media files are a separate copy. Configuration export omits secrets, webhook URLs, raw tokens, and the values the current export already strips. Keep those two artifacts distinct. Tests live in `tests/test_recovery.py` and `tests/test_service.py`. Use a temporary directory, never a user's `.campix/` or `/var/tmp/campix-dev`.
