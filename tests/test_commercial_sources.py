from datetime import datetime, timezone
from unittest.mock import Mock, call
from xml.etree.ElementTree import ParseError
from xml.sax.saxutils import escape

import pytest
from defusedxml.common import DefusedXmlException

from cyprus.fetch import FetchError, Fetcher
from cyprus.models import Category
from cyprus.sources import Source, available, create
from cyprus.sources.dealnews import DealNewsSource
from cyprus.sources.gutenberg import GutenbergSource
from cyprus.sources.openlibrary import OpenLibrarySource

# Synthetic metadata for offline parser tests; these are not live listings.
WORK = {
    "key": "/works/OL123W",
    "title": "  Synthetic catalogue title  ",
    "authors": [{"name": "Fixture Author"}, {"name": "Second Author"}],
    "first_publish_year": 1901,
    "edition_count": 12,
    "has_fulltext": True,
    "ia": ["synthetic_identifier"],
}
OPENLIBRARY_JSON = {"works": [WORK]}
CHANNEL_DATE = "<pubDate>Sat, 14 Nov 2026 08:00:00 GMT</pubDate>"
GUTENBERG_ENTRY = """<item>
<title>  Synthetic ebook  </title>
<link> https://www.gutenberg.org/ebooks/123 </link>
<description><![CDATA[<style>hidden styles</style><script>hidden script</script>
<p>A <b>complete</b> ebook &amp; its metadata.</p>]]></description>
</item>"""
OFFER_HTML = """<style>hidden styles</style><script>hidden script</script>
<p>Use <b>FIXTURE</b> at checkout &amp; pay shipping.</p>
<p>Shipping is $5; members only. Expires tonight; exclusions apply.</p>"""
DEALNEWS_ENTRY = f"""<item>
<title>  Synthetic  &amp; Test &lt;offer&gt; for $19.99  </title>
<link> https://www.dealnews.com/example/123.html?ref=feed&amp;code=A%2FB+X </link>
<guid isPermaLink="false">fixture-offer-123</guid>
<pubDate>Sat, 14 Nov 2026 08:00:00 -0500</pubDate>
<description><![CDATA[{OFFER_HTML}]]></description>
<d:retailer><![CDATA[<b>Fixture Shop</b> &amp; Partners<script>hidden</script>]]></d:retailer>
<d:price currency="USD"> $1,299.95 </d:price>
<d:expires> Tonight, while supplies last </d:expires>
<d:category>Electronics</d:category><d:dealType>Coupon</d:dealType>
<d:staffPick>true</d:staffPick>
</item>"""


def rss(entries, channel_metadata=""):
    return (
        '<rss xmlns:d="https://www.dealnews.com/ns/rss/1.0.htm"><channel>'
        + channel_metadata + entries + "</channel></rss>"
    )


GUTENBERG_XML = rss(GUTENBERG_ENTRY, CHANNEL_DATE)
DEALNEWS_XML = rss(DEALNEWS_ENTRY)
SOURCES = [OpenLibrarySource, GutenbergSource, DealNewsSource]
RSS_SOURCES = [GutenbergSource, DealNewsSource]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    monkeypatch.setattr(
        "httpx.Client.send", Mock(side_effect=AssertionError("No live network calls allowed"))
    )


@pytest.mark.parametrize("source_type,name,category,interval", [
    (OpenLibrarySource, "openlibrary", Category.BOOKS, 86400),
    (GutenbergSource, "gutenberg", Category.BOOKS, 86400),
    (DealNewsSource, "dealnews", Category.MARKETPLACE, 1800),
])
def test_registered_sources(source_type, name, category, interval):
    source = create(name)
    assert available()[name] is source_type
    assert isinstance(source, Source) and isinstance(source, source_type)
    assert source.name == name and source.category == category
    assert source.minimum_interval == interval
    assert not source.complete_snapshot


def test_openlibrary_catalogue_metadata():
    item = OpenLibrarySource().parse(OPENLIBRARY_JSON)[0]
    assert item.title == "Synthetic catalogue title"
    assert item.url == "https://openlibrary.org/works/OL123W"
    assert item.source == "openlibrary" and item.category == Category.BOOKS
    assert item.price is None and item.currency is None and item.company is None
    assert item.published_at is None
    assert "catalogue record" in item.summary and "not a price or stock listing" in item.summary
    assert "Fixture Author, Second Author" in item.summary
    assert item.extra == {
        "authors": ["Fixture Author", "Second Author"], "first_publish_year": 1901,
        "edition_count": 12, "has_fulltext": True, "ia": ["synthetic_identifier"],
        "attribution": "Open Library", "kind": "Book catalogue",
    }


