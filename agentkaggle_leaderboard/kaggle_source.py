from __future__ import annotations

import csv
import io
import os
import re
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal, InvalidOperation
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from requests import exceptions as requests_exceptions

from .models import Competition, LeaderboardEntry, LeaderboardSnapshot
from .settings import KaggleCredential, LegacyKaggleCredential, normalize_team_name


_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class InvalidKaggleResponse(RuntimeError):
    pass


class UnsafePrivateLeaderboard(RuntimeError):
    pass


class KaggleAuthenticationError(RuntimeError):
    pass


def competition_slug(ref: str) -> str:
    parsed = urlparse(ref)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.hostname not in {"kaggle.com", "www.kaggle.com"}:
        raise InvalidKaggleResponse("Kaggle returned an invalid competition reference")
    if len(parts) != 2 or parts[0] != "competitions":
        raise InvalidKaggleResponse("Kaggle returned an invalid competition reference")
    slug = parts[1]
    if not _SLUG_PATTERN.fullmatch(slug):
        raise InvalidKaggleResponse("Kaggle returned an invalid competition reference")
    return slug


def competition_from_api(item: object) -> Competition:
    ref = str(getattr(item, "ref", "") or "")
    slug = competition_slug(ref)
    return Competition(
        slug=slug,
        title=str(getattr(item, "title", "") or slug).strip(),
        url=f"https://www.kaggle.com/competitions/{slug}",
        category=str(getattr(item, "category", "") or "Unspecified").strip(),
        reward=str(getattr(item, "reward", "") or "").strip(),
        deadline=getattr(item, "deadline", None),
        api_team_count=max(0, int(getattr(item, "team_count", 0) or 0)),
        awards_points=bool(getattr(item, "awards_points", False)),
    )


def _as_utc_iso(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value or "").strip()


def _parse_rank(value: str) -> int:
    try:
        rank = int(value.replace(",", "").strip())
    except (AttributeError, ValueError) as exc:
        raise InvalidKaggleResponse("Kaggle leaderboard contains an invalid rank") from exc
    if rank < 0:
        raise InvalidKaggleResponse("Kaggle leaderboard contains an invalid rank")
    return rank


def _numeric_score(value: str) -> Decimal | None:
    try:
        score = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None
    return score if score.is_finite() else None


def _infer_score_order(ranked_scores: list[tuple[int, str]]) -> str:
    numeric_scores = sorted(
        (
            (rank, score)
            for rank, raw_score in ranked_scores
            if (score := _numeric_score(raw_score)) is not None
        ),
        key=lambda item: item[0],
    )
    for index, (better_rank, better_score) in enumerate(numeric_scores):
        for worse_rank, worse_score in numeric_scores[index + 1 :]:
            if worse_rank <= better_rank or better_score == worse_score:
                continue
            return "higher" if better_score > worse_score else "lower"
    return "unknown"


def _normalized_headers(fieldnames: list[str] | None) -> dict[str, str]:
    if not fieldnames:
        return {}
    return {re.sub(r"[^a-z0-9]", "", name.casefold()): name for name in fieldnames}


def validate_leaderboard_visibility(
    snapshot: LeaderboardSnapshot,
    competition: Competition,
    *,
    now: datetime | None = None,
) -> None:
    """Fail closed unless the downloaded leaderboard is safe to publish."""
    if snapshot.kind == "unknown":
        raise InvalidKaggleResponse("Kaggle returned an unrecognized leaderboard archive")
    if snapshot.kind != "private":
        return

    deadline = competition.deadline
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    current_time = now or datetime.now(timezone.utc)
    if deadline is None or deadline > current_time:
        raise UnsafePrivateLeaderboard(
            "Refusing to publish a private leaderboard before the competition deadline"
        )


def authenticated_kaggle_api(credential: KaggleCredential):
    """Authenticate one isolated SDK instance without retaining credential environment state."""
    from kaggle.api.kaggle_api_extended import KaggleApi

    credential_variables = (
        "KAGGLE_API_TOKEN",
        "KAGGLE_API_TOKENS",
        "KAGGLE_LEGACY_CREDENTIALS",
        "KAGGLE_USERNAME",
        "KAGGLE_KEY",
    )
    previous_values = {name: os.environ.get(name) for name in credential_variables}

    with tempfile.TemporaryDirectory(prefix="kaggle-auth-") as temp_dir:
        try:
            for name in credential_variables:
                os.environ.pop(name, None)
            api = KaggleApi()
            api.config = str(Path(temp_dir, "kaggle.json"))
            if isinstance(credential, LegacyKaggleCredential):
                empty_access_token = Path(temp_dir, "access_token")
                empty_access_token.write_text("", encoding="utf-8")
                os.environ["KAGGLE_API_TOKEN"] = str(empty_access_token)
                os.environ["KAGGLE_USERNAME"] = credential.username
                os.environ["KAGGLE_KEY"] = credential.key
            else:
                os.environ["KAGGLE_API_TOKEN"] = credential

            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    api.authenticate()
            except SystemExit as exc:
                raise KaggleAuthenticationError("Kaggle rejected a credential") from exc
        finally:
            for name, value in previous_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    return api


