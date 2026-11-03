"""Build enabled collectors from config/sources.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from collectors.ats_collector import AtsBoard, AtsCollector
from collectors.base_collector import BaseCollector, HttpFetcher
from collectors.yc_collector import YCCollector
from config.settings import PROJECT_ROOT

DEFAULT_SOURCES_FILE = PROJECT_ROOT / "config" / "sources.yaml"


def load_sources_config(path: Path = DEFAULT_SOURCES_FILE) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def make_fetcher(config: dict) -> HttpFetcher:
    http = config.get("http") or {}
    return HttpFetcher(
        timeout=http.get("timeout_seconds", 20),
        max_retries=http.get("max_retries", 3),
        min_interval=http.get("min_seconds_between_requests_per_host", 2.0),
        host_intervals={host: float(sec) for host, sec in (http.get("per_host_seconds") or {}).items()},
    )


def build_collectors(
    config: dict,
    fetcher: HttpFetcher,
    *,
    known_job_ids: dict[str, set[str]] | None = None,
    extra_boards: list[AtsBoard] | None = None,
) -> list[BaseCollector]:
    """`extra_boards`: boards found by YC board discovery, polled alongside the configured ones."""
    known_job_ids = known_job_ids or {}
    collectors: list[BaseCollector] = []

    yc = config.get("yc") or {}
    if yc.get("enabled"):
        collectors.append(
            YCCollector(
                fetcher,
                role_paths=yc.get("role_paths") or [],
                known_job_ids=known_job_ids.get("yc"),
                max_detail_fetches=yc.get("max_detail_fetches_per_run", 30),
            )
        )

    ats = config.get("ats") or {}
    if ats.get("enabled"):
        boards = [
            AtsBoard(b["ats"], b["slug"], company_name=b.get("company"), website=b.get("website"))
            for b in ats.get("boards") or []
        ]
        configured = {(b.ats, b.slug) for b in boards}
        boards += [b for b in extra_boards or [] if (b.ats, b.slug) not in configured]
        collectors.append(AtsCollector(fetcher, boards))

    return collectors
