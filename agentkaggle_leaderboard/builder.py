from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from statistics import fmean
from typing import Callable

from requests import exceptions as requests_exceptions

from .kaggle_source import (
    InvalidKaggleResponse,
    KaggleAuthenticationError,
    UnsafePrivateLeaderboard,
)
from .medals import medal_candidate
from .models import (
    Competition,
    CompetitionSource,
    LateSubmissionEntry,
    LeaderboardEntry,
    LeaderboardSnapshot,
    ScanFailure,
)
from .settings import Settings, normalize_team_name


ProgressCallback = Callable[[int, int], None]
MINIMUM_SCAN_SUCCESS_RATIO = 0.5


def _iso_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _competition_state(deadline: datetime | None, generated_at: datetime) -> str:
    if deadline is None:
        return "unknown"
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return "active" if deadline >= generated_at else "ended"


def _safe_failure_kind(exc: BaseException) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in {401, 403}:
        return "access_denied"
    if isinstance(exc, KaggleAuthenticationError):
        return "access_denied"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            requests_exceptions.ConnectionError,
            requests_exceptions.Timeout,
        ),
    ):
        return "network"
    if isinstance(exc, UnsafePrivateLeaderboard):
        return "unsafe_private_leaderboard"
    if isinstance(exc, InvalidKaggleResponse):
        return "invalid_response"
    return "unexpected"


