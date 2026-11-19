from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import cyprus.runner as runner_module
import cyprus.store as store_module
from cyprus.fetch import Fetcher
from cyprus.models import Category, Item
from cyprus.runner import run_source
from cyprus.sources import Source
from cyprus.store import Store, row_to_dict


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_item(key="1", **overrides):
    fields = {
        "source": "feed",
        "category": Category.NEWS,
        "title": f"Story {key}",
        "url": f"https://example.test/items/{key}",
        "price": 10.0,
        "extra": {"tag": "local"},
    }
    return Item(**(fields | overrides))


class FeedSource(Source):
    name = "feed"
    category = Category.NEWS

    def __init__(self, items=(), *, options=None, failure=None):
        super().__init__(options)
        self.items = items
        self.failure = failure
        self.fetch_calls = 0

    def fetch(self, fetcher):
        self.fetch_calls += 1
        yield from self.items
        if self.failure is not None:
            raise self.failure

    def parse(self, payload):
        return list(payload)


class SnapshotSource(FeedSource):
    complete_snapshot = True


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Keep persisted run timestamps on the same clock as explicit seen_at values.
    monkeypatch.setattr(store_module, "now_utc", lambda: START)
    with Store(tmp_path / "items.sqlite3") as database:
        yield database


@pytest.fixture
def fetcher():
    # Runner tests use in-memory adapters, never a real HTTP client.
    client = Mock(spec=Fetcher)
    for method in ("get", "get_text", "get_json"):
        getattr(client, method).side_effect = AssertionError("Unexpected fetch")
    return client


def snapshot(store):
    rows = [dict(row) for row in store.query(active_only=False, limit=None)]
    rows.sort(key=lambda row: row["item_id"])
    history = {
        row["item_id"]: [dict(change) for change in store.history(row["item_id"])]
        for row in rows
    }
    return rows, history


def assert_failed_run(store, result, before):
    assert not result.ok
    assert result.error
    assert result.skipped is None
    assert (result.fetched, result.new, result.changed, result.unchanged, result.removed) == (0, 0, 0, 0, 0)
    # A second connection checks that the rollback and error record are durable.
    with Store(store.path) as reopened:
        assert snapshot(reopened) == before
        run = reopened.latest_run(result.source)
        assert run["status"] == "error"
        assert run["error"] == result.error
        assert run["finished_at"] is not None
        assert tuple(run[field] for field in ("fetched", "new", "changed", "removed")) == (0, 0, 0, 0)


@pytest.mark.parametrize("overrides", [{"url": "javascript:alert(1)"}, {"title": ""}, {"title": " \t\n"}])
def test_item_rejects_unsafe_url_and_blank_title(overrides):
    with pytest.raises(ValueError):
        make_item(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "Edited story"}, {"price": 12.0}, {"currency": "EUR"},
        {"location": "Nicosia"}, {"company": "Example Ltd"},
        {"summary": "Updated summary"}, {"extra": {"tag": "changed"}},
    ],
)
def test_item_identity_is_stable_when_mutable_content_changes(overrides):
    original = make_item()
    changed = replace(original, **overrides)
    assert changed.item_id == original.item_id
    assert changed.fingerprint != original.fingerprint


def test_item_identity_uses_source_and_url_and_fingerprint_is_deterministic():
    original = make_item(extra={"a": 1, "b": {"x": 2, "y": 3}})
    equivalent = make_item(extra={"b": {"y": 3, "x": 2}, "a": 1})
    assert equivalent.item_id == original.item_id
    assert equivalent.fingerprint == original.fingerprint
    assert replace(original, source="other").item_id != original.item_id
    assert make_item("2").item_id != original.item_id


def test_sqlite_insert_unchanged_changed_removed_and_returned_history(store):
    original = make_item()
    other_source = make_item("other", source="other")
    assert store.upsert(original, START) == "new"
    assert store.upsert(other_source, START) == "new"
    assert store.upsert(replace(original), START + timedelta(minutes=1)) == "unchanged"
    assert [row["change"] for row in store.history(original.item_id)] == ["new"]
    row = store.query(source="feed")[0]
    assert row["first_seen"] == START.isoformat()
    assert row["last_seen"] == (START + timedelta(minutes=1)).isoformat()

    changed = replace(original, title="Updated story", price=8.5)
    assert store.upsert(changed, START + timedelta(minutes=2)) == "changed"
    assert store.mark_missing("feed", [], START + timedelta(minutes=3)) == 1
    assert store.mark_missing("feed", [], START + timedelta(minutes=4)) == 0
    assert store.query(source="feed") == []
    assert store.query(source="feed", active_only=False)[0]["active"] == 0
    assert store.query(source="other")[0]["active"] == 1
    assert store.upsert(changed, START + timedelta(minutes=5)) == "changed"
    store.commit()

    with Store(store.path) as reopened:
        row = row_to_dict(reopened.query(source="feed")[0])
        assert row["item_id"] == original.item_id
        assert row["active"] == 1
        assert row["price"] == 8.5
        assert row["title"] == "Updated story"
        assert row["extra"] == {"tag": "local"}
        assert row["first_seen"] == START.isoformat()
        assert row["last_seen"] == (START + timedelta(minutes=5)).isoformat()
        history = reopened.history(original.item_id)
        assert [entry["change"] for entry in history] == ["new", "changed", "removed", "returned"]
        assert [entry["price"] for entry in history] == [10.0, 8.5, 8.5, 8.5]
        assert [entry["fingerprint"] for entry in history] == [
            original.fingerprint, changed.fingerprint, changed.fingerprint, changed.fingerprint,
        ]
        assert [row["change"] for row in reopened.history(other_source.item_id)] == ["new"]
        changes = reopened.changes_since(START + timedelta(minutes=3))
        assert [entry["change"] for entry in changes] == ["returned", "removed"]