class _KaggleRequestSource:
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        api=None,
        *,
        retry_attempts: int = 6,
        min_request_interval_seconds: float = 2.0,
    ) -> None:
        if api is None:
            # Lazy import keeps credential-free unit tests and pull requests isolated.
            from kaggle.api.kaggle_api_extended import KaggleApi

            api = KaggleApi()
            api.authenticate()
        self._api = api
        self._retry_attempts = retry_attempts
        self._min_request_interval_seconds = min_request_interval_seconds
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0

    def _wait_for_request_slot(self) -> None:
        with self._request_lock:
            now = time.monotonic()
            delay = self._next_request_at - now
            if delay > 0:
                time.sleep(delay)
            self._next_request_at = time.monotonic() + self._min_request_interval_seconds

    def _postpone_requests(self, delay: float) -> None:
        """Apply a server-directed cooldown to every worker sharing this source."""
        with self._request_lock:
            self._next_request_at = max(
                self._next_request_at,
                time.monotonic() + delay,
            )

    def _call_with_retry(self, operation):
        for attempt in range(self._retry_attempts):
            try:
                self._wait_for_request_slot()
                return operation()
            except Exception as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                retryable = status in self.RETRYABLE_STATUS_CODES or isinstance(
                    exc,
                    (
                        TimeoutError,
                        ConnectionError,
                        requests_exceptions.ConnectionError,
                        requests_exceptions.Timeout,
                    ),
                )
                if not retryable or attempt + 1 >= self._retry_attempts:
                    raise
                retry_after = (
                    getattr(response, "headers", {}).get("Retry-After") if response is not None else None
                )
                try:
                    delay = max(0.0, min(float(retry_after), 60.0))
                except (TypeError, ValueError):
                    try:
                        retry_at = parsedate_to_datetime(str(retry_after))
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        delay = max(
                            0.0,
                            min((retry_at - datetime.now(timezone.utc)).total_seconds(), 60.0),
                        )
                    except (TypeError, ValueError, OverflowError):
                        delay = min(2**attempt, 30)
                self._postpone_requests(delay)
        raise AssertionError("unreachable")


