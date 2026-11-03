"""Runtime settings loaded from environment variables (and a local .env file).

Secrets are never hardcoded. Locally they come from `.env`; in GitHub Actions
they come from repository secrets exposed as environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseModel):
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'job_radar.db'}"

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def _blank_chat_id_is_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def require_token(self) -> str:
        if self.telegram_bot_token is None or not self.telegram_bot_token.get_secret_value().strip():
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN is not set. Add it to .env locally or to GitHub Secrets."
            )
        return self.telegram_bot_token.get_secret_value().strip()

    def require_chat_id(self) -> int:
        if self.telegram_chat_id is None:
            raise ConfigError(
                "TELEGRAM_CHAT_ID is not set. Run `python -m scripts.telegram_setup register` first."
            )
        return self.telegram_chat_id


def load_settings(env_file: Path | None = PROJECT_ROOT / ".env") -> Settings:
    """Load settings; real environment variables take precedence over .env."""
    if env_file is not None and env_file.exists():
        load_dotenv(env_file, override=False)
    return Settings(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        **({"database_url": os.environ["DATABASE_URL"]} if os.environ.get("DATABASE_URL") else {}),
    )
