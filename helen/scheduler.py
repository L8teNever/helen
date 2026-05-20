"""APScheduler jobs: daily task generation + Google polling."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from helen import db, google_api, sync

log = logging.getLogger("helen.scheduler")

_scheduler: Optional[BackgroundScheduler] = None
_loop: Optional[asyncio.AbstractEventLoop] = None

# Weekday bitmask: Mon=1 Tue=2 Wed=4 Thu=8 Fri=16 Sat=32 Sun=64
WEEKDAY_BITS = [1, 2, 4, 8, 16, 32, 64]


def _due_today(task_def, today: date) -> bool:
    if not task_def["active"]:
        return False
    if task_def["schedule_type"] == "daily":
        return True
    bit = WEEKDAY_BITS[today.weekday()]
    return bool(task_def["weekdays_mask"] & bit)


def _due_iso_z(today: date, hhmm: str) -> str:
    h, m = hhmm.split(":")
    dt = datetime(today.year, today.month, today.day, int(h), int(m), tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def generate_today(today: Optional[date] = None) -> int:
    """Create missing task_instances + Google tasks for today's due definitions.

    Returns count created.
    """
    if today is None:
        today = date.today()
    today_str = today.isoformat()
    created = 0

    if not google_api.is_connected():
        log.info("Skip generate_today: Google nicht verbunden.")
        return 0

    for d in db.list_task_defs(active_only=True):
        if not _due_today(d, today):
            continue
        existing = db.get_or_none_instance_for(d["id"], today_str)
        if existing is not None:
            continue
        title = f'{d["name"]} ({d["time_of_day"]})'
        due_iso = _due_iso_z(today, d["time_of_day"])
        try:
            gt = google_api.create_task(title=title, due_iso_z=due_iso)
            db.create_instance(d["id"], today_str, d["time_of_day"], gt.get("id"))
            created += 1
        except Exception:
            log.exception("create_task failed for def %s", d["id"])

    if created:
        log.info("generate_today created %d task instance(s).", created)
    return created


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
