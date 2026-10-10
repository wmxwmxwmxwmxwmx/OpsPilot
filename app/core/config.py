from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
        hide_input_in_errors=True,
    )

    llm_mode: Literal["mock", "openai"] = "mock"
    max_steps: int = Field(default=6, ge=1, le=100)
    llm_timeout_seconds: float = Field(default=30, gt=0, le=300)
    tool_timeout_seconds: float = Field(default=10, gt=0, le=300)
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = ""
    openai_model: str = ""

    @model_validator(mode="after")
    def validate_provider(self) -> Self:
        if self.llm_mode == "openai":
            if not all((self.openai_api_key.get_secret_value().strip(),
                        self.openai_base_url.strip(), self.openai_model.strip())):
                raise ValueError("openai mode requires OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL")
            url = urlsplit(self.openai_base_url)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.password or url.query or url.fragment):
                raise ValueError("OPENAI_BASE_URL must be an HTTP(S) API root without credentials or query")
        return self
