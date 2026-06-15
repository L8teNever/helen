"""Bidirectional sync between local SQLite state and Google Calendar bundles.

Each bundle = one Calendar event shared by every task_instance scheduled at
the same (date, time). Toggling completion locally triggers a re-render of
that bundle event. Reverse-sync (Calendar → local) is only safe for
single-member bundles, because a colour change on a multi-member event is
ambiguous about which sub-task changed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from helen import db, google_api, sse

log = logging.getLogger("helen.sync")


async def toggle_instance(inst_id: int, completed: bool, loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
    """Toggle a task instance locally and re-render its Calendar bundle."""
    inst = db.get_instance(inst_id)
    if inst is None:
        return False
    db.set_instance_completed(inst_id, completed)

    # Lazy import to avoid scheduler ↔ sync circular import.
    from helen import scheduler
    try:
        await asyncio.to_thread(scheduler.reconcile_bundle, inst["due_date"], inst["due_time"])
    except Exception:
        log.exception("Reconcile after toggle failed for inst %s", inst_id)

    await sse.broadcast(
        "instance_changed",
        {"id": inst_id, "completed": completed, "source": "local"},
    )
    return True


async def toggle_instances(inst_ids: list[int], completed: bool) -> bool:
    """Toggle multiple task instances locally and re-render their Calendar bundles."""
    touched_bundles: set[tuple[str, str]] = set()
    success = False

    for inst_id in inst_ids:
        inst = db.get_instance(inst_id)
        if inst is None:
            continue
        db.set_instance_completed(inst_id, completed)
        touched_bundles.add((inst["due_date"], inst["due_time"]))

        await sse.broadcast(
            "instance_changed",
            {"id": inst_id, "completed": completed, "source": "local"},
        )
        success = True

    # Lazy import to avoid scheduler ↔ sync circular import.
    from helen import scheduler
    for due_date, due_time in touched_bundles:
        try:
            await asyncio.to_thread(scheduler.reconcile_bundle, due_date, due_time)
        except Exception:
            log.exception("Reconcile after toggle failed for bundle %s %s", due_date, due_time)

    return success


def poll_google_once(loop: asyncio.AbstractEventLoop) -> None:
    """Pull Calendar state and reconcile completion for single-member bundles only."""
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
        bundle = db.list_instances_by_google_id(eid)
        if len(bundle) != 1:
            continue  # Multi-member bundles: completion is owned by Helen.
        inst = bundle[0]
        remote_completed = ev.get("colorId") == google_api.COMPLETED_COLOR_ID
        if bool(inst["completed"]) != remote_completed:
            db.set_instance_completed(inst["id"], remote_completed)
            changed.append({"id": inst["id"], "completed": remote_completed, "source": "google"})

    for c in changed:
        sse.broadcast_threadsafe(loop, "instance_changed", c)
