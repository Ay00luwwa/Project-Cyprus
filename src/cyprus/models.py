from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit


class Category(StrEnum):
    BOOKS = "books"
    MARKETPLACE = "marketplace"
    NEWS = "news"
    JOBS = "jobs"


@dataclass(slots=True)
class Item:
    source: str
    category: Category
    title: str
    url: str
    price: float | None = None
    currency: str | None = None
    location: str | None = None
    company: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("Item URL must be HTTP(S) without embedded credentials")
        if not self.title.strip() or not self.source.strip():
            raise ValueError("Items must have a title and source")

    @property
    def item_id(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.url}".encode()).hexdigest()

    @property
    def fingerprint(self) -> str:
        """Hash of the mutable fields; changes when the listing itself changes."""
        payload = {
            "title": self.title,
            "price": self.price,
            "currency": self.currency,
            "location": self.location,
            "company": self.company,
            "summary": self.summary,
            "extra": self.extra,
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["category"] = str(self.category)
        row["published_at"] = self.published_at.isoformat() if self.published_at else None
        row["extra"] = json.dumps(self.extra, sort_keys=True, default=str)
        return row


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
