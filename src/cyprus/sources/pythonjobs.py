from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Iterator

from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring

from ..fetch import Fetcher
from ..models import Category, Item
from . import Source, register

FEED_URL = "https://www.python.org/jobs/feed/rss/"


@register
class PythonJobsSource(Source):
    name = "pythonjobs"
    category = Category.JOBS
    description = "Python.org Jobs RSS feed"

    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        limit = self.options.get("limit", 25)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Python jobs limit must be an integer between 1 and 100")
        yield from self.parse(fetcher.get_text(FEED_URL))[:limit]

    def parse(self, payload: str) -> list[Item]:
        root = fromstring(payload)
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise ValueError("Python.org returned an invalid RSS feed")
        items = []
        for entry in channel.findall("item"):
            title = (entry.findtext("title") or "").strip()
            url = (entry.findtext("link") or "").strip()
            description = BeautifulSoup(entry.findtext("description") or "", "html.parser")
            for element in description.select("script, style"):
                element.decompose()
            text = description.get_text(" ", strip=True)
            date = entry.findtext("pubDate")
            items.append(Item(
                source=self.name,
                category=self.category,
                title=title,
                url=url,
                summary=" ".join(text.split())[:600],
                published_at=parsedate_to_datetime(date) if date else None,
                extra={"attribution": "Python.org Jobs"},
            ))
        return items
