"""SQLite layer: schema, connection pool, typed helpers."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

DB_PATH = os.environ.get("HELEN_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "helen.db"))

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    token_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_defs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    time_of_day TEXT NOT NULL,
    schedule_type TEXT NOT NULL CHECK (schedule_type IN ('daily','weekdays')),
    weekdays_mask INTEGER NOT NULL DEFAULT 127,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    image_filename TEXT,
    notes TEXT,
    times TEXT NOT NULL DEFAULT '',
    timer_duration INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_def_id INTEGER NOT NULL REFERENCES task_defs(id) ON DELETE CASCADE,
    due_date TEXT NOT NULL,
    due_time TEXT NOT NULL,
    google_task_id TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    last_synced_at TEXT,
    UNIQUE(task_def_id, due_date, due_time)
);

CREATE INDEX IF NOT EXISTS idx_task_instances_date ON task_instances(due_date);
CREATE INDEX IF NOT EXISTS idx_task_instances_google ON task_instances(google_task_id);

CREATE TABLE IF NOT EXISTS triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trigger_tasks (
    trigger_id INTEGER NOT NULL REFERENCES triggers(id) ON DELETE CASCADE,
    task_def_id INTEGER NOT NULL REFERENCES task_defs(id) ON DELETE CASCADE,
    PRIMARY KEY (trigger_id, task_def_id)
);

CREATE TABLE IF NOT EXISTS trigger_companions (
    trigger_id INTEGER NOT NULL REFERENCES triggers(id) ON DELETE CASCADE,
    task_def_id INTEGER NOT NULL REFERENCES task_defs(id) ON DELETE CASCADE,
    PRIMARY KEY (trigger_id, task_def_id)
);

"""


def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


