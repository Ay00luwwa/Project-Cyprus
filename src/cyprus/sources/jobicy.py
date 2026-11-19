from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ..fetch import Fetcher
from ..models import Category, Item
from . import Source, register

API_URL = "https://jobicy.com/api/jobs"


def _text(job: dict[str, Any], field: str, *, required: bool = False) -> str:
    value = job.get(field)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"Jobicy job has invalid {field}")
    return value.strip()


def _plain_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for element in soup.select("script, style"):
        element.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


@register
class JobicySource(Source):
    name = "jobicy"
    category = Category.JOBS
    description = (
        "Jobicy remote jobs published within seven days, with a three-hour publishing delay; "
        "location restrictions apply"
    )
    minimum_interval = 3600

    def fetch(self, fetcher: Fetcher) -> Iterator[Item]:
        count = self.options.get("count", 100)
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("Jobicy count must be an integer between 1 and 100")
        params = {"count": str(count)}
        for field in ("tag", "geo"):
            value = self.options.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"Jobicy {field} must be a string")
            if value.strip():
                params[field] = value.strip()
        # The upstream feed enforces its publication window and delay. Only read
        # the first page; never follow response-provided URLs or cursors.
        yield from self.parse(fetcher.get_json(f"{API_URL}?{urlencode(params)}"))[:count]

    def parse(self, payload: Any) -> list[Item]:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Jobicy payload must contain a jobs list")

        items = []
        for job in payload["jobs"]:
            if not isinstance(job, dict):
                raise ValueError("Jobicy job must be an object")
            title = _text(job, "jobTitle", required=True)
            url = _text(job, "url", required=True)
            company = _text(job, "companyName")
            location = _text(job, "jobGeo")
            excerpt = _text(job, "jobExcerpt")
            description = _text(job, "jobDescription")
            timestamp = _text(job, "pubDate")
            published_at = None
            if timestamp:
                published_at = datetime.fromisoformat(timestamp)
                # Jobicy documents publication times in UTC, including legacy
                # timestamps without an explicit offset.
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                published_at = published_at.astimezone(timezone.utc)

            job_types = job.get("jobType")
            if job_types is None:
                job_types = []
            if not isinstance(job_types, list) or any(not isinstance(value, str) for value in job_types):
                raise ValueError("Jobicy job has invalid jobType")
            extra = {
                "remote": True,
                "job_types": job_types,
                "kind": "Job opening",
                "attribution": "Jobicy",
            }
            job_id = job.get("id")
            if job_id is not None:
                if type(job_id) is not int or job_id < 1:
                    raise ValueError("Jobicy job has invalid id")
                extra["id"] = job_id
            # Preserve explicit salary strings, without turning numeric bounds
            # into an invented salary description or marketplace price.
            for field in ("salaryMin", "salaryMax", "salaryCurrency", "salaryPeriod"):
                value = job.get(field)
                if isinstance(value, str):
                    extra[field] = value

            items.append(Item(
                source=self.name,
                category=self.category,
                title=title,
                url=url,
                company=company,
                location=location,
                published_at=published_at,
                # Keep the publisher's excerpt intact instead of replacing or
                # truncating it with a locally generated summary.
                summary=_plain_text(excerpt) if excerpt else _plain_text(description)[:600],
                extra=extra,
            ))
        return items
