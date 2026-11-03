"""Inspect stored jobs. Read-only.

    python -m scripts.show_jobs                      # jobs that passed the filters, best score first
    python -m scripts.show_jobs --explain            # with score explanations
    python -m scripts.show_jobs --status rejected --limit 30
    python -m scripts.show_jobs --reasons            # why jobs were rejected, counted
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from sqlalchemy import select

from config.settings import load_settings
from database.models import Job
from database.repository import init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", default="passed", choices=["passed", "review", "rejected"])
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--reasons", action="store_true", help="count rejection reasons")
    parser.add_argument("--explain", action="store_true", help="show score explanations")
    args = parser.parse_args(argv)

    with init_db(load_settings().database_url)() as session:
        if args.reasons:
            counts = Counter()
            for details in session.scalars(select(Job.filter_details).where(Job.filter_status == "rejected")):
                reason = (details or {}).get("reasons", ["?"])[0]
                counts[reason if reason.startswith("experience") else reason.split("(")[0].strip()] += 1
            for reason, n in counts.most_common(40):
                print(f"{n:5d}  {reason}")
            return 0

        jobs = session.scalars(
            select(Job).where(Job.filter_status == args.status)
            .order_by(Job.relevance_score.desc().nulls_last(), Job.first_seen_at.desc()).limit(args.limit)
        ).all()
        for job in jobs:
            details = job.filter_details or {}
            sources = ", ".join(sorted(f"{s.source}{'' if s.is_primary else '*'}" for s in job.sources))
            score = f"{job.relevance_score:3.0f} " if job.relevance_score is not None else "  - "
            print(f"{score}#{job.id} {job.title} | {job.company.name} | {job.location} | exp={details.get('required_years')}"
                  f" | {job.salary_text or ''} | [{sources}]")
            if args.explain and job.score_details:
                d = job.score_details
                print(f"     {d['reason']}\n     skills: {', '.join(d['matched_skills']) or '-'}"
                      f" | missing: {', '.join(d['missing_skills']) or '-'} | sim={d['semantic_similarity']}"
                      f" | adj={d['adjustments'] or '-'}")
            notes = {"rejected": details.get("reasons"), "review": details.get("review_reasons")}.get(
                args.status, details.get("flags"))
            if notes:
                print(f"     {'; '.join(notes)}")
        print(f"\n{len(jobs)} {args.status} jobs shown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
