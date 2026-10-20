"""Petit cache SQLite transactionnel ; aucun changement du CSV historique."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, fetched REAL, payload TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def key(*parts):
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()

    def get(self, key, ttl=None):
        with self.connect() as db:
            row = db.execute("SELECT fetched,payload FROM cache WHERE key=?", (key,)).fetchone()
        # ttl=0 (--refresh) doit toujours expirer, même à horloge identique.
        if not row or (ttl is not None and datetime.now(timezone.utc).timestamp() - row[0] >= ttl):
            return None
        return json.loads(row[1])

    def put(self, key, payload):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                       (key, datetime.now(timezone.utc).timestamp(), encoded))
