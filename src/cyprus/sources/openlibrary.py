from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import urlencode

from ..fetch import Fetcher
from ..models import Category, Item
from . import Source, register

SUBJECT_URL = "https://openlibrary.org/subjects/{subject}.json"


@register
class OpenLibrarySource(Source):
    name = "openlibrary"
    category = Category.BOOKS
    description = "Open Library book catalogue records, not price or stock listings"
    minimum_interval = 86400

    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        subject = self.options.get("subject", "science_fiction")
        if not isinstance(subject, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", subject):
            raise ValueError("Open Library subject must be a slug of 1 to 80 lowercase letters, digits or underscores")
        limit = self.options.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Open Library limit must be an integer between 1 and 50")
        url = SUBJECT_URL.format(subject=subject) + "?" + urlencode({"limit": limit})
        yield from self.parse(fetcher.get_json(url))[:limit]

    def parse(self, payload: Any) -> list[Item]:
        if not isinstance(payload, dict) or not isinstance(payload.get("works"), list):
            raise ValueError("Open Library payload must contain a works list")
        items = []
        for work in payload["works"]:
            if not isinstance(work, dict):
                raise ValueError("Open Library work must be an object")
            for field in ("title", "key"):
                value = work.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Open Library work requires {field}")
            key = work["key"].strip()
            if not re.fullmatch(r"/works/OL[0-9]+W", key):
                raise ValueError("Open Library work key must have the form /works/OL123W")

            authors = []
            if isinstance(work.get("authors"), list):
                for author in work["authors"]:
                    if isinstance(author, dict):
                        name = author.get("name")
                        if isinstance(name, str) and name.strip():
                            authors.append(name.strip())
            summary = "Open Library catalogue record; not a price or stock listing."
            if authors:
                summary += " Authors: " + ", ".join(authors) + "."
            extra = {
                "authors": authors,
                "first_publish_year": work.get("first_publish_year"),
                "attribution": "Open Library",
                "kind": "Book catalogue",
            }
            for field in ("edition_count", "has_fulltext", "ia"):
                if field in work:
                    extra[field] = work[field]
            items.append(Item(
                source=self.name,
                category=self.category,
                title=work["title"].strip(),
                url="https://openlibrary.org" + key,
                summary=summary,
                extra=extra,
            ))
        return items
