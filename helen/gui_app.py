"""FastAPI GUI app (port 8002). Daily task list, toggles, trigger animation."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from helen import db, images as image_store, sse, sync, trigger_logic

log = logging.getLogger("helen.gui")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _parse_date(s: str | None) -> date:
    if not s:
        return date.today()
    try:
        return date.fromisoformat(s)
    except ValueError:
        return date.today()


def build_app() -> FastAPI:
    app = FastAPI(title="HELEN", docs_url=None, redoc_url=None)
    secret = os.environ.get("HELEN_SECRET_KEY", "dev-secret-change-me-please-32-bytes-min")
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax")
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, d: str | None = None):
        target = _parse_date(d)
        prev_d = (target - timedelta(days=1)).isoformat()
        next_d = (target + timedelta(days=1)).isoformat()
        instances = db.list_instances_for_date(target.isoformat())
        return templates.TemplateResponse(
            "gui_home.html",
            {
                "request": request,
                "instances": instances,
                "date": target,
                "today": date.today(),
                "prev_date": prev_d,
                "next_date": next_d,
            },
        )

    @app.post("/api/instances/{inst_id}/toggle")
    async def toggle(inst_id: int, request: Request):
        body = await request.json()
        completed = bool(body.get("completed"))
        ok = await sync.toggle_instance(inst_id, completed)
        if not ok:
            raise HTTPException(404)
        return JSONResponse({"id": inst_id, "completed": completed})

    @app.get("/api/instances")
    async def list_instances(d: str | None = None):
        target = _parse_date(d)
        rows = db.list_instances_for_date(target.isoformat())
        return [
            {
                "id": r["id"],
                "name": r["def_name"],
                "due_time": r["due_time"],
                "completed": bool(r["completed"]),
                "completed_at": r["completed_at"],
            }
            for r in rows
        ]

    @app.get("/api/instances/{inst_id}/preview")
    async def instance_preview(inst_id: int):
        inst = db.get_instance(inst_id)
        if inst is None:
            raise HTTPException(404)
        d = db.get_task_def(inst["task_def_id"])
        if d is None:
            raise HTTPException(404)
        return {
            "id": inst["id"],
            "def_id": d["id"],
            "name": d["name"],
            "due_time": inst["due_time"],
            "notes": d["notes"],
            "image_url": f"/img/{d['id']}?v={d['image_filename']}" if d["image_filename"] else None,
            "completed": bool(inst["completed"]),
            "completed_at": inst["completed_at"],
        }

    # ---- Image hosting (served from data/images via FileResponse) ----
    @app.get("/img/{def_id}")
    async def image(def_id: int):
        d = db.get_task_def(def_id)
        if d is None or not d["image_filename"]:
            raise HTTPException(404)
        p = image_store.path_for(d["image_filename"])
        if p is None:
            raise HTTPException(404)
        return FileResponse(str(p), headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/preview/{def_id}", response_class=HTMLResponse)
    async def preview_page(def_id: int, request: Request):
        d = db.get_task_def(def_id)
        if d is None:
            raise HTTPException(404)
        return templates.TemplateResponse(
            "preview.html",
            {
                "request": request,
                "def_row": d,
                "image_url": f"/img/{d['id']}?v={d['image_filename']}" if d["image_filename"] else None,
            },
        )

    # ---- PWA: manifest + service worker ----
    @app.get("/manifest.webmanifest")
    async def manifest():
        return JSONResponse({
            "name": "HELEN",
            "short_name": "HELEN",
            "description": "Tagesaufgaben und Erinnerungen",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#1d1b20",
            "theme_color": "#6750a4",
            "lang": "de",
            "orientation": "portrait",
            "icons": [
                {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            ],
        })

    @app.get("/sw.js")
    async def service_worker():
        sw_path = BASE_DIR / "static" / "sw.js"
        return FileResponse(str(sw_path), media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/events")
    async def events():
        queue = await sse.subscribe()

        async def stream():
            try:
                yield "retry: 5000\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=20)
                        yield payload
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                await sse.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ---- Trigger links ----

    def _today_open_companion_instances(
        trigger_id: int,
        anchor_time: str | None,
        exclude_inst_id: int | None = None,
    ) -> list[dict]:
        """Companion instances open today AT THE ANCHOR TIME.

        Filter logic: a companion task_def only contributes if it has a today
        instance scheduled at exactly `anchor_time`. Companions configured for
        other times are silently skipped so morning-Tabletten don't get hooked
        when the evening trigger fires.
        """
        if not anchor_time:
            return []
        today = date.today().isoformat()
        out: list[dict] = []
        for did in db.list_trigger_companion_def_ids(trigger_id):
            inst = db.get_or_none_instance_for(did, today, anchor_time)
            if inst is None or inst["completed"]:
                continue
            if exclude_inst_id is not None and inst["id"] == exclude_inst_id:
                continue
            d = db.get_task_def(did)
            out.append({
                "id": inst["id"],
                "name": d["name"] if d else "?",
                "due_time": anchor_time,
            })
        return out

    @app.get("/t/{slug}", response_class=HTMLResponse)
    async def trigger(slug: str, request: Request):
        result = trigger_logic.resolve(slug, datetime.now())

        if result.status == "open_match" and result.instance_id is not None:
            await sync.toggle_instance(result.instance_id, True)
            result.status = "completed_now"

        companions: list[dict] = []
        anchor_time = result.due_time
        if result.status in ("completed_now", "already_completed"):
            trig = db.get_trigger_by_slug(slug)
            if trig is not None:
                companions = _today_open_companion_instances(
                    trig["id"], anchor_time, result.instance_id,
                )

        return templates.TemplateResponse(
            "trigger_animation.html",
            {
                "request": request,
                "result": result,
                "companions": companions,
                "anchor_time": anchor_time,
                "slug": slug,
                "gui_url": "/",
            },
            status_code=200 if result.status != "unknown_trigger" else 404,
        )

    @app.post("/t/{slug}/companions", response_class=HTMLResponse)
    async def trigger_companions(slug: str, request: Request):
        form = await request.form()
        anchor_time = (form.get("anchor_time") or "").strip() or None
        trig = db.get_trigger_by_slug(slug)
        if trig is None:
            raise HTTPException(404)
        insts = _today_open_companion_instances(trig["id"], anchor_time)
        for inst in insts:
            await sync.toggle_instance(inst["id"], True)
        return templates.TemplateResponse(
            "trigger_animation.html",
            {
                "request": request,
                "result": trigger_logic.TriggerResult(
                    status="companions_done",
                    trigger_name=trig["name"],
                    due_time=anchor_time,
                ),
                "companions_count": len(insts),
                "companions": [],
                "slug": slug,
                "gui_url": "/",
            },
        )

    return app
