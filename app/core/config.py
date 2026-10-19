from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.cache.keys import DEFAULT_KEY_PREFIX, validate_key_prefix
from app.db.config import validate_database_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
        hide_input_in_errors=True,
    )

    llm_mode: Literal["mock", "openai"] = "mock"
    agent_engine: Literal["legacy", "langgraph"] = "legacy"
    workflow_enabled: bool = False
    workflow_local_demo: bool = False
    workflow_api_token: SecretStr = SecretStr("")
    checkpoint_database_url: SecretStr = SecretStr("")
    workflow_retention_days: int = Field(default=7, ge=1, le=90)
    max_steps: int = Field(default=6, ge=1, le=100)
    llm_timeout_seconds: float = Field(default=30, gt=0, le=300)
    tool_timeout_seconds: float = Field(default=10, gt=0, le=300)
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = ""
    openai_model: str = ""
    session_enabled: bool = False
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    session_ttl_seconds: int = Field(default=1800, ge=1, le=604800)
    session_max_turns: int = Field(default=10, ge=1, le=100)
    session_max_message_chars: int = Field(default=16000, ge=4000, le=32000)
    session_max_bytes: int = Field(default=262144, ge=1024, le=1048576)
    incident_persistence_enabled: bool = False
    database_url: SecretStr = SecretStr("")
    cache_enabled: bool = False
    cache_ttl_seconds: int = Field(default=300, ge=1, le=604800)
    cache_key_prefix: str = DEFAULT_KEY_PREFIX
    cache_max_bytes: int = Field(default=65536, ge=1, le=1048576)
    cache_io_timeout_seconds: float = Field(default=0.5, gt=0, le=10)

    @model_validator(mode="after")
    def validate_provider(self) -> Self:
        if self.agent_engine == "langgraph":
            self.workflow_enabled = True
        if self.workflow_enabled:
            if not self.workflow_local_demo or len(self.workflow_api_token.get_secret_value()) < 32:
                raise ValueError("Workflow requires local demo mode and a token of at least 32 characters")
            validate_database_url(self.database_url.get_secret_value())
            from app.workflow.checkpoint import validate_checkpoint_url
            validate_checkpoint_url(self.checkpoint_database_url.get_secret_value())
        validate_key_prefix(self.cache_key_prefix)
        if self.cache_enabled and not self.incident_persistence_enabled:
            raise ValueError("CACHE_ENABLED requires INCIDENT_PERSISTENCE_ENABLED")
        if self.incident_persistence_enabled:
            validate_database_url(self.database_url.get_secret_value())
        try:
            redis_url = urlsplit(self.redis_url.get_secret_value())
            if (redis_url.scheme not in {"redis", "rediss"} or not redis_url.hostname
                    or redis_url.query or redis_url.fragment
                    or (redis_url.path not in {"", "/"} and not redis_url.path[1:].isdigit())):
                raise ValueError
            if redis_url.port is not None and not 1 <= redis_url.port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError("REDIS_URL must be a redis(s) URL with a numeric database and no query") from None
        if self.llm_mode == "openai":
            if not all((self.openai_api_key.get_secret_value().strip(),
                        self.openai_base_url.strip(), self.openai_model.strip())):
                raise ValueError("openai mode requires OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL")
            url = urlsplit(self.openai_base_url)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.password or url.query or url.fragment):
                raise ValueError("OPENAI_BASE_URL must be an HTTP(S) API root without credentials or query")
        return self
