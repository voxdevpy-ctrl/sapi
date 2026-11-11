"""Persistência SQLite: itens, tarefas (máquina de estados), eventos, relatórios, decisões."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config

# tarefas que ainda podem usar o material de origem (a retenção não exclui enquanto existirem)
ACTIVE = ("PENDENTE", "EM_EXECUCAO", "EM_VALIDACAO", "AUDITADO", "PENDENTE_AUDITORIA", "FALHA")

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities(
  id TEXT PRIMARY KEY, created_at TEXT, topic TEXT, summary TEXT, reason TEXT, source_url TEXT,
  source_title TEXT, channel TEXT, channel_id TEXT, published_at TEXT, niche TEXT, evidence TEXT,
  potential TEXT, usage_notes TEXT, confidence REAL, features TEXT, availability TEXT,
  authorization TEXT, state TEXT, rec_explain TEXT, is_test INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY, opportunity_id TEXT, url TEXT, title TEXT, channel TEXT, duration REAL,
  acquired_at TEXT, original_path TEXT, original_size INTEGER, original_sha256 TEXT,
  compressed_path TEXT, compressed_size INTEGER, compressed_sha256 TEXT, compressed_at TEXT,
  compression_note TEXT, retention_until TEXT, retention_state TEXT, deleted_at TEXT,
  freed_bytes INTEGER DEFAULT 0, probe TEXT, state TEXT, is_test INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS transcripts(
  video_id TEXT PRIMARY KEY, path TEXT, language TEXT, participants TEXT, topics TEXT,
  summary TEXT, low_conf TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS cuts(
  id TEXT PRIMARY KEY, video_id TEXT, start REAL, end REAL, title_tmp TEXT, topic TEXT,
  summary TEXT, transcript TEXT, participants TEXT, context_before TEXT, context_after TEXT,
  reason TEXT, scores TEXT, score_total REAL, alerts TEXT, preview_path TEXT, segment_path TEXT,
  segment_offset REAL DEFAULT 0, features TEXT, criteria_version TEXT, state TEXT,
  created_at TEXT, is_test INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, item_id TEXT, score INTEGER, label TEXT,
  reason TEXT, features TEXT, snapshot TEXT, decided_at TEXT, criteria_version TEXT,
  undone INTEGER DEFAULT 0, is_test INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, stage TEXT, item_type TEXT, item_id TEXT, agent TEXT, supervisor TEXT,
  state TEXT, attempts INTEGER DEFAULT 0, not_before TEXT, payload TEXT, output TEXT, error TEXT,
  created_at TEXT, updated_at TEXT, UNIQUE(stage, item_id));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, task_id TEXT, item_id TEXT, agent TEXT,
  supervisor TEXT, requested TEXT, performed TEXT, result TEXT, files TEXT, error TEXT,
  exit_code INTEGER, state TEXT);
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, stage TEXT, agent TEXT, supervisor TEXT,
  ts TEXT, status TEXT, body TEXT, manager_decision TEXT);
CREATE TABLE IF NOT EXISTS exports(
  id TEXT PRIMARY KEY, cut_id TEXT UNIQUE, video_path TEXT, srt_path TEXT, folder TEXT,
  produced_at TEXT, exported_at TEXT, scheduled_for TEXT, qc_status TEXT, qc TEXT, title TEXT,
  description TEXT, hashtags TEXT, platform TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS publications(
  id TEXT PRIMARY KEY, export_id TEXT, platform TEXT, idem_key TEXT UNIQUE, state TEXT,
  external_id TEXT, url TEXT, attempts INTEGER DEFAULT 0, published_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS postings(
  export_id TEXT, platform TEXT, url TEXT, posted_at TEXT, PRIMARY KEY(export_id, platform));
CREATE TABLE IF NOT EXISTS retention_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, video_id TEXT, action TEXT, path TEXT,
  bytes INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS profile_changes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, feature TEXT, old REAL, new REAL, n_eff REAL,
  explanation TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS page_videos(
  id TEXT PRIMARY KEY, lote TEXT, source_url TEXT UNIQUE, title TEXT, channel TEXT, views INTEGER,
  duration REAL, jokes TEXT, meta TEXT, state TEXT, approval1 TEXT, approval2 TEXT, auto INTEGER DEFAULT 0,
  caption TEXT, final_path TEXT, folder TEXT, description TEXT, hashtags TEXT, cover TEXT, created_at TEXT);
"""

JSON_COLS = {"evidence", "features", "availability", "authorization", "probe", "participants",
             "topics", "low_conf", "scores", "alerts", "snapshot", "payload", "output", "files",
             "body", "qc", "hashtags", "rec_explain", "jokes", "meta"}

_lock = threading.RLock()


def db_path() -> Path:
    return config.PROD / "cortes.db"


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def conn() -> sqlite3.Connection:
    global _conn, _conn_path
    if _conn is None or _conn_path != db_path():
        _conn = connect()
        _conn_path = db_path()
        _conn.executescript(SCHEMA)
        _migrate(_conn)
    return _conn


MIGRATIONS = [  # colunas acrescentadas depois da criação do banco (idempotente)
    ("opportunities", "language", "TEXT"), ("opportunities", "language_source", "TEXT"),
    ("videos", "language", "TEXT"),
    ("exports", "video_deleted_at", "TEXT"), ("exports", "cloud_path", "TEXT"),
    ("exports", "downloaded_at", "TEXT"), ("exports", "download_count", "INTEGER"),
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, col, typ in MIGRATIONS:
        if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    c.commit()


def reset_connection() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


@contextmanager
def tx():
    with _lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _enc(k, v):
    return json.dumps(v, ensure_ascii=False) if k in JSON_COLS and v is not None and not isinstance(v, str) else v


def row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str) and d[k][:1] in "[{":
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def insert(table: str, data: dict, replace: bool = False) -> None:
    cols = ",".join(data)
    q = ",".join("?" * len(data))
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    with tx() as c:
        c.execute(f"{verb} INTO {table}({cols}) VALUES({q})", [_enc(k, v) for k, v in data.items()])


def update(table: str, key: str, data: dict, key_col: str = "id") -> None:
    sets = ",".join(f"{k}=?" for k in data)
    with tx() as c:
        c.execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?", [_enc(k, v) for k, v in data.items()] + [key])


def get(table: str, key: str, key_col: str = "id") -> dict | None:
    with _lock:
        return row(conn().execute(f"SELECT * FROM {table} WHERE {key_col}=?", (key,)).fetchone())


def query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [row(r) for r in conn().execute(sql, params).fetchall()]


def kv_get(k: str, default=None):
    r = query("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r[0]["v"]) if r else default


def kv_set(k: str, v) -> None:
    insert("kv", {"k": k, "v": json.dumps(v)}, replace=True)
