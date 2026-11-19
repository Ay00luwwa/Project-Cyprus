from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from cyprus.fetch import FetchError, Fetcher
from cyprus.models import Category
from cyprus.sources import Source
from cyprus.sources.arbeitnow import ArbeitnowSource
from cyprus.sources.jobicy import JobicySource


@pytest.fixture
def jobicy_job():
    return {
        "id": 101,
        "jobTitle": " Platform Engineer ",
        "companyName": " Example Labs ",
        "jobGeo": " USA ",
        "jobType": ["Full-Time"],
        "url": "https://jobicy.com/jobs/101-platform-engineer",
        "jobExcerpt": "<p>Build <b>useful</b> tools &amp; services.</p>",
        "jobDescription": "<p>A longer description that should not replace the supplied excerpt.</p>",
        "pubDate": "2026-11-10T04:30:45+00:00",
        "salaryMin": "90000",
        "salaryMax": "125000",
        "salaryCurrency": "USD",
        "salaryPeriod": "year",
    }


@pytest.fixture
def arbeitnow_job():
    return {
        "slug": "platform-engineer-example-labs",
        "title": " Platform Engineer ",
        "company_name": " Example Labs ",
        "remote": True,
        "location": " Berlin, Germany ",
        "created_at": 1794285045,
        "url": "https://www.arbeitnow.com/jobs/companies/example-labs/platform-engineer",
        "description": "<p>Build <b>useful</b> tools &amp; services.</p>",
        "tags": ["Engineering", "Python"],
        "job_types": ["Full-time"],
    }


@pytest.fixture(params=["jobicy", "arbeitnow"])
def feed(request, jobicy_job, arbeitnow_job):
    if request.param == "jobicy":
        return JobicySource, "jobs", "jobTitle", jobicy_job
    return ArbeitnowSource, "data", "title", arbeitnow_job


def test_jobicy_schema_and_publisher_attribution(jobicy_job):
    source = JobicySource()
    items = source.parse({
        "jobs": [jobicy_job], "jobCount": 1, "lastUpdate": "2026-11-10T10:00:00+00:00",
        "nextCursor": "opaque-token", "hasMore": True,
    })
    assert len(items) == 1
    item = items[0]
    assert isinstance(source, Source)
    assert source.name == item.source == "jobicy"
    assert source.category == item.category == Category.JOBS
    assert source.minimum_interval == 3600
    assert not source.complete_snapshot
    assert item.title == "Platform Engineer"
    assert item.company == "Example Labs"
    assert item.location == "USA"
    assert item.url == jobicy_job["url"]
    assert item.published_at == datetime(2026, 11, 10, 4, 30, 45, tzinfo=timezone.utc)
    assert item.summary == "Build useful tools & services."
    assert item.extra == {
        "id": 101, "remote": True, "job_types": ["Full-Time"], "kind": "Job opening",
        "attribution": "Jobicy", "salaryMin": "90000", "salaryMax": "125000",
        "salaryCurrency": "USD", "salaryPeriod": "year",
    }
    assert item.price is None and item.currency is None


def test_arbeitnow_schema_preserves_regional_eligibility(arbeitnow_job):
    source = ArbeitnowSource()
    item = source.parse({"data": [arbeitnow_job], "links": {}, "meta": {}})[0]
    assert isinstance(source, Source)
    assert source.name == item.source == "arbeitnow"
    assert source.category == item.category == Category.JOBS
    assert source.minimum_interval == 3600
    assert not source.complete_snapshot
    assert item.title == "Platform Engineer"
    assert item.company == "Example Labs"
    assert item.location == "Berlin, Germany"
    assert item.url == arbeitnow_job["url"]
    assert item.published_at == datetime(2026, 11, 10, 4, 30, 45, tzinfo=timezone.utc)
    assert item.summary == "Build useful tools & services."
    assert item.extra == {
        "slug": "platform-engineer-example-labs", "remote": True,
        "tags": ["Engineering", "Python"], "job_types": ["Full-time"],
        "kind": "Job opening", "attribution": "Arbeitnow",
    }
    assert item.price is None and item.currency is None


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_jobicy_optional_metadata_and_absent_date(blank):
    job = {
        "jobTitle": "Engineer", "url": "https://jobicy.com/jobs/101-engineer",
        **dict.fromkeys(("companyName", "jobGeo", "jobExcerpt", "jobDescription", "pubDate"), blank),
        "jobType": None,
    }
    item = JobicySource().parse({"jobs": [job]})[0]
    assert item.company == item.location == item.summary == ""
    assert item.published_at is None
    assert item.extra["job_types"] == []


