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

    rag_enabled: bool = False
    rag_top_k: int = Field(default=4, ge=1, le=10)
    rag_max_context_chars: int = Field(default=4000, ge=128, le=12000)
    rag_min_score: float = Field(default=0.35, ge=-1, le=1)
    rag_timeout_seconds: float = Field(default=10, gt=0, le=60)
    rag_runbook_path: str = "runbooks"
    rag_chunk_size: int = Field(default=1000, ge=128, le=8000)
    rag_chunk_overlap: int = Field(default=120, ge=0)
    rag_embedding_provider: Literal["fake", "api"] = "fake"
    rag_embedding_model: str = "fake-hash-v1"
    rag_embedding_dimensions: int = Field(default=64, ge=8, le=4096)
    rag_embedding_base_url: str = ""
    rag_embedding_api_key: SecretStr = SecretStr("")
    mcp_enabled: bool = False
    mcp_server_url: str = "http://127.0.0.1:8765/mcp"
    mcp_timeout_seconds: float = Field(default=5, gt=0, le=30)
    metrics_enabled: bool = False
    metrics_token: SecretStr = SecretStr("")
    metrics_allowed_cidrs: list[str] = ["127.0.0.0/8", "::1/128"]
    tracing_enabled: bool = False
    otel_service_name: str = "opspilot"
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:4318/v1/traces"
    trace_sample_ratio: float = Field(default=1.0, ge=0, le=1)

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
        if (self.rag_enabled or self.mcp_enabled) and not self.workflow_enabled:
            raise ValueError("RAG/MCP requires WORKFLOW_ENABLED and the Workflow API")
        if self.rag_enabled:
            validate_database_url(self.database_url.get_secret_value())
        if self.rag_chunk_overlap >= self.rag_chunk_size // 2:
            raise ValueError("RAG overlap must be less than half chunk size")
        if self.rag_embedding_provider == "api":
            url = urlsplit(self.rag_embedding_base_url)
            if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                    or url.password or url.query or url.fragment or not self.rag_embedding_model
                    or not self.rag_embedding_api_key.get_secret_value()):
                raise ValueError("Embedding API requires an explicit URL, model and key")
        if self.rag_embedding_provider == "fake" and self.rag_embedding_model != "fake-hash-v1":
            raise ValueError("Fake embeddings must use fake-hash-v1")
        if self.mcp_enabled:
            from app.mcp.client import validate_server_url
            validate_server_url(self.mcp_server_url)
        import ipaddress
        for cidr in self.metrics_allowed_cidrs:
            ipaddress.ip_network(cidr, strict=True)
        if self.metrics_token.get_secret_value() and len(self.metrics_token.get_secret_value()) < 32:
            raise ValueError("Metrics token must contain at least 32 characters")
        if self.tracing_enabled:
            url = urlsplit(self.otel_exporter_otlp_endpoint)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query:
                raise ValueError("OTLP requires an explicit HTTP endpoint without credentials")
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