_conn: Optional[sqlite3.Connection] = None


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
        return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    with _lock:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def init_db() -> None:
    conn = get_conn()
    with _lock:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema migrations for existing databases."""
    # task_defs columns
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_defs)")}
    if "image_filename" not in cols:
        conn.execute("ALTER TABLE task_defs ADD COLUMN image_filename TEXT")
    if "notes" not in cols:
        conn.execute("ALTER TABLE task_defs ADD COLUMN notes TEXT")
    if "times" not in cols:
        conn.execute("ALTER TABLE task_defs ADD COLUMN times TEXT NOT NULL DEFAULT ''")
        # Seed `times` from the legacy single `time_of_day` column.
        conn.execute("UPDATE task_defs SET times = time_of_day WHERE times = ''")
    if "timer_duration" not in cols:
        conn.execute("ALTER TABLE task_defs ADD COLUMN timer_duration INTEGER DEFAULT 0")

    # task_instances: relax UNIQUE(task_def_id, due_date) → (task_def_id, due_date, due_time).
    # SQLite can't ALTER a UNIQUE constraint, so we rebuild the table when the old shape is detected.
    needs_rebuild = False
    for idx in conn.execute("PRAGMA index_list(task_instances)").fetchall():
        if idx[2]:  # unique
            idx_cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{idx[1]}")')]
            if idx_cols == ["task_def_id", "due_date"]:
                needs_rebuild = True
                break
    if needs_rebuild:
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            BEGIN;
            CREATE TABLE task_instances_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_def_id INTEGER NOT NULL REFERENCES task_defs(id) ON DELETE CASCADE,
                due_date TEXT NOT NULL,
                due_time TEXT NOT NULL,
                google_task_id TEXT,
                completed INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                last_synced_at TEXT,
                UNIQUE(task_def_id, due_date, due_time)
            );
            INSERT INTO task_instances_new
                (id, task_def_id, due_date, due_time, google_task_id, completed, completed_at, last_synced_at)
                SELECT id, task_def_id, due_date, due_time, google_task_id, completed, completed_at, last_synced_at
                FROM task_instances;
            DROP TABLE task_instances;
            ALTER TABLE task_instances_new RENAME TO task_instances;
            CREATE INDEX idx_task_instances_date ON task_instances(due_date);
            CREATE INDEX idx_task_instances_google ON task_instances(google_task_id);
            COMMIT;
            PRAGMA foreign_keys=ON;
        """)

    # trigger_companions: collapse to (trigger_id, task_def_id) PK.
    # The previous shape pinned each entry to a specific time-of-day; the new
    # semantic uses the trigger's resolved task time as the filter at scan time,
    # so we de-duplicate any legacy rows down to one per (trigger, def).
    tc_cols = {r[1] for r in conn.execute("PRAGMA table_info(trigger_companions)")}
    if "time_of_day" in tc_cols:
        old_pairs = list(conn.execute("SELECT DISTINCT trigger_id, task_def_id FROM trigger_companions"))
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            DROP TABLE trigger_companions;
            CREATE TABLE trigger_companions (
                trigger_id INTEGER NOT NULL REFERENCES triggers(id) ON DELETE CASCADE,
                task_def_id INTEGER NOT NULL REFERENCES task_defs(id) ON DELETE CASCADE,
                PRIMARY KEY (trigger_id, task_def_id)
            );
            PRAGMA foreign_keys=ON;
        """)
        for trig_id, def_id in old_pairs:
            conn.execute(
                "INSERT OR IGNORE INTO trigger_companions(trigger_id, task_def_id) VALUES (?,?)",
                (trig_id, def_id),
            )

    # One-shot migration: Google Tasks → Google Calendar. Drop today+future
    # local instances so the scheduler recreates them as Calendar events.
    # Past instances are kept as history. Old Google task IDs are abandoned —
    # the user deletes the legacy "Helen" task list in Google Tasks manually.
    marker = conn.execute("SELECT value FROM config WHERE key='helen_migration_calendar_done'").fetchone()
    if not marker:
        today = datetime.utcnow().date().isoformat()
        conn.execute("DELETE FROM task_instances WHERE due_date >= ?", (today,))
        conn.execute(
            "INSERT INTO config(key,value) VALUES('helen_migration_calendar_done','1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        # Drop the legacy tasklist id so the new Calendar flow starts clean.
        conn.execute("DELETE FROM config WHERE key='helen_tasklist_id'")

    # push notification setup
    ti_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_instances)")}
    if "push_notified" not in ti_cols:
        conn.execute("ALTER TABLE task_instances ADD COLUMN push_notified INTEGER DEFAULT 0")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            endpoint TEXT PRIMARY KEY,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)



# ---------- config ----------

def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    row = get_conn().execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: Optional[str]) -> None:
    with _lock:
        if value is None:
            get_conn().execute("DELETE FROM config WHERE key=?", (key,))
        else:
            get_conn().execute(
                "INSERT INTO config(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def all_config() -> dict[str, str]:
    return {r["key"]: r["value"] for r in get_conn().execute("SELECT key,value FROM config")}


# ---------- oauth ----------

def save_oauth_token(token_json: str) -> None:
    with _lock:
        get_conn().execute(
            "INSERT INTO oauth_tokens(id,token_json,updated_at) VALUES(1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET token_json=excluded.token_json, updated_at=excluded.updated_at",
            (token_json, datetime.utcnow().isoformat()),
        )


def load_oauth_token() -> Optional[str]:
    row = get_conn().execute("SELECT token_json FROM oauth_tokens WHERE id=1").fetchone()
    return row["token_json"] if row else None


def clear_oauth_token() -> None:
    with _lock:
        get_conn().execute("DELETE FROM oauth_tokens")


# ---------- task defs ----------

def list_task_defs(active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM task_defs"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY time_of_day, id"
    return list(get_conn().execute(sql))


def get_task_def(def_id: int) -> Optional[sqlite3.Row]:
    return get_conn().execute("SELECT * FROM task_defs WHERE id=?", (def_id,)).fetchone()


def _times_csv(times: list[str]) -> str:
    return ",".join(times)


def parse_times(csv: Optional[str]) -> list[str]:
    if not csv:
        return []
    return [t.strip() for t in csv.split(",") if t.strip()]


def create_task_def(
    name: str, times: list[str], schedule_type: str, weekdays_mask: int,
    notes: Optional[str] = None, image_filename: Optional[str] = None,
    timer_duration: int = 0,
) -> int:
    if not times:
        raise ValueError("Mindestens eine Uhrzeit erforderlich.")
    primary = times[0]
    with _lock:
        cur = get_conn().execute(
            "INSERT INTO task_defs(name,time_of_day,schedule_type,weekdays_mask,active,created_at,notes,image_filename,times,timer_duration) "
            "VALUES(?,?,?,?,1,?,?,?,?,?)",
            (name, primary, schedule_type, weekdays_mask, datetime.utcnow().isoformat(),
             notes, image_filename, _times_csv(times), timer_duration),
        )
        return cur.lastrowid


def update_task_def(
    def_id: int, name: str, times: list[str], schedule_type: str, weekdays_mask: int, active: int,
    notes: Optional[str] = None, image_filename: Optional[str] = None,
    timer_duration: int = 0,
) -> None:
    if not times:
        raise ValueError("Mindestens eine Uhrzeit erforderlich.")
    primary = times[0]
    with _lock:
        get_conn().execute(
            "UPDATE task_defs SET name=?, time_of_day=?, schedule_type=?, weekdays_mask=?, active=?, "
            "notes=?, image_filename=?, times=?, timer_duration=? WHERE id=?",
            (name, primary, schedule_type, weekdays_mask, active, notes, image_filename,
             _times_csv(times), timer_duration, def_id),
        )


def set_task_def_image(def_id: int, image_filename: Optional[str]) -> None:
    with _lock:
        get_conn().execute(
            "UPDATE task_defs SET image_filename=? WHERE id=?",
            (image_filename, def_id),
        )


def delete_task_def(def_id: int) -> None:
    with _lock:
        get_conn().execute("DELETE FROM task_defs WHERE id=?", (def_id,))


# ---------- task instances ----------

def list_instances_by_def(def_id: int, from_date: Optional[str] = None) -> list[sqlite3.Row]:
    if from_date is None:
        return list(get_conn().execute(
            "SELECT * FROM task_instances WHERE task_def_id=? ORDER BY due_date",
            (def_id,),
        ))
    return list(get_conn().execute(
        "SELECT * FROM task_instances WHERE task_def_id=? AND due_date >= ? ORDER BY due_date",
        (def_id, from_date),
    ))


def delete_instance(inst_id: int) -> None:
    with _lock:
        get_conn().execute("DELETE FROM task_instances WHERE id=?", (inst_id,))


def list_instances_for_date(due_date: str) -> list[sqlite3.Row]:
    return list(get_conn().execute(
        "SELECT ti.*, td.name AS def_name, td.time_of_day AS def_time "
        "FROM task_instances ti JOIN task_defs td ON td.id=ti.task_def_id "
        "WHERE ti.due_date=? ORDER BY ti.due_time, ti.id",
        (due_date,),
    ))


def get_instance(inst_id: int) -> Optional[sqlite3.Row]:
    return get_conn().execute(
        "SELECT ti.*, td.name AS def_name FROM task_instances ti "
        "JOIN task_defs td ON td.id=ti.task_def_id WHERE ti.id=?",
        (inst_id,),
    ).fetchone()


def get_instance_by_google_id(google_task_id: str) -> Optional[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM task_instances WHERE google_task_id=?",
        (google_task_id,),
    ).fetchone()


def list_instances_by_google_id(google_task_id: str) -> list[sqlite3.Row]:
    """All instances sharing a Calendar event id (bundle members)."""
    return list(get_conn().execute(
        "SELECT ti.*, td.name AS def_name, td.notes AS def_notes "
        "FROM task_instances ti JOIN task_defs td ON td.id=ti.task_def_id "
        "WHERE ti.google_task_id=? ORDER BY td.name, ti.id",
        (google_task_id,),
    ))


def list_instances_for_bundle(due_date: str, due_time: str) -> list[sqlite3.Row]:
    """All instances scheduled for the same (date, time) — joined with task_def info."""
    return list(get_conn().execute(
        "SELECT ti.*, td.name AS def_name, td.notes AS def_notes, td.active AS def_active "
        "FROM task_instances ti JOIN task_defs td ON td.id=ti.task_def_id "
        "WHERE ti.due_date=? AND ti.due_time=? ORDER BY td.name, ti.id",
        (due_date, due_time),
    ))


def get_or_none_instance_for(def_id: int, due_date: str, due_time: Optional[str] = None) -> Optional[sqlite3.Row]:
    if due_time is None:
        return get_conn().execute(
            "SELECT * FROM task_instances WHERE task_def_id=? AND due_date=? ORDER BY due_time LIMIT 1",
            (def_id, due_date),
        ).fetchone()
    return get_conn().execute(
        "SELECT * FROM task_instances WHERE task_def_id=? AND due_date=? AND due_time=?",
        (def_id, due_date, due_time),
    ).fetchone()


def list_instances_for_def_on_date(def_id: int, due_date: str) -> list[sqlite3.Row]:
    return list(get_conn().execute(
        "SELECT * FROM task_instances WHERE task_def_id=? AND due_date=? ORDER BY due_time",
        (def_id, due_date),
    ))


def create_instance(def_id: int, due_date: str, due_time: str, google_task_id: Optional[str]) -> int:
    with _lock:
        cur = get_conn().execute(
            "INSERT INTO task_instances(task_def_id,due_date,due_time,google_task_id,last_synced_at) "
            "VALUES(?,?,?,?,?)",
            (def_id, due_date, due_time, google_task_id, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def set_instance_google_id(inst_id: int, google_task_id: str) -> None:
    with _lock:
        get_conn().execute(
            "UPDATE task_instances SET google_task_id=?, last_synced_at=? WHERE id=?",
            (google_task_id, datetime.utcnow().isoformat(), inst_id),
        )


def set_instance_completed(inst_id: int, completed: bool) -> None:
    with _lock:
        get_conn().execute(
            "UPDATE task_instances SET completed=?, completed_at=?, last_synced_at=? WHERE id=?",
            (
                1 if completed else 0,
                datetime.utcnow().isoformat() if completed else None,
                datetime.utcnow().isoformat(),
                inst_id,
            ),
        )


# ---------- triggers ----------

def list_triggers() -> list[sqlite3.Row]:
    return list(get_conn().execute("SELECT * FROM triggers ORDER BY created_at DESC"))


def get_trigger_by_slug(slug: str) -> Optional[sqlite3.Row]:
    return get_conn().execute("SELECT * FROM triggers WHERE slug=?", (slug,)).fetchone()


def get_trigger(trigger_id: int) -> Optional[sqlite3.Row]:
    return get_conn().execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()


def create_trigger(slug: str, name: str) -> int:
    with _lock:
        cur = get_conn().execute(
            "INSERT INTO triggers(slug,name,created_at) VALUES(?,?,?)",
            (slug, name, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def delete_trigger(trigger_id: int) -> None:
    with _lock:
        get_conn().execute("DELETE FROM triggers WHERE id=?", (trigger_id,))


def set_trigger_tasks(trigger_id: int, task_def_ids: list[int]) -> None:
    with _lock, tx() as conn:
        conn.execute("DELETE FROM trigger_tasks WHERE trigger_id=?", (trigger_id,))
        conn.executemany(
            "INSERT INTO trigger_tasks(trigger_id,task_def_id) VALUES(?,?)",
            [(trigger_id, t) for t in task_def_ids],
        )


def list_trigger_task_def_ids(trigger_id: int) -> list[int]:
    return [r["task_def_id"] for r in get_conn().execute(
        "SELECT task_def_id FROM trigger_tasks WHERE trigger_id=?", (trigger_id,)
    )]


def list_trigger_task_defs(trigger_id: int) -> list[sqlite3.Row]:
    return list(get_conn().execute(
        "SELECT td.* FROM task_defs td JOIN trigger_tasks tt ON tt.task_def_id=td.id "
        "WHERE tt.trigger_id=? AND td.active=1 ORDER BY td.time_of_day",
        (trigger_id,),
    ))


def set_trigger_companions(trigger_id: int, task_def_ids: list[int]) -> None:
    """Replace this trigger's companion task_defs."""
    with _lock, tx() as conn:
        conn.execute("DELETE FROM trigger_companions WHERE trigger_id=?", (trigger_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO trigger_companions(trigger_id,task_def_id) VALUES(?,?)",
            [(trigger_id, did) for did in task_def_ids],
        )


def list_trigger_companion_def_ids(trigger_id: int) -> list[int]:
    return [r["task_def_id"] for r in get_conn().execute(
        "SELECT task_def_id FROM trigger_companions WHERE trigger_id=?", (trigger_id,)
    )]


# ---------- push subscriptions ----------

def add_push_subscription(endpoint: str, p256dh: str, auth: str) -> None:
    with _lock:
        get_conn().execute(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, created_at=excluded.created_at",
            (endpoint, p256dh, auth, datetime.utcnow().isoformat()),
        )

def list_push_subscriptions() -> list[sqlite3.Row]:
    return list(get_conn().execute("SELECT * FROM push_subscriptions"))

def delete_push_subscription(endpoint: str) -> None:
    with _lock:
        get_conn().execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))

def mark_instance_push_notified(instance_id: int) -> None:
    with _lock:
        get_conn().execute("UPDATE task_instances SET push_notified=1 WHERE id=?", (instance_id,))



