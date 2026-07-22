from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Competition:
    slug: str
    title: str
    url: str
    category: str
    reward: str
    deadline: datetime | None
    api_team_count: int
    awards_points: bool = False


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    configured_team_name: str
    rank: int
    score: str
    submission_date: str


@dataclass(frozen=True, slots=True)
class LeaderboardSnapshot:
    team_count: int
    kind: str
    matches: tuple[LeaderboardEntry, ...]
    score_order: str = "unknown"
    score_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LateSubmissionEntry:
    competition_slug: str
    competition_title: str
    competition_url: str
    deadline: datetime
    configured_team_name: str
    public_score: str
    private_score: str
    submission_date: datetime


class CompetitionSource(Protocol):
    def list_competitions(self, max_competitions: int | None = None) -> list[Competition]: ...

    def get_leaderboard(
        self,
        competition: Competition,
        normalized_teams: dict[str, str],
    ) -> LeaderboardSnapshot: ...


@dataclass(frozen=True, slots=True)
class ScanFailure:
    competition_slug: str
    kind: str
