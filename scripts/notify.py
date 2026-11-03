"""Send pending Telegram alerts / digest and answer commands from stored jobs, without collecting.

    python -m scripts.notify --dry-run      # show what would be sent, send nothing
    python -m scripts.notify                # answer commands, send pending alerts, digest if due
    python -m scripts.notify --digest-now   # also send today's digest regardless of the hour
"""

from __future__ import annotations

import argparse
import logging
import sys

from config.settings import ConfigError, load_settings
from core.models import utcnow
from core.preferences import load_preferences
from database.repository import JobRepository, init_db
from notifications.formatter import format_alert
from notifications.notifier import TelegramNotifier, to_view
from notifications.telegram_bot import TelegramClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--digest-now", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = load_settings()
    prefs = load_preferences()
    factory = init_db(settings.database_url)
    try:
        chat_id = settings.require_chat_id()
        token = settings.require_token()
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        with factory() as session:
            jobs = JobRepository(session).alert_candidates(prefs, str(chat_id), utcnow())
            print(f"{len(jobs)} alert(s) would be sent:")
            for job in jobs:
                print(f"  {job.relevance_score:.0f}  {job.title} | {job.company.name} | {job.verification_status}")
            if jobs:
                print("\n--- first message ---\n" + format_alert(to_view(jobs[0])))
        return 0

    if args.digest_now:
        prefs = type(prefs).model_validate(
            {**prefs.model_dump(), "notifications": {**prefs.notifications.model_dump(), "digest_hour": 0}})
    with TelegramClient(token) as client:
        stats = TelegramNotifier(client, chat_id).run(factory, prefs, utcnow())
    print(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
