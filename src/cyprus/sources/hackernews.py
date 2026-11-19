from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..fetch import Fetcher
from ..models import Category, Item
from . import Source, register

FRONT_URL = "https://news.ycombinator.com/"


def _story_url(href: str) -> str:
    url = urljoin(FRONT_URL, href)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Hacker News story URL must use HTTP(S)")
    return url


@register
class HackerNewsSource(Source):
    name = "hackernews"
    category = Category.NEWS
    description = "Hacker News front page"

    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        yield from self.parse(fetcher.get_text(FRONT_URL))

    def parse(self, payload: Any) -> list[Item]:
        if not isinstance(payload, str):
            raise ValueError("Hacker News payload must be HTML text")
        soup = BeautifulSoup(payload, "html.parser")
        submissions = soup.select("tr.athing.submission")
        if not submissions:
            raise ValueError("Hacker News page contains no submissions")

        items = []
        for row in submissions:
            submission_id = row.get("id", "").strip()
            link = row.select_one(".titleline > a")
            title = link.get_text(" ", strip=True) if link else ""
            href = link.get("href", "").strip() if link else ""
            if not re.fullmatch(r"[0-9]+", submission_id) or int(submission_id) < 1:
                raise ValueError("Hacker News submission requires a positive ID")
            if not title or not href:
                raise ValueError("Hacker News submission requires a title and story URL")

            rank_node = row.select_one(".rank")
            rank_text = rank_node.get_text(strip=True).rstrip(".") if rank_node else ""
            extra = {
                "id": int(submission_id),
                "story_url": _story_url(href),
                "rank": int(rank_text) if rank_text else None,
                "score": None,
                "comments": None,
                "author": None,
            }
            published_at = None
            sibling = row.find_next_sibling("tr")
            subtext = sibling.select_one(".subtext") if sibling else None
            if subtext is not None:
                score = subtext.select_one(".score")
                score_text = score.get_text(strip=True).split() if score else []
                if score_text:
                    extra["score"] = int(score_text[0])
                author = subtext.select_one('a[href^="user?id="]')
                if author is not None:
                    extra["author"] = author.get_text(strip=True)
                age = subtext.select_one(".age")
                timestamp = age.get("title", "").split() if age else []
                if timestamp:
                    published_at = datetime.fromisoformat(timestamp[0])
                for anchor in subtext.select("a"):
                    text = anchor.get_text(" ", strip=True)
                    count = re.fullmatch(r"(\d+)\s+comments?", text)
                    if count:
                        extra["comments"] = int(count.group(1))
                    elif text == "discuss":
                        extra["comments"] = 0

            items.append(
                Item(
                    source=self.name,
                    category=self.category,
                    title=title,
                    url=f"{FRONT_URL}item?id={submission_id}",
                    published_at=published_at,
                    extra=extra,
                )
            )
        return items
