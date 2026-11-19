from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(slots=True)
class SourceConfig:
    name: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Settings:
    sources: list[SourceConfig]
    delay: float = 1.0
    retries: int = 3


def database_path() -> Path:
    load_dotenv(Path.cwd() / ".env")
    return Path(os.environ.get("CYPRUS_DB", "data/cyprus.db"))


def load_settings(path: str | Path | None = None) -> Settings:
    load_dotenv(Path.cwd() / ".env")
    path = Path(path or os.environ.get("CYPRUS_CONFIG", "config/sources.yaml"))
    if not path.is_file():
        raise ValueError(f"Source configuration not found: {path}. Run from the project directory or set CYPRUS_CONFIG.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML in source configuration") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), dict):
        raise ValueError("Configuration must contain a sources mapping")
    fetch = raw.get("fetch", {})
    if not isinstance(fetch, dict):
        raise ValueError("fetch must be a mapping")
    delay, retries = fetch.get("delay", 1.0), fetch.get("retries", 3)
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not 0 <= delay <= 60:
        raise ValueError("fetch.delay must be between 0 and 60 seconds")
    if type(retries) is not int or not 1 <= retries <= 5:
        raise ValueError("fetch.retries must be an integer between 1 and 5")
    if fetch.get("respect_robots", True) is not True:
        raise ValueError("robots.txt checks cannot be disabled")
    sources = []
    for name, cfg in raw["sources"].items():
        if not isinstance(name, str) or not isinstance(cfg, dict):
            raise ValueError("Each source must have a string name and a settings mapping")
        enabled, options = cfg.get("enabled", True), cfg.get("options", {})
        if type(enabled) is not bool or not isinstance(options, dict):
            raise ValueError(f"Invalid enabled/options settings for {name}")
        cooldown = options.get("cooldown_seconds", 0)
        if type(cooldown) is not int or cooldown < 0:
            raise ValueError(f"{name}: cooldown_seconds must be a non-negative integer")
        if type(options.get("track_removals", False)) is not bool:
            raise ValueError(f"{name}: track_removals must be true or false")
        sources.append(SourceConfig(name=name, enabled=enabled, options=options))
    return Settings(sources=sources, delay=float(delay), retries=retries)
