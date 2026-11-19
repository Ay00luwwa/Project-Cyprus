from __future__ import annotations

from math import ceil
from pathlib import Path
from urllib.parse import urlencode

import bleach
from markupsafe import Markup

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import database_path, load_settings
from ..digest import build_digest
from ..export import csv_text, json_text
from ..models import Category, now_utc
from ..sources import available
from ..store import Store, row_to_dict

ROOT = Path(__file__).parent
LABELS = {
    "openlibrary": "Open Library", "gutenberg": "Project Gutenberg", "dealnews": "DealNews",
    "jobicy": "Jobicy", "arbeitnow": "Arbeitnow", "bbc": "BBC News",
    "hackernews": "Hacker News", "pythonjobs": "Python.org Jobs",
    "books": "Books to Scrape (retired demo)", "remotive": "Remotive",
}
NOTES = {
    "openlibrary": "Actual books and authors from Open Library. Catalogue records, not sale prices; checked daily.",
    "gutenberg": "Real ebooks added or updated by Project Gutenberg. Feed rebuilt nightly; check local copyright before downloading.",
    "dealnews": "Real retailer offers via DealNews. Checked every 30 minutes; confirm expiry, shipping and checkout prices at the source.",
    "jobicy": "Remote roles across industries. Checked hourly; Jobicy delays publication by three hours and includes seven days of jobs. Location restrictions apply.",
    "arbeitnow": "Remote-marked vacancies, mainly Germany and Europe. Checked hourly; language and location eligibility still apply.",
    "bbc": "BBC News headlines and publisher summaries, checked every 15 minutes. Original reporting stays at BBC News.",
    "hackernews": "Technology stories and discussions from the real Hacker News front page. Checked every five minutes.",
    "pythonjobs": "Real developer vacancies from Python.org. Checked every 30 minutes; verify closing dates on the listing.",
    "books": "Retired demonstration adapter; excluded from the default collection.",
    "remotive": "Not enabled: robots.txt blocks its API.",
}


def create_app(db_path: str | Path | None = None, config_path: str | Path | None = None) -> FastAPI:
    db_path = db_path or database_path()
    settings = load_settings(config_path)
    registry = available()
    visible_sources = [cfg.name for cfg in settings.sources if cfg.enabled]
    unknown = {source.name for source in settings.sources} - registry.keys()
    if unknown:
        raise ValueError(f"Unknown configured sources: {', '.join(sorted(unknown))}")
    app = FastAPI(title="Project Cyprus", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")

    def offer_html(value: str) -> Markup:
        return Markup(bleach.clean(value, tags={"p", "br", "a", "b", "strong", "em", "i", "ul", "ol", "li", "div", "span"},
                                  attributes={"a": ["href", "title"]}, protocols={"http", "https"}, strip=True))

    templates.env.filters["offer_html"] = offer_html

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def validate_source(source: str) -> None:
        if source and source not in registry:
            raise HTTPException(422, "Unknown source")

    def url(category: str = "", source: str = "", q: str = "", page: int = 1, path: str = "/") -> str:
        params = {key: value for key, value in {"category": category, "source": source, "q": q}.items() if value}
        if page != 1:
            params["page"] = str(page)
        return path + ("?" + urlencode(params) if params else "")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        category: str = Query("", pattern="^(|books|marketplace|news|jobs)$"),
        source: str = Query("", max_length=64),
        q: str = Query("", max_length=200),
        page: int = Query(1, ge=1),
    ):
        validate_source(source)
        q = q.strip()
        with Store(db_path) as store:
            matching = [row_to_dict(row) for row in store.query(category=category, source=source, search=q, limit=None, sources=visible_sources)]
            counts = [dict(row) for row in store.source_stats() if row["source"] in visible_sources]
            runs = [dict(row) for row in store.recent_runs(50) if row["source"] in visible_sources][:12]
            today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
            new_today = len(store.query(since=today, limit=None, sources=visible_sources))
            latest = {name: store.latest_run(name) for name in registry}
        category_counts = {key.value: sum(row["active"] for row in counts if row["category"] == key.value) for key in Category}
        total = sum(category_counts.values())
        pages = max(1, ceil(len(matching) / 12))
        page = min(page, pages)
        configured = {cfg.name: cfg for cfg in settings.sources}
        sources = []
        for name, cls in registry.items():
            cfg, run = configured.get(name), latest[name]
            if cfg is None:
                continue
            sources.append({
                "name": name, "label": LABELS[name], "category": cls.category.value,
                "enabled": bool(cfg and cfg.enabled), "note": NOTES[name],
                "count": sum(row["active"] for row in counts if row["source"] == name),
                "last_status": run["status"] if run else "never",
                "last_run": (run["finished_at"] or run["started_at"])[:16].replace("T", " ") + " UTC" if run else "Not yet",
                "error": run["error"] if run else None,
            })
        last_seen = max((row["last_seen"] for row in counts), default=None)
        stats = {
            "total": total, "new_today": new_today,
            "active_sources": sum(cfg.enabled for cfg in settings.sources),
            "last_seen": last_seen[:16].replace("T", " ") + " UTC" if last_seen else "Not yet",
        }
        navigation = [{"id": key, "label": label, "count": category_counts.get(key, total), "url": url(key, source, q), "active": key == category}
                      for key, label in [("", "All items"), ("books", "Books"), ("marketplace", "Marketplace"), ("news", "News"), ("jobs", "Jobs")]]
        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "stats": stats, "nav_categories": navigation, "items": matching[(page - 1) * 12:page * 12],
            "count": len(matching), "page": page, "pages": pages,
            "prev_url": url(category, source, q, page - 1) if page > 1 else None,
            "next_url": url(category, source, q, page + 1) if page < pages else None,
            "selected_category": category, "selected_source": source, "q": q,
            "sources": sources, "runs": runs,
            "csv_url": url(category, source, q, path="/export/csv"),
            "json_url": url(category, source, q, path="/export/json"),
        })

    @app.get("/export/{format}")
    def export(
        format: str,
        category: str = Query("", pattern="^(|books|marketplace|news|jobs)$"),
        source: str = Query("", max_length=64),
        q: str = Query("", max_length=200),
    ):
        validate_source(source)
        if format not in {"csv", "json"}:
            raise HTTPException(404, "Choose csv or json")
        with Store(db_path) as store:
            rows = [row_to_dict(row) for row in store.query(category=category, source=source, search=q.strip(), limit=None, sources=visible_sources)]
        content = csv_text(rows) if format == "csv" else json_text(rows)
        return Response(content, media_type="text/csv" if format == "csv" else "application/json",
                        headers={"Content-Disposition": f'attachment; filename="cyprus.{format}"'})

    @app.get("/digest", response_class=HTMLResponse)
    def digest(hours: int = Query(24, ge=1, le=720)):
        with Store(db_path) as store:
            html, _, _ = build_digest(store, hours, sources=visible_sources)
        return HTMLResponse(html)

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "local"}

    return app
