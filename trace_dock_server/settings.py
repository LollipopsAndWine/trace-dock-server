"""Typed service configuration loaded from environment and the repository .env."""
from functools import lru_cache
import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SERVER_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    trace_dock_dev: bool = False
    token_pepper: SecretStr
    database_url: SecretStr
    http_host: str = Field(default="127.0.0.1", min_length=1)
    http_port: int = Field(default=8080, ge=1, le=65535)
    oss_endpoint: str
    oss_read_endpoint: str | None = None
    oss_bucket: str = Field(min_length=1)
    oss_role_arn: str = Field(min_length=1)
    aliyun_region: str = "cn-hangzhou"
    aliyun_access_key_id: SecretStr = Field(min_length=1)
    aliyun_access_key_secret: SecretStr = Field(min_length=1)
    @model_validator(mode="after")
    def validate_settings(self):
        database_url = self.database_url.get_secret_value()
        if database_url.startswith("sqlite:///"):
            if not self.trace_dock_dev:
                raise ValueError("SQLite requires TRACE_DOCK_DEV=1")
        elif not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must use PostgreSQL or explicitly enabled SQLite")
        if len(self.token_pepper.get_secret_value()) < 32:
            raise ValueError("TOKEN_PEPPER must contain at least 32 characters")
        for name in ("oss_endpoint", "oss_read_endpoint"):
            value = getattr(self, name)
            if value:
                endpoint = urlsplit(value)
                if endpoint.scheme != "https" or not endpoint.hostname:
                    raise ValueError(f"{name.upper()} must be an HTTPS URL")
        return self


@lru_cache
def get_settings() -> Settings:
    env_file = os.getenv("TRACE_DOCK_ENV_FILE")
    if env_file:
        return Settings(_env_file=Path(env_file).expanduser())
    return Settings()
