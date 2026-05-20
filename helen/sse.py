"""In-process SSE broadcaster for live UI updates."""
from __future__ import annotations

import asyncio
import json
from typing import Any

_subscribers: set[asyncio.Queue] = set()
_lock = asyncio.Lock()


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    async with _lock:
        _subscribers.add(q)
    return q


async def unsubscribe(q: asyncio.Queue) -> None:
    async with _lock:
        _subscribers.discard(q)


async def broadcast(event: str, data: dict[str, Any]) -> None:
    payload = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    async with _lock:
        dead: list[asyncio.Queue] = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)


def broadcast_threadsafe(loop: asyncio.AbstractEventLoop, event: str, data: dict[str, Any]) -> None:
    """Schedule a broadcast from a non-async context (e.g. APScheduler thread)."""
    if loop.is_closed():
        return
    asyncio.run_coroutine_threadsafe(broadcast(event, data), loop)
