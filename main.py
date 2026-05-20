"""HELEN entrypoint: launches two uvicorn servers + APScheduler in one process."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import uvicorn

from helen import db, gui_app, scheduler, settings_app

logging.basicConfig(
    level=os.environ.get("HELEN_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("helen.main")

SETTINGS_PORT = int(os.environ.get("HELEN_SETTINGS_PORT", "8001"))
GUI_PORT = int(os.environ.get("HELEN_GUI_PORT", "8002"))


async def _run_server(app, port: int, name: str):
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level=os.environ.get("HELEN_LOG_LEVEL", "info").lower(),
        access_log=True,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    log.info("Starting %s on :%d", name, port)
    await server.serve()


async def _main() -> None:
    db.init_db()
    loop = asyncio.get_running_loop()
    sched = scheduler.start(loop)

    s_app = settings_app.build_app()
    g_app = gui_app.build_app()

    stop_event = asyncio.Event()

    def _request_stop(*_):
        log.info("Shutdown requested.")
        stop_event.set()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, _request_stop)
        loop.add_signal_handler(signal.SIGTERM, _request_stop)

    try:
        await asyncio.gather(
            _run_server(s_app, SETTINGS_PORT, "settings"),
            _run_server(g_app, GUI_PORT, "gui"),
        )
    finally:
        scheduler.shutdown()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
