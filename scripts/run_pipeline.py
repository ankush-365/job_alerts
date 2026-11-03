"""Run one full pipeline pass (what the scheduler calls).

    python -m scripts.run_pipeline
"""

from __future__ import annotations

import json
import logging
import sys

from collectors.registry import load_sources_config
from config.settings import load_settings
from core.pipeline import run_pipeline
from core.preferences import load_preferences
from database.repository import init_db
from notifications.notifier import TelegramNotifier
from notifications.telegram_bot import TelegramClient

logger = logging.getLogger("run_pipeline")


def make_notifier(settings) -> TelegramNotifier | None:
    if settings.telegram_bot_token is None or settings.telegram_chat_id is None:
        logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: jobs are collected but no alerts are sent")
        return None
    return TelegramNotifier(TelegramClient(settings.require_token()), settings.require_chat_id())


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    session_factory = init_db(settings.database_url)
    notifier = make_notifier(settings)
    try:
        summary = run_pipeline(session_factory, load_sources_config(), preferences=load_preferences(),
                               notifier=notifier)
    finally:
        if notifier is not None:
            notifier.client.close()
    print(json.dumps({"run_id": summary.run_id, "status": summary.status,
                      "new_jobs": len(summary.new_job_ids), "sources": summary.stats}, indent=2))
    for error in summary.errors[:20]:
        print(f"! {error}", file=sys.stderr)
    # Exit non-zero only if every source failed, so partial outages don't mark the scheduled run red.
    return 1 if summary.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