def test_missing_optional_metadata_does_not_invent_dates_or_locations(feed):
    source_type, envelope, title_field, job = feed
    item = source_type().parse({envelope: [{title_field: job[title_field], "url": job["url"]}]})[0]
    assert item.company == item.location == item.summary == ""
    assert item.published_at is None
    assert item.price is None and item.currency is None
    if source_type is ArbeitnowSource:
        assert item.extra["remote"] is None
        assert item.extra["tags"] == item.extra["job_types"] == []


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_arbeitnow_blank_optional_metadata(blank, arbeitnow_job):
    arbeitnow_job.update(dict.fromkeys(("company_name", "location", "description", "slug"), blank))
    arbeitnow_job.update(tags=None, job_types=None)
    item = ArbeitnowSource().parse({"data": [arbeitnow_job]})[0]
    assert item.company == item.location == item.summary == item.extra["slug"] == ""
    assert item.extra["tags"] == item.extra["job_types"] == []


def test_plain_text_removes_scripts_styles_and_keeps_all_text_nodes(feed):
    source_type, envelope, _, job = feed
    description_field = "jobDescription" if source_type is JobicySource else "description"
    job.pop("jobExcerpt", None)
    job[description_field] = (
        "<style>hidden styles</style><script>hidden script</script>"
        "<p>Build <b>great</b> things &amp; learn.</p><div>Remote team.</div>"
    )
    assert source_type().parse({envelope: [job]})[0].summary == "Build great things & learn. Remote team."
    job[description_field] = "<div>" + "x" * 650 + "</div>"
    assert source_type().parse({envelope: [job]})[0].summary == "x" * 600


def test_jobicy_preserves_publisher_excerpt_without_truncation(jobicy_job):
    jobicy_job["jobExcerpt"] = "<style>hidden</style><script>hidden</script><p>" + "x" * 650 + "</p>"
    item = JobicySource().parse({"jobs": [jobicy_job]})[0]
    assert item.summary == "x" * 650


@pytest.mark.parametrize("timestamp", ["2026-11-10T04:30:45Z", "2026-11-10 04:30:45", "2026-11-10T06:30:45+02:00"])
def test_jobicy_dates_are_normalized_to_utc(timestamp, jobicy_job):
    jobicy_job["pubDate"] = timestamp
    item = JobicySource().parse({"jobs": [jobicy_job]})[0]
    assert item.published_at == datetime(2026, 11, 10, 4, 30, 45, tzinfo=timezone.utc)
    assert item.published_at.tzinfo is timezone.utc


@pytest.mark.parametrize("timestamp", ["not a date", "2026-02-30T00:00:00Z", 1794285045, True, {}, []])
def test_jobicy_rejects_bad_dates(timestamp, jobicy_job):
    jobicy_job["pubDate"] = timestamp
    with pytest.raises(ValueError):
        JobicySource().parse({"jobs": [jobicy_job]})


@pytest.mark.parametrize("timestamp", [None, "", "1794285045", "not a date", True, 1.5, {}, [], 10**100, -(10**100)])
def test_arbeitnow_rejects_present_malformed_timestamps(timestamp, arbeitnow_job):
    arbeitnow_job["created_at"] = timestamp
    with pytest.raises(ValueError, match="created_at"):
        ArbeitnowSource().parse({"data": [arbeitnow_job]})


def test_arbeitnow_preserves_epoch_timestamp(arbeitnow_job):
    arbeitnow_job["created_at"] = 0
    item = ArbeitnowSource().parse({"data": [arbeitnow_job]})[0]
    assert item.published_at == datetime(1970, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("field", ["title", "url"])
def test_missing_required_fields_fail(feed, field):
    source_type, envelope, title_field, job = feed
    del job[title_field if field == "title" else field]
    with pytest.raises(ValueError):
        source_type().parse({envelope: [job]})


@pytest.mark.parametrize("field", ["title", "url"])
@pytest.mark.parametrize("value", [None, "", "   ", 123, True, [], {}])
def test_invalid_required_fields_fail(feed, field, value):
    source_type, envelope, title_field, job = feed
    job[title_field if field == "title" else field] = value
    with pytest.raises(ValueError):
        source_type().parse({envelope: [job]})


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,test", "ftp://example.com/file", "/jobs/1",
    "https:///jobs/1", "https://user:password@example.com/jobs/1", "http://user@example.com/jobs/1",
])
def test_item_rejects_unsafe_urls(feed, url):
    source_type, envelope, _, job = feed
    job["url"] = url
    with pytest.raises(ValueError, match="Item URL"):
        source_type().parse({envelope: [job]})


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_http_listing_urls_are_preserved(feed, scheme):
    source_type, envelope, _, job = feed
    job["url"] = f"{scheme}://example.com/jobs/1"
    assert source_type().parse({envelope: [job]})[0].url == job["url"]


