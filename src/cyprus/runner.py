from __future__ import annotations

import logging
from datetime import datetime
from math import ceil

from .fetch import FetchError, Fetcher
from .models import now_utc
from .sources import Source
from .store import RunResult, Store

log = logging.getLogger(__name__)


def run_source(source: Source, store: Store, fetcher: Fetcher, seen_at: datetime | None = None) -> RunResult:
    seen_at = seen_at or now_utc()
    result = RunResult(source=source.name)
    previous = store.latest_run(source.name)
    cooldown = max(source.minimum_interval, source.options.get("cooldown_seconds", 0), 21600 if source.name == "remotive" else 0)
    if previous:
        elapsed = (seen_at - datetime.fromisoformat(previous["started_at"])).total_seconds()
        if elapsed < cooldown:
            result.skipped = f"Cooldown: try again in {ceil(cooldown - elapsed)} seconds"
            return result
    run_id = store.start_run(source.name)
    try:
        track_removals = source.options.get("track_removals", False)
        if track_removals and not source.complete_snapshot:
            raise ValueError("Removal tracking requires a complete source snapshot, not a feed or limited page")
        items = {item.item_id: item for item in source.fetch(fetcher)}
        if not items:
            raise FetchError("Source returned no items; existing data has been preserved")
        for item in items.values():
            if item.source != source.name or item.category != source.category:
                raise ValueError("Item source or category does not match its adapter")
            status = store.upsert(item, seen_at)
            result.fetched += 1
            setattr(result, status, getattr(result, status) + 1)
        if track_removals:
            result.removed = store.mark_missing(source.name, items, seen_at)
    except Exception as exc:
        store.rollback()
        log.warning("Source %s failed: %s", source.name, exc)
        result = RunResult(source=source.name, error=f"{type(exc).__name__}: {exc}")
    store.finish_run(run_id, result)
    return result
