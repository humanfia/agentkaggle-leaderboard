from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .builder import build_leaderboard
from .kaggle_source import KaggleCompetitionSource
from .output import write_json_atomic
from .settings import ConfigurationError, Settings


LOGGER = logging.getLogger("agentkaggle_leaderboard")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build an aggregated Kaggle team leaderboard")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/leaderboard.json"),
        help="JSON file consumed by Hugo (default: data/leaderboard.json)",
    )
    parser.add_argument(
        "--max-competitions",
        type=_positive_int,
        default=None,
        help="Limit scans for local validation; omit for the full catalog",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        settings = Settings.from_environment()
        last_reported = 0

        def report_progress(completed: int, total: int) -> None:
            nonlocal last_reported
            if completed == total or completed - last_reported >= 25:
                LOGGER.info("Scanned %d of %d competitions", completed, total)
                last_reported = completed

        payload = build_leaderboard(
            KaggleCompetitionSource(
                min_request_interval_seconds=settings.request_interval_seconds,
            ),
            settings,
            max_competitions=args.max_competitions,
            progress=report_progress,
        )
        write_json_atomic(payload, args.output)
        summary = payload["summary"]
        LOGGER.info(
            "Wrote sanitized leaderboard: %d matched competitions, %d participations, %d scan failures",
            summary["matched_competition_count"],
            summary["participation_count"],
            summary["failed_competition_count"],
        )
        return 0
    except ConfigurationError as exc:
        LOGGER.error("Configuration error: %s", exc)
    except Exception as exc:
        LOGGER.error("Build failed safely (%s)", type(exc).__name__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