@pytest.mark.parametrize("payload", [None, [], "{}", {}, {"error": "unavailable"}])
def test_invalid_envelopes_fail(feed, payload):
    source_type, _, _, _ = feed
    with pytest.raises(ValueError):
        source_type().parse(payload)


@pytest.mark.parametrize("value", [None, {}, "", 0, True, [None], [[]], ["job"], [{}]])
def test_invalid_jobs_lists_and_entries_fail(feed, value):
    source_type, envelope, _, _ = feed
    with pytest.raises(ValueError):
        source_type().parse({envelope: value})


def test_valid_empty_feed_is_not_a_schema_failure(feed):
    source_type, envelope, _, _ = feed
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {envelope: []}
    assert source_type().parse({envelope: []}) == []
    assert list(source_type().fetch(fetcher)) == []


@pytest.mark.parametrize("source_type,envelope,title_field,list_field", [
    (JobicySource, "jobs", "jobTitle", "jobType"),
    (ArbeitnowSource, "data", "title", "tags"),
    (ArbeitnowSource, "data", "title", "job_types"),
])
@pytest.mark.parametrize("value", ["", "Full-Time", {}, 1, True, [1], [None], [{}]])
def test_rejects_bad_metadata_lists(source_type, envelope, title_field, list_field, value):
    job = {title_field: "Engineer", "url": "https://example.com/jobs/1", list_field: value}
    with pytest.raises(ValueError, match=list_field):
        source_type().parse({envelope: [job]})


@pytest.mark.parametrize("field", ["companyName", "jobGeo", "jobExcerpt", "jobDescription"])
def test_jobicy_rejects_invalid_text_metadata(jobicy_job, field):
    jobicy_job[field] = {}
    with pytest.raises(ValueError, match=field):
        JobicySource().parse({"jobs": [jobicy_job]})


@pytest.mark.parametrize("field", ["company_name", "location", "description", "slug"])
def test_arbeitnow_rejects_invalid_text_metadata(arbeitnow_job, field):
    arbeitnow_job[field] = []
    with pytest.raises(ValueError, match=field):
        ArbeitnowSource().parse({"data": [arbeitnow_job]})


@pytest.mark.parametrize("value", [None, "true", "false", 1, 0, [], {}])
def test_arbeitnow_requires_a_boolean_remote_flag(value, arbeitnow_job):
    arbeitnow_job["remote"] = value
    with pytest.raises(ValueError, match="remote"):
        ArbeitnowSource().parse({"data": [arbeitnow_job]})


@pytest.mark.parametrize("value", ["101", True, 0, -1, 1.5])
def test_jobicy_rejects_invalid_optional_ids(value, jobicy_job):
    jobicy_job["id"] = value
    with pytest.raises(ValueError, match="id"):
        JobicySource().parse({"jobs": [jobicy_job]})


@pytest.mark.parametrize("salary", [90000, None, {}, True])
def test_jobicy_does_not_synthesize_salary_strings(salary, jobicy_job):
    jobicy_job.update(salaryMin=salary, salaryMax=salary, salaryCurrency=salary, salaryPeriod=salary)
    item = JobicySource().parse({"jobs": [jobicy_job]})[0]
    assert not any(key.startswith("salary") for key in item.extra)
    assert item.price is None and item.currency is None


@pytest.mark.parametrize("salary", ["EUR 70,000–90,000", 70000, None, {}])
def test_arbeitnow_only_keeps_explicit_salary_strings(salary, arbeitnow_job):
    arbeitnow_job["salary"] = salary
    item = ArbeitnowSource().parse({"data": [arbeitnow_job]})[0]
    if isinstance(salary, str):
        assert item.extra["salary"] == salary
    else:
        assert "salary" not in item.extra
    assert item.price is None and item.currency is None


@pytest.mark.parametrize("options,expected", [({}, 100), ({"count": 1}, 1), ({"count": 100}, 100)])
def test_jobicy_fetch_count_and_first_page_only(options, expected, jobicy_job):
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {
        "jobs": [dict(jobicy_job, id=index + 1, url=f"https://jobicy.com/jobs/{index + 1}") for index in range(101)],
        "hasMore": True, "nextCursor": "https://untrusted.example/next-page",
    }
    options = dict(options, url="https://untrusted.example/feed", cursor="not-used")
    items = list(JobicySource(options).fetch(fetcher))
    assert len(items) == expected
    fetcher.get_json.assert_called_once_with(f"https://jobicy.com/api/jobs?count={expected}")
    fetcher.get_text.assert_not_called()


def test_jobicy_only_sends_documented_filters(jobicy_job):
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {"jobs": [jobicy_job]}
    source = JobicySource({"count": 20, "tag": " python & data ", "geo": " usa ", "extra": "ignored"})
    assert len(list(source.fetch(fetcher))) == 1
    fetcher.get_json.assert_called_once()
    url = urlsplit(fetcher.get_json.call_args.args[0])
    assert (url.scheme, url.netloc, url.path) == ("https", "jobicy.com", "/api/jobs")
    assert parse_qs(url.query) == {"count": ["20"], "tag": ["python & data"], "geo": ["usa"]}


