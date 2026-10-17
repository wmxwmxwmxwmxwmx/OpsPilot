from typing import Self
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError as SQLAlchemyArgumentError


def validate_database_url(value: str) -> None:
    try:
        url = make_url(value)
        if (url.drivername != "postgresql+asyncpg" or not url.host or not url.database
                or urlsplit(value).fragment or (url.port is not None and not 1 <= url.port <= 65535)):
            raise ValueError
    except (ValueError, SQLAlchemyArgumentError):
        raise ValueError("Database URL must use postgresql+asyncpg with host and database") from None


class MigrationSettings(BaseSettings):
    """Independent DDL credentials; never inherit runtime/provider configuration."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    migration_database_url: SecretStr

    @model_validator(mode="after")
    def validate_url(self) -> Self:
        validate_database_url(self.migration_database_url.get_secret_value())
        return self
