from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..fetch import Fetcher
from ..models import Category, Item
from . import Source, register

CATALOGUE_URL = "https://books.toscrape.com/catalogue/page-1.html"
RATINGS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


def _product_url(href: str) -> str:
    url = urljoin(CATALOGUE_URL, href)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Books product URL must use HTTP(S)")
    return url


@register
class BooksSource(Source):
    name = "books"
    category = Category.MARKETPLACE
    description = "Books to Scrape catalogue"

    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        pages = self.options.get("pages", 1)
        if type(pages) is not int:
            raise ValueError("Books pages must be an integer")
        pages = max(1, min(5, pages))
        for page in range(1, pages + 1):
            url = f"https://books.toscrape.com/catalogue/page-{page}.html"
            yield from self.parse(fetcher.get_text(url))

    def parse(self, payload: Any) -> list[Item]:
        if not isinstance(payload, str):
            raise ValueError("Books payload must be HTML text")
        soup = BeautifulSoup(payload, "html.parser")
        products = soup.select("article.product_pod")
        if not products:
            raise ValueError("Books page contains no products")

        items = []
        for product in products:
            link = product.select_one("h3 a")
            title = link.get("title", "").strip() if link else ""
            href = link.get("href", "").strip() if link else ""
            if not title or not href:
                raise ValueError("Books product requires a full title and URL")

            price_node = product.select_one(".price_color")
            price_text = price_node.get_text(strip=True) if price_node else ""
            if not re.fullmatch(r"£\d+(?:\.\d{2})?", price_text):
                raise ValueError(f"Invalid GBP price for book {title!r}")
            availability = product.select_one(".availability")
            rating_node = product.select_one(".star-rating")
            rating = next(
                (RATINGS[value] for value in rating_node.get("class", []) if value in RATINGS),
                None,
            ) if rating_node else None
            items.append(
                Item(
                    source=self.name,
                    category=self.category,
                    title=title,
                    url=_product_url(href),
                    price=float(price_text[1:]),
                    currency="GBP",
                    extra={
                        "availability": " ".join(availability.stripped_strings) if availability else "",
                        "rating": rating,
                    },
                )
            )
        return items
