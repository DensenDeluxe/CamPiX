from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .capture import CaptureError
from .models import Schedule

LOG = logging.getLogger(__name__)


def in_window(schedule, now):
    local = dt.datetime.fromtimestamp(now, ZoneInfo(schedule.timezone))
    start, end = schedule.start_hour, schedule.end_hour
    if start <= end:
        return local.weekday() in schedule.days and start <= local.hour < end
    # After midnight belongs to the previous day's overnight schedule.
    day = (local.weekday() - 1) % 7 if local.hour < end else local.weekday()
    return day in schedule.days and (local.hour >= start or local.hour < end)


class Scheduler:
    def __init__(self, db, cameras, store, bus):
        self.db, self.cameras, self.store, self.bus = db, cameras, store, bus
        self.halt = threading.Event()
        self.thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self.last_cleanup = time.monotonic()

    def start(self):
        self.thread.start()

    def list(self):
        return [
            {**json.loads(row["config"]), "next_run": row["next_run"]}
            for row in self.db.all("SELECT * FROM schedules ORDER BY next_run")
        ]

    def save(self, schedule: Schedule):
        self.cameras.get(schedule.camera_id)
        existing = self.db.one(
            "SELECT config, next_run FROM schedules WHERE id=?", (schedule.id,)
        )
        now = time.time()
        next_run = now + schedule.interval_seconds
        if existing:
            # Pausing, renaming, or editing the window must not skip a day.
            next_run = float(existing["next_run"])
            try:
                previous = json.loads(existing["config"])
            except json.JSONDecodeError:
                previous = {}
            if not isinstance(previous, dict):
                previous = {}
            if previous.get("interval_seconds") != schedule.interval_seconds:
                next_run = now + schedule.interval_seconds
            elif schedule.enabled and previous.get("enabled", True) is False and next_run < now:
                next_run = now
        self.db.execute(
            "INSERT INTO schedules VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config,next_run=excluded.next_run",
            (schedule.id, schedule.model_dump_json(), next_run),
        )
        return next(row for row in self.list() if row["id"] == schedule.id)

    def tick(self, now=None):
        now = now if now is not None else time.time()
        # Claim before execution. Restart never replays a backlog of missed jobs.
        jobs = []
        invalid = []
        with self.db.transaction() as conn:
            for row in conn.execute("SELECT * FROM schedules WHERE next_run<=?", (now,)).fetchall():
                try:
                    payload = json.loads(row["config"])
                    schedule = Schedule.model_validate(payload, context={"allow_overlap": True})
                except (json.JSONDecodeError, ValidationError):
                    conn.execute(
                        "UPDATE schedules SET next_run=? WHERE id=?",
                        (now + 3600, row["id"]),
                    )
                    invalid.append(row["id"])
                    continue
                conn.execute(
                    "UPDATE schedules SET next_run=? WHERE id=?",
                    (now + schedule.interval_seconds, schedule.id),
                )
                if schedule.enabled and in_window(schedule, now):
                    jobs.append(schedule)
        for schedule_id in invalid:
            self.bus.publish(
                "schedule.error",
                "Scheduled action is no longer valid",
                data={"schedule_id": schedule_id},
            )
        for schedule in jobs:
            try:
                camera = self.cameras.get(schedule.camera_id)
                if schedule.action == "snapshot":
                    camera.snapshot(trigger="schedule:" + schedule.id)
                else:
                    camera.start_recording(
                        duration=schedule.duration_seconds, trigger="schedule:" + schedule.id
                    )
                self.bus.publish(
                    "schedule.completed",
                    "Scheduled action executed",
                    schedule.camera_id,
                    {"schedule_id": schedule.id},
                )
            except CaptureError as exc:
                if schedule.action == "record" and "already in progress" in str(exc):
                    # The claim already moved next_run by one interval. A longer
                    # take must not fail again on every shorter interval.
                    self.db.execute(
                        "UPDATE schedules SET next_run=MAX(next_run, ?) WHERE id=?",
                        (now + schedule.duration_seconds, schedule.id),
                    )
                    self.bus.publish(
                        "schedule.skipped",
                        "Scheduled recording skipped because a recording is already in progress",
                        schedule.camera_id,
                        {"schedule_id": schedule.id},
                    )
                    continue
                self.bus.publish(
                    "schedule.error",
                    f"Scheduled action failed ({type(exc).__name__})",
                    schedule.camera_id,
                    {"schedule_id": schedule.id},
                )
            except Exception as exc:
                self.bus.publish(
                    "schedule.error",
                    f"Scheduled action failed ({type(exc).__name__})",
                    schedule.camera_id,
                    {"schedule_id": schedule.id},
                )

    def _run(self):
        while not self.halt.wait(1):
            try:
                self.tick()
                if time.monotonic() - self.last_cleanup >= 3600:
                    self.store.retention()
                    self.last_cleanup = time.monotonic()
            except Exception:
                LOG.exception("Scheduler tick failed")

    def stop(self):
        self.halt.set()
        if self.thread.is_alive():
            self.thread.join(timeout=20)