@pytest.mark.parametrize("value", [None, "", "   "])
def test_jobicy_omits_blank_filters(value):
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {"jobs": []}
    list(JobicySource({"tag": value, "geo": value}).fetch(fetcher))
    fetcher.get_json.assert_called_once_with("https://jobicy.com/api/jobs?count=100")


@pytest.mark.parametrize("field", ["tag", "geo"])
@pytest.mark.parametrize("value", [1, True, [], {}])
def test_jobicy_rejects_invalid_filters_before_fetch(field, value):
    fetcher = Mock(spec=Fetcher)
    with pytest.raises(ValueError, match=field):
        list(JobicySource({field: value}).fetch(fetcher))
    assert fetcher.mock_calls == []


@pytest.mark.parametrize("source_type,option,maximum", [(JobicySource, "count", 100), (ArbeitnowSource, "limit", 300)])
@pytest.mark.parametrize("value", [None, True, False, 1.5, "10", [], {}, 0, -1, "above-bound"])
def test_invalid_bounds_and_types_fail_before_fetch(source_type, option, maximum, value):
    if value == "above-bound":
        value = maximum + 1
    fetcher = Mock(spec=Fetcher)
    with pytest.raises(ValueError, match=option):
        list(source_type({option: value}).fetch(fetcher))
    assert fetcher.mock_calls == []


@pytest.mark.parametrize("value", [None, 1, 0, "true", "false", [], {}])
def test_arbeitnow_rejects_non_boolean_remote_option(value):
    fetcher = Mock(spec=Fetcher)
    with pytest.raises(ValueError, match="remote_only"):
        list(ArbeitnowSource({"remote_only": value}).fetch(fetcher))
    assert fetcher.mock_calls == []


@pytest.mark.parametrize("options,expected", [({}, 23), ({"limit": 1}, 1), ({"limit": 10}, 10), ({"limit": 300}, 23)])
def test_arbeitnow_filters_remote_before_limit(options, expected, arbeitnow_job):
    jobs = [dict(arbeitnow_job, remote=index >= 277, url=f"https://www.arbeitnow.com/jobs/{index}") for index in range(300)]
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {"data": jobs, "links": {"next": "https://untrusted.example/next"}}
    items = list(ArbeitnowSource(dict(options, url="https://untrusted.example/feed")).fetch(fetcher))
    assert len(items) == expected
    assert [item.url for item in items] == [job["url"] for job in jobs[277:277 + expected]]
    assert all(item.extra["remote"] is True for item in items)
    assert all(item.location == "Berlin, Germany" for item in items)
    fetcher.get_json.assert_called_once_with("https://www.arbeitnow.com/api/job-board-api")
    fetcher.get_text.assert_not_called()


@pytest.mark.parametrize("options,expected", [({"remote_only": False}, 50), ({"remote_only": False, "limit": 300}, 300)])
def test_arbeitnow_can_include_onsite_jobs(options, expected, arbeitnow_job):
    jobs = [dict(arbeitnow_job, remote=False, url=f"https://www.arbeitnow.com/jobs/{index}") for index in range(300)]
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {"data": jobs}
    items = list(ArbeitnowSource(options).fetch(fetcher))
    assert len(items) == expected
    assert all(item.extra["remote"] is False for item in items)
    assert len(ArbeitnowSource().parse({"data": jobs})) == 300


def test_no_remote_matches_is_valid(arbeitnow_job):
    del arbeitnow_job["remote"]
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {"data": [arbeitnow_job, dict(arbeitnow_job, remote=False)]}
    assert list(ArbeitnowSource().fetch(fetcher)) == []


def test_fetch_validates_all_jobs_before_limiting_or_filtering(feed):
    source_type, envelope, title_field, job = feed
    bad_job = dict(job, remote=False)
    del bad_job[title_field]
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.return_value = {envelope: [job, bad_job]}
    option = "count" if source_type is JobicySource else "limit"
    with pytest.raises(ValueError):
        list(source_type({option: 1}).fetch(fetcher))


@pytest.mark.parametrize("error", [FetchError("Unavailable"), json.JSONDecodeError("Invalid JSON", "{", 1)])
def test_fetch_errors_propagate_without_extra_requests(feed, error):
    source_type, _, _, _ = feed
    fetcher = Mock(spec=Fetcher)
    fetcher.get_json.side_effect = error
    with pytest.raises(type(error)):
        list(source_type().fetch(fetcher))
    fetcher.get_json.assert_called_once()
