"""Re-apply config/job_preferences.yaml to every stored job and re-score them, without collecting anything.

Use after editing preferences to see the effect immediately:
    python -m scripts.reevaluate            # filters + scoring (semantic model if available)
    python -m scripts.reevaluate --rules    # skip the semantic model
(The scheduled pipeline also does this automatically when it notices the preferences changed.)
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from sqlalchemy import select

from config.settings import load_settings
from core.pipeline import PREFERENCES_KEY, make_matcher
from core.preferences import load_preferences, preferences_fingerprint
from database.models import Job
from database.repository import JobRepository, init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rules", action="store_true", help="score without the semantic model")
    args = parser.parse_args(argv)

    prefs = load_preferences()
    factory = init_db(load_settings().database_url)
    with factory.begin() as session:
        before = Counter(session.scalars(select(Job.filter_status)))
        repo = JobRepository(session)
        after = repo.reevaluate_all(prefs)
        repo.set_state(PREFERENCES_KEY, preferences_fingerprint(prefs))
        scored = repo.score_active_jobs(prefs, None if args.rules else make_matcher(prefs))
    for status in ("passed", "review", "rejected"):
        print(f"{status:9s} {before.get(status, 0):6d} -> {after.get(status, 0):6d}")
    print("scored:", {k: v for k, v in scored.items() if k != "semantic"},
          "| semantic similarity:", "used" if scored.get("semantic") else "not used")
    return 0


if __name__ == "__main__":
    sys.exit(main())
