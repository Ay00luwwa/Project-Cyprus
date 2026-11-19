from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping
from typing import Any

FIELDS = [
    "item_id", "source", "category", "title", "url", "price", "currency", "company",
    "location", "published_at", "summary", "first_seen", "last_seen", "active",
]


def csv_text(rows: Iterable[Mapping[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = dict(row)
        for key, value in safe.items():
            if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))):
                safe[key] = "'" + value
        writer.writerow(safe)
    return output.getvalue()


def json_text(rows: Iterable[Mapping[str, Any]]) -> str:
    return json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2)
