import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_default_settings_need_no_api_key() -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_mode == "mock" and settings.max_steps == 6


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_STEPS", "3")
    assert Settings(_env_file=None).max_steps == 3


def test_openai_requires_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_mode="openai")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:password@example.com", "https://example.com/?token=x"])
def test_provider_url_rejects_credentials_and_wrong_scheme(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_mode="openai", openai_api_key="fake", openai_model="test", openai_base_url=url)


@pytest.mark.parametrize("steps", [0, -1, 101])
def test_invalid_step_limits(steps: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_steps=steps)
