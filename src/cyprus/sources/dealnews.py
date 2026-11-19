from __future__ import annotations

import math
import re
from datetime import timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Iterator

from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring

from ..fetch import Fetcher
from ..models import Category, Item
from . import Source, register

FEED_URL = "https://www.dealnews.com/?rss=1&sort=time"
_PRICE = re.compile(r"\$?\s*(?:(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?|\.[0-9]+)")


def _plain_text(html: str) -> str:
    description = BeautifulSoup(html, "html.parser")
    for element in description.select("script, style"):
        element.decompose()
    return " ".join(description.get_text(" ", strip=True).split())


def _price(text: str) -> float | None:
    value = text.strip()
    if not _PRICE.fullmatch(value):
        return None
    # Parse only the explicit price field, never promotional copy or titles.
    amount = float(Decimal(value.removeprefix("$").strip().replace(",", "")))
    return amount if math.isfinite(amount) else None


@register
class DealNewsSource(Source):
    name = "dealnews"
    category = Category.MARKETPLACE
    description = "Retail offer metadata from DealNews; checkout prices are not verified"
    minimum_interval = 1800

    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        limit = self.options.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("DealNews limit must be an integer between 1 and 100")
        yield from self.parse(fetcher.get_text(FEED_URL))[:limit]

    def parse(self, payload: str) -> list[Item]:
        if not isinstance(payload, (str, bytes)):
            raise ValueError("DealNews returned an invalid RSS feed")
        root = fromstring(payload)
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise ValueError("DealNews returned an invalid RSS feed")
        items = []
        for entry in channel.findall("item"):
            # Match extension fields by local name, independent of namespace prefix.
            fields = {node.tag.rsplit("}", 1)[-1]: node for node in entry}
            text = {name: node.text or "" for name, node in fields.items()}
            offer_html = text.get("description", "")
            extra = {
                "attribution": "DealNews",
                "kind": "Retail offer",
                # Untrusted publisher HTML: rendering must sanitize this separately.
                "offer_html": offer_html,
            }
            for field in ("guid", "expires", "category", "dealType", "staffPick", "pubDate"):
                if field in text:
                    extra[field] = text[field]
            price = None
            currency = None
            if "price" in fields:
                extra["price_text"] = text["price"]
                price = _price(text["price"])
                currency = (fields["price"].get("currency") or "").strip() or None

            published = None
            if "pubDate" in text:
                try:
                    parsed = parsedate_to_datetime(text["pubDate"].strip())
                    if parsed.tzinfo is not None:
                        published = parsed.astimezone(timezone.utc)
                except (ValueError, TypeError, OverflowError):
                    pass
            items.append(Item(
                source=self.name,
                category=self.category,
                title=text.get("title", "").strip(),
                url=text.get("link", "").strip(),
                price=price,
                currency=currency,
                company=_plain_text(text.get("retailer", "")) or None,
                published_at=published,
                summary=_plain_text(offer_html),
                extra=extra,
            ))
        return items
