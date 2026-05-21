"""FastAPI settings app (port 8001). Configuration only — no completion toggles."""
from __future__ import annotations

import logging
import os
import re
import secrets
import string
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

SLUG_ALPHABET = string.ascii_letters + string.digits  # A-Z a-z 0-9
SLUG_LENGTH = 8


def _validate_times(raw: list[str]) -> list[str]:
    """Normalize a list of HH:MM strings: drop blanks, validate format, dedupe, sort."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for t in raw:
        s = (t or "").strip()
        if not s:
            continue
        if not HHMM_RE.match(s):
            raise HTTPException(400, f"Uhrzeit ungültig: {s!r}")
        if s in seen:
            continue
        seen.add(s)
        cleaned.append(s)
    if not cleaned:
        raise HTTPException(400, "Mindestens eine Uhrzeit eingeben.")
    cleaned.sort()
    return cleaned


def _generate_slug() -> str:
    for _ in range(50):
        candidate = "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))
        if db.get_trigger_by_slug(candidate) is None:
            return candidate
    raise RuntimeError("Konnte keinen freien Trigger-Slug erzeugen.")

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from helen import db, google_api, images as image_store, scheduler

log = logging.getLogger("helen.settings")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
WEEKDAY_BITS = [1, 2, 4, 8, 16, 32, 64]


def _connected() -> bool:
    return google_api.is_connected()


def build_app() -> FastAPI:
    app = FastAPI(title="HELEN Settings", docs_url=None, redoc_url=None)
    secret = os.environ.get("HELEN_SECRET_KEY", "dev-secret-change-me-please-32-bytes-min")
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax")
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(
            "settings_home.html",
            {
                "request": request,
                "connected": _connected(),
                "client_id_set": bool(db.get_config("oauth_client_id")),
                "client_secret_set": bool(db.get_config("oauth_client_secret")),
                "redirect_uri": google_api.redirect_url(),
                "helen_calendar_id": db.get_config("helen_calendar_id"),
                "active_page": "home",
            },
        )

    # ---- OAuth credentials ----

    @app.post("/oauth/credentials")
    async def save_credentials(
        request: Request,
        client_id: str = Form(...),
        client_secret: str = Form(...),
    ):
        db.set_config("oauth_client_id", client_id.strip())
        db.set_config("oauth_client_secret", client_secret.strip())
        request.session["flash"] = "Google-Zugangsdaten gespeichert."
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/oauth/start")
    async def oauth_start(request: Request):
        if not db.get_config("oauth_client_id"):
            raise HTTPException(400, "Erst Client-ID/Secret eintragen.")
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state
        try:
            url = google_api.authorization_url(state)
        except Exception as e:
            raise HTTPException(500, f"OAuth-Konfiguration fehlerhaft: {e}")
        return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/oauth/callback")
    async def oauth_callback(request: Request):
        expected_state = request.session.pop("oauth_state", None)
        got_state = request.query_params.get("state")
        if not expected_state or expected_state != got_state:
            raise HTTPException(400, "OAuth state mismatch.")
        try:
            google_api.exchange_code(got_state, str(request.url))
            google_api.ensure_helen_calendar()
            try:
                scheduler.generate_today()
            except Exception:
                log.exception("Initial generate_today after OAuth failed.")
            request.session["flash"] = "Mit Google verbunden. Helen-Kalender bereit."
        except Exception as e:
            log.exception("OAuth exchange failed.")
            request.session["flash"] = f"OAuth-Fehler: {e}"
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/oauth/disconnect")
    async def oauth_disconnect(request: Request):
        google_api.disconnect()
        request.session["flash"] = "Google-Verbindung entfernt."
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    # ---- Task defs ----

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request):
        return templates.TemplateResponse(
            "settings_tasks.html",
            {
                "request": request,
                "connected": _connected(),
                "defs": db.list_task_defs(),
                "active_page": "tasks",
                "trigger_base": os.environ.get("HELEN_TRIGGER_BASE_URL", "https://helen.l8tenever.com").rstrip("/"),
            },
        )

    @app.post("/tasks/create")
    async def create_task(
        request: Request,
        name: str = Form(...),
        times: List[str] = Form(...),
        schedule_type: str = Form(...),
        notes: Optional[str] = Form(None),
        image: Optional[UploadFile] = File(None),
        mon: Optional[str] = Form(None),
        tue: Optional[str] = Form(None),
        wed: Optional[str] = Form(None),
        thu: Optional[str] = Form(None),
        fri: Optional[str] = Form(None),
        sat: Optional[str] = Form(None),
        sun: Optional[str] = Form(None),
    ):
        if not name.strip():
            raise HTTPException(400, "Name fehlt.")
        times_clean = _validate_times(times)
        if schedule_type not in ("daily", "weekdays"):
            raise HTTPException(400, "Schedule-Typ ungültig.")
        mask = 127 if schedule_type == "daily" else _mask_from_flags(mon, tue, wed, thu, fri, sat, sun)
        if schedule_type == "weekdays" and mask == 0:
            raise HTTPException(400, "Mindestens einen Wochentag wählen.")
        notes_clean = (notes or "").strip() or None
        new_id = db.create_task_def(name.strip(), times_clean, schedule_type, mask, notes=notes_clean)
        if image and image.filename:
            raw = await image.read()
            if raw:
                try:
                    fname = image_store.save(new_id, raw, image.filename)
                    db.set_task_def_image(new_id, fname)
                except ValueError as e:
                    request.session["flash"] = f"Aufgabe angelegt, Bild abgelehnt: {e}"
        try:
            today = date.today()
            n = scheduler.generate_window(
                today, today + timedelta(days=scheduler.LOOKAHEAD_DAYS), only_def_id=new_id,
            )
            request.session["flash"] = f"Aufgabe gespeichert. {n} Instanz(en) angelegt."
        except Exception as e:
            log.exception("generate_window after create failed")
            request.session["flash"] = f"Aufgabe gespeichert (Generierung schlug fehl: {e})."
        return RedirectResponse("/tasks", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/tasks/{def_id}/update")
    async def update_task(
        request: Request,
        def_id: int,
        name: str = Form(...),
        times: List[str] = Form(...),
        schedule_type: str = Form(...),
        notes: Optional[str] = Form(None),
        image: Optional[UploadFile] = File(None),
        remove_image: Optional[str] = Form(None),
        active: Optional[str] = Form(None),
        mon: Optional[str] = Form(None),
        tue: Optional[str] = Form(None),
        wed: Optional[str] = Form(None),
        thu: Optional[str] = Form(None),
        fri: Optional[str] = Form(None),
        sat: Optional[str] = Form(None),
        sun: Optional[str] = Form(None),
    ):
        existing = db.get_task_def(def_id)
        if existing is None:
            raise HTTPException(404)
        times_clean = _validate_times(times)
        mask = 127 if schedule_type == "daily" else _mask_from_flags(mon, tue, wed, thu, fri, sat, sun)
        new_active = 1 if active else 0
        notes_clean = (notes or "").strip() or None

        new_image_filename = existing["image_filename"]
        if remove_image:
            image_store.delete(existing["image_filename"])
            new_image_filename = None
        if image and image.filename:
            raw = await image.read()
            if raw:
                try:
                    fname = image_store.save(def_id, raw, image.filename)
                    image_store.delete(new_image_filename)
                    new_image_filename = fname
                except ValueError as e:
                    request.session["flash"] = f"Bild abgelehnt: {e}"

        db.update_task_def(
            def_id, name.strip(), times_clean, schedule_type, mask, new_active,
            notes=notes_clean, image_filename=new_image_filename,
        )
        # Wipe today+future (could be stale due to schedule/time/active change),
        # then regenerate the lookahead window. Past instances are preserved as history.
        today = date.today()
        try:
            wiped = scheduler.wipe_def_instances(def_id, today)
            n_new = 0
            if new_active:
                n_new = scheduler.generate_window(
                    today, today + timedelta(days=scheduler.LOOKAHEAD_DAYS), only_def_id=def_id,
                )
            request.session["flash"] = f"Aufgabe aktualisiert ({wiped} entfernt, {n_new} neu)."
        except Exception as e:
            log.exception("Refresh after update failed")
            request.session["flash"] = f"Aufgabe aktualisiert (Sync schlug fehl: {e})."
        return RedirectResponse("/tasks", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/tasks/{def_id}/delete")
    async def delete_task(request: Request, def_id: int):
        existing = db.get_task_def(def_id)
        try:
            wiped = scheduler.wipe_def_instances(def_id, None)
        except Exception as e:
            log.exception("Wipe before delete failed.")
            wiped = -1
        if existing is not None:
            image_store.delete(existing["image_filename"])
        db.delete_task_def(def_id)
        request.session["flash"] = (
            f"Aufgabe gelöscht ({wiped} Termin(e) auch im Google Kalender entfernt)."
            if wiped >= 0
            else "Aufgabe lokal gelöscht (Google-Cleanup schlug fehl, ggf. manuell aufräumen)."
        )
        return RedirectResponse("/tasks", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/tasks/generate-now")
    async def generate_now(request: Request):
        try:
            n = scheduler.generate_today()
            request.session["flash"] = f"{n} neue Instanz(en) im Lookahead-Fenster erzeugt."
        except Exception as e:
            request.session["flash"] = f"Fehler: {e}"
        return RedirectResponse("/tasks", status_code=status.HTTP_303_SEE_OTHER)

    # ---- Triggers ----

    @app.get("/triggers", response_class=HTMLResponse)
    async def triggers_page(request: Request):
        trigger_base = os.environ.get("HELEN_TRIGGER_BASE_URL", "https://helen.l8tenever.com").rstrip("/")
        rows = []
        for t in db.list_triggers():
            assigned_ids = set(db.list_trigger_task_def_ids(t["id"]))
            rows.append({
                "trigger": t,
                "assigned_ids": assigned_ids,
                "url": f"{trigger_base}/t/{t['slug']}",
            })
        return templates.TemplateResponse(
            "settings_triggers.html",
            {
                "request": request,
                "connected": _connected(),
                "rows": rows,
                "all_defs": db.list_task_defs(),
                "active_page": "triggers",
            },
        )

    @app.post("/triggers/create")
    async def create_trigger(
        request: Request,
        name: str = Form(...),
    ):
        if not name.strip():
            raise HTTPException(400, "Name fehlt.")
        slug = _generate_slug()
        db.create_trigger(slug, name.strip())
        request.session["flash"] = f"Trigger angelegt. Slug: {slug}"
        return RedirectResponse("/triggers", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/triggers/{trigger_id}/assign")
    async def assign_trigger(request: Request, trigger_id: int):
        form = await request.form()
        task_def_ids = [int(v) for k, v in form.multi_items() if k == "task_def_ids"]
        if db.get_trigger(trigger_id) is None:
            raise HTTPException(404)
        db.set_trigger_tasks(trigger_id, task_def_ids)
        request.session["flash"] = "Zuordnung gespeichert."
        return RedirectResponse("/triggers", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/triggers/{trigger_id}/delete")
    async def delete_trigger(request: Request, trigger_id: int):
        db.delete_trigger(trigger_id)
        request.session["flash"] = "Trigger entfernt."
        return RedirectResponse("/triggers", status_code=status.HTTP_303_SEE_OTHER)

    return app


def _mask_from_flags(mon, tue, wed, thu, fri, sat, sun) -> int:
    flags = [mon, tue, wed, thu, fri, sat, sun]
    return sum(WEEKDAY_BITS[i] for i, f in enumerate(flags) if f)
