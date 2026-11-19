from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

from bs4 import BeautifulSoup

from ..fetch import Fetcher
from ..models import Category, Item
from . import Source, register

API_URL = "https://www.arbeitnow.com/api/job-board-api"


def _text(job: dict[str, Any], field: str, *, required: bool = False) -> str:
    value = job.get(field)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"Arbeitnow job has invalid {field}")
    return value.strip()


def _strings(job: dict[str, Any], field: str) -> list[str]:
    value = job.get(field)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise ValueError(f"Arbeitnow job has invalid {field}")
    return value


@register
class ArbeitnowSource(Source):
    name = "arbeitnow"
    category = Category.JOBS
    description = "Arbeitnow job openings; remote by default, with location restrictions preserved"
    minimum_interval = 3600

    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        limit = self.options.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 300:
            raise ValueError("Arbeitnow limit must be an integer between 1 and 300")
        remote_only = self.options.get("remote_only", True)
        if type(remote_only) is not bool:
            raise ValueError("Arbeitnow remote_only must be a boolean")
        items = self.parse(fetcher.get_json(API_URL))
        # Remote listings can occur beyond the first limit entries in this
        # mixed feed, so validate and filter the entire page before slicing.
        if remote_only:
            items = [item for item in items if item.extra["remote"] is True]
        yield from items[:limit]

    def parse(self, payload: Any) -> list[Item]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Arbeitnow payload must contain a data list")

        items = []
        for job in payload["data"]:
            if not isinstance(job, dict):
                raise ValueError("Arbeitnow job must be an object")
            title = _text(job, "title", required=True)
            url = _text(job, "url", required=True)
            company = _text(job, "company_name")
            location = _text(job, "location")
            description = BeautifulSoup(_text(job, "description"), "html.parser")
            for element in description.select("script, style"):
                element.decompose()
            summary = " ".join(description.get_text(" ", strip=True).split())[:600]

            remote = job.get("remote")
            if "remote" in job and type(remote) is not bool:
                raise ValueError("Arbeitnow job has invalid remote")
            published_at = None
            if "created_at" in job:
                timestamp = job["created_at"]
                if type(timestamp) is not int:
                    raise ValueError("Arbeitnow job has invalid created_at")
                try:
                    published_at = datetime.fromtimestamp(timestamp, timezone.utc)
                except (ValueError, OverflowError, OSError) as exc:
                    raise ValueError("Arbeitnow job has invalid created_at") from exc

            extra = {
                "slug": _text(job, "slug"),
                "remote": remote,
                "tags": _strings(job, "tags"),
                "job_types": _strings(job, "job_types"),
                "kind": "Job opening",
                "attribution": "Arbeitnow",
            }
            salary = job.get("salary")
            if isinstance(salary, str):
                extra["salary"] = salary
            items.append(Item(
                source=self.name,
                category=self.category,
                title=title,
                url=url,
                company=company,
                location=location,
                published_at=published_at,
                summary=summary,
                extra=extra,
            ))
        return items
