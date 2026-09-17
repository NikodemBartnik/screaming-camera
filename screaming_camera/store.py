"""SQLite event log + JPEG snapshots."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite
import cv2
import numpy as np

from .analyzer import Analysis
from .config import StorageConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    camera_id TEXT NOT NULL,
    camera_name TEXT NOT NULL,
    trigger TEXT NOT NULL,
    threat_level INTEGER NOT NULL,
    scene TEXT NOT NULL,
    people TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    message TEXT NOT NULL,
    spoken INTEGER NOT NULL,
    decision TEXT NOT NULL,
    speakers TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    model TEXT NOT NULL,
    error TEXT NOT NULL,
    armed INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts DESC);
"""


class EventStore:
    def __init__(self, cfg: StorageConfig):
        self.cfg = cfg
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self.cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cfg.snapshots_dir).mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.cfg.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    def save_snapshot(self, image: np.ndarray, camera_id: str, ts: float) -> str:
        name = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(ts))}-{int((ts % 1) * 1000):03d}-{camera_id}.jpg"
        path = Path(self.cfg.snapshots_dir) / name
        cv2.imwrite(str(path), image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return name

    async def add(self, *, ts: float, camera_id: str, camera_name: str, trigger: str, analysis: Analysis,
                  spoken: bool, decision: str, speakers: list[str], snapshot: str, armed: bool) -> dict[str, Any]:
        assert self.db is not None
        row = {
            "ts": ts, "camera_id": camera_id, "camera_name": camera_name, "trigger": trigger,
            "threat_level": analysis.threat_level, "scene": analysis.scene,
            "people": json.dumps([p.model_dump() for p in analysis.people]),
            "reasoning": analysis.reasoning, "message": analysis.message, "spoken": int(spoken),
            "decision": decision, "speakers": json.dumps(speakers), "snapshot": snapshot,
            "latency_ms": analysis.latency_ms, "model": analysis.model, "error": analysis.error, "armed": int(armed),
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = await self.db.execute(f"INSERT INTO events ({cols}) VALUES ({marks})", tuple(row.values()))
        await self.db.commit()
        row["id"] = cur.lastrowid
        return self._public(row)

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        d = dict(row)
        d["people"] = json.loads(d["people"]) if isinstance(d["people"], str) else d["people"]
        d["speakers"] = json.loads(d["speakers"]) if isinstance(d["speakers"], str) else d["speakers"]
        d["spoken"] = bool(d["spoken"])
        d["armed"] = bool(d["armed"])
        return d

    async def list(self, limit: int = 100, before_id: int | None = None, camera_id: str | None = None,
                   spoken_only: bool = False) -> list[dict[str, Any]]:
        assert self.db is not None
        where, args = [], []
        if before_id:
            where.append("id < ?"); args.append(before_id)
        if camera_id:
            where.append("camera_id = ?"); args.append(camera_id)
        if spoken_only:
            where.append("spoken = 1")
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        async with self.db.execute(sql, args) as cur:
            return [self._public(r) for r in await cur.fetchall()]

    async def stats(self) -> dict[str, Any]:
        assert self.db is not None
        day_ago = time.time() - 86400
        async with self.db.execute(
            "SELECT COUNT(*) AS n, SUM(spoken) AS spoken, MAX(threat_level) AS max_threat FROM events WHERE ts > ?",
            (day_ago,)) as cur:
            r = await cur.fetchone()
        return {"events_24h": r["n"] or 0, "spoken_24h": r["spoken"] or 0, "max_threat_24h": r["max_threat"] or 0}

    async def cleanup(self) -> int:
        """Delete events (and snapshots) older than keep_days."""
        assert self.db is not None
        cutoff = time.time() - self.cfg.keep_days * 86400
        async with self.db.execute("SELECT snapshot FROM events WHERE ts < ?", (cutoff,)) as cur:
            old = [r["snapshot"] for r in await cur.fetchall()]
        for name in old:
            try:
                (Path(self.cfg.snapshots_dir) / name).unlink(missing_ok=True)
            except OSError:
                pass
        await self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        await self.db.commit()
        return len(old)
