"""Bidirectional sync helpers between local SQLite state and Google Calendar.

Completion is signalled on the Google side by the event's `colorId`:
graphite (id "8") means "done". Local toggles update the colour; the polling
job reconciles when the user changes the colour directly in Google Calendar.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from helen import db, google_api, sse

log = logging.getLogger("helen.sync")


async def toggle_instance(inst_id: int, completed: bool, loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
    """Toggle a task instance both locally and on Google. Returns success."""
    inst = db.get_instance(inst_id)
    if inst is None:
        return False
    db.set_instance_completed(inst_id, completed)
    event_id = inst["google_task_id"]
    if event_id:
        try:
            await asyncio.to_thread(google_api.patch_event_completed, event_id, completed)
        except Exception:
            log.exception("Failed to patch Google event %s", event_id)
    await sse.broadcast(
        "instance_changed",
        {"id": inst_id, "completed": completed, "source": "local"},
    )
    return True


def poll_google_once(loop: asyncio.AbstractEventLoop) -> None:
    """Pull current state from Google Calendar and reconcile completion locally.

    Runs in the APScheduler thread. Broadcasts via the provided loop.
    Only inspects events from the last ~2 days onward to keep payload small.
    """
    if not google_api.is_connected():
        return
    time_min = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    try:
        events = google_api.list_events(time_min_iso=time_min)
    except Exception:
        log.exception("Google list_events failed.")
        return

    changed: list[dict] = []
    for ev in events:
        eid = ev.get("id")
        if not eid:
            continue
        inst = db.get_instance_by_google_id(eid)
        if inst is None:
            continue
        remote_completed = ev.get("colorId") == google_api.COMPLETED_COLOR_ID
        if bool(inst["completed"]) != remote_completed:
            db.set_instance_completed(inst["id"], remote_completed)
            changed.append({"id": inst["id"], "completed": remote_completed, "source": "google"})

    for c in changed:
        sse.broadcast_threadsafe(loop, "instance_changed", c)
