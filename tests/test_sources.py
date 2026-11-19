from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from bs4 import BeautifulSoup

from cyprus.fetch import FetchError, Fetcher
from cyprus.models import Category
from cyprus.sources import Source, available, create
from cyprus.sources.books import BooksSource
from cyprus.sources.hackernews import HackerNewsSource
from cyprus.sources.remotive import RemotiveSource

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def books_html():
    return (FIXTURES / "books_page1.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def hn_html():
    return (FIXTURES / "hn_front.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def jobs_payload():
    return json.loads((FIXTURES / "remotive.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("name", "source_type", "category"),
    [
        ("books", BooksSource, Category.MARKETPLACE),
        ("hackernews", HackerNewsSource, Category.NEWS),
        ("remotive", RemotiveSource, Category.JOBS),
    ],
)
def test_sources_registered(name, source_type, category):
    source = create(name)
    assert available()[name] is source_type
    assert isinstance(source, Source)
    assert isinstance(source, source_type)
    assert source.name == name
    assert source.category == category


def test_books_fixture(books_html):
    items = BooksSource().parse(books_html)
    assert len(items) == 20
    assert len({item.item_id for item in items}) == 20
    assert all(item.source == "books" and item.category == Category.MARKETPLACE for item in items)
    assert all(item.currency == "GBP" for item in items)
    assert all(item.extra["availability"] == "In stock" for item in items)
    assert [item.price for item in items] == [
        51.77, 53.74, 50.10, 47.82, 54.23, 22.65, 33.34, 17.93, 22.60, 52.15,
        13.99, 20.66, 17.46, 52.29, 35.02, 57.25, 23.88, 37.59, 51.33, 45.17,
    ]
    assert [item.extra["rating"] for item in items] == [
        3, 1, 1, 4, 5, 1, 4, 3, 4, 1, 2, 4, 5, 5, 5, 3, 1, 1, 2, 2,
    ]
    assert items[0].title == "A Light in the Attic"
    assert items[0].url == "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
    assert items[7].title == (
        "The Coming Woman: A Novel Based on the Life of the Infamous Feminist, Victoria Woodhull"
    )
    assert items[-1].title == "It's Only the Himalayas"
    assert items[-1].url == "https://books.toscrape.com/catalogue/its-only-the-himalayas_981/index.html"
    for item, link in zip(items, BeautifulSoup(books_html, "html.parser").select("h3 a")):
        assert item.title == link["title"]
        assert item.url == "https://books.toscrape.com/catalogue/" + link["href"]


@pytest.mark.parametrize("field", ["title", "href"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_books_missing_required_data(books_html, field, value):
    soup = BeautifulSoup(books_html, "html.parser")
    link = soup.select("article.product_pod h3 a")[-1]
    if value is None:
        del link[field]
    else:
        link[field] = value
    with pytest.raises(ValueError):
        BooksSource().parse(str(soup))


@pytest.mark.parametrize("price", [None, "", "£invalid", "$51.77", "£NaN"])
def test_books_invalid_price(books_html, price):
    soup = BeautifulSoup(books_html, "html.parser")
    node = soup.select_one(".price_color")
    if price is None:
        node.decompose()
    else:
        node.string = price
    with pytest.raises(ValueError):
        BooksSource().parse(str(soup))


def test_books_optional_metadata(books_html):
    soup = BeautifulSoup(books_html, "html.parser")
    soup.select_one(".availability").decompose()
    soup.select_one(".star-rating").decompose()
    item = BooksSource().parse(str(soup))[0]
    assert item.extra["availability"] == ""
    assert item.extra["rating"] is None


def test_hackernews_fixture(hn_html):
    items = HackerNewsSource().parse(hn_html)
    assert len(items) == 30
    assert len({item.item_id for item in items}) == 30
    assert all(item.source == "hackernews" and item.category == Category.NEWS for item in items)
    assert [item.extra["rank"] for item in items] == list(range(1, 31))
    assert all(item.url == f"https://news.ycombinator.com/item?id={item.extra['id']}" for item in items)
    first = items[0]
    assert first.title == "FAA Issues National Ground Stop"
    assert first.url == "https://news.ycombinator.com/item?id=50489641"
    assert first.extra["story_url"] == (
        "https://www.fly.faa.gov/adv/adv_otherdis?adv_date=11132026&advn=111"
    )
    assert first.extra["score"] == 893
    assert first.extra["comments"] == 519
    assert first.extra["author"] == "zulln"
    assert first.published_at == datetime(2026, 11, 13, 21, 5, 53)
    assert all(item.published_at.tzinfo is None for item in items)
    hiring = items[9]
    assert hiring.extra["score"] is None
    assert hiring.extra["author"] is None
    assert hiring.extra["comments"] is None
    assert hiring.published_at == datetime(2026, 11, 13, 21, 1, 11)
    assert items[13].extra["comments"] == 1


@pytest.mark.parametrize("field", ["id", "title", "href"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_hackernews_missing_required_data(hn_html, field, value):
    soup = BeautifulSoup(hn_html, "html.parser")
    row = soup.select("tr.athing.submission")[-1]
    link = row.select_one(".titleline > a")
    if field == "title":
        link.clear()
        if value is not None:
            link.string = value
    else:
        node = row if field == "id" else link
        if value is None:
            del node[field]
        else:
            node[field] = value
    with pytest.raises(ValueError):
        HackerNewsSource().parse(str(soup))


@pytest.mark.parametrize("submission_id", ["0", "-1", "abc", "1.5"])
def test_hackernews_invalid_id(hn_html, submission_id):
    soup = BeautifulSoup(hn_html, "html.parser")
    soup.select_one("tr.athing.submission")["id"] = submission_id
    with pytest.raises(ValueError):
        HackerNewsSource().parse(str(soup))


def test_hackernews_identity_and_relative_story_links():
    html = (
        '<table><tr class="athing submission" id="123">'
        '<td class="titleline"><a href="item?id=123">Ask HN: Question?</a></td></tr></table>'
    )
    original = HackerNewsSource().parse(html)[0]
    changed = HackerNewsSource().parse(html.replace("item?id=123", "https://example.com/story"))[0]
    other = HackerNewsSource().parse(html.replace('id="123"', 'id="456"'))[0]
    assert original.url == "https://news.ycombinator.com/item?id=123"
    assert original.extra["story_url"] == original.url
    assert original.item_id == changed.item_id
    assert original.item_id != other.item_id
    assert original.published_at is None
    for field in ("rank", "score", "comments", "author"):
        assert original.extra[field] is None


@pytest.mark.parametrize("text, expected", [("discuss", 0), ("1 comment", 1), ("12 comments", 12)])
def test_hackernews_comment_counts(text, expected):
    html = (
        '<table><tr class="athing submission" id="123"><td class="titleline">'
        '<a href="item?id=123">Story</a></td></tr><tr><td class="subtext">'
        f'<a href="item?id=123">{text}</a></td></tr></table>'
    )
    assert HackerNewsSource().parse(html)[0].extra["comments"] == expected


@pytest.mark.parametrize(
    "timestamp, expected",
    [
        ("2026-11-13T21:05:53 1794603953", datetime(2026, 11, 13, 21, 5, 53)),
        ("2026-11-13T21:05:53Z 1794603953", datetime.fromisoformat("2026-11-13T21:05:53+00:00")),
        ("", None),
    ],
)
def test_hackernews_timestamp_token(hn_html, timestamp, expected):
    soup = BeautifulSoup(hn_html, "html.parser")
    soup.select_one(".age")["title"] = timestamp
    assert HackerNewsSource().parse(str(soup))[0].published_at == expected


def test_remotive_fixture(jobs_payload):
    items = RemotiveSource({"limit": 5}).parse(jobs_payload)
    assert len(items) == 16
    assert len({item.item_id for item in items}) == 16
    first = items[0]
    assert first.title == "Remote Office Assistant"
    assert first.company == "Coalition Technologies"
    assert first.location == "Worldwide"
    assert first.extra["salary"] == "$35,3k- $52k"
    assert first.extra["id"] == 1680495
    assert first.published_at == datetime(2026, 11, 11, 20, 17, 48)
    assert items[2].extra["salary"] == "$14 - $20/hour"
    for item, job in zip(items, jobs_payload["jobs"]):
        assert item.source == "remotive" and item.category == Category.JOBS
        assert item.title == job["title"].strip()
        assert item.url == job["url"]
        assert item.url.startswith("https://remotive.com/remote-jobs/")
        assert item.company == job["company_name"].strip()
        assert item.location == job["candidate_required_location"].strip()
        assert item.published_at.tzinfo is None
        assert item.price is None and item.currency is None
        assert item.extra["attribution"] == "Remotive"
        assert item.extra["salary"] == job["salary"]
        assert len(item.summary) <= 600
        assert not BeautifulSoup(item.summary, "html.parser").find()


def test_remotive_summary_and_optional_fields(jobs_payload):
    job = {"id": 1, "title": "Engineer", "url": jobs_payload["jobs"][0]["url"]}
    item = RemotiveSource().parse({"jobs": [job]})[0]
    assert item.company == item.location == item.summary == item.extra["salary"] == ""
    assert item.published_at is None
    job.update({
        "company_name": "", "candidate_required_location": "", "publication_date": "",
        "salary": "Negotiable + equity", "description": (
            '<style>hidden styles</style><script>hidden script</script>'
            '<p>Build <b>great</b> things &amp; learn.</p><div>Remote team.</div>'
        ),
    })
    item = RemotiveSource().parse({"jobs": [job]})[0]
    assert item.summary == "Build great things & learn. Remote team."
    assert item.extra["salary"] == "Negotiable + equity"
    job["description"] = "<p>" + "x" * 650 + "</p>"
    assert RemotiveSource().parse({"jobs": [job]})[0].summary == "x" * 600
    job["publication_date"] = "2026-11-13T21:05:53+02:00"
    assert RemotiveSource().parse({"jobs": [job]})[0].published_at.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize("field", ["id", "title", "url"])
def test_remotive_missing_required_data(jobs_payload, field):
    job = dict(jobs_payload["jobs"][0])
    del job[field]
    with pytest.raises(ValueError):
        RemotiveSource().parse({"jobs": [jobs_payload["jobs"][1], job]})


@pytest.mark.parametrize(
    "field, value",
    [
        ("id", None), ("id", "123"), ("id", 0), ("id", -1), ("id", True), ("id", 1.5),
        ("title", None), ("title", ""), ("title", "   "), ("title", 12),
        ("url", None), ("url", ""), ("url", "   "), ("url", []),
        ("description", {}), ("salary", 120000), ("company_name", []),
        ("candidate_required_location", {}), ("publication_date", "not a date"),
    ],
)
def test_remotive_invalid_fields(jobs_payload, field, value):
    job = dict(jobs_payload["jobs"][0], **{field: value})
    with pytest.raises(ValueError):
        RemotiveSource().parse({"jobs": [job]})


@pytest.mark.parametrize("payload", [None, [], "{}", {}, {"jobs": None}, {"jobs": {}}, {"jobs": [None]}])
def test_remotive_invalid_payload(payload):
    with pytest.raises(ValueError):
        RemotiveSource().parse(payload)


@pytest.mark.parametrize("source_type", [BooksSource, HackerNewsSource])
@pytest.mark.parametrize("payload", [None, {}, [], "", "<html>Access denied</html>"])
def test_html_sources_invalid_payload(source_type, payload):
    with pytest.raises(ValueError):
        source_type().parse(payload)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,test", "ftp://example.com/file"])
@pytest.mark.parametrize("source_type", [BooksSource, HackerNewsSource, RemotiveSource])
def test_sources_reject_non_http_urls(source_type, url, books_html, hn_html, jobs_payload):
    if source_type is RemotiveSource:
        payload = {"jobs": [dict(jobs_payload["jobs"][0], url=url)]}
    else:
        soup = BeautifulSoup(books_html if source_type is BooksSource else hn_html, "html.parser")
        selector = "article.product_pod h3 a" if source_type is BooksSource else ".titleline > a"
        soup.select_one(selector)["href"] = url
        payload = str(soup)
    with pytest.raises(ValueError):
        source_type().parse(payload)


@pytest.mark.parametrize("options, expected", [({}, 1), ({"pages": 3}, 3), ({"pages": 0}, 1), ({"pages": -2}, 1), ({"pages": 99}, 5)])
def test_books_fetch_bounds(books_html, options, expected):
    fetcher = Mock(spec=Fetcher)
    fetcher.get_text.return_value = books_html
    items = list(BooksSource(options).fetch(fetcher))
    assert len(items) == 20 * expected
    assert fetcher.get_text.call_args_list == [
        call(f"https://books.toscrape.com/catalogue/page-{page}.html")
        for page in range(1, expected + 1)
    ]
    fetcher.get_json.assert_not_called()


def test_hackernews_fetch_one_page(hn_html):
    fetcher = Mock(spec=Fetcher)
    fetcher.get_text.return_value = hn_html
    assert len(list(HackerNewsSource({"pages": 5}).fetch(fetcher))) == 30
    fetcher.get_text.assert_called_once_with("https://news.ycombinator.com/")
    fetcher.get_json.assert_not_called()


@pytest.mark.parametrize("options, expected", [({}, 25), ({"limit": 5}, 5), ({"limit": 0}, 1), ({"limit": -2}, 1), ({"limit": 999}, 100)])
def test_remotive_fetch_bounds(jobs_payload, options, expected):
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = jobs_payload
    items = list(RemotiveSource(options).fetch(fetcher))
    assert len(items) == min(expected, 16)
    assert [item.url for item in items] == [job["url"] for job in jobs_payload["jobs"][:expected]]
    fetcher.get_json.assert_called_once_with(f"https://remotive.com/api/remote-jobs?limit={expected}")
    fetcher.get_text.assert_not_called()


@pytest.mark.parametrize("source_type, option", [(BooksSource, "pages"), (RemotiveSource, "limit")])
@pytest.mark.parametrize("value", [None, True, 1.5, "invalid", "2"])
def test_invalid_fetch_options(source_type, option, value):
    fetcher = Mock(spec=Fetcher)
    with pytest.raises(ValueError):
        list(source_type({option: value}).fetch(fetcher))
    assert fetcher.mock_calls == []


def test_remotive_empty_list_fetch_fails():
    source = RemotiveSource()
    assert source.parse({"jobs": []}) == []
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {"jobs": []}
    with pytest.raises(FetchError, match="no jobs"):
        list(source.fetch(fetcher))


def test_remotive_validates_jobs_beyond_limit(jobs_payload):
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {"jobs": [*jobs_payload["jobs"], {"id": 123}]}
    with pytest.raises(ValueError):
        list(RemotiveSource({"limit": 1}).fetch(fetcher))


def test_remotive_malformed_json_propagates():
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.side_effect = json.JSONDecodeError("Invalid JSON", "{", 1)
    with pytest.raises(json.JSONDecodeError):
        list(RemotiveSource().fetch(fetcher))


@pytest.mark.parametrize("source_type, method", [(BooksSource, "get_text"), (HackerNewsSource, "get_text"), (RemotiveSource, "get_json")])
def test_fetch_errors_propagate(source_type, method):
    fetcher = Mock(spec=Fetcher)
    getattr(fetcher, method).side_effect = FetchError("Unavailable")
    with pytest.raises(FetchError, match="Unavailable"):
        list(source_type().fetch(fetcher))


def test_books_later_page_error_propagates(books_html):
    fetcher = Mock(spec=Fetcher)
    fetcher.get_text.side_effect = [books_html, "<html>Blocked</html>"]
    with pytest.raises(ValueError):
        list(BooksSource({"pages": 2}).fetch(fetcher))
    assert fetcher.get_text.call_count == 2
