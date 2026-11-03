"""Phase 1 helper: verify the bot token, register your chat ID, send a test message.

Usage:
    python -m scripts.telegram_setup check      # verify TELEGRAM_BOT_TOKEN works
    python -m scripts.telegram_setup register   # send /start to your bot first, then run this
    python -m scripts.telegram_setup test       # send a test message to TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import html
import logging
import sys

from config.settings import ConfigError, load_settings
from notifications.telegram_bot import TelegramClient, TelegramError, next_offset, parse_commands

log = logging.getLogger("telegram_setup")


def cmd_check(client: TelegramClient) -> int:
    me = client.get_me()
    print(f"Token OK. Bot: @{me.get('username')} (id {me.get('id')})")
    return 0


def cmd_register(client: TelegramClient) -> int:
    updates = client.get_updates(long_poll_seconds=5)
    starts = [c for c in parse_commands(updates) if c.command == "start" and c.chat_type == "private"]
    if not starts:
        print("No /start found. Open your bot in Telegram, send /start, then run this again.")
        return 1

    latest = starts[-1]
    client.send_message(
        latest.chat_id,
        "✅ <b>AI Job Radar</b> registered this chat.\n"
        f"Chat ID: <code>{latest.chat_id}</code>",
    )
    # Acknowledge processed updates so they are not returned again.
    client.get_updates(offset=next_offset(updates))

    who = f"@{latest.username}" if latest.username else "unknown user"
    print(f"Found /start from {who}.")
    print(f"Add this line to your .env (and later to GitHub Secrets):\n\nTELEGRAM_CHAT_ID={latest.chat_id}\n")
    if len({c.chat_id for c in starts}) > 1:
        print("Warning: /start came from more than one chat. Make sure the ID above is yours.")
    return 0


def cmd_test(client: TelegramClient, chat_id: int) -> int:
    client.send_message(
        chat_id,
        "🧪 <b>AI Job Radar test message</b>\n"
        f"Telegram integration is working. {html.escape('<HTML escaping works too>')}",
    )
    print(f"Test message sent to chat {chat_id}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["check", "register", "test"])
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        with TelegramClient(settings.require_token()) as client:
            if args.action == "check":
                return cmd_check(client)
            if args.action == "register":
                return cmd_register(client)
            return cmd_test(client, settings.require_chat_id())
    except (ConfigError, TelegramError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
