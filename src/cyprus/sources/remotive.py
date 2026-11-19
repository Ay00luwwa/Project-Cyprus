from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..fetch import FetchError, Fetcher
from ..models import Category, Item
from . import Source, register

API_URL = "https://remotive.com/api/remote-jobs"


def _listing_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Remotive listing URL must use HTTP(S)")
    return url


@register
class RemotiveSource(Source):
    name = "remotive"
    category = Category.JOBS
    description = "Remote jobs from Remotive"

    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        limit = self.options.get("limit", 25)
        if type(limit) is not int:
            raise ValueError("Remotive limit must be an integer")
        limit = max(1, min(100, limit))
        items = self.parse(fetcher.get_json(f"{API_URL}?limit={limit}"))
        if not items:
            raise FetchError("Remotive returned no jobs")
        yield from items[:limit]

    def parse(self, payload: Any) -> list[Item]:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Remotive payload must contain a jobs list")

        items = []
        for job in payload["jobs"]:
            if not isinstance(job, dict):
                raise ValueError("Remotive job must be an object")
            job_id = job.get("id")
            if type(job_id) is not int or job_id < 1:
                raise ValueError("Remotive job requires a positive integer ID")
            for field in ("title", "url"):
                value = job.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Remotive job {job_id} requires {field}")

            optional = {}
            for field in (
                "company_name", "candidate_required_location", "publication_date",
                "description", "salary",
            ):
                value = job.get(field)
                if value is None:
                    value = ""
                if not isinstance(value, str):
                    raise ValueError(f"Remotive job {job_id} has invalid {field}")
                optional[field] = value

            description = BeautifulSoup(optional["description"], "html.parser")
            for element in description.select("script, style"):
                element.decompose()
            summary = " ".join(description.get_text(" ", strip=True).split())[:600]
            timestamp = optional["publication_date"].strip()
            items.append(
                Item(
                    source=self.name,
                    category=self.category,
                    title=job["title"].strip(),
                    url=_listing_url(job["url"].strip()),
                    company=optional["company_name"].strip(),
                    location=optional["candidate_required_location"].strip(),
                    published_at=datetime.fromisoformat(timestamp) if timestamp else None,
                    summary=summary,
                    extra={
                        "id": job_id,
                        "salary": optional["salary"],
                        "attribution": "Remotive",
                    },
                )
            )
        return items
