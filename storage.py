"""SQLite storage for data-source configs and LLM settings.

Replaces the former datasources.json / llm_config.json files. On the first
start with a fresh database, the legacy JSON files are imported and renamed
to *.json.migrated as a local backup so they are not re-imported later.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "shovel.db"
LEGACY_SOURCES_JSON = BASE_DIR / "datasources.json"
LEGACY_LLM_CFG_JSON = BASE_DIR / "llm_config.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasources (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    url        TEXT NOT NULL,
    prompt     TEXT NOT NULL DEFAULT '',
    example    TEXT NOT NULL,               -- JSON: object or array of objects
    images     TEXT NOT NULL DEFAULT '[]',  -- JSON array of uploaded filenames
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_settings (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    base_url TEXT NOT NULL DEFAULT '',
    api_key  TEXT NOT NULL DEFAULT '',
    model    TEXT NOT NULL DEFAULT ''
);
"""

_JSON_COLUMNS = {"example", "images"}


@contextmanager
def _db():
    """One short-lived connection per call: commit on success, close always.
    Concurrent Flask threads each get their own connection; SQLite serializes
    writes, and the 10s connect timeout absorbs brief lock contention."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _row_to_source(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "prompt": row["prompt"],
        "example": json.loads(row["example"]),
        "images": json.loads(row["images"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
def list_sources() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM datasources ORDER BY rowid").fetchall()
    return [_row_to_source(row) for row in rows]


def get_source(source_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM datasources WHERE id = ?", (source_id,)).fetchone()
    return _row_to_source(row) if row else None


def insert_source(source: dict) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO datasources (id, name, url, prompt, example, images, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source["id"], source["name"], source["url"], source.get("prompt") or "",
             json.dumps(source["example"], ensure_ascii=False),
             json.dumps(source.get("images") or [], ensure_ascii=False),
             source["created_at"], source["updated_at"]),
        )


def update_source(source_id: str, fields: dict) -> bool:
    """Set the given columns (example/images are JSON-encoded); returns False
    when the id does not exist. Column names come from internal code only."""
    sets, values = [], []
    for key, value in fields.items():
        if key in _JSON_COLUMNS:
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key} = ?")
        values.append(value)
    if not sets:
        return get_source(source_id) is not None
    values.append(source_id)
    with _db() as conn:
        cur = conn.execute(f"UPDATE datasources SET {', '.join(sets)} WHERE id = ?", values)
    return cur.rowcount > 0


def delete_source(source_id: str) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM datasources WHERE id = ?", (source_id,))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# LLM settings (single row, id = 1)
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT base_url, api_key, model FROM llm_settings WHERE id = 1").fetchone()
    if row is None:
        return {"base_url": "", "api_key": "", "model": ""}
    return dict(row)


def save_settings(settings: dict) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO llm_settings (id, base_url, api_key, model) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET base_url = excluded.base_url, "
            "api_key = excluded.api_key, model = excluded.model",
            (settings.get("base_url", ""), settings.get("api_key", ""), settings.get("model", "")),
        )


# ---------------------------------------------------------------------------
# Schema & one-time legacy import
# ---------------------------------------------------------------------------
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _import_legacy_json()


def _import_legacy_json() -> None:
    with _db() as conn:
        have_sources = conn.execute("SELECT COUNT(*) FROM datasources").fetchone()[0]
        have_settings = conn.execute("SELECT COUNT(*) FROM llm_settings").fetchone()[0]

        if LEGACY_SOURCES_JSON.exists():
            rows = _read_json(LEGACY_SOURCES_JSON)
            if isinstance(rows, list):
                if not have_sources:
                    for row in rows:
                        row.pop("chat", None)  # leftover of the removed chat-history feature
                        conn.execute(
                            "INSERT OR REPLACE INTO datasources "
                            "(id, name, url, prompt, example, images, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (row.get("id"), row.get("name"), row.get("url"), row.get("prompt") or "",
                             json.dumps(row.get("example") or {}, ensure_ascii=False),
                             json.dumps(row.get("images") or [], ensure_ascii=False),
                             row.get("created_at") or "", row.get("updated_at") or ""),
                        )
                    print(f"[storage] 已从 datasources.json 导入 {len(rows)} 个数据源")
                _archive(LEGACY_SOURCES_JSON)

        if LEGACY_LLM_CFG_JSON.exists():
            settings = _read_json(LEGACY_LLM_CFG_JSON)
            if isinstance(settings, dict):
                if not have_settings:
                    conn.execute(
                        "INSERT INTO llm_settings (id, base_url, api_key, model) VALUES (1, ?, ?, ?)",
                        (settings.get("base_url", ""), settings.get("api_key", ""),
                         settings.get("model", "")),
                    )
                    print("[storage] 已从 llm_config.json 导入 LLM 设置")
                _archive(LEGACY_LLM_CFG_JSON)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[storage] 读取 {path.name} 失败，未导入: {exc}")
        return None


def _archive(path: Path) -> None:
    target = path.with_suffix(".json.migrated")
    try:
        if target.exists():  # keep the original backup; the stray copy stays put
            print(f"[storage] {target.name} 已存在且库中已有数据，{path.name} 未导入，请手动确认后删除")
            return
        path.rename(target)
    except OSError as exc:
        print(f"[storage] 无法归档 {path.name}: {exc}")
