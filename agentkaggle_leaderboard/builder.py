from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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


def _numeric_value(value: object) -> Decimal | None:
    try:
        score = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None
    return score if score.is_finite() else None


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
    snapshot: LeaderboardSnapshot | None,
    generated_at: datetime,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    best_by_team: dict[str, LeaderboardEntry] = {}
    if snapshot is not None:
        for entry in snapshot.matches:
            key = normalize_team_name(entry.configured_team_name)
            existing = best_by_team.get(key)
            if existing is None or entry.rank < existing.rank:
                best_by_team[key] = entry

    for entry in sorted(
        best_by_team.values(),
        key=lambda item: (item.rank, item.configured_team_name),
    ):
        if snapshot is None:
            raise AssertionError("Official entries require a leaderboard snapshot")
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
                "late_public_score": "",
                "late_private_score": "",
                "late_submission_date": "",
                "late_rank": None,
                "late_top_percent": None,
                "late_rank_team_count": None,
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
        "leaderboard_kind": snapshot.kind if snapshot is not None else "unavailable",
        "leaderboard_team_count": snapshot.team_count if snapshot is not None else 0,
        "api_team_count": competition.api_team_count,
        "awards_points": competition.awards_points,
        "entries": entries,
    }


def _public_late_submissions(
    late_submissions: tuple[LateSubmissionEntry, ...],
    score_orders: dict[str, str],
) -> list[dict[str, object]]:
    def numeric_score(entry: LateSubmissionEntry) -> Decimal | None:
        for value in (entry.private_score, entry.public_score):
            score = _numeric_value(value)
            if score is not None:
                return score
        return None

    def is_better(candidate: LateSubmissionEntry, existing: LateSubmissionEntry) -> bool:
        score_order = score_orders.get(candidate.competition_slug, "unknown")
        candidate_score = numeric_score(candidate)
        existing_score = numeric_score(existing)
        if score_order in {"higher", "lower"}:
            if candidate_score is not None and existing_score is None:
                return True
            if candidate_score is not None and existing_score is not None:
                if candidate_score != existing_score:
                    return (
                        candidate_score > existing_score
                        if score_order == "higher"
                        else candidate_score < existing_score
                    )
        return candidate.submission_date > existing.submission_date

    unique: dict[tuple[str, str], LateSubmissionEntry] = {}
    for entry in late_submissions:
        key = (
            entry.competition_slug,
            normalize_team_name(entry.configured_team_name),
        )
        existing = unique.get(key)
        if existing is None or is_better(entry, existing):
            unique[key] = entry

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


