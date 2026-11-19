from __future__ import annotations

import os
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import now_utc
from .store import Store

TEMPLATES = Path(__file__).parent / "web" / "templates"


def build_digest(store: Store, hours: int = 24, sources: list[str] | None = None) -> tuple[str, str, int]:
    if not 1 <= hours <= 720:
        raise ValueError("Digest hours must be between 1 and 720")
    since = now_utc() - timedelta(hours=hours)
    latest = {}
    for row in store.changes_since(since):
        if sources is None or row["source"] in sources:
            latest.setdefault(row["item_id"], dict(row))
    entries = list(latest.values())[:100]
    environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    html = environment.get_template("digest.html").render(
        entries=entries, hours=hours, total=len(latest), since=since.strftime("%d %b %Y, %H:%M UTC"),
    )
    lines = [f"Project Cyprus: {len(latest)} listing updates in the last {hours} hours", ""]
    for entry in entries:
        lines.extend([f"[{entry['change']}] {entry['current_title']} ({entry['source']})", entry["url"], ""])
    if len(latest) > len(entries):
        lines.append("Showing the latest 100 listings. Open the local dashboard for the full collection.")
    return html, "\n".join(lines), len(latest)


def send_digest(html: str, text: str, count: int) -> None:
    required = ["SMTP_HOST", "SMTP_FROM", "SMTP_TO"]
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    if missing:
        raise ValueError(f"Email is not configured. Set: {', '.join(missing)}")
    host, sender, recipients = (os.environ[key].strip() for key in required)
    if any(char in host + sender + recipients for char in "\r\n"):
        raise ValueError("SMTP settings cannot contain line breaks")
    addresses = [address.strip() for address in recipients.split(",")]
    for address in [sender, *addresses]:
        parsed = parseaddr(address)[1]
        if not parsed or parsed.count("@") != 1 or not all(parsed.split("@")):
            raise ValueError("SMTP_FROM and SMTP_TO must contain valid email addresses")
    security = os.environ.get("SMTP_SECURITY", "starttls")
    if security not in {"starttls", "ssl"}:
        raise ValueError("SMTP_SECURITY must be starttls or ssl")
    try:
        port = int(os.environ.get("SMTP_PORT", "465" if security == "ssl" else "587"))
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535")
    username, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if bool(username) != bool(password):
        raise ValueError("Set both SMTP_USER and SMTP_PASSWORD for authenticated email")
    message = EmailMessage()
    message["Subject"] = f"Project Cyprus | {count} listing updates"
    message["From"], message["To"] = sender, ", ".join(addresses)
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=30)
    with client:
        if security == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if username:
            client.login(username, password)
        client.send_message(message)
