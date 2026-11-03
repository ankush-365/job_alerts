import json

import httpx
import pytest

from config.settings import ConfigError, Settings, load_settings
from notifications.telegram_bot import (
    MAX_MESSAGE_LENGTH,
    TelegramClient,
    TelegramError,
    next_offset,
    parse_commands,
)

TOKEN = "123456:TEST-secret-token"


def make_client(handler, **kwargs):
    sleeps = []
    client = TelegramClient(
        TOKEN,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        **kwargs,
    )
    return client, sleeps


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def test_send_message_payload():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return ok({"message_id": 1})

    client, _ = make_client(handler)
    client.send_message(42, "<b>hi</b>")
    assert seen["path"] == f"/bot{TOKEN}/sendMessage"
    assert seen["body"]["chat_id"] == 42
    assert seen["body"]["parse_mode"] == "HTML"
    assert seen["body"]["link_preview_options"] == {"is_disabled": True}


def test_rejects_overlong_message():
    client, _ = make_client(lambda r: ok({}))
    with pytest.raises(ValueError):
        client.send_message(1, "x" * (MAX_MESSAGE_LENGTH + 1))


def test_retries_on_429_using_retry_after():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}
            )
        return ok({"message_id": 2})

    client, sleeps = make_client(handler)
    client.send_message(1, "hello")
    assert len(calls) == 2
    assert sleeps == [7.0]


def test_retries_network_errors_with_backoff_then_fails():
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client, sleeps = make_client(handler, max_retries=2)
    with pytest.raises(TelegramError) as exc:
        client.get_me()
    assert sleeps == [1.0, 2.0]
    assert TOKEN not in str(exc.value)


def test_unauthorized_is_not_retried_and_token_redacted():
    def handler(request):
        return httpx.Response(401, json={"ok": False, "description": f"Unauthorized {TOKEN}"})

    client, sleeps = make_client(handler)
    with pytest.raises(TelegramError) as exc:
        client.get_me()
    assert exc.value.status_code == 401
    assert sleeps == []
    assert TOKEN not in str(exc.value)


def test_malformed_token_rejected():
    with pytest.raises(ValueError):
        TelegramClient("not-a-token")


def test_parse_commands_and_offset():
    updates = [
        {"update_id": 10, "message": {"text": "/start", "chat": {"id": 99, "type": "private"},
                                       "from": {"username": "me"}}},
        {"update_id": 11, "message": {"text": "hello", "chat": {"id": 99, "type": "private"}}},
        {"update_id": 12, "message": {"text": "/Latest@RadarBot 5", "chat": {"id": -5, "type": "group"}}},
        {"update_id": 13, "edited_message": {"text": "/start"}},
    ]
    cmds = parse_commands(updates)
    assert [(c.command, c.chat_id, c.args) for c in cmds] == [("start", 99, ""), ("latest", -5, "5")]
    assert cmds[0].username == "me"
    assert next_offset(updates) == 14
    assert next_offset([]) is None


def test_settings_require_values(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    settings = load_settings(env_file=None)
    with pytest.raises(ConfigError):
        settings.require_token()
    with pytest.raises(ConfigError):
        settings.require_chat_id()


def test_settings_secret_not_in_repr():
    settings = Settings(telegram_bot_token=TOKEN, telegram_chat_id="123")
    assert TOKEN not in repr(settings)
    assert settings.require_token() == TOKEN
    assert settings.require_chat_id() == 123
