from __future__ import annotations

import logging
import smtplib
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import database_path, load_settings
from .digest import build_digest, send_digest
from .export import csv_text, json_text
from .fetch import Fetcher
from .models import Category
from .runner import run_source
from .sources import available, create
from .store import Store, row_to_dict

app = typer.Typer(no_args_is_help=True, help="Project Cyprus: collect public listings into your local workspace.")
console = Console(markup=False)


class ExportFormat(StrEnum):
    CSV = "csv"
    JSON = "json"


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[Path | None, typer.Option(help="SQLite database path.")] = None,
    config: Annotated[Path | None, typer.Option(help="Source YAML configuration.")] = None,
) -> None:
    default_db = database_path()
    ctx.obj = {"db": db or default_db, "config": config}
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def _settings(ctx: typer.Context):
    try:
        settings = load_settings(ctx.obj["config"])
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    unknown = {source.name for source in settings.sources} - available().keys()
    if unknown:
        raise typer.BadParameter(f"Unknown configured source(s): {', '.join(sorted(unknown))}")
    return settings


@app.command()
def sources(ctx: typer.Context) -> None:
    """List source adapters and whether collection is enabled."""
    settings = _settings(ctx)
    configured = {source.name: source for source in settings.sources}
    table = Table("Source", "Category", "Enabled", "Description", "Minimum interval")
    for name, cls in available().items():
        cfg = configured.get(name)
        if cfg is None:
            continue
        interval = max(cls.minimum_interval, cfg.options.get("cooldown_seconds", 0))
        table.add_row(name, cls.category.value, "yes" if cfg.enabled else "no", cls.description, f"{interval}s")
    console.print(table)
    console.print("Real sources only in the default collection. LinkedIn and Upwork require authorized integrations; they are not being scraped.")


@app.command()
def run(ctx: typer.Context, source: Annotated[str, typer.Argument(help="A source name, or all.")] = "all") -> None:
    """Collect enabled sources once, respecting robots policies and cooldowns."""
    settings = _settings(ctx)
    selected = [cfg for cfg in settings.sources if cfg.enabled and (source == "all" or cfg.name == source)]
    if not selected:
        raise typer.BadParameter(f"Source '{source}' is unknown, disabled, or no sources are enabled. Run cyprus sources.")
    failed = False
    with Store(ctx.obj["db"]) as store, Fetcher(delay=settings.delay, retries=settings.retries) as fetcher:
        for cfg in selected:
            console.print(f"Collecting {cfg.name} (source crawl delays may apply)...")
            result = run_source(create(cfg.name, cfg.options), store, fetcher)
            if result.skipped:
                console.print(f"{cfg.name}: skipped — {result.skipped}")
            elif result.error:
                console.print(f"{cfg.name}: failed — {result.error}")
                failed = True
            else:
                console.print(f"{cfg.name}: {result.fetched} items / {result.new} new / {result.changed} changed / {result.unchanged} unchanged")
    if failed:
        raise typer.Exit(1)


@app.command()
def watch(
    ctx: typer.Context,
    interval: Annotated[int, typer.Option(min=60, max=86400, help="Seconds between collection checks; individual source cooldowns still apply.")] = 300,
    cycles: Annotated[int | None, typer.Option(min=1, help="Stop after this many checks; omit to run until Ctrl+C.")] = None,
) -> None:
    """Keep enabled sources refreshed while this terminal is running; never sends email."""
    count = 0
    try:
        while cycles is None or count < cycles:
            try:
                run(ctx, "all")
            except typer.Exit as exc:
                if exc.exit_code != 1:
                    raise
                console.print("Some sources failed; later checks will retry after their cooldowns.")
            count += 1
            if cycles is None or count < cycles:
                console.print(f"Next collection check in {interval}s. Ctrl+C stops the watcher.")
                time.sleep(interval)
    except KeyboardInterrupt:
        console.print("Collection watcher stopped.")


