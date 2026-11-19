from __future__ import annotations

import pytest
import yaml

import cyprus.config as config_module
from cyprus.config import load_settings


@pytest.fixture(autouse=True)
def isolate_dotenv(monkeypatch):
    # Explicit temporary config files must not read or mutate the user's environment.
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture
def write_config(tmp_path):
    def write(document):
        path = tmp_path / "sources.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return path

    return write


def test_missing_config_file_raises_actionable_value_error(tmp_path):
    missing = tmp_path / "missing.yaml"
    with pytest.raises(ValueError, match="not found"):
        load_settings(missing)
    assert not missing.exists()


def test_malformed_yaml_raises_value_error(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("sources: [unterminated\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML"):
        load_settings(path)


@pytest.mark.parametrize(
    "document",
    [
        None, "scalar", [], {},
        {"sources": None}, {"sources": "feed"}, {"sources": []},
        {"sources": {}, "fetch": None}, {"sources": {}, "fetch": "fast"},
        {"sources": {}, "fetch": []},
        {"sources": {"feed": None}}, {"sources": {"feed": True}},
        {"sources": {"feed": "enabled"}}, {"sources": {"feed": []}},
        {"sources": {42: {}}},
    ],
)
def test_config_requires_mappings_and_string_source_names(write_config, document):
    with pytest.raises(ValueError):
        load_settings(write_config(document))


@pytest.mark.parametrize("delay", [-1, 60.1, True, "1", None, [], float("nan"), float("inf")])
def test_invalid_fetch_delay_is_rejected(write_config, delay):
    path = write_config({"sources": {}, "fetch": {"delay": delay}})
    with pytest.raises(ValueError, match="delay"):
        load_settings(path)


@pytest.mark.parametrize("retries", [0, -1, 6, True, 2.5, "3", None])
def test_invalid_retry_budget_is_rejected(write_config, retries):
    path = write_config({"sources": {}, "fetch": {"retries": retries}})
    with pytest.raises(ValueError, match="retries"):
        load_settings(path)


@pytest.mark.parametrize("respect_robots", [False, "false", "true", 0, 1, None])
def test_robots_checks_cannot_be_disabled_or_coerced(write_config, respect_robots):
    path = write_config({"sources": {}, "fetch": {"respect_robots": respect_robots}})
    with pytest.raises(ValueError, match="robots"):
        load_settings(path)


@pytest.mark.parametrize("enabled", ["true", "false", 0, 1, None])
def test_source_enabled_requires_a_real_boolean(write_config, enabled):
    path = write_config({"sources": {"feed": {"enabled": enabled}}})
    with pytest.raises(ValueError):
        load_settings(path)


@pytest.mark.parametrize("options", [None, "pages=2", [], True, 1])
def test_source_options_must_be_a_mapping(write_config, options):
    path = write_config({"sources": {"feed": {"options": options}}})
    with pytest.raises(ValueError):
        load_settings(path)


@pytest.mark.parametrize("cooldown", [-1, True, 1.5, "60", None, []])
def test_cooldown_requires_a_nonnegative_integer(write_config, cooldown):
    path = write_config({"sources": {"feed": {"options": {"cooldown_seconds": cooldown}}}})
    with pytest.raises(ValueError, match="cooldown"):
        load_settings(path)


@pytest.mark.parametrize("track_removals", ["true", "false", 0, 1, None])
def test_removal_option_requires_a_real_boolean(write_config, track_removals):
    path = write_config({"sources": {"feed": {"options": {"track_removals": track_removals}}}})
    with pytest.raises(ValueError, match="track_removals"):
        load_settings(path)


def test_minimal_valid_config_uses_safe_defaults(write_config):
    settings = load_settings(write_config({"sources": {"hackernews": {}}}))
    assert settings.delay == 1.0
    assert settings.retries == 3
    assert len(settings.sources) == 1
    source = settings.sources[0]
    assert source.name == "hackernews"
    assert source.enabled is True
    assert source.options == {}


@pytest.mark.parametrize("delay, retries", [(0, 1), (0.5, 3), (60, 5)])
def test_valid_config_preserves_enabled_sources_and_options(write_config, delay, retries):
    options = {"cooldown_seconds": 0, "track_removals": True, "pages": 2}
    document = {
        "fetch": {"delay": delay, "retries": retries, "respect_robots": True},
        "sources": {
            "books": {"enabled": True, "options": options},
            "remotive": {"enabled": False, "options": {"cooldown_seconds": 21600, "limit": 25}},
        },
    }
    settings = load_settings(write_config(document))
    assert settings.delay == float(delay)
    assert settings.retries == retries
    sources = {source.name: source for source in settings.sources}
    assert set(sources) == {"books", "remotive"}
    assert sources["books"].enabled is True
    assert sources["books"].options == options
    assert sources["remotive"].enabled is False
    assert sources["remotive"].options == {"cooldown_seconds": 21600, "limit": 25}
