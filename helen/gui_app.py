"""FastAPI GUI app (port 8002). Daily task list, toggles, trigger animation."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from helen import db, sse, sync, trigger_logic

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

    @app.get("/t/{slug}", response_class=HTMLResponse)
    async def trigger(slug: str, request: Request):
        result = trigger_logic.resolve(slug, datetime.now())

        if result.status == "open_match" and result.instance_id is not None:
            await sync.toggle_instance(result.instance_id, True)
            result.status = "completed_now"

        return templates.TemplateResponse(
            "trigger_animation.html",
            {
                "request": request,
                "result": result,
                "gui_url": "/",
            },
            status_code=200 if result.status != "unknown_trigger" else 404,
        )

    return app
