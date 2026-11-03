"""Run the enabled collectors once against the live sources and print a summary.

Nothing is stored or sent. Usage:
    python -m scripts.collect_preview [--source yc|ats] [--show 10] [--grep "machine learning"]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter

from collectors.registry import build_collectors, load_sources_config, make_fetcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=["yc", "ats"], help="run only this collector")
    parser.add_argument("--show", type=int, default=10, help="sample jobs to print per source")
    parser.add_argument("--grep", help="only show sample jobs whose title matches this regex")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config = load_sources_config()
    fetcher = make_fetcher(config)
    pattern = re.compile(args.grep, re.I) if args.grep else None
    exit_code = 0
    try:
        for collector in build_collectors(config, fetcher):
            if args.source and collector.source != args.source:
                continue
            result = collector.run()
            exit_code |= 0 if result.ok else 1
            jobs = result.jobs
            print(f"\n=== {result.source}: ok={result.ok} jobs={len(jobs)} requests={result.requests_made}")
            for err in result.errors[:10]:
                print(f"  ! {err}")
            print("  by source:", dict(Counter(j.source for j in jobs)))
            print("  with description:", sum(bool(j.description) for j in jobs),
                  "| with posted_at:", sum(j.posted_at is not None for j in jobs),
                  "| with salary:", sum(bool(j.salary_text or j.salary_min) for j in jobs))
            sample = [j for j in jobs if not pattern or pattern.search(j.title)][: args.show]
            for j in sample:
                print(f"  - {j.title} | {j.company_name} | {j.location} | {j.work_arrangement.value}"
                      f" | exp={j.experience_text} | {j.salary_text or ''}\n    {j.job_url}")
    finally:
        fetcher.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
