from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from .builder import _safe_failure_kind, build_leaderboard
from .kaggle_source import KaggleCompetitionSource, authenticated_kaggle_api
from .late_submissions import KaggleLateSubmissionSource
from .output import write_json_atomic
from .settings import (
    ConfigurationError,
    KaggleCredential,
    Settings,
    credential_secret_values,
    merge_team_names,
    normalize_team_name,
)


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
    parser.add_argument(
        "--skip-late-submissions",
        action="store_true",
        help="Skip authenticated My Submissions scans",
    )
    return parser


def _mask_api_credentials(api_credentials: tuple[KaggleCredential, ...]) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for credential in api_credentials:
        for secret_value in credential_secret_values(credential):
            print(f"::add-mask::{secret_value}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        settings = Settings.from_environment()
        _mask_api_credentials(settings.api_tokens)
        primary_api = authenticated_kaggle_api(settings.api_tokens[0])
        last_reported = 0

        def report_progress(completed: int, total: int) -> None:
            nonlocal last_reported
            if completed == total or completed - last_reported >= 25:
                LOGGER.info("Scanned %d of %d competitions", completed, total)
                last_reported = completed

        late_submissions = []
        discovered_team_names: list[str] = []
        late_failure_kinds: list[str] = []
        discovery_tokens = (
            set(settings.team_discovery_api_tokens)
            if settings.auto_discover_teams
            else set()
        )
        if not args.skip_late_submissions:
            for index, credential in enumerate(settings.api_tokens):
                try:
                    api = (
                        primary_api
                        if index == 0
                        else authenticated_kaggle_api(credential)
                    )
                    scan = KaggleLateSubmissionSource(
                        api,
                        min_request_interval_seconds=settings.request_interval_seconds,
                    ).collect(
                        settings.normalized_teams,
                        discover_teams=credential in discovery_tokens,
                    )
                    late_submissions.extend(scan.entries)
                    discovered_team_names.extend(scan.discovered_team_names)
                except Exception as exc:
                    failure_kind = _safe_failure_kind(exc)
                    late_failure_kinds.append(failure_kind)
                    LOGGER.warning(
                        "Late-submission account %d of %d failed (%s)",
                        index + 1,
                        len(settings.api_tokens),
                        failure_kind,
                    )

        effective_teams = merge_team_names(
            settings.teams,
            discovered_team_names if settings.auto_discover_teams else (),
        )
        if not effective_teams:
            raise ConfigurationError(
                "No team names were configured or discovered from My Submissions"
            )
        effective_settings = replace(settings, teams=effective_teams)
        canonical_teams = effective_settings.normalized_teams
        late_submissions = [
            replace(
                entry,
                configured_team_name=canonical_teams[
                    normalize_team_name(entry.configured_team_name)
                ],
            )
            for entry in late_submissions
        ]
        if settings.auto_discover_teams:
            LOGGER.info(
                "Tracking %d teams (%d configured, %d auto-discovered)",
                len(effective_teams),
                len(settings.teams),
                len(effective_teams) - len(settings.teams),
            )

        payload = build_leaderboard(
            KaggleCompetitionSource(
                primary_api,
                min_request_interval_seconds=settings.request_interval_seconds,
            ),
            effective_settings,
            max_competitions=args.max_competitions,
            progress=report_progress,
            late_submissions=tuple(late_submissions),
            late_submission_account_count=(
                0 if args.skip_late_submissions else len(settings.api_tokens)
            ),
            late_submission_failure_kinds=tuple(late_failure_kinds),
        )
        write_json_atomic(payload, args.output)
        summary = payload["summary"]
        LOGGER.info(
            "Wrote sanitized leaderboard: %d matched competitions, %d participations, "
            "%d late submissions, %d leaderboard failures, %d account failures",
            summary["matched_competition_count"],
            summary["participation_count"],
            summary["late_submission_count"],
            summary["failed_competition_count"],
            summary["failed_late_submission_account_count"],
        )
        return 0
    except ConfigurationError as exc:
        LOGGER.error("Configuration error: %s", exc)
    except Exception as exc:
        LOGGER.error("Build failed safely (%s)", type(exc).__name__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
