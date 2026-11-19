from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator

from ..fetch import Fetcher
from ..models import Category, Item


class Source(ABC):
    """One site/feed. Subclasses implement `fetch` (network) and `parse` (pure)."""

    name: str
    category: Category
    description: str = ""
    complete_snapshot: bool = False
    minimum_interval: int = 0

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}

    @abstractmethod
    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        """Download pages and yield items. Should delegate extraction to `parse`."""

    @abstractmethod
    def parse(self, payload: Any) -> list[Item]:
        """Extract items from one page/response. Pure so it can be tested on fixtures."""


_REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    _REGISTRY[cls.name] = cls
    return cls


def available() -> dict[str, type[Source]]:
    from . import arbeitnow, bbc, books, dealnews, gutenberg, hackernews, jobicy, openlibrary, pythonjobs, remotive

    return dict(_REGISTRY)


def create(name: str, options: dict[str, Any] | None = None) -> Source:
    sources = available()
    if name not in sources:
        raise KeyError(f"Unknown source '{name}'. Available: {', '.join(sorted(sources))}")
    return sources[name](options)
