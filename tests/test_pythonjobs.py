from pathlib import Path
from unittest.mock import Mock

import pytest
from defusedxml.common import DefusedXmlException

from cyprus.sources.pythonjobs import PythonJobsSource

FIXTURE = Path(__file__).parent / "fixtures" / "pythonjobs.xml"


def test_rss_extracts_plain_text_and_preserves_absent_date():
    items = PythonJobsSource().parse(FIXTURE.read_text(encoding="utf-8"))
    assert len(items) == 2
    assert items[0].title == "Python Engineer, Example Team"
    assert items[0].url == "https://www.python.org/jobs/100/"
    assert items[0].published_at is None
    assert "Python services & data tools" in items[0].summary
    assert "script" not in items[0].summary and "alert" not in items[0].summary
    assert items[1].published_at.utcoffset().total_seconds() == 0
    assert items[0].extra["attribution"] == "Python.org Jobs"


def test_rss_limit():
    fetcher = Mock()
    fetcher.get_text.return_value = FIXTURE.read_text(encoding="utf-8")
    assert len(list(PythonJobsSource({"limit": 1}).fetch(fetcher))) == 1


@pytest.mark.parametrize("limit", [0, 101, True, "10", -1])
def test_rss_rejects_invalid_limits(limit):
    with pytest.raises(ValueError):
        list(PythonJobsSource({"limit": limit}).fetch(Mock()))


@pytest.mark.parametrize("payload", ["<html>blocked</html>", "<rss></rss>", '<rss><channel><item><title>Hi</title><link>javascript:alert(1)</link></item></channel></rss>', '<rss><channel><item><link>https://example.com/job</link></item></channel></rss>'])
def test_rss_rejects_bad_payload(payload):
    with pytest.raises(ValueError):
        PythonJobsSource().parse(payload)


def test_rss_rejects_entity_expansion():
    with pytest.raises(DefusedXmlException):
        PythonJobsSource().parse('<!DOCTYPE rss [<!ENTITY x "secret">]><rss><channel><title>&x;</title></channel></rss>')
