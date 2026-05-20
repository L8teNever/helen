"""Image storage for task definitions.

Files live next to the SQLite DB under data/images/{def_id}_{random}.{ext}.
DB stores only the filename (not full path) so the file can be relocated.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

log = logging.getLogger("helen.images")

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_BYTES = 5 * 1024 * 1024  # 5 MiB


def _data_dir() -> Path:
    db_path = os.environ.get(
        "HELEN_DB_PATH",
        str(Path(__file__).resolve().parent.parent / "data" / "helen.db"),
    )
    return Path(db_path).resolve().parent


def images_dir() -> Path:
    p = _data_dir() / "images"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save(def_id: int, raw: bytes, original_filename: str) -> str:
    """Store image bytes for task_def `def_id`. Returns the stored filename.

    Raises ValueError on invalid input.
    """
    if not raw:
        raise ValueError("Datei ist leer.")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"Datei zu groß (max {MAX_BYTES // 1024 // 1024} MiB).")
    ext = Path(original_filename or "").suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"Dateityp {ext or '?'} nicht erlaubt. Erlaubt: {sorted(ALLOWED_EXTS)}")
    filename = f"{def_id}_{secrets.token_hex(4)}{ext}"
    dest = images_dir() / filename
    dest.write_bytes(raw)
    return filename


def delete(filename: Optional[str]) -> None:
    if not filename:
        return
    p = images_dir() / filename
    try:
        if p.exists():
            p.unlink()
    except Exception:
        log.exception("Failed to delete image %s", p)


def path_for(filename: Optional[str]) -> Optional[Path]:
    if not filename:
        return None
    # Defend against path traversal.
    if "/" in filename or "\\" in filename or ".." in filename:
        return None
    p = images_dir() / filename
    return p if p.exists() else None