@pytest.mark.parametrize("field", ["title", "summary", "company"])
@pytest.mark.parametrize("literal, decoy", [("100%", "1000"), ("part_one", "partXone"), (r"folder\name", "foldername")])
def test_sqlite_search_treats_like_metacharacters_literally(store, field, literal, decoy):
    wanted = make_item("match", **{field: f"Before {literal} after"})
    unwanted = make_item("decoy", **{field: f"Before {decoy} after"})
    store.upsert(wanted, START)
    store.upsert(unwanted, START)
    store.commit()
    assert [row["item_id"] for row in store.query(search=literal)] == [wanted.item_id]
    assert store.query(search="' OR 1=1 --") == []


def test_sqlite_query_combines_source_category_active_since_and_limit(store):
    recent = START + timedelta(days=1)
    entries = [
        (make_item("old", title="Match old"), START),
        (make_item("a", title="Match Alpha"), recent),
        (make_item("b", title="Match Beta"), recent),
        (make_item("inactive", title="Match inactive"), recent),
        (make_item("other", source="other", title="Match other"), recent),
        (make_item("job", category=Category.JOBS, title="Match job"), recent),
    ]
    for item, seen_at in entries:
        store.upsert(item, seen_at)
    inactive = entries[3][0]
    assert store.mark_missing("feed", [item.item_id for item, _ in entries if item != inactive], recent) == 1
    store.commit()

    filters = dict(source="feed", category="news", search="Match", since=recent)
    assert [row["title"] for row in store.query(**filters, limit=None)] == ["Match Alpha", "Match Beta"]
    assert [row["title"] for row in store.query(**filters, limit=1)] == ["Match Alpha"]
    assert {row["title"] for row in store.query(**filters, active_only=False)} == {
        "Match Alpha", "Match Beta", "Match inactive",
    }
    assert len(store.query(active_only=False, limit=None)) == 6
    assert store.query(source="feed' OR 1=1 --") == []


def test_runner_failure_after_yield_preserves_items_and_history(store, fetcher):
    original, unseen = make_item(), make_item("unseen")
    for item in (original, unseen):
        store.upsert(item, START)
    store.upsert(replace(original, price=9.0), START + timedelta(minutes=1))
    store.commit()
    before = snapshot(store)
    source = SnapshotSource(
        [replace(original, price=1.0), make_item("new")],
        options={"track_removals": True},
        failure=RuntimeError("second page failed"),
    )
    result = run_source(source, store, fetcher, START + timedelta(minutes=2))
    assert "second page failed" in result.error
    assert source.fetch_calls == 1
    assert_failed_run(store, result, before)


@pytest.mark.parametrize("mismatch", [{"source": "wrong"}, {"category": Category.JOBS}])
def test_runner_rolls_back_writes_when_later_item_has_wrong_adapter(store, fetcher, mismatch):
    original = make_item()
    store.upsert(original, START)
    store.commit()
    before = snapshot(store)
    source = FeedSource([replace(original, price=1), make_item("new"), make_item("bad", **mismatch)])
    result = run_source(source, store, fetcher, START + timedelta(minutes=1))
    assert_failed_run(store, result, before)


@pytest.mark.parametrize("source_type, options", [(FeedSource, {}), (SnapshotSource, {"track_removals": True})])
def test_runner_successful_empty_iterator_is_an_error(store, fetcher, source_type, options):
    store.upsert(make_item(), START)
    store.commit()
    before = snapshot(store)
    source = source_type([], options=options)
    result = run_source(source, store, fetcher, START + timedelta(minutes=1))
    assert source.fetch_calls == 1
    assert "no items" in result.error.lower()
    assert_failed_run(store, result, before)


