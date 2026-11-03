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


def window_minutes(schedule):
    """Inclusive start and exclusive end, in minutes from local midnight."""
    return (
        schedule.start_hour * 60 + schedule.start_minute,
        schedule.end_hour * 60 + schedule.end_minute,
    )


def in_window(schedule, now):
    local = dt.datetime.fromtimestamp(now, ZoneInfo(schedule.timezone))
    minute = local.hour * 60 + local.minute
    start, end = window_minutes(schedule)
    # Equal bounds are an empty window. 00:00–24:00 stays a full day because 0 < 1440.
    if start == end:
        return False
    if start < end:
        return local.weekday() in schedule.days and start <= minute < end
    # After midnight belongs to the previous day's overnight schedule.
    day = (local.weekday() - 1) % 7 if minute < end else local.weekday()
    return day in schedule.days and (minute >= start or minute < end)


class Scheduler:
    def __init__(self, db, cameras, store, bus):
        self.db, self.cameras, self.store, self.bus = db, cameras, store, bus
        self.halt = threading.Event()
        self.thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self.last_cleanup = time.monotonic()
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.bus.add_listener(self._finish_recording)

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
        previous = {}
        if existing:
            try:
                loaded = json.loads(existing["config"])
            except json.JSONDecodeError:
                loaded = {}
            if isinstance(loaded, dict):
                previous = loaded
        if schedule.enabled is None:
            kept = previous.get("enabled", True) if existing else True
            if not isinstance(kept, bool):
                kept = True
            schedule = schedule.model_copy(update={"enabled": kept})
        now = time.time()
        next_run = now + schedule.interval_seconds
        if existing:
            # Pausing, renaming, or editing the window must not skip a day.
            next_run = float(existing["next_run"])
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
                if schedule.enabled is None:
                    schedule = schedule.model_copy(update={"enabled": True})
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
                    self.bus.publish(
                        "schedule.completed",
                        "Scheduled action executed",
                        schedule.camera_id,
                        {"schedule_id": schedule.id},
                    )
                else:
                    started = camera.start_recording(
                        duration=schedule.duration_seconds, trigger="schedule:" + schedule.id
                    )
                    media_id = started.get("media_id") if isinstance(started, dict) else None
                    if isinstance(media_id, str):
                        with self.pending_lock:
                            self.pending[media_id] = (schedule.id, schedule.camera_id)
                            if len(self.pending) > 64:
                                self.pending.pop(next(iter(self.pending)))
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

    def _finish_recording(self, event):
        """A scheduled take is complete only after the library indexes it."""
        data = event.get("data") or {}
        media_id = data.get("media_id")
        if event["kind"] not in {"media.saved", "recording.error"} or not isinstance(media_id, str):
            return
        with self.pending_lock:
            job = self.pending.pop(media_id, None)
        if not job:
            return
        schedule_id, camera_id = job
        if event["kind"] == "media.saved":
            self.bus.publish(
                "schedule.completed",
                "Scheduled recording saved",
                camera_id,
                {"schedule_id": schedule_id, "media_id": media_id},
            )
            return
        self.bus.publish(
            "schedule.error",
            "Scheduled recording was not saved",
            camera_id,
            {"schedule_id": schedule_id, "media_id": media_id},
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
