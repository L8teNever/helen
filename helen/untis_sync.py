"""WebUntis → Google Calendar sync.

Pulls the configured class's timetable for the current and next week, then
mirrors each period into the "Helen-Untis" Google Calendar:

    regular  → default colour
    cancelled → tomato (colorId 11)
    irregular → banana (colorId 5)
    exam      → blueberry (colorId 9)

Each Untis period.id is tracked in the local `untis_events` table together
with a fingerprint of its salient fields. On subsequent syncs we patch only
events whose fingerprint changed and delete events for periods that no
longer appear in the fetched window.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import date, datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from helen import db, google_api

log = logging.getLogger("helen.untis")

CONFIG_KEYS = ("untis_server", "untis_school", "untis_username", "untis_password", "untis_klasse")

STATUS_COLOR = {
    "cancelled": "11",  # tomato — class dropped
    "irregular": "5",   # banana — substitution / room change
    "exam":      "9",   # blueberry
}
STATUS_LABEL = {
    "cancelled": "entfällt",
    "irregular": "geändert",
    "exam":      "Klausur",
}


# ---------- config ----------

def is_configured() -> bool:
    return all(db.get_config(k) for k in CONFIG_KEYS)


def get_config_summary() -> dict[str, Optional[str]]:
    """Return current Untis config — password is replaced by a placeholder."""
    out: dict[str, Optional[str]] = {k: db.get_config(k) for k in CONFIG_KEYS}
    if out.get("untis_password"):
        out["untis_password"] = "···"
    return out


def save_config(server: str, school: str, username: str, password: str, klasse: str) -> None:
    db.set_config("untis_server", server.strip())
    db.set_config("untis_school", school.strip())
    db.set_config("untis_username", username.strip())
    db.set_config("untis_password", password)
    db.set_config("untis_klasse", klasse.strip())


def clear_config() -> None:
    for k in CONFIG_KEYS:
        db.set_config(k, None)


# ---------- sync ----------

def _names(items) -> str:
    """Compact join of Untis entity .name strings, sorted for stable fingerprints."""
    if not items:
        return ""
    parts: list[str] = []
    for it in items:
        n = getattr(it, "name", None) or getattr(it, "long_name", None) or str(it)
        if n:
            parts.append(str(n))
    return ", ".join(sorted(parts))


def _period_status(period) -> str:
    code = getattr(period, "code", None)
    if not code:
        return "regular"
    return str(code).lower()


def _fingerprint(period) -> str:
    """Stable hash of the fields we care about — bumps on substitution/room change."""
    parts = [
        period.start.isoformat() if period.start else "",
        period.end.isoformat() if period.end else "",
        _names(getattr(period, "subjects", None)),
        _names(getattr(period, "rooms", None)),
        _names(getattr(period, "teachers", None)),
        _period_status(period),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _event_body(period, tz_name: str) -> tuple[str, str, str, str, Optional[str]]:
    """Return (summary, description, start_iso, end_iso, color_id) for the period."""
    tz = ZoneInfo(tz_name)
    start = period.start.astimezone(tz) if period.start.tzinfo else period.start.replace(tzinfo=tz)
    end = period.end.astimezone(tz) if period.end.tzinfo else period.end.replace(tzinfo=tz)

    subj = _names(getattr(period, "subjects", None)) or "Stunde"
    rooms = _names(getattr(period, "rooms", None))
    teachers = _names(getattr(period, "teachers", None))
    status = _period_status(period)

    label = STATUS_LABEL.get(status)
    summary = subj
    if label:
        summary = f"{subj} ({label})"
    if rooms:
        summary = f"{summary} · {rooms}"

    desc_lines = [
        f"Fach: {subj}",
    ]
    if teachers:
        desc_lines.append(f"Lehrer: {teachers}")
    if rooms:
        desc_lines.append(f"Raum: {rooms}")
    desc_lines.append(f"Status: {STATUS_LABEL.get(status, 'regulär')}")
    description = "\n".join(desc_lines)

    return (
        summary,
        description,
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
        STATUS_COLOR.get(status),
    )


def _session_ctx():
    """Build a logged-in webuntis.Session. Caller must session.logout() in finally."""
    import webuntis
    server = db.get_config("untis_server")
    school = db.get_config("untis_school")
    username = db.get_config("untis_username")
    password = db.get_config("untis_password")
    if not all((server, school, username, password)):
        raise RuntimeError("Untis nicht vollständig konfiguriert.")
    s = webuntis.Session(
        server=server,
        school=school,
        username=username,
        password=password,
        useragent="HELEN-Untis-Sync/1.0",
    )
    s.login()
    return s


def _resolve_klasse(session, klasse_name: str):
    matches = [k for k in session.klassen() if (k.name or "").lower() == klasse_name.lower()]
    if not matches:
        raise RuntimeError(f"Klasse {klasse_name!r} bei Untis nicht gefunden.")
    return matches[0]


def _week_window(today: Optional[date] = None) -> tuple[date, date]:
    """Monday of current week through Sunday of next week (14 days)."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    end_sunday = monday + timedelta(days=13)
    return monday, end_sunday