def test_runner_deduplicates_yields_and_records_committed_counts(store, fetcher):
    item = make_item()
    source = FeedSource([item, replace(item), item])
    first = run_source(source, store, fetcher, START)
    assert first.ok
    assert (first.fetched, first.new, first.changed, first.unchanged, first.removed) == (1, 1, 0, 0, 0)
    source.items = [replace(item, price=8), replace(item, price=8)]
    second = run_source(source, store, fetcher, START + timedelta(minutes=1))
    assert second.ok
    assert (second.fetched, second.new, second.changed, second.unchanged) == (1, 0, 1, 0)
    third = run_source(source, store, fetcher, START + timedelta(minutes=2))
    assert third.ok
    assert (third.fetched, third.new, third.changed, third.unchanged) == (1, 0, 0, 1)
    with Store(store.path) as reopened:
        assert len(reopened.query()) == 1
        assert [row["change"] for row in reopened.history(item.item_id)] == ["new", "changed"]
        assert reopened.query()[0]["price"] == 8
        runs = {row["id"]: row for row in reopened.recent_runs()}
        assert len(runs) == 3
        assert all(row["status"] == "ok" and row["error"] is None for row in runs.values())
        assert [(runs[key]["fetched"], runs[key]["new"], runs[key]["changed"]) for key in sorted(runs)] == [
            (1, 1, 0), (1, 0, 1), (1, 0, 0),
        ]


@pytest.mark.parametrize("source_type", [FeedSource, SnapshotSource])
def test_runner_never_marks_missing_without_explicit_opt_in(store, fetcher, source_type):
    present, absent = make_item("present"), make_item("absent")
    for item in (present, absent):
        store.upsert(item, START)
    store.commit()
    result = run_source(source_type([present]), store, fetcher, START + timedelta(minutes=1))
    assert result.ok and result.removed == 0
    assert (result.fetched, result.unchanged) == (1, 1)
    assert {row["item_id"] for row in store.query()} == {present.item_id, absent.item_id}
    assert [row["change"] for row in store.history(absent.item_id)] == ["new"]


@pytest.mark.parametrize("extra_options", [{}, {"complete_snapshot": True}])
def test_runner_removals_require_adapter_snapshot_capability(store, fetcher, extra_options):
    item = make_item()
    store.upsert(item, START)
    store.commit()
    before = snapshot(store)
    source = FeedSource([item], options={"track_removals": True, **extra_options})
    result = run_source(source, store, fetcher, START + timedelta(minutes=1))
    assert "complete" in result.error.lower()
    assert_failed_run(store, result, before)


def test_runner_opted_in_complete_snapshot_marks_only_its_missing_items(store, fetcher):
    present, absent = make_item("present"), make_item("absent")
    other = make_item("other", source="other")
    for item in (present, absent, other):
        store.upsert(item, START)
    store.commit()
    source = SnapshotSource([present, present], options={"track_removals": True})
    result = run_source(source, store, fetcher, START + timedelta(minutes=1))
    assert result.ok
    assert (result.fetched, result.unchanged, result.removed) == (1, 1, 1)
    assert {row["item_id"] for row in store.query()} == {present.item_id, other.item_id}
    assert [row["change"] for row in store.history(absent.item_id)] == ["new", "removed"]
    assert [row["change"] for row in store.history(other.item_id)] == ["new"]
    assert store.latest_run("feed")["removed"] == 1


@pytest.mark.parametrize("source_name, options, cooldown", [("feed", {"cooldown_seconds": 120}, 120), ("remotive", {"cooldown_seconds": 0}, 21600)])
@pytest.mark.parametrize("failed_previous", [False, True])
def test_runner_cooldown_skips_fetch_and_run_record_until_boundary(
    store, fetcher, monkeypatch, source_name, options, cooldown, failed_previous,
):
    monkeypatch.setattr(store_module, "now_utc", lambda: START)
    monkeypatch.setattr(runner_module, "now_utc", lambda: START)
    source = FeedSource(
        [make_item(source=source_name)], options=options,
        failure=RuntimeError("temporary failure") if failed_previous else None,
    )
    source.name = source_name
    initial = run_source(source, store, fetcher)
    assert initial.ok is not failed_previous
    previous = dict(store.latest_run(source_name))
    before = snapshot(store)
    source.failure = None
    skipped = run_source(source, store, fetcher, START + timedelta(seconds=cooldown - 1))
    assert skipped.skipped and "cooldown" in skipped.skipped.lower()
    assert skipped.error is None
    assert (skipped.fetched, skipped.new, skipped.changed, skipped.unchanged, skipped.removed) == (0, 0, 0, 0, 0)
    assert source.fetch_calls == 1
    assert fetcher.mock_calls == []
    assert snapshot(store) == before
    assert dict(store.latest_run(source_name)) == previous
    assert len(store.recent_runs()) == 1

    resumed = run_source(source, store, fetcher, START + timedelta(seconds=cooldown))
    assert resumed.ok and resumed.skipped is None
    assert source.fetch_calls == 2
    assert len(store.recent_runs()) == 2
