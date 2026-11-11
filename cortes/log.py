"""Registro técnico estruturado (SQLite + JSONL diário), com remoção de segredos."""
from __future__ import annotations

import json
import re

from . import config, db

_SECRET = re.compile(r"(?i)(token|senha|password|secret|cookie|authorization|api[_-]?key)([\"'=:\s]+)([^\s\"',;]+)")


def redact(text):
    if text is None:
        return None
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", str(text))


def event(*, task_id=None, item_id=None, agent=None, supervisor=None, requested=None,
          performed=None, result=None, files=None, error=None, exit_code=None, state=None) -> None:
    rec = {"ts": config.iso(), "task_id": task_id, "item_id": item_id, "agent": agent,
           "supervisor": supervisor, "requested": requested, "performed": redact(performed),
           "result": redact(result), "files": files, "error": redact(error),
           "exit_code": exit_code, "state": state}
    db.insert("events", rec)
    f = config.DIRS["logs"] / f"{rec['ts'][:10]}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def task_events(task_id: str) -> list[dict]:
    return db.query("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))
