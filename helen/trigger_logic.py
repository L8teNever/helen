"""Trigger-link resolution: pick the best task instance by current time."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from helen import db


@dataclass
class TriggerResult:
    status: str  # "completed_now" | "already_completed" | "nothing_today" | "unknown_trigger"
    trigger_name: Optional[str] = None
    instance_id: Optional[int] = None
    task_name: Optional[str] = None
    due_time: Optional[str] = None
    completed_at: Optional[str] = None


def _parse_hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def resolve(slug: str, now: datetime) -> TriggerResult:
    trigger = db.get_trigger_by_slug(slug)
    if trigger is None:
        return TriggerResult(status="unknown_trigger")

    today = now.date().isoformat()
    def_ids = db.list_trigger_task_def_ids(trigger["id"])
    if not def_ids:
        return TriggerResult(status="nothing_today", trigger_name=trigger["name"])

    candidates: list = []
    for def_id in def_ids:
        inst = db.get_or_none_instance_for(def_id, today)
        if inst is not None:
            candidates.append(inst)

    if not candidates:
        return TriggerResult(status="nothing_today", trigger_name=trigger["name"])

    now_min = now.hour * 60 + now.minute

    open_candidates = [c for c in candidates if not c["completed"]]
    if not open_candidates:
        best = min(candidates, key=lambda c: abs(_parse_hhmm(c["due_time"]) - now_min))
        td = db.get_task_def(best["task_def_id"])
        return TriggerResult(
            status="already_completed",
            trigger_name=trigger["name"],
            instance_id=best["id"],
            task_name=td["name"] if td else "?",
            due_time=best["due_time"],
            completed_at=best["completed_at"],
        )

    best = min(open_candidates, key=lambda c: abs(_parse_hhmm(c["due_time"]) - now_min))
    td = db.get_task_def(best["task_def_id"])
    return TriggerResult(
        status="open_match",
        trigger_name=trigger["name"],
        instance_id=best["id"],
        task_name=td["name"] if td else "?",
        due_time=best["due_time"],
    )
