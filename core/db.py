"""SQLite job store — mirrors job/chunk/segment state from sever gốc."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import app_dir

_DB: threading.local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_DB, "conn") or _DB.conn is None:
        p = app_dir() / "jobs.db"
        _DB.conn = sqlite3.connect(str(p), check_same_thread=False)
        _DB.conn.row_factory = sqlite3.Row
        _DB.conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(_DB.conn)
    return _DB.conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT DEFAULT '',
            source_url TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            source_language TEXT DEFAULT 'zh',
            target_language TEXT DEFAULT 'vi',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            video_path TEXT DEFAULT '',
            audio_dir TEXT DEFAULT '',
            output_dir TEXT DEFAULT '',
            chunks INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            progress REAL DEFAULT 0.0,
            current_stage TEXT DEFAULT '',
            settings_json TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS provider_configs (
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            config_json TEXT DEFAULT '{}',
            PRIMARY KEY (category, name)
        );
    """)
    conn.commit()


# ── Job CRUD ──────────────────────────────────────────────

def create_job(source_url: str, title: str = "", source_lang: str = "zh",
               target_lang: str = "vi", settings_override: dict | None = None) -> str:
    import uuid
    job_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    conn.execute(
        "INSERT INTO jobs (id, title, source_url, source_language, target_language, "
        "created_at, updated_at, settings_json) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, title, source_url, source_lang, target_lang, now, now,
         json.dumps(settings_override or {}, ensure_ascii=False)),
    )
    conn.commit()
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    row = _get_conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["settings_json"] = json.loads(d.get("settings_json", "{}"))
    return d


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [job_id]
    _get_conn().execute(f"UPDATE jobs SET {sets} WHERE id=?", vals)
    _get_conn().commit()


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    rows = _get_conn().execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_next_pending_job() -> dict[str, Any] | None:
    row = _get_conn().execute(
        "SELECT * FROM jobs WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def count_pending_jobs() -> int:
    row = _get_conn().execute(
        "SELECT COUNT(*) AS c FROM jobs WHERE status='pending'"
    ).fetchone()
    return row["c"] if row else 0


def reset_stale_running_jobs() -> int:
    """Thu hồi trạng thái các job bị kẹt ở status 'running' khi app crash hoặc tắt đột ngột."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE jobs SET status='failed', error='Tiến trình bị gián đoạn do ứng dụng khởi động lại' "
        "WHERE status='running'"
    )
    conn.commit()
    return cur.rowcount


def delete_job(job_id: str) -> None:
    _get_conn().execute("DELETE FROM jobs WHERE id=?", (job_id,))
    _get_conn().commit()


# ── Provider config ──────────────────────────────────────

def get_provider_configs(category: str) -> dict[str, dict[str, Any]]:
    rows = _get_conn().execute(
        "SELECT name, config_json FROM provider_configs WHERE category=?", (category,)
    ).fetchall()
    return {r["name"]: json.loads(r["config_json"]) for r in rows}


def save_provider_config(category: str, name: str, config: dict[str, Any]) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO provider_configs (category, name, config_json) VALUES (?,?,?)",
        (category, name, json.dumps(config, ensure_ascii=False)),
    )
    conn.commit()


def delete_provider_config(category: str, name: str) -> None:
    _get_conn().execute(
        "DELETE FROM provider_configs WHERE category=? AND name=?", (category, name)
    )
    _get_conn().commit()
