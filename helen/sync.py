"""Bidirectional sync helpers between local SQLite state and Google Tasks."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from helen import db, google_api, sse

log = logging.getLogger("helen.sync")


async def toggle_instance(inst_id: int, completed: bool, loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
    """Toggle a task instance both locally and on Google. Returns success."""
    inst = db.get_instance(inst_id)
    if inst is None:
        return False
    db.set_instance_completed(inst_id, completed)
    google_task_id = inst["google_task_id"]
    if google_task_id:
        try:
            await asyncio.to_thread(google_api.patch_task_status, google_task_id, completed)
        except Exception:
            log.exception("Failed to patch Google task %s", google_task_id)
    await sse.broadcast(
        "instance_changed",
        {"id": inst_id, "completed": completed, "source": "local"},
    )
    return True


def poll_google_once(loop: asyncio.AbstractEventLoop) -> None:
    """Pull current state from Google and reconcile completion status locally.

    Runs in the APScheduler thread. Broadcasts via the provided loop.
    """
    if not google_api.is_connected():
        return
    try:
        tasks = google_api.list_tasks(show_completed=True, show_hidden=True)
    except Exception:
        log.exception("Google list_tasks failed.")
        return

    changed: list[dict] = []
    for t in tasks:
        gid = t.get("id")
        if not gid:
            continue
        inst = db.get_instance_by_google_id(gid)
        if inst is None:
            continue
        remote_completed = t.get("status") == "completed"
        if bool(inst["completed"]) != remote_completed:
            db.set_instance_completed(inst["id"], remote_completed)
            changed.append({"id": inst["id"], "completed": remote_completed, "source": "google"})

    for c in changed:
        sse.broadcast_threadsafe(loop, "instance_changed", c)