def _merge_late_results_into_competitions(
    competitions: list[dict[str, object]],
    late_submissions: list[dict[str, object]],
    snapshots: dict[str, LeaderboardSnapshot],
) -> None:
    competitions_by_slug = {
        str(competition["slug"]): competition for competition in competitions
    }

    for late_submission in late_submissions:
        slug = str(late_submission["competition_slug"])
        competition = competitions_by_slug.get(slug)
        if competition is None:
            competition = {
                "slug": slug,
                "title": late_submission["competition_title"],
                "url": late_submission["competition_url"],
                "category": "Entered",
                "reward": "",
                "deadline": late_submission["deadline"],
                "state": "ended",
                "leaderboard_kind": "unavailable",
                "leaderboard_team_count": 0,
                "api_team_count": 0,
                "awards_points": False,
                "entries": [],
            }
            competitions.append(competition)
            competitions_by_slug[slug] = competition

        entries = competition["entries"]
        if not isinstance(entries, list):
            raise TypeError("Competition entries must be a list")
        team_key = normalize_team_name(str(late_submission["team_name"]))
        matching_entry = next(
            (
                entry
                for entry in entries
                if normalize_team_name(str(entry["team_name"])) == team_key
            ),
            None,
        )
        if matching_entry is None:
            matching_entry = {
                "team_name": late_submission["team_name"],
                "rank": None,
                "top_percent": None,
                "score": "",
                "submission_date": "",
                "medal_candidate": "unavailable",
                "late_public_score": "",
                "late_private_score": "",
                "late_submission_date": "",
                "late_rank": None,
                "late_top_percent": None,
                "late_rank_team_count": None,
            }
            entries.append(matching_entry)

        matching_entry["late_public_score"] = late_submission["public_score"]
        matching_entry["late_private_score"] = late_submission["private_score"]
        matching_entry["late_submission_date"] = late_submission["submission_date"]
        snapshot = snapshots.get(slug)
        late_score = next(
            (
                score
                for value in (
                    late_submission["private_score"],
                    late_submission["public_score"],
                )
                if (score := _numeric_value(value)) is not None
            ),
            None,
        )
        leaderboard_scores = (
            [score for value in snapshot.score_values if (score := _numeric_value(value)) is not None]
            if snapshot is not None
            else []
        )
        official_score = _numeric_value(matching_entry["score"])
        if official_score is not None and official_score in leaderboard_scores:
            leaderboard_scores.remove(official_score)
        if (
            snapshot is not None
            and snapshot.team_count > 0
            and snapshot.score_order in {"higher", "lower"}
            and late_score is not None
            and leaderboard_scores
        ):
            better_count = sum(
                score > late_score
                if snapshot.score_order == "higher"
                else score < late_score
                for score in leaderboard_scores
            )
            late_rank = better_count + 1
            matching_entry["late_rank"] = late_rank
            matching_entry["late_top_percent"] = round(
                (late_rank / snapshot.team_count) * 100,
                4,
            )
            matching_entry["late_rank_team_count"] = snapshot.team_count

    for competition in competitions:
        entries = competition["entries"]
        if not isinstance(entries, list):
            raise TypeError("Competition entries must be a list")
        entries.sort(
            key=lambda entry: (
                entry["rank"] is None,
                int(entry["rank"]) if entry["rank"] is not None else 0,
                str(entry["team_name"]).casefold(),
            )
        )