@app.command("list")
def list_items(
    ctx: typer.Context,
    category: Category | None = None,
    source: str | None = None,
    search: str | None = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 20,
) -> None:
    """Browse stored listings without making network requests."""
    with Store(ctx.obj["db"]) as store:
        rows = store.query(category=category, source=source, search=search, limit=limit,
                           sources=[cfg.name for cfg in _settings(ctx).sources if cfg.enabled])
    table = Table("ID", "Category", "Title", "Source", "Price", "Link")
    for row in rows:
        price = f"{row['currency'] or ''} {row['price']:.2f}" if row["price"] is not None else "—"
        table.add_row(row["item_id"][:10], row["category"], row["title"], row["source"], price, row["url"])
    console.print(table)
    console.print(f"{len(rows)} listings. Use cyprus serve for the dashboard.")


@app.command()
def export(
    ctx: typer.Context,
    format: Annotated[ExportFormat, typer.Argument()] = ExportFormat.CSV,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="New output file; omit for stdout.")] = None,
    category: Category | None = None,
    source: str | None = None,
    search: str | None = None,
) -> None:
    """Export all matching listings as CSV or JSON (CSV formulas are escaped)."""
    with Store(ctx.obj["db"]) as store:
        rows = [row_to_dict(row) for row in store.query(category=category, source=source, search=search, limit=None,
                sources=[cfg.name for cfg in _settings(ctx).sources if cfg.enabled])]
    content = csv_text(rows) if format == ExportFormat.CSV else json_text(rows)
    if output:
        try:
            with output.open("x", encoding="utf-8", newline="") as file:
                file.write(content)
        except OSError as exc:
            raise typer.BadParameter(f"Cannot create output: {exc}", param_hint="--output") from exc
        console.print(f"Exported {len(rows)} listings to {output}")
    else:
        typer.echo(content, nl=False)


@app.command()
def history(ctx: typer.Context, item_id: str) -> None:
    """Show recorded changes for a full or unique shortened listing ID."""
    with Store(ctx.obj["db"]) as store:
        matches = [row for row in store.query(active_only=False, limit=None) if row["item_id"].startswith(item_id)]
        if len(matches) != 1:
            raise typer.BadParameter("Provide a unique item ID from cyprus list or cyprus export json.")
        rows = store.history(matches[0]["item_id"])
    table = Table("Seen (UTC)", "Change", "Title", "Price")
    for row in rows:
        table.add_row(row["seen_at"], row["change"], row["title"], str(row["price"]) if row["price"] is not None else "—")
    console.print(table)


@app.command()
def digest(
    ctx: typer.Context,
    hours: Annotated[int, typer.Option(min=1, max=720)] = 24,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    send: Annotated[bool, typer.Option(help="Explicitly send via configured SMTP; otherwise preview only.")] = False,
) -> None:
    """Preview a digest, or explicitly send it using local SMTP configuration."""
    with Store(ctx.obj["db"]) as store:
        html, text, count = build_digest(store, hours, sources=[cfg.name for cfg in _settings(ctx).sources if cfg.enabled])
    if output:
        try:
            with output.open("x", encoding="utf-8") as file:
                file.write(html)
        except OSError as exc:
            raise typer.BadParameter(f"Cannot create preview: {exc}", param_hint="--output") from exc
        console.print(f"Digest preview: {output}")
    if send:
        if not count:
            console.print("No listing updates; no email sent.")
            return
        try:
            send_digest(html, text, count)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        except (smtplib.SMTPException, OSError) as exc:
            console.print(f"Email delivery failed ({type(exc).__name__}); check your SMTP configuration.")
            raise typer.Exit(1) from exc
        console.print("Digest sent to the configured recipients.")
    elif not output:
        typer.echo(text)
        console.print("Preview only. No email sent. Use cyprus serve and open /digest for the HTML preview.")


@app.command()
def serve(ctx: typer.Context, port: Annotated[int, typer.Option(min=1024, max=65535)] = 8000) -> None:
    """Start the read-only dashboard on 127.0.0.1; collection stays in the CLI."""
    import uvicorn
    from .web.app import create_app

    _settings(ctx)
    web = create_app(ctx.obj["db"], ctx.obj["config"])
    console.print(f"Local dashboard: http://127.0.0.1:{port}")
    uvicorn.run(web, host="127.0.0.1", port=port, access_log=False)
