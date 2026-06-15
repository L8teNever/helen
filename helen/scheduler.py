"""APScheduler jobs: bundle generation + Google polling.

Multiple task_defs that fire at the same (date, time) collapse into ONE
Google Calendar event (a "bundle"). The event's summary carries a counter
`{done}/{total}` and its description holds a checklist of every member with
a preview link. Reconciliation is centralised in `reconcile_bundle`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from helen import db, google_api, sync

log = logging.getLogger("helen.scheduler")

_scheduler: Optional[BackgroundScheduler] = None
_loop: Optional[asyncio.AbstractEventLoop] = None

WEEKDAY_BITS = [1, 2, 4, 8, 16, 32, 64]
LOOKAHEAD_DAYS = int(os.environ.get("HELEN_LOOKAHEAD_DAYS", "14"))
EVENT_DURATION_MIN = int(os.environ.get("HELEN_EVENT_DURATION_MIN", "30"))
TRIGGER_BASE = os.environ.get("HELEN_TRIGGER_BASE_URL", "https://helen.l8tenever.com").rstrip("/")



def _due_today(task_def, today: date) -> bool:
    if not task_def["active"]:
        return False
    if task_def["schedule_type"] == "daily":
        return True
    bit = WEEKDAY_BITS[today.weekday()]
    return bool(task_def["weekdays_mask"] & bit)


def _event_window(today: date, hhmm: str) -> tuple[str, str, str]:
    """Return (start_iso, end_iso, tz_name) for an event at `today HH:MM`."""
    tz_name = os.environ.get("HELEN_TZ", "Europe/Berlin")
    tz = ZoneInfo(tz_name)
    h, m = hhmm.split(":")
    start = datetime(today.year, today.month, today.day, int(h), int(m), tzinfo=tz)
    end = start + timedelta(minutes=EVENT_DURATION_MIN)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), tz_name


def _times_of(d) -> list[str]:
    return db.parse_times(d["times"]) or [d["time_of_day"]]


def _truncate_names(names: list[str], limit: int = 60) -> str:
    joined = ", ".join(names)
    if len(joined) <= limit:
        return joined
    out: list[str] = []
    used = 0
    for n in names:
        sep = 2 if out else 0
        if used + sep + len(n) > limit - 1:
            out.append("…")
            break
        out.append(n)
        used += sep + len(n)
    return ", ".join(out)


SEP = "————————————————————"


def _render_bundle(insts: list) -> tuple[str, str, bool]:
    """Build (summary, description, all_done) from a list of bundle instances.

    Each row must include `def_name`, `def_notes`, `task_def_id`, `completed`.
    Single-member bundles render as a plain titled event; multi-member bundles
    show a `{done}/{total} — names` summary and a vertically spaced checklist
    description with one item per block (name, link, optional note).
    """
    total = len(insts)
    done = sum(1 for i in insts if i["completed"])
    all_done = total > 0 and done == total

    if total == 1:
        i = insts[0]
        summary = i["def_name"]
        parts: list[str] = []
        if i["def_notes"]:
            parts.append(i["def_notes"])
        parts.append(f"Details: {TRIGGER_BASE}/preview/{i['task_def_id']}")
        return summary, "\n\n".join(parts), all_done

    names = [i["def_name"] for i in insts]
    summary = f"{done}/{total} — {_truncate_names(names)}"

    blocks: list[str] = [f"Status: {done} von {total} erledigt", SEP]
    for i in insts:
        marker = "[x]" if i["completed"] else "[ ]"
        block_lines = [f"{marker} {i['def_name']}"]
        if i["def_notes"]:
            for nl in i["def_notes"].splitlines():
                block_lines.append(f"    {nl}")
        block_lines.append(f"    {TRIGGER_BASE}/preview/{i['task_def_id']}")
        blocks.append("\n".join(block_lines))
    return summary, "\n\n".join(blocks), all_done


def reconcile_bundle(day_str: str, hhmm: str) -> None:
    """Bring the Google Calendar event for (day_str, hhmm) in sync with local state.

    Creates the event if no member yet has an id; otherwise patches summary,
    description, and completion colour. Caller is responsible for deleting
    events when no bundle members remain.
    """
    if not google_api.is_connected():
        return
    insts = db.list_instances_for_bundle(day_str, hhmm)
    if not insts:
        return

    # Legacy state: in the pre-bundle era each member had its own Calendar
    # event. Keep the first id and drop the rest so we collapse into a single
    # event without leaving orphans behind.
    ids_seen = list(dict.fromkeys(i["google_task_id"] for i in insts if i["google_task_id"]))
    existing_event_id = ids_seen[0] if ids_seen else None
    for stale_id in ids_seen[1:]:
        try:
            google_api.delete_event(stale_id)
        except Exception:
            log.exception("Failed to delete legacy per-task event %s", stale_id)

    summary, description, all_done = _render_bundle(insts)

    if existing_event_id:
        try:
            google_api.update_event(existing_event_id, summary, description, all_done)
        except Exception:
            log.exception("update_event failed for bundle %s %s", day_str, hhmm)
            return
        for i in insts:
            if i["google_task_id"] != existing_event_id:
                db.set_instance_google_id(i["id"], existing_event_id)
        return

    day = date.fromisoformat(day_str)
    start_iso, end_iso, tz_name = _event_window(day, hhmm)
    try:
        ev = google_api.create_event(
            title=summary, start_iso=start_iso, end_iso=end_iso,
            tz=tz_name, description=description,
        )
    except Exception:
        log.exception("create_event failed for bundle %s %s", day_str, hhmm)
        return
    new_id = ev.get("id")
    for i in insts:
        db.set_instance_google_id(i["id"], new_id)
    if all_done and new_id:
        try:
            google_api.patch_event_completed(new_id, True)
        except Exception:
            log.exception("Initial completion patch failed for %s", new_id)


def generate_window(start: date, end: date, only_def_id: Optional[int] = None) -> int:
    """Pre-create local instances over [start, end] and reconcile each touched bundle.

    With `only_def_id`, only that def contributes new rows — but every bundle
    it lands in is re-rendered so the Calendar event picks up siblings that
    were already created from earlier runs.
    """
    if not google_api.is_connected():
        log.info("Skip generate_window: Google nicht verbunden.")
        return 0

    active = db.list_task_defs(active_only=True)
    creators = [d for d in active if only_def_id is None or d["id"] == only_def_id]

    created = 0
    touched: set[tuple[str, str]] = set()
    cursor = start
    while cursor <= end:
        day_str = cursor.isoformat()
        for d in creators:
            if not _due_today(d, cursor):
                continue
            for hhmm in _times_of(d):
                if db.get_or_none_instance_for(d["id"], day_str, hhmm) is None:
                    db.create_instance(d["id"], day_str, hhmm, None)
                    created += 1
                touched.add((day_str, hhmm))
        cursor += timedelta(days=1)

    for day_str, hhmm in sorted(touched):
        reconcile_bundle(day_str, hhmm)

    if created:
        log.info("generate_window created %d task instance(s).", created)
    return created


def generate_today(today: Optional[date] = None) -> int:
    if today is None:
        today = date.today()
    return generate_window(today, today + timedelta(days=LOOKAHEAD_DAYS))


def wipe_def_instances(def_id: int, from_date: Optional[date] = None) -> int:
    """Remove instances of `def_id` and reconcile (or delete) affected bundles.

    For each Calendar event a wiped row pointed at:
      - if no other members survive → delete the event,
      - else → re-render the event without the removed entries.
    """
    rows = db.list_instances_by_def(def_id, from_date.isoformat() if from_date else None)
    removed = 0
    connected = google_api.is_connected()
    affected_event_ids: set[str] = set()

    for r in rows:
        gid = r["google_task_id"]
        if gid:
            affected_event_ids.add(gid)
        db.delete_instance(r["id"])
        removed += 1

    for gid in affected_event_ids:
        siblings = db.list_instances_by_google_id(gid)
        if not siblings:
            if connected:
                try:
                    google_api.delete_event(gid)
                except Exception:
                    log.exception("Failed to delete Google event %s", gid)
        elif connected:
            s = siblings[0]
            reconcile_bundle(s["due_date"], s["due_time"])

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
