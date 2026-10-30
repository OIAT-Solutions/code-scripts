"""Durable per-business-event recovery records. No approval or posting authority."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
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
            if "first_seen" not in {r[1] for r in db.execute("PRAGMA table_info(work)")}:
                db.execute("ALTER TABLE work ADD COLUMN first_seen TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE work SET first_seen=updated_at WHERE first_seen=''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, namespace: str, identity: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT source_sha,status,evidence,updated_at,first_seen FROM work WHERE namespace=? AND identity=?",
                             (namespace, identity)).fetchone()
        return ({"source_sha": row[0], "status": row[1], "evidence": json.loads(row[2]), "updated_at": row[3], "first_seen": row[4]}
                if row else None)

    def put(self, namespace: str, identity: str, source_sha: str, status: str, evidence: dict):
        self.put_many([(namespace, identity, source_sha, status, evidence)])

    def put_many(self, records):
        """One durable commit for source discovery; financial outcomes still commit individually."""
        stamp = datetime.now(timezone.utc).isoformat()
        values = [(ns, key, sha, status, json.dumps(evidence, sort_keys=True, default=str), stamp, stamp)
                  for ns, key, sha, status, evidence in records]
        with self.connect() as db:
            db.executemany("INSERT INTO work VALUES (?,?,?,?,?,?,?) ON CONFLICT(namespace,identity) DO UPDATE SET "
                       "source_sha=excluded.source_sha,status=excluded.status,evidence=excluded.evidence,updated_at=excluded.updated_at",
                           values)

    def unresolved(self, namespace: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT identity,status,updated_at,evidence,first_seen,source_sha FROM work WHERE namespace=? "
                              "AND status NOT IN ('verified','posted','already posted') ORDER BY first_seen,identity",
                              (namespace,)).fetchall()
        return [{"identity": r[0], "status": r[1], "updated_at": r[2], "evidence": json.loads(r[3]),
                 "first_seen": r[4], "source_sha": r[5]} for r in rows]
