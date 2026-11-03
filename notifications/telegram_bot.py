"""Minimal client for the official Telegram Bot API (https://core.telegram.org/bots/api).

Only the methods this project needs are implemented. The bot token is part of
every request URL, so it is redacted from all errors and httpx request logging
is silenced below WARNING.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096  # Telegram limit, counted after entity parsing


class TelegramError(RuntimeError):
    """A Telegram API call failed. Messages never contain the bot token."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class IncomingCommand:
    update_id: int
    chat_id: int
    chat_type: str
    command: str  # lowercase, without leading slash or @botname suffix
    args: str
    username: str | None


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        sleep=time.sleep,
    ):
        if not token or ":" not in token:
            raise ValueError("Telegram bot token looks malformed (expected '<id>:<secret>').")
        self._token = token
        self._http = http_client or httpx.Client(timeout=timeout)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<redacted>")

    def _call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        url = f"{API_BASE}/bot{self._token}/{method}"
        last_error: TelegramError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.post(url, json=payload or {}, timeout=timeout or httpx.USE_CLIENT_DEFAULT)
                last_error = self._parse_error(method, response)
                if last_error is None:
                    return response.json()["result"]
            except httpx.HTTPError as exc:
                last_error = TelegramError(
                    f"{method}: network error: {self._redact(str(exc))}", retryable=True
                )

            if not last_error.retryable or attempt == self._max_retries:
                break
            delay = getattr(last_error, "retry_after", None) or self._backoff_base * (2**attempt)
            logger.warning("Telegram %s failed (%s); retrying in %.1fs", method, last_error, delay)
            self._sleep(delay)

        assert last_error is not None
        raise last_error

    def _parse_error(self, method: str, response: httpx.Response) -> TelegramError | None:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200 and body.get("ok"):
            return None

        description = self._redact(str(body.get("description") or response.text[:200]))
        status = response.status_code
        error = TelegramError(
            f"{method}: HTTP {status}: {description}",
            status_code=status,
            retryable=status == 429 or status >= 500,
        )
        retry_after = (body.get("parameters") or {}).get("retry_after")
        if retry_after:
            error.retry_after = float(retry_after)  # type: ignore[attr-defined]
        return error

    # --- API methods -----------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        disable_preview: bool = True,
    ) -> dict[str, Any]:
        if len(text) > MAX_MESSAGE_LENGTH:
            raise ValueError(f"Message is {len(text)} chars; Telegram allows {MAX_MESSAGE_LENGTH}.")
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": disable_preview},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._call("sendMessage", payload)

    def get_updates(self, offset: int | None = None, *, long_poll_seconds: int = 0) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": long_poll_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=long_poll_seconds + 10)


def parse_commands(updates: list[dict[str, Any]]) -> list[IncomingCommand]:
    """Extract bot commands (e.g. '/start', '/latest@MyBot 5') from raw updates."""
    commands: list[IncomingCommand] = []
    for update in updates:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        if not text.startswith("/") or "id" not in chat:
            continue
        head, _, args = text.partition(" ")
        command = head[1:].split("@", 1)[0].lower()
        if not command:
            continue
        commands.append(
            IncomingCommand(
                update_id=update["update_id"],
                chat_id=int(chat["id"]),
                chat_type=chat.get("type", ""),
                command=command,
                args=args.strip(),
                username=(message.get("from") or {}).get("username"),
            )
        )
    return commands


def next_offset(updates: list[dict[str, Any]]) -> int | None:
    """Offset that acknowledges all given updates so Telegram stops resending them."""
    if not updates:
        return None
    return max(u["update_id"] for u in updates) + 1