def _public_competition(
    competition: Competition,
    snapshot: LeaderboardSnapshot,
    generated_at: datetime,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    best_by_team: dict[str, LeaderboardEntry] = {}
    for entry in snapshot.matches:
        key = normalize_team_name(entry.configured_team_name)
        existing = best_by_team.get(key)
        if existing is None or entry.rank < existing.rank:
            best_by_team[key] = entry

    for entry in sorted(
        best_by_team.values(),
        key=lambda item: (item.rank, item.configured_team_name),
    ):
        top_percent = round((entry.rank / snapshot.team_count) * 100, 4)
        entries.append(
            {
                "team_name": entry.configured_team_name,
                "rank": entry.rank,
                "top_percent": top_percent,
                "score": entry.score,
                "submission_date": entry.submission_date,
                "medal_candidate": (
                    medal_candidate(entry.rank, snapshot.team_count)
                    if competition.awards_points
                    else "not_eligible"
                ),
            }
        )

    return {
        "slug": competition.slug,
        "title": competition.title,
        "url": competition.url,
        "category": competition.category,
        "reward": competition.reward,
        "deadline": _iso_utc(competition.deadline),
        "state": _competition_state(competition.deadline, generated_at),
        "leaderboard_kind": snapshot.kind,
        "leaderboard_team_count": snapshot.team_count,
        "api_team_count": competition.api_team_count,
        "awards_points": competition.awards_points,
        "entries": entries,
    }


def _public_late_submissions(
    late_submissions: tuple[LateSubmissionEntry, ...],
) -> list[dict[str, object]]:
    unique = {
        (
            entry.competition_slug,
            entry.configured_team_name,
            entry.submission_date,
            entry.public_score,
            entry.private_score,
        ): entry
        for entry in late_submissions
    }
    ordered = sorted(
        unique.values(),
        key=lambda entry: (
            -entry.submission_date.timestamp(),
            entry.competition_title.casefold(),
            entry.configured_team_name.casefold(),
        ),
    )
    return [
        {
            "competition_slug": entry.competition_slug,
            "competition_title": entry.competition_title,
            "competition_url": entry.competition_url,
            "deadline": _iso_utc(entry.deadline),
            "team_name": entry.configured_team_name,
            "public_score": entry.public_score,
            "private_score": entry.private_score,
            "submission_date": _iso_utc(entry.submission_date),
        }
        for entry in ordered
    ]


def _team_summaries(
    teams: tuple[str, ...],
    competitions: list[dict[str, object]],
    late_submissions: list[dict[str, object]],
) -> list[dict[str, object]]:
    entries_by_team: dict[str, list[dict[str, object]]] = {team: [] for team in teams}
    late_counts = Counter(str(entry["team_name"]) for entry in late_submissions)
    for competition in competitions:
        for entry in competition["entries"]:  # type: ignore[index]
            entries_by_team[entry["team_name"]].append(entry)  # type: ignore[index]

    summaries: list[dict[str, object]] = []
    for team in teams:
        entries = entries_by_team[team]
        top_percents = [float(entry["top_percent"]) for entry in entries]
        medal_count = sum(
            entry["medal_candidate"] in {"gold", "silver", "bronze"} for entry in entries
        )
        summaries.append(
            {
                "name": team,
                "competition_count": len(entries),
                "best_rank": min((int(entry["rank"]) for entry in entries), default=None),
                "average_top_percent": round(fmean(top_percents), 4) if top_percents else None,
                "medal_candidate_count": medal_count,
                "late_submission_count": late_counts[team],
            }
        )
    return sorted(
        summaries,
        key=lambda item: (
            -int(item["competition_count"]),
            -int(item["late_submission_count"]),
            float(item["average_top_percent"]) if item["average_top_percent"] is not None else float("inf"),
            str(item["name"]).casefold(),
        ),
    )


def build_leaderboard(
    source: CompetitionSource,
    settings: Settings,
    *,
    max_competitions: int | None = None,
    generated_at: datetime | None = None,
    progress: ProgressCallback | None = None,
    late_submissions: tuple[LateSubmissionEntry, ...] = (),
    late_submission_account_count: int = 0,
    late_submission_failure_kinds: tuple[str, ...] = (),
) -> dict[str, object]:
    generated_at = generated_at or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)

    competitions = source.list_competitions(max_competitions=max_competitions)
    if not competitions:
        raise RuntimeError("Kaggle returned no competitions")
    snapshots: dict[str, LeaderboardSnapshot] = {}
    failures: list[ScanFailure] = []

    with ThreadPoolExecutor(max_workers=settings.workers) as executor:
        futures = {
            executor.submit(source.get_leaderboard, competition, settings.normalized_teams): competition
            for competition in competitions
        }
        completed = 0
        for future in as_completed(futures):
            competition = futures[future]
            try:
                snapshots[competition.slug] = future.result()
            except Exception as exc:  # Each competition is an independent, best-effort source.
                failures.append(ScanFailure(competition.slug, _safe_failure_kind(exc)))
            completed += 1
            if progress:
                progress(completed, len(competitions))

    if competitions and not snapshots:
        raise RuntimeError("Kaggle returned no usable competition leaderboards")
    scan_success_ratio = len(snapshots) / len(competitions)
    if scan_success_ratio < MINIMUM_SCAN_SUCCESS_RATIO:
        raise RuntimeError("Kaggle scan was too degraded to replace the last good snapshot")

    public_competitions = [
        _public_competition(competition, snapshots[competition.slug], generated_at)
        for competition in competitions
        if competition.slug in snapshots and snapshots[competition.slug].matches
    ]
    public_competitions.sort(
        key=lambda item: (
            item["state"] != "active",
            str(item["deadline"] or "0000"),
            str(item["title"]).casefold(),
        ),
        reverse=False,
    )

    participation_count = sum(len(item["entries"]) for item in public_competitions)
    public_late_submissions = _public_late_submissions(late_submissions)
    late_competition_count = len(
        {str(entry["competition_slug"]) for entry in public_late_submissions}
    )
    truncated = max_competitions is not None and len(competitions) >= max_competitions
    status = "partial" if failures or late_submission_failure_kinds or truncated else "ready"
    error_counts = dict(sorted(Counter(failure.kind for failure in failures).items()))
    late_error_counts = dict(sorted(Counter(late_submission_failure_kinds).items()))

    return {
        "schema_version": 2,
        "generated_at": _iso_utc(generated_at),
        "status": status,
        "summary": {
            "tracked_team_count": len(settings.teams),
            "discovered_competition_count": len(competitions),
            "scanned_competition_count": len(snapshots),
            "failed_competition_count": len(failures),
            "matched_competition_count": len(public_competitions),
            "participation_count": participation_count,
            "late_submission_account_count": late_submission_account_count,
            "failed_late_submission_account_count": len(late_submission_failure_kinds),
            "late_submission_competition_count": late_competition_count,
            "late_submission_count": len(public_late_submissions),
            "late_submission_error_counts": late_error_counts,
            "truncated": truncated,
            "error_counts": error_counts,
        },
        "teams": _team_summaries(settings.teams, public_competitions, public_late_submissions),
        "competitions": public_competitions,
        "late_submissions": public_late_submissions,
        "methodology": {
            "rank": "Official Rank from Kaggle's complete leaderboard CSV.",
            "top_percent": (
                "Each team contributes at most once per competition using its best official rank. "
                "That rank is divided by the deduplicated number of teams in the leaderboard, "
                "then multiplied by 100."
            ),
            "score": "Score is preserved as text exactly as provided by Kaggle.",
            "late_submission": (
                "A completed submission made after the competition deadline and returned by the "
                "authenticated account's My Submissions API. It is not an official rank."
            ),
            "medal_candidate": (
                "Shown only when Kaggle marks the competition as awarding points. It remains a rank-only "
                "estimate: team eligibility, disqualification, verification and active standings can change it."
            ),
        },
    }
