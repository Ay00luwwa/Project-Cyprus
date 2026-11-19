from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Iterator

from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring

from ..fetch import Fetcher
from ..models import Category, Item
from . import Source, register

FEED_URL = "https://www.gutenberg.org/cache/epub/feeds/today.rss"


@register
class GutenbergSource(Source):
    name = "gutenberg"
    category = Category.BOOKS
    description = "Free ebook metadata from Project Gutenberg's nightly RSS feed"
    minimum_interval = 86400

    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        limit = self.options.get("limit", 25)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Gutenberg limit must be an integer between 1 and 100")
        yield from self.parse(fetcher.get_text(FEED_URL))[:limit]

    def parse(self, payload: str) -> list[Item]:
        if not isinstance(payload, (str, bytes)):
            raise ValueError("Project Gutenberg returned an invalid RSS feed")
        root = fromstring(payload)
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise ValueError("Project Gutenberg returned an invalid RSS feed")
        items = []
        for entry in channel.findall("item"):
            description = BeautifulSoup(entry.findtext("description") or "", "html.parser")
            for element in description.select("script, style"):
                element.decompose()
            summary = " ".join(description.get_text(" ", strip=True).split())
            extra = {"attribution": "Project Gutenberg", "kind": "Free ebook"}
            published = None
            # Only item dates are eligible; the channel date describes the feed update.
            date = entry.findtext("pubDate")
            if date is not None:
                extra["pubDate"] = date
                try:
                    parsed = parsedate_to_datetime(date.strip())
                    if parsed.tzinfo is not None:
                        published = parsed.astimezone(timezone.utc)
                except (ValueError, TypeError, OverflowError):
                    pass
            items.append(Item(
                source=self.name,
                category=self.category,
                title=(entry.findtext("title") or "").strip(),
                url=(entry.findtext("link") or "").strip(),
                summary=summary,
                published_at=published,
                extra=extra,
            ))
        return items
