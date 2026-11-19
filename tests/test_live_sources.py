from datetime import timedelta
from unittest.mock import Mock

import pytest
from defusedxml.common import DefusedXmlException
from typer.testing import CliRunner

from cyprus.cli import app
from cyprus.digest import build_digest
from cyprus.models import Category, Item, now_utc
from cyprus.runner import run_source
from cyprus.sources.bbc import BBCSource
from cyprus.store import Store

BBC_XML = '''<rss><channel><title>BBC News</title><item><title>World headline</title><link>https://www.bbc.co.uk/news/articles/example</link><description>Publisher's summary, unmodified.</description><pubDate>Sat, 14 Nov 2026 08:00:00 GMT</pubDate></item></channel></rss>'''


def test_bbc_publisher_metadata_and_utc_date():
    item = BBCSource().parse(BBC_XML)[0]
    assert item.title == "World headline"
    assert item.source == "bbc" and item.category == Category.NEWS
    assert item.extra["attribution"] == "BBC News"
    assert item.summary == "Publisher's summary, unmodified."
    assert item.published_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("payload", ["<html>blocked</html>", "<rss></rss>", BBC_XML.replace("https://www.bbc.co.uk/news/articles/example", "javascript:alert(1)")])
def test_bbc_invalid_payloads(payload):
    with pytest.raises(ValueError):
        BBCSource().parse(payload)


def test_bbc_no_xml_entities():
    with pytest.raises(DefusedXmlException):
        BBCSource().parse('<!DOCTYPE rss [<!ENTITY x "secret">]><rss><channel><title>&x;</title></channel></rss>')


def test_bbc_source_limit():
    fetcher = Mock()
    fetcher.get_text.return_value = BBC_XML
    assert len(list(BBCSource({"limit": 1}).fetch(fetcher))) == 1
    with pytest.raises(ValueError):
        list(BBCSource({"limit": True}).fetch(fetcher))


def test_selected_sources_hide_retired_demo_without_deleting(tmp_path):
    with Store(tmp_path / "sources.db") as store:
        demo = Item(source="books", category=Category.MARKETPLACE, title="Old demo", url="https://example.com/demo")
        real = BBCSource().parse(BBC_XML)[0]
        store.upsert(demo)
        store.upsert(real)
        store.commit()
        assert [row["source"] for row in store.query(sources=["bbc"])] == ["bbc"]
        assert store.query(sources=[]) == []
        assert len(store.query()) == 2
        html, _, count = build_digest(store, sources=["bbc"])
        assert count == 1 and "Old demo" not in html
        assert len(store.history(demo.item_id)) == 1


def test_publisher_minimum_cannot_be_lowered_in_config(tmp_path):
    source = BBCSource({"cooldown_seconds": 0})
    fetcher = Mock()
    fetcher.get_text.return_value = BBC_XML
    with Store(tmp_path / "cooldown.db") as store:
        start = now_utc()
        assert run_source(source, store, fetcher, start).ok
        fetcher.reset_mock()
        assert run_source(source, store, fetcher, start + timedelta(seconds=100)).skipped
        fetcher.get_text.assert_not_called()


def test_watch_cycles_respect_interval_and_never_send_email(monkeypatch):
    collect = Mock()
    sleep = Mock()
    send = Mock(side_effect=AssertionError("watch must never send email"))
    monkeypatch.setattr("cyprus.cli.run", collect)
    monkeypatch.setattr("cyprus.cli.time.sleep", sleep)
    monkeypatch.setattr("cyprus.cli.send_digest", send)
    result = CliRunner().invoke(app, ["watch", "--cycles", "2", "--interval", "60"])
    assert result.exit_code == 0, result.output
    assert collect.call_count == 2
    sleep.assert_called_once_with(60)
    send.assert_not_called()


def test_watch_recovers_failed_sources_and_stops_cleanly(monkeypatch):
    import typer
    collect = Mock(side_effect=[typer.Exit(1), None])
    monkeypatch.setattr("cyprus.cli.run", collect)
    monkeypatch.setattr("cyprus.cli.time.sleep", lambda _: None)
    result = CliRunner().invoke(app, ["watch", "--cycles", "2"])
    assert result.exit_code == 0 and collect.call_count == 2
    monkeypatch.setattr("cyprus.cli.run", Mock(side_effect=KeyboardInterrupt))
    result = CliRunner().invoke(app, ["watch"])
    assert result.exit_code == 0 and "stopped" in result.output