def test_openlibrary_minimal_and_optional_author_metadata():
    work = {"title": "Minimal record", "key": "/works/OL1W"}
    item = OpenLibrarySource().parse({"works": [work]})[0]
    assert item.extra["authors"] == [] and item.extra["first_publish_year"] is None
    assert item.published_at is None and item.company is None and item.price is None
    work["authors"] = [None, {}, {"name": None}, {"name": " "}, {"name": " Author "}, "invalid"]
    assert OpenLibrarySource().parse({"works": [work]})[0].extra["authors"] == ["Author"]
    assert OpenLibrarySource().parse({"works": []}) == []


@pytest.mark.parametrize("payload", [None, [], "{}", {}, {"works": None}, {"works": {}}, {"works": [None]}])
def test_openlibrary_invalid_documents(payload):
    with pytest.raises(ValueError):
        OpenLibrarySource().parse(payload)


@pytest.mark.parametrize("field", ["title", "key"])
@pytest.mark.parametrize("value", [None, "", "   ", 123, True, [], {}])
def test_openlibrary_invalid_required_fields(field, value):
    with pytest.raises(ValueError):
        OpenLibrarySource().parse({"works": [dict(WORK, **{field: value})]})


@pytest.mark.parametrize("field", ["title", "key"])
def test_openlibrary_missing_required_fields(field):
    work = dict(WORK)
    del work[field]
    with pytest.raises(ValueError):
        OpenLibrarySource().parse({"works": [work]})


@pytest.mark.parametrize("key", [
    "/works/OLW", "/works/OL123M", "/works/OLabcW", "/works/OL１２３W",
    "/works/OL123W?ref=x", "/works/OL123W/../other", "//evil.example/works/OL123W",
    "https://openlibrary.org/works/OL123W", "/works/OL123W\ntrailing",
])
def test_openlibrary_invalid_work_key(key):
    with pytest.raises(ValueError):
        OpenLibrarySource().parse({"works": [dict(WORK, key=key)]})


def test_gutenberg_metadata_ignores_channel_publication_date():
    item = GutenbergSource().parse(GUTENBERG_XML)[0]
    assert item.source == "gutenberg" and item.category == Category.BOOKS
    assert item.title == "Synthetic ebook" and item.url == "https://www.gutenberg.org/ebooks/123"
    assert item.summary == "A complete ebook & its metadata."
    assert item.published_at is None and item.price is None and item.company is None
    assert item.extra == {"attribution": "Project Gutenberg", "kind": "Free ebook"}


def test_dealnews_preserves_offer_metadata():
    item = DealNewsSource().parse(DEALNEWS_XML)[0]
    assert item.source == "dealnews" and item.category == Category.MARKETPLACE
    assert item.title == "Synthetic  & Test <offer> for $19.99"
    assert item.url == "https://www.dealnews.com/example/123.html?ref=feed&code=A%2FB+X"
    assert item.company == "Fixture Shop & Partners"
    assert item.price == 1299.95 and item.currency == "USD"
    assert item.published_at == datetime(2026, 11, 14, 13, tzinfo=timezone.utc)
    assert item.summary == (
        "Use FIXTURE at checkout & pay shipping. Shipping is $5; members only. "
        "Expires tonight; exclusions apply."
    )
    assert item.extra == {
        "attribution": "DealNews", "kind": "Retail offer", "offer_html": OFFER_HTML,
        "guid": "fixture-offer-123", "price_text": " $1,299.95 ",
        "expires": " Tonight, while supplies last ", "category": "Electronics",
        "dealType": "Coupon", "staffPick": "true", "pubDate": "Sat, 14 Nov 2026 08:00:00 -0500",
    }


@pytest.mark.parametrize("source_type", RSS_SOURCES)
def test_rss_full_plain_text_summary(source_type):
    text = "Full description. " * 100 + "Shipping extra; expires at midnight; members only."
    html = "<script>hidden script</script><style>hidden styles</style><p>" + text + "</p>"
    item = source_type().parse(rss(
        '<item><title>Long entry</title><link>https://example.org/item</link>'
        f'<description><![CDATA[{html}]]></description></item>'
    ))[0]
    assert item.summary == text and len(item.summary) > 600
    if source_type is DealNewsSource:
        assert item.extra["offer_html"] == html