def _team_board(
    teams: tuple[str, ...],
    competitions: list[dict[str, object]],
    late_submissions: list[dict[str, object]],
    *,
    mode: str,
) -> list[dict[str, object]]:
    results_by_team: dict[str, list[tuple[int, float]]] = {team: [] for team in teams}
    competition_slugs_by_team: dict[str, set[str]] = {
        team: set() for team in teams
    }
    late_counts = Counter(str(entry["team_name"]) for entry in late_submissions)
    official_medal_counts = Counter()

    for competition in competitions:
        for entry in competition["entries"]:  # type: ignore[index]
            team_name = str(entry["team_name"])  # type: ignore[index]
            slug = str(competition["slug"])
            official_result = (
                (int(entry["rank"]), float(entry["top_percent"]))  # type: ignore[index]
                if entry["rank"] is not None  # type: ignore[index]
                else None
            )
            late_result = (
                (int(entry["late_rank"]), float(entry["late_top_percent"]))  # type: ignore[index]
                if entry["late_rank"] is not None  # type: ignore[index]
                else None
            )
            if entry["medal_candidate"] in {"gold", "silver", "bronze"}:  # type: ignore[index]
                official_medal_counts[team_name] += 1

            selected_result: tuple[int, float] | None
            has_result = False
            if mode == "ongoing":
                selected_result = official_result
                has_result = official_result is not None
            elif mode == "late":
                selected_result = late_result
                has_result = bool(entry["late_submission_date"])  # type: ignore[index]
            elif mode == "overall":
                available_results = [
                    result
                    for result in (official_result, late_result)
                    if result is not None
                ]
                selected_result = min(
                    available_results,
                    key=lambda result: (result[1], result[0]),
                    default=None,
                )
                has_result = official_result is not None or bool(  # type: ignore[index]
                    entry["late_submission_date"]
                )
            else:
                raise ValueError("Unsupported team leaderboard mode")

            if has_result:
                competition_slugs_by_team[team_name].add(slug)
            if selected_result is not None:
                results_by_team[team_name].append(selected_result)

    summaries: list[dict[str, object]] = []
    for team in teams:
        results = results_by_team[team]
        top_percents = [top_percent for _, top_percent in results]
        summaries.append(
            {
                "position": None,
                "name": team,
                "competition_count": len(competition_slugs_by_team[team]),
                "best_rank": min((rank for rank, _ in results), default=None),
                "average_top_percent": round(fmean(top_percents), 4) if top_percents else None,
                "medal_candidate_count": (
                    official_medal_counts[team] if mode in {"overall", "ongoing"} else 0
                ),
                "late_submission_count": (
                    late_counts[team] if mode in {"overall", "late"} else 0
                ),
            }
        )
    ordered = sorted(
        summaries,
        key=lambda item: (
            float(item["average_top_percent"]) if item["average_top_percent"] is not None else float("inf"),
            -int(item["competition_count"]),
            int(item["best_rank"]) if item["best_rank"] is not None else 2**31,
            str(item["name"]).casefold(),
        ),
    )
    position = 0
    for item in ordered:
        if item["average_top_percent"] is not None:
            position += 1
            item["position"] = position
    return ordered


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

    score_orders = {
        competition_slug: snapshot.score_order
        for competition_slug, snapshot in snapshots.items()
    }
    public_late_submissions = _public_late_submissions(late_submissions, score_orders)
    late_competition_slugs = {
        str(entry["competition_slug"]) for entry in public_late_submissions
    }
    public_competitions = [
        _public_competition(competition, snapshots.get(competition.slug), generated_at)
        for competition in competitions
        if (
            (
                competition.slug in snapshots
                and bool(snapshots[competition.slug].matches)
            )
            or competition.slug in late_competition_slugs
        )
    ]
    _merge_late_results_into_competitions(
        public_competitions,
        public_late_submissions,
        snapshots,
    )
    public_competitions.sort(
        key=lambda item: (
            item["state"] != "active",
            str(item["deadline"] or "0000"),
            str(item["title"]).casefold(),
        ),
        reverse=False,
    )

    participation_count = sum(len(item["entries"]) for item in public_competitions)
    late_competition_count = len(
        late_competition_slugs
    )
    truncated = max_competitions is not None and len(competitions) >= max_competitions
    status = "partial" if failures or late_submission_failure_kinds or truncated else "ready"
    error_counts = dict(sorted(Counter(failure.kind for failure in failures).items()))
    late_error_counts = dict(sorted(Counter(late_submission_failure_kinds).items()))

    overall_teams = _team_board(
        settings.teams,
        public_competitions,
        public_late_submissions,
        mode="overall",
    )
    late_teams = _team_board(
        settings.teams,
        public_competitions,
        public_late_submissions,
        mode="late",
    )
    ongoing_teams = _team_board(
        settings.teams,
        public_competitions,
        public_late_submissions,
        mode="ongoing",
    )

    return {
        "schema_version": 4,
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
        "teams": overall_teams,
        "late_teams": late_teams,
        "ongoing_teams": ongoing_teams,
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
                "The best completed post-deadline result per team and competition, returned by "
                "the authenticated account's My Submissions API. Score direction is inferred "
                "from the official leaderboard; the latest result is used when direction is unknown. "
                "Public and private late scores are merged into the competition table, including "
                "competitions where the tracked team has no official leaderboard row. A starred late "
                "rank compares that score with the complete leaderboard score distribution; it is not "
                "Kaggle's official Rank."
            ),
            "medal_candidate": (
                "Shown only when Kaggle marks the competition as awarding points. It remains a rank-only "
                "estimate: team eligibility, disqualification, verification and active standings can change it."
            ),
        },
    }
