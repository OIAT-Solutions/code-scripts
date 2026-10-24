"""Durable per-business-event recovery records. No approval or posting authority."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class WorkState:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS work (
                namespace TEXT NOT NULL, identity TEXT NOT NULL, source_sha TEXT NOT NULL,
                status TEXT NOT NULL, evidence TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(namespace, identity))""")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def get(self, namespace: str, identity: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT source_sha,status,evidence,updated_at FROM work WHERE namespace=? AND identity=?",
                             (namespace, identity)).fetchone()
        return ({"source_sha": row[0], "status": row[1], "evidence": json.loads(row[2]), "updated_at": row[3]}
                if row else None)

    def put(self, namespace: str, identity: str, source_sha: str, status: str, evidence: dict):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO work VALUES (?,?,?,?,?,?)",
                       (namespace, identity, source_sha, status, json.dumps(evidence, sort_keys=True, default=str),
                        datetime.now(timezone.utc).isoformat()))

    def unresolved(self, namespace: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT identity,status,updated_at,evidence FROM work WHERE namespace=? "
                              "AND status NOT IN ('verified','posted','already posted') ORDER BY updated_at",
                              (namespace,)).fetchall()
        return [{"identity": r[0], "status": r[1], "updated_at": r[2], "evidence": json.loads(r[3])} for r in rows]
