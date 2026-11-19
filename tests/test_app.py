import csv
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cyprus.cli import app
from cyprus.digest import build_digest, send_digest
from cyprus.export import csv_text
from cyprus.models import Category, Item
from cyprus.store import Store
from cyprus.web.app import create_app

CONFIG = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    with Store(path) as store:
        for index in range(15):
            store.upsert(Item(source="dealnews", category=Category.MARKETPLACE, title=f"Book {index}", url=f"https://example.com/books/{index}", price=10 + index, currency="GBP"))
        store.upsert(Item(source="pythonjobs", category=Category.JOBS, title='Python <script>alert("x")</script>', url="https://www.python.org/jobs/100/", summary="Python developer & data engineer"))
        store.upsert(Item(source="hackernews", category=Category.NEWS, title="Database news", url="https://news.ycombinator.com/item?id=1"))
        store.commit()
    return path


@pytest.fixture
def client(db):
    with TestClient(create_app(db, CONFIG), base_url="http://127.0.0.1") as client:
        yield client


def test_dashboard_filters_pagination_and_escaping(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Project Cyprus" in response.text
    assert "Book" in response.text
    assert 'page=2' in response.text
    assert '<script>alert(' not in response.text
    filtered = client.get("/", params={"category": "jobs", "q": "Python"})
    assert filtered.status_code == 200
    assert 'Python &lt;script&gt;' in filtered.text
    assert 'href="https://example.com/books/' not in filtered.text
    assert client.get("/?page=2").status_code == 200
    assert client.get("/?page=9999").status_code == 200
    assert "No finds match" in client.get("/?q=not-a-real-listing").text
    assert "script-src 'none'" in response.headers["content-security-policy"]


def test_empty_dashboard(tmp_path):
    with TestClient(create_app(tmp_path / "empty.db", CONFIG), base_url="http://localhost") as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "cyprus run all" in response.text
        assert "No" in response.text or "empty" in response.text.lower()
        assert "No new or changed listings" in client.get("/digest").text


@pytest.mark.parametrize("path", ["/?page=0", "/?category=invalid", "/?source=unknown", "/?q=" + "a" * 201, "/digest?hours=0"])
def test_bad_query_is_client_error(client, path):
    assert client.get(path).status_code == 422


def test_downloads_are_filtered_and_read_only(client, monkeypatch):
    monkeypatch.setattr("cyprus.fetch.Fetcher.get", Mock(side_effect=AssertionError("No live calls in dashboard")))
    response = client.get("/export/json?category=jobs")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["source"] == "pythonjobs"
    assert isinstance(response.json()[0]["extra"], dict)
    csv_response = client.get("/export/csv?category=marketplace")
    assert "attachment" in csv_response.headers["content-disposition"]
    assert len(list(csv.DictReader(io.StringIO(csv_response.text)))) == 15
    assert client.get("/export/xml").status_code == 404
    assert client.post("/").status_code == 405
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 400


@pytest.mark.parametrize("title", ["=SUM(A1)", "+formula", "-formula", "@formula", "\tformula", "  =SUM(A1)"])
def test_csv_formula_escaping(title):
    row = list(csv.DictReader(io.StringIO(csv_text([{"title": title}]))))[0]
    assert row["title"] == "'" + title


def test_digest_is_escaped_and_deduplicated(db):
    with Store(db) as store:
        item = Item(source="pythonjobs", category=Category.JOBS, title='Python <script>alert("x")</script>', url="https://www.python.org/jobs/100/", summary="Changed summary")
        store.upsert(item)
        store.commit()
        html, text, count = build_digest(store)
    assert count == 17
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert text.count("https://www.python.org/jobs/100/") == 1


def test_smtp_tls_and_opt_in(db, monkeypatch):
    settings = {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "from@example.com", "SMTP_TO": "to@example.com", "SMTP_USER": "test-user", "SMTP_PASSWORD": "test-password", "SMTP_SECURITY": "starttls", "SMTP_PORT": "587"}
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    smtp = Mock()
    smtp.__enter__ = Mock(return_value=smtp)
    smtp.__exit__ = Mock(return_value=False)
    factory = Mock(return_value=smtp)
    monkeypatch.setattr("cyprus.digest.smtplib.SMTP", factory)
    runner = CliRunner()
    preview = runner.invoke(app, ["--db", str(db), "digest"])
    assert preview.exit_code == 0, preview.output
    factory.assert_not_called()
    sent = runner.invoke(app, ["--db", str(db), "digest", "--send"])
    assert sent.exit_code == 0, sent.output
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("test-user", "test-password")
    smtp.send_message.assert_called_once()
    assert "test-password" not in sent.output


def test_missing_smtp_fails_without_network(monkeypatch):
    for key in ["SMTP_HOST", "SMTP_FROM", "SMTP_TO"]:
        monkeypatch.delenv(key, raising=False)
    smtp = Mock()
    monkeypatch.setattr("cyprus.digest.smtplib.SMTP", smtp)
    with pytest.raises(ValueError, match="SMTP_HOST"):
        send_digest("html", "text", 1)
    smtp.assert_not_called()


def test_cli_export_never_overwrites_and_errors_are_nonzero(db, tmp_path):
    runner = CliRunner()
    output = tmp_path / "export.json"
    result = runner.invoke(app, ["--db", str(db), "export", "json", "-o", str(output), "--category", "jobs"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(output.read_text(encoding="utf-8"))) == 1
    contents = output.read_bytes()
    repeat = runner.invoke(app, ["--db", str(db), "export", "json", "-o", str(output)])
    assert repeat.exit_code != 0
    assert output.read_bytes() == contents
    assert runner.invoke(app, ["--config", str(CONFIG), "run", "remotive"]).exit_code != 0
    assert runner.invoke(app, ["--config", str(CONFIG), "run", "unknown"]).exit_code != 0
    assert runner.invoke(app, ["--db", str(db), "list", "--limit", "0"]).exit_code != 0


def test_cli_custom_db_still_loads_environment(db, monkeypatch):
    loader = Mock(return_value=Path("unused.db"))
    monkeypatch.setattr("cyprus.cli.database_path", loader)
    result = CliRunner().invoke(app, ["--db", str(db), "digest"])
    assert result.exit_code == 0, result.output
    loader.assert_called_once_with()


def test_cli_history_preserves_zero_price(db):
    item = Item(source="dealnews", category=Category.MARKETPLACE, title="Free book", url="https://example.com/free", price=0, currency="GBP")
    with Store(db) as store:
        store.upsert(item)
        store.commit()
    result = CliRunner().invoke(app, ["--db", str(db), "history", item.item_id])
    assert result.exit_code == 0, result.output
    assert "0.0" in result.output


def test_books_category_and_retired_demo_are_consistent(db):
    with Store(db) as store:
        store.upsert(Item(source="openlibrary", category=Category.BOOKS, title="A real catalogue entry", url="https://openlibrary.org/works/OL1W", extra={"first_publish_year": 1965}))
        store.upsert(Item(source="books", category=Category.MARKETPLACE, title="Retired fake product", url="https://books.toscrape.com/demo"))
        store.commit()
    with TestClient(create_app(db, CONFIG), base_url="http://localhost") as client:
        html = client.get("/?category=books").text
        assert "A real catalogue entry" in html and "First published 1965" in html
        assert "Retired fake product" not in client.get("/").text
        assert "Retired fake product" not in client.get("/digest").text
        exported = client.get("/export/json").json()
        assert len(exported) == 18
        assert all(row["source"] != "books" for row in exported)
        assert "LinkedIn" in html and "Upwork" in html and "Not connected" in html
        assert "three hours" in html
    runner = CliRunner()
    result = runner.invoke(app, ["--db", str(db), "export", "json"])
    assert result.exit_code == 0
    assert all(row["source"] != "books" for row in json.loads(result.output))
    assert "Retired fake product" not in runner.invoke(app, ["--db", str(db), "digest"]).output
    with Store(db) as store:
        assert len(store.query()) == 19


def test_original_offer_html_is_sanitized_without_losing_links(db):
    offer = '<p>Shipping $8. Coupon SAVE10 required. <a href="https://shop.example/product?ref=dealnews&amp;coupon=SAVE10">Buy with coupon</a><a href="javascript:alert(1)" onclick="alert(2)">bad</a><script>alert(3)</script><img src="bad" onerror="alert(4)"></p>'
    with Store(db) as store:
        store.upsert(Item(source="dealnews", category=Category.MARKETPLACE, title="Test offer", url="https://www.dealnews.com/test", extra={"offer_html": offer, "expires": "2026-11-15"}))
        store.commit()
    with TestClient(create_app(db, CONFIG), base_url="http://localhost") as client:
        html = client.get("/?q=Test+offer").text
        assert 'href="https://shop.example/product?ref=dealnews&amp;coupon=SAVE10"' in html
        assert "Shipping $8. Coupon SAVE10 required." in html
        assert 'href="javascript:' not in html and "onclick=" not in html and "onerror=" not in html and "<script>" not in html
        assert "Full offer conditions and links" in html


def test_cli_source_failure_is_reported(db, monkeypatch):
    from cyprus.fetch import FetchError
    from cyprus.sources.dealnews import DealNewsSource

    def fail(self, fetcher):
        raise FetchError("test failure")
    monkeypatch.setattr(DealNewsSource, "fetch", fail)
    result = CliRunner().invoke(app, ["--db", str(db), "--config", str(CONFIG), "run", "dealnews"])
    assert result.exit_code == 1
    assert "test failure" in result.output
    with Store(db) as store:
        assert len(store.query(limit=None)) == 17
        assert store.recent_runs()[0]["status"] == "error"
