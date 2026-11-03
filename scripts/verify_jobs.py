"""Verify stored jobs (VERIFIED / NEEDS_REVIEW / SUSPICIOUS) and show the evidence.

    python -m scripts.verify_jobs                 # offline: uses stored data only
    python -m scripts.verify_jobs --discover 5    # also look up the job boards of up to 5 YC companies
                                                  # (small live requests) and collect only those boards
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from collectors.ats_collector import AtsCollector
from collectors.registry import load_sources_config, make_fetcher
from config.settings import load_settings
from core.models import utcnow
from core.pipeline import _discover_yc_boards, make_matcher, run_pipeline
from core.preferences import load_preferences
from database.models import Job
from database.repository import JobRepository, init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--discover", type=int, default=0, metavar="N", help="check up to N YC companies' boards")
    parser.add_argument("--show", type=int, default=5, help="examples to print per status")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    prefs = load_preferences()
    factory = init_db(load_settings().database_url)

    if args.discover:
        config = load_sources_config()
        fetcher = make_fetcher(config)
        try:
            ats = dict(config.get("ats") or {}, max_discoveries_per_run=args.discover)
            print("discovery:", _discover_yc_boards(factory, fetcher, ats))
            with factory() as session:
                boards = JobRepository(session).discovered_boards()
            if boards:
                summary = run_pipeline(factory, {}, fetcher=fetcher, collectors=[AtsCollector(fetcher, boards)],
                                       preferences=prefs, matcher=make_matcher(prefs))
                print("collected discovered boards:", summary.stats.get("ats"))
        finally:
            fetcher.close()

    with factory.begin() as session:
        repo = JobRepository(session)
        now = utcnow()
        repo.refresh_board_identities(now)
        counts = repo.verify_active_jobs(now)
    print("verification:", counts)

    with factory() as session:
        for status in ("SUSPICIOUS", "NEEDS_REVIEW", "VERIFIED"):
            jobs = session.scalars(
                select(Job).where(Job.verification_status == status)
                .order_by(Job.relevance_score.desc().nulls_last()).limit(args.show)
            ).all()
            if jobs:
                print(f"\n== {status}")
            for job in jobs:
                info = job.verification_evidence or {}
                print(f"  {job.relevance_score or '-':>3} {job.title} | {job.company.name} | {job.filter_status}")
                print(f"      {info.get('summary')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
