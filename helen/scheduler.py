"""APScheduler jobs: daily task generation + Google polling."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from helen import db, google_api, sync

log = logging.getLogger("helen.scheduler")

_scheduler: Optional[BackgroundScheduler] = None
_loop: Optional[asyncio.AbstractEventLoop] = None

# Weekday bitmask: Mon=1 Tue=2 Wed=4 Thu=8 Fri=16 Sat=32 Sun=64
WEEKDAY_BITS = [1, 2, 4, 8, 16, 32, 64]

# How many days ahead to pre-create task instances in Google Tasks.
LOOKAHEAD_DAYS = int(os.environ.get("HELEN_LOOKAHEAD_DAYS", "14"))


def _due_today(task_def, today: date) -> bool:
    if not task_def["active"]:
        return False
    if task_def["schedule_type"] == "daily":
        return True
    bit = WEEKDAY_BITS[today.weekday()]
    return bool(task_def["weekdays_mask"] & bit)


def _due_iso_z(today: date, hhmm: str) -> str:
    """Build an RFC-3339 timestamp for `today HH:MM` with explicit offset.

    Google Tasks v1 historically discarded the time portion of `due` (all-day
    only). Newer Tasks clients preserve the time when the timestamp is
    timezone-aware with an explicit offset (e.g. `+02:00`) rather than the
    `Z`-notation we previously sent. TZ defaults to Europe/Berlin and is
    overridable via HELEN_TZ.
    """
    tz = ZoneInfo(os.environ.get("HELEN_TZ", "Europe/Berlin"))
    h, m = hhmm.split(":")
    local_dt = datetime(today.year, today.month, today.day, int(h), int(m), tzinfo=tz)
    return local_dt.isoformat(timespec="seconds")


def _build_notes(d) -> str:
    """Compose the Google Task notes: user notes + preview link (with image)."""
    base = os.environ.get("HELEN_TRIGGER_BASE_URL", "https://helen.l8tenever.com").rstrip("/")
    parts = []
    if d["notes"]:
        parts.append(d["notes"])
    parts.append(f"{base}/preview/{d['id']}")
    return "\n\n".join(parts)


def _times_of(d) -> list[str]:
    """Return the list of HH:MM times for a task_def, falling back to legacy time_of_day."""
    return db.parse_times(d["times"]) or [d["time_of_day"]]


def _create_one(d, day: date) -> int:
    """Create one Google task + local instance per configured time-of-day for `day`.

    Returns the number of new instances created (0 if def not due today / all exist).
    """
    if not _due_today(d, day):
        return 0
    day_str = day.isoformat()
    notes = _build_notes(d)
    created = 0
    for hhmm in _times_of(d):
        if db.get_or_none_instance_for(d["id"], day_str, hhmm) is not None:
            continue
        title = f'{d["name"]} ({hhmm})'
        due_iso = _due_iso_z(day, hhmm)
        try:
            gt = google_api.create_task(title=title, due_iso_z=due_iso, notes=notes)
            db.create_instance(d["id"], day_str, hhmm, gt.get("id"))
            created += 1
        except Exception:
            log.exception("create_task failed for def %s on %s %s", d["id"], day_str, hhmm)
    return created


def generate_window(start: date, end: date, only_def_id: Optional[int] = None) -> int:
    """Pre-create missing task_instances over [start, end] inclusive.

    If `only_def_id` is given, restrict to that one task_def. Returns count.
    """
    if not google_api.is_connected():
        log.info("Skip generate_window: Google nicht verbunden.")
        return 0
    defs = [db.get_task_def(only_def_id)] if only_def_id is not None else db.list_task_defs(active_only=True)
    defs = [d for d in defs if d is not None and d["active"]]
    created = 0
    cursor = start
    while cursor <= end:
        for d in defs:
            created += _create_one(d, cursor)
        cursor += timedelta(days=1)
    if created:
        log.info("generate_window created %d task instance(s).", created)
    return created


def generate_today(today: Optional[date] = None) -> int:
    """Pre-create today + LOOKAHEAD_DAYS for all active defs."""
    if today is None:
        today = date.today()
    return generate_window(today, today + timedelta(days=LOOKAHEAD_DAYS))


def wipe_def_instances(def_id: int, from_date: Optional[date] = None) -> int:
    """Remove instances of `def_id` from Google Tasks + local DB.

    If `from_date` is None, wipes ALL instances (history included).
    Otherwise wipes instances with due_date >= from_date.
    Returns count removed.
    """
    rows = db.list_instances_by_def(def_id, from_date.isoformat() if from_date else None)
    removed = 0
    connected = google_api.is_connected()
    for r in rows:
        gid = r["google_task_id"]
        if gid and connected:
            try:
                google_api.delete_task(gid)
            except Exception:
                log.exception("Failed to delete Google task %s", gid)
        db.delete_instance(r["id"])
        removed += 1
    if removed:
        log.info("wipe_def_instances removed %d instance(s) for def %s.", removed, def_id)
    return removed


def _poll_job():
    if _loop is None:
        return
    try:
        sync.poll_google_once(_loop)
    except Exception:
        log.exception("poll_google_once failed.")


def _midnight_job():
    try:
        generate_today()
    except Exception:
        log.exception("generate_today failed.")


def start(loop: asyncio.AbstractEventLoop) -> BackgroundScheduler:
    global _scheduler, _loop
    _loop = loop
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(_midnight_job, CronTrigger(hour=0, minute=5), id="midnight_generate", replace_existing=True)
    sched.add_job(_poll_job, IntervalTrigger(seconds=30), id="poll_google", replace_existing=True, max_instances=1)
    sched.add_job(_midnight_job, "date", run_date=datetime.utcnow() + timedelta(seconds=5), id="boot_generate")
    sched.start()
    _scheduler = sched
    log.info("Scheduler started.")
    return sched


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