@pytest.mark.parametrize("source_type", RSS_SOURCES)
def test_rss_minimal_and_blank_optional_fields(source_type):
    entry = '<item><title>Minimal $19.99 record</title><link>http://example.org/item</link>{}</item>'
    for optional in ("", "<description/><pubDate> </pubDate><d:price/><d:retailer> </d:retailer>"):
        item = source_type().parse(rss(entry.format(optional), CHANNEL_DATE))[0]
        assert item.summary == "" and item.company is None
        assert item.price is None and item.currency is None and item.published_at is None
    assert source_type().parse(rss("")) == []


@pytest.mark.parametrize("source_type", RSS_SOURCES)
@pytest.mark.parametrize("field", ["title", "link"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_rss_required_fields(source_type, field, value):
    fields = {"title": "Record", "link": "https://example.org/item"}
    fields[field] = value
    entry = "<item>" + "".join(
        f"<{name}>{text}</{name}>" for name, text in fields.items() if text is not None
    ) + "</item>"
    with pytest.raises(ValueError):
        source_type().parse(rss(entry))


@pytest.mark.parametrize("source_type", RSS_SOURCES)
@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,test", "ftp://example.org/item",
    "//example.org/item", "https:///missing-host", "https://user:secret@example.org/item",
])
def test_rss_unsafe_item_links(source_type, url):
    with pytest.raises(ValueError):
        source_type().parse(rss(f"<item><title>Record</title><link>{escape(url)}</link></item>"))


@pytest.mark.parametrize("source_type", RSS_SOURCES)
@pytest.mark.parametrize("payload", [None, {}, [], "<html>Blocked</html>", "<rss/>", "<feed><channel/></feed>"])
def test_rss_invalid_documents(source_type, payload):
    with pytest.raises(ValueError):
        source_type().parse(payload)


@pytest.mark.parametrize("source_type", RSS_SOURCES)
def test_rss_malformed_xml(source_type):
    with pytest.raises(ParseError):
        source_type().parse("<rss><channel>")


@pytest.mark.parametrize("source_type", RSS_SOURCES)
@pytest.mark.parametrize("entity", ['"secret"', 'SYSTEM "file:///must-not-be-read"', 'SYSTEM "https://example.org/must-not-fetch"'])
def test_rss_rejects_xml_entities(source_type, entity):
    with pytest.raises(DefusedXmlException):
        source_type().parse(
            f'<!DOCTYPE rss [<!ENTITY x {entity}>]>'
            + rss('<item><title>&x;</title><link>https://example.org/item</link></item>')
        )


@pytest.mark.parametrize("source_type", RSS_SOURCES)
@pytest.mark.parametrize("date,expected", [
    ("Sat, 14 Nov 2026 08:00:00 -0500", datetime(2026, 11, 14, 13, tzinfo=timezone.utc)),
    ("Sat, 14 Nov 2026 08:00:00 +0200", datetime(2026, 11, 14, 6, tzinfo=timezone.utc)),
    ("Sat, 14 Nov 2026 08:00:00 GMT", datetime(2026, 11, 14, 8, tzinfo=timezone.utc)),
    ("invalid date", None), (" ", None),
    ("Sat, 14 Nov 2026 08:00:00 -0000", None),
    ("Sat, 14 Nov 2026 08:00:00", None),
])
def test_rss_optional_item_date(source_type, date, expected):
    item = source_type().parse(rss(
        '<item><title>Record</title><link>https://example.org/item</link>'
        f'<pubDate>{date}</pubDate></item>', CHANNEL_DATE
    ))[0]
    assert item.published_at == expected
    assert item.extra["pubDate"] == date


@pytest.mark.parametrize("price,expected", [
    ("$1,299.95", 1299.95), ("  $ 9.99  ", 9.99), ("0", 0.0), ("29", 29.0),
    ("12.50", 12.5), (".99", 0.99), ("", None), ("Free shipping", None),
    ("20% off", None), ("from $10", None), ("$9.99 with code", None),
    ("$10-$20", None), ("1,23.00", None), ("1.299,95", None), ("-1", None),
    ("NaN", None), ("Infinity", None), ("1e3", None), ("9" * 400, None),
])
def test_dealnews_explicit_price_only(price, expected):
    item = DealNewsSource().parse(rss(
        '<item><title>Deal for $19.99</title><link>https://example.org/item</link>'
        f'<d:price currency="USD">{price}</d:price></item>'
    ))[0]
    assert item.price == expected and item.extra["price_text"] == price
    assert item.currency == "USD"