def sync_window(today: Optional[date] = None) -> dict:
    """Run one sync pass. Returns a small stats dict.

    Raises on Untis/Google errors so callers can surface them to the UI.
    """
    if not is_configured():
        raise RuntimeError("Untis-Zugangsdaten fehlen.")
    if not google_api.is_connected():
        raise RuntimeError("Nicht mit Google verbunden.")

    tz_name = os.environ.get("HELEN_TZ", "Europe/Berlin")
    cal_id = google_api.ensure_helen_untis_calendar()
    start, end = _week_window(today)

    klasse_name = db.get_config("untis_klasse") or ""
    session = _session_ctx()
    try:
        klasse = _resolve_klasse(session, klasse_name)
        periods = list(session.timetable(klasse=klasse, start=start, end=end))
    finally:
        try:
            session.logout()
        except Exception:
            pass

    stats = {"created": 0, "updated": 0, "deleted": 0, "skipped": 0}
    seen_ids: set[str] = set()

    for p in periods:
        period_id = str(getattr(p, "id", None) or "")
        if not period_id:
            continue
        seen_ids.add(period_id)

        fp = _fingerprint(p)
        existing = db.get_untis_event(period_id)
        summary, description, start_iso, end_iso, color_id = _event_body(p, tz_name)
        event_date = (p.start.astimezone(ZoneInfo(tz_name)) if p.start.tzinfo
                      else p.start.replace(tzinfo=ZoneInfo(tz_name))).date().isoformat()

        if existing is None:
            try:
                ev = google_api.create_event(
                    title=summary, start_iso=start_iso, end_iso=end_iso, tz=tz_name,
                    description=description, calendar_id=cal_id, color_id=color_id,
                )
            except Exception:
                log.exception("create_event failed for period %s", period_id)
                continue
            db.upsert_untis_event(period_id, ev["id"], event_date, fp)
            stats["created"] += 1
            continue

        if existing["fingerprint"] == fp:
            stats["skipped"] += 1
            continue

        try:
            google_api.patch_event_fields(
                event_id=existing["google_event_id"],
                summary=summary, description=description,
                start_iso=start_iso, end_iso=end_iso, tz=tz_name,
                color_id=color_id, calendar_id=cal_id,
            )
        except Exception:
            log.exception("patch_event_fields failed for period %s", period_id)
            continue
        db.upsert_untis_event(period_id, existing["google_event_id"], event_date, fp)
        stats["updated"] += 1

    # Drop locally tracked events in the window that didn't show up this run.
    for row in db.list_untis_events_in_range(start.isoformat(), end.isoformat()):
        if row["period_id"] in seen_ids:
            continue
        try:
            google_api.delete_event(row["google_event_id"], calendar_id=cal_id)
        except Exception:
            log.exception("Failed to delete stale Untis event %s", row["google_event_id"])
        db.delete_untis_event(row["period_id"])
        stats["deleted"] += 1

    db.set_config("untis_last_sync_at", datetime.utcnow().isoformat())
    db.set_config("untis_last_sync_stats", f"{stats['created']}+{stats['updated']}~{stats['skipped']} −{stats['deleted']}")
    log.info("Untis sync: %s", stats)
    return stats
