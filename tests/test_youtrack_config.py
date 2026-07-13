from __future__ import annotations

import pytest
from ironrag_connector.config import RunMode
from pydantic import ValidationError

from youtrack_connector.config import YouTrackSettings


def _settings(**overrides: object) -> YouTrackSettings:
    data: dict[str, object] = {
        "youtrack_base_url": "https://tracker.example.com/youtrack/",
        "youtrack_token": "perm:synthetic-token",
        "ironrag_base_url": "https://ironrag.example.com",
        "ironrag_api_token": "synthetic-ironrag-token",
        "admin_bearer_token": "synthetic-admin-token",
    }
    data.update(overrides)
    return YouTrackSettings(**data)


def test_settings_normalize_base_url_and_default_to_polling() -> None:
    settings = _settings()

    assert settings.youtrack_base_url == "https://tracker.example.com/youtrack"
    assert settings.run_mode is RunMode.POLL
    assert settings.youtrack_snapshot_validation_passes == 2


def test_project_filters_are_trimmed_deduplicated_and_ordered() -> None:
    settings = _settings(
        youtrack_projects=" DOCS,0-7,DOCS, ",
        youtrack_exclude_projects="ARCHIVE, 0-9,ARCHIVE",
    )

    assert settings.included_projects() == ("DOCS", "0-7")
    assert settings.excluded_projects() == ("ARCHIVE", "0-9")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://tracker.example.com",
        "https://user:password@tracker.example.com",
        "https://tracker.example.com/path?token=value",
    ],
)
def test_settings_reject_unsafe_base_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        _settings(youtrack_base_url=url)


def test_token_is_redacted_from_settings_repr() -> None:
    settings = _settings(youtrack_token="perm:must-not-leak")

    assert "must-not-leak" not in repr(settings)


def test_single_snapshot_validation_pass_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(youtrack_snapshot_validation_passes=1)
