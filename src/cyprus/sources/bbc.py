from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime

from defusedxml.ElementTree import fromstring

from ..models import Category, Item
from . import Source, register

FEED_URL = "https://feeds.bbci.co.uk/news/rss.xml"


@register
class BBCSource(Source):
    name = "bbc"
    category = Category.NEWS
    description = "BBC News headlines and publisher summaries"
    minimum_interval = 900

    def fetch(self, fetcher):
        limit = self.options.get("limit", 30)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("BBC limit must be an integer between 1 and 100")
        yield from self.parse(fetcher.get_text(FEED_URL))[:limit]

    def parse(self, payload: str) -> list[Item]:
        root = fromstring(payload)
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError("BBC News returned an invalid RSS feed")
        items = []
        for entry in root.findall("./channel/item"):
            date = entry.findtext("pubDate")
            published = parsedate_to_datetime(date).astimezone(timezone.utc) if date else None
            items.append(Item(
                source=self.name, category=self.category,
                title=(entry.findtext("title") or "").strip(),
                url=(entry.findtext("link") or "").strip(),
                summary=(entry.findtext("description") or "").strip(),
                published_at=published, extra={"attribution": "BBC News", "kind": "News headline"},
            ))
        return items
