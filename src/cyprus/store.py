from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal

from .models import Item, now_utc

Status = Literal["new", "changed", "unchanged"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    category     TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    price        REAL,
    currency     TEXT,
    location     TEXT,
    company      TEXT,
    published_at TEXT,
    summary      TEXT,
    extra        TEXT NOT NULL DEFAULT '{}',
    fingerprint  TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
CREATE INDEX IF NOT EXISTS idx_items_last_seen ON items(last_seen);

CREATE TABLE IF NOT EXISTS item_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    change      TEXT NOT NULL,
    price       REAL,
    title       TEXT,
    fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_item ON item_history(item_id);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    fetched     INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
"""


@dataclass(slots=True)
class RunResult:
    source: str
    fetched: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    error: str | None = None
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def upsert(self, item: Item, seen_at: datetime | None = None) -> Status:
        seen = (seen_at or now_utc()).isoformat()
        row = item.to_row()
        fingerprint = item.fingerprint
        existing = self._conn.execute(
            "SELECT fingerprint, active FROM items WHERE item_id = ?", (item.item_id,)
        ).fetchone()

        if existing is None:
            self._conn.execute(
                """INSERT INTO items (item_id, source, category, title, url, price, currency, location,
                   company, published_at, summary, extra, fingerprint, first_seen, last_seen, active)
                   VALUES (:item_id, :source, :category, :title, :url, :price, :currency, :location,
                   :company, :published_at, :summary, :extra, :fingerprint, :seen, :seen, 1)""",
                {**row, "item_id": item.item_id, "fingerprint": fingerprint, "seen": seen},
            )
            self._record(item, seen, "new", fingerprint)
            return "new"

        status: Status = "unchanged"
        if existing["fingerprint"] != fingerprint:
            status = "changed"
        elif not existing["active"]:
            status = "changed"

        self._conn.execute(
            """UPDATE items SET title=:title, price=:price, currency=:currency, location=:location,
               company=:company, published_at=:published_at, summary=:summary, extra=:extra,
               fingerprint=:fingerprint, last_seen=:seen, active=1 WHERE item_id=:item_id""",
            {**row, "item_id": item.item_id, "fingerprint": fingerprint, "seen": seen},
        )
        if status == "changed":
            self._record(item, seen, "changed" if existing["active"] else "returned", fingerprint)
        return status

    def mark_missing(self, source: str, seen_item_ids: Iterable[str], seen_at: datetime | None = None) -> int:
        seen = (seen_at or now_utc()).isoformat()
        ids = set(seen_item_ids)
        rows = self._conn.execute(
            "SELECT item_id, title, price, fingerprint FROM items WHERE source = ? AND active = 1", (source,)
        ).fetchall()
        removed = 0
        for row in rows:
            if row["item_id"] in ids:
                continue
            self._conn.execute("UPDATE items SET active = 0 WHERE item_id = ?", (row["item_id"],))
            self._conn.execute(
                "INSERT INTO item_history (item_id, seen_at, change, price, title, fingerprint) VALUES (?,?,?,?,?,?)",
                (row["item_id"], seen, "removed", row["price"], row["title"], row["fingerprint"]),
            )
            removed += 1
        return removed

    def start_run(self, source: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs (source, started_at, status) VALUES (?, ?, 'running')",
            (source, now_utc().isoformat()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, result: RunResult) -> None:
        self._conn.execute(
            """UPDATE runs SET finished_at=?, status=?, fetched=?, new=?, changed=?, removed=?, error=?
               WHERE id=?""",
            (
                now_utc().isoformat(),
                "ok" if result.ok else "error",
                result.fetched,
                result.new,
                result.changed,
                result.removed,
                result.error,
                run_id,
            ),
        )
        self._conn.commit()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def latest_run(self, source: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs WHERE source = ? ORDER BY id DESC LIMIT 1", (source,)
        ).fetchone()

    def query(
        self,
        source: str | None = None,
        category: str | None = None,
        search: str | None = None,
        active_only: bool = True,
        since: datetime | None = None,
        limit: int | None = 100,
        sources: Iterable[str] | None = None,
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if sources is not None:
            names = list(sources)
            if not names:
                return []
            clauses.append(f"source IN ({','.join('?' for _ in names)})")
            params.extend(names)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if search:
            clauses.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\')")
            literal = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{literal}%"] * 3)
        if active_only:
            clauses.append("active = 1")
        if since:
            clauses.append("first_seen >= ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM items {where} ORDER BY COALESCE(published_at, first_seen) DESC, title"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._conn.execute(sql, params).fetchall()

    def history(self, item_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM item_history WHERE item_id = ? ORDER BY seen_at", (item_id,)
        ).fetchall()

    def changes_since(self, since: datetime) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT h.*, i.source, i.category, i.url, i.title AS current_title, i.price AS current_price,
                      i.currency
               FROM item_history h JOIN items i ON i.item_id = h.item_id
               WHERE h.seen_at >= ? ORDER BY h.seen_at DESC""",
            (since.isoformat(),),
        ).fetchall()

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def source_stats(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT source, category, COUNT(*) AS total, SUM(active) AS active,
                      MAX(last_seen) AS last_seen
               FROM items GROUP BY source, category ORDER BY source"""
        ).fetchall()

    def _record(self, item: Item, seen: str, change: str, fingerprint: str) -> None:
        self._conn.execute(
            "INSERT INTO item_history (item_id, seen_at, change, price, title, fingerprint) VALUES (?,?,?,?,?,?)",
            (item.item_id, seen, change, item.price, item.title, fingerprint),
        )


def row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    if isinstance(data.get("extra"), str):
        data["extra"] = json.loads(data["extra"])
    return data