@pytest.mark.parametrize("prefix", ["dn", "offer", ""])
def test_dealnews_extension_local_names(prefix):
    payload = DEALNEWS_XML.replace("xmlns:d=", f'xmlns:{prefix}=' if prefix else 'xmlns=')
    payload = payload.replace("d:", f"{prefix}:" if prefix else "")
    if not prefix:
        # Unqualified extension fields also occur; don't give the RSS itself a namespace.
        payload = payload.replace(' xmlns="https://www.dealnews.com/ns/rss/1.0.htm"', "")
    item = DealNewsSource().parse(payload)[0]
    assert item.company == "Fixture Shop & Partners" and item.price == 1299.95
    assert item.extra["dealType"] == "Coupon" and item.extra["staffPick"] == "true"


@pytest.mark.parametrize("source_type,method,payload,url,default,maximum", [
    (OpenLibrarySource, "get_json", {"works": [WORK] * 101},
     "https://openlibrary.org/subjects/science_fiction.json?limit={limit}", 20, 50),
    (GutenbergSource, "get_text", rss(GUTENBERG_ENTRY * 101),
     "https://www.gutenberg.org/cache/epub/feeds/today.rss", 25, 100),
    (DealNewsSource, "get_text", rss(DEALNEWS_ENTRY * 101),
     "https://www.dealnews.com/?rss=1&sort=time", 50, 100),
])
def test_fetch_fixed_urls_and_limits(source_type, method, payload, url, default, maximum):
    for options, expected in [({}, default), ({"limit": 1}, 1), ({"limit": maximum}, maximum)]:
        fetcher = Mock(spec=Fetcher)
        getattr(fetcher, method).return_value = payload
        # Unknown URL options cannot turn an adapter into an arbitrary URL fetcher.
        source = source_type({**options, "url": "https://untrusted.example/"})
        assert len(list(source.fetch(fetcher))) == expected
        assert fetcher.mock_calls == [getattr(call, method)(url.format(limit=expected))]


@pytest.mark.parametrize("subject", ["history", "science_fiction", "a" * 80, "a_012"])
def test_openlibrary_subject_fetch(subject):
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = OPENLIBRARY_JSON
    assert len(list(OpenLibrarySource({"subject": subject, "limit": 5}).fetch(fetcher))) == 1
    fetcher.get_json.assert_called_once_with(f"https://openlibrary.org/subjects/{subject}.json?limit=5")


@pytest.mark.parametrize("subject", [
    None, True, 123, [], {}, "", " ", "Science_fiction", "science-fiction", "a" * 81,
    "../search", "x?limit=100", "x&limit=100", "https://example.org", "fiction\n", "café",
])
def test_openlibrary_invalid_subject_before_fetch(subject):
    fetcher = Mock(spec=Fetcher)
    with pytest.raises(ValueError):
        list(OpenLibrarySource({"subject": subject}).fetch(fetcher))
    assert fetcher.mock_calls == []


@pytest.mark.parametrize("source_type", SOURCES)
@pytest.mark.parametrize("limit", [None, True, False, 1.0, "1", [], {}, 0, -1, 101])
def test_invalid_limits_before_fetch(source_type, limit):
    fetcher = Mock(spec=Fetcher)
    with pytest.raises(ValueError):
        list(source_type({"limit": limit}).fetch(fetcher))
    assert fetcher.mock_calls == []


def test_openlibrary_rejects_limit_above_fifty():
    fetcher = Mock(spec=Fetcher)
    with pytest.raises(ValueError):
        list(OpenLibrarySource({"limit": 51}).fetch(fetcher))
    assert fetcher.mock_calls == []


@pytest.mark.parametrize("source_type,method,payload", [
    (OpenLibrarySource, "get_json", {"works": [WORK, {}]}),
    (GutenbergSource, "get_text", rss(GUTENBERG_ENTRY + "<item/>")),
    (DealNewsSource, "get_text", rss(DEALNEWS_ENTRY + "<item/>")),
])
def test_fetch_validates_whole_payload(source_type, method, payload):
    fetcher = Mock(spec=Fetcher)
    getattr(fetcher, method).return_value = payload
    with pytest.raises(ValueError):
        list(source_type({"limit": 1}).fetch(fetcher))


@pytest.mark.parametrize("source_type,method", [
    (OpenLibrarySource, "get_json"), (GutenbergSource, "get_text"), (DealNewsSource, "get_text"),
])
def test_fetch_errors_propagate(source_type, method):
    fetcher = Mock(spec=Fetcher)
    getattr(fetcher, method).side_effect = FetchError("Unavailable")
    with pytest.raises(FetchError, match="Unavailable"):
        list(source_type().fetch(fetcher))