class KaggleCompetitionSource(_KaggleRequestSource):
    PUBLIC_GROUPS = ("general", "community")

    def list_competitions(self, max_competitions: int | None = None) -> list[Competition]:
        competitions: list[Competition] = []
        seen_slugs: set[str] = set()
        for group in self.PUBLIC_GROUPS:
            page_token: str | None = None
            seen_page_tokens: set[str] = set()
            while True:
                response = self._call_with_retry(
                    lambda group=group, page_token=page_token: self._api.competitions_list(
                        group=group,
                        category="all",
                        sort_by="recentlyCreated",
                        page=-1,
                        page_size=200,
                        page_token=page_token,
                    )
                )
                api_competitions = list(response.competitions or []) if response else []

                for item in api_competitions:
                    competition = competition_from_api(item)
                    if competition.slug in seen_slugs:
                        continue
                    seen_slugs.add(competition.slug)
                    competitions.append(competition)
                    if max_competitions is not None and len(competitions) >= max_competitions:
                        return competitions

                next_page_token = str(getattr(response, "next_page_token", "") or "")
                if not next_page_token:
                    break
                if next_page_token in seen_page_tokens:
                    raise InvalidKaggleResponse("Kaggle returned a repeated competition page token")
                seen_page_tokens.add(next_page_token)
                page_token = next_page_token

        return competitions

    def get_leaderboard(
        self,
        competition: Competition,
        normalized_teams: dict[str, str],
    ) -> LeaderboardSnapshot:
        if competition.api_team_count == 0:
            return LeaderboardSnapshot(team_count=0, kind="empty", matches=())

        with tempfile.TemporaryDirectory(prefix="kaggle-leaderboard-") as temp_dir:
            self._call_with_retry(
                lambda: self._api.competition_leaderboard_download(competition.slug, temp_dir, quiet=True)
            )
            archive_path = Path(temp_dir, f"{competition.slug}.zip")
            if not archive_path.is_file():
                raise InvalidKaggleResponse("Kaggle did not return a leaderboard archive")
            snapshot = self._read_archive(archive_path, normalized_teams)
            validate_leaderboard_visibility(snapshot, competition)
            return snapshot

    @staticmethod
    def _read_archive(
        archive_path: Path,
        normalized_teams: dict[str, str],
    ) -> LeaderboardSnapshot:
        with zipfile.ZipFile(archive_path) as archive:
            csv_members = [name for name in archive.namelist() if name.casefold().endswith(".csv")]
            if len(csv_members) != 1:
                raise InvalidKaggleResponse("Kaggle leaderboard archive must contain one CSV file")
            member = csv_members[0]
            lowered_member = member.casefold()
            if "privateleaderboard" in lowered_member:
                kind = "private"
            elif "publicleaderboard" in lowered_member:
                kind = "public"
            else:
                kind = "unknown"

            with archive.open(member) as binary_file:
                text_file = io.TextIOWrapper(binary_file, encoding="utf-8-sig", newline="")
                reader = csv.DictReader(text_file)
                headers = _normalized_headers(reader.fieldnames)
                required = {"rank", "teamname", "score"}
                if not required.issubset(headers):
                    raise InvalidKaggleResponse("Kaggle leaderboard CSV is missing required columns")

                date_header = headers.get("lastsubmissiondate") or headers.get("submissiondate")
                matches: dict[str, LeaderboardEntry] = {}
                seen_teams: set[str] = set()
                best_score_by_team: dict[str, tuple[int, str]] = {}
                team_id_header = headers.get("teamid")
                team_count = 0
                for row_index, row in enumerate(reader):
                    rank = _parse_rank(row.get(headers["rank"]) or "")
                    if rank == 0:
                        continue
                    kaggle_team_name = (row.get(headers["teamname"]) or "").strip()
                    key = normalize_team_name(kaggle_team_name)
                    team_identity = (
                        (row.get(team_id_header) or "").strip()
                        if team_id_header
                        else key
                    ) or f"row-{row_index}"
                    if team_identity not in seen_teams:
                        seen_teams.add(team_identity)
                        team_count += 1
                    score = (row.get(headers["score"]) or "").strip()
                    existing_score = best_score_by_team.get(team_identity)
                    if existing_score is None or rank < existing_score[0]:
                        best_score_by_team[team_identity] = (rank, score)
                    configured_name = normalized_teams.get(key)
                    if configured_name is None:
                        continue
                    candidate = LeaderboardEntry(
                        configured_team_name=configured_name,
                        rank=rank,
                        score=score,
                        submission_date=_as_utc_iso(row.get(date_header) if date_header else ""),
                    )
                    existing = matches.get(key)
                    if existing is None or candidate.rank < existing.rank:
                        matches[key] = candidate

        ordered_matches = tuple(
            sorted(matches.values(), key=lambda entry: (entry.rank, entry.configured_team_name))
        )
        return LeaderboardSnapshot(
            team_count=team_count,
            kind=kind,
            matches=ordered_matches,
            score_order=_infer_score_order(list(best_score_by_team.values())),
        )


class KaggleAggregatedCompetitionSource:
    """Merge public competitions with account-entered competitions and route access."""

    def __init__(
        self,
        primary_api,
        entered_competition_access: list[tuple[Competition, object]],
        *,
        min_request_interval_seconds: float = 2.0,
    ) -> None:
        self._primary_source = KaggleCompetitionSource(
            primary_api,
            min_request_interval_seconds=min_request_interval_seconds,
        )
        self._entered_competitions: dict[str, Competition] = {}
        self._sources_by_slug: dict[str, list[KaggleCompetitionSource]] = {}
        source_by_api_id = {id(primary_api): self._primary_source}

        for competition, api in entered_competition_access:
            self._entered_competitions.setdefault(competition.slug, competition)
            source = source_by_api_id.get(id(api))
            if source is None:
                source = KaggleCompetitionSource(
                    api,
                    min_request_interval_seconds=min_request_interval_seconds,
                )
                source_by_api_id[id(api)] = source
            sources = self._sources_by_slug.setdefault(competition.slug, [])
            if source not in sources:
                sources.append(source)

    def list_competitions(self, max_competitions: int | None = None) -> list[Competition]:
        public_competitions = self._primary_source.list_competitions(
            max_competitions=max_competitions
        )
        merged = {competition.slug: competition for competition in public_competitions}
        for slug, competition in self._entered_competitions.items():
            merged.setdefault(slug, competition)
        competitions = list(merged.values())
        return competitions[:max_competitions] if max_competitions is not None else competitions

    def get_leaderboard(
        self,
        competition: Competition,
        normalized_teams: dict[str, str],
    ) -> LeaderboardSnapshot:
        sources = list(self._sources_by_slug.get(competition.slug, ()))
        if self._primary_source not in sources:
            sources.append(self._primary_source)

        last_error: Exception | None = None
        for source in sources:
            try:
                return source.get_leaderboard(competition, normalized_teams)
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise AssertionError("competition source routing produced no candidates")
