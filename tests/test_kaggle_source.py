from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType
from unittest.mock import patch

from agentkaggle_leaderboard.kaggle_source import (
    InvalidKaggleResponse,
    KaggleAggregatedCompetitionSource,
    KaggleAuthenticationError,
    KaggleCompetitionSource,
    UnsafePrivateLeaderboard,
    authenticated_kaggle_api,
    competition_slug,
    validate_leaderboard_visibility,
)
from agentkaggle_leaderboard.models import Competition, LeaderboardSnapshot
from agentkaggle_leaderboard.settings import LegacyKaggleCredential


class KaggleSourceTests(unittest.TestCase):
    @staticmethod
    def _competition(deadline: datetime | None) -> Competition:
        return Competition(
            slug="safe-test",
            title="Safe test",
            url="https://www.kaggle.com/competitions/safe-test",
            category="Featured",
            reward="",
            deadline=deadline,
            api_team_count=10,
            awards_points=True,
        )

    def test_internal_api_is_explicitly_authenticated(self) -> None:
        class FakeKaggleApi:
            def __init__(self) -> None:
                self.authenticated = False

            def authenticate(self) -> None:
                self.authenticated = True

        kaggle_module = ModuleType("kaggle")
        kaggle_module.__path__ = []
        api_module = ModuleType("kaggle.api")
        api_module.__path__ = []
        extended_module = ModuleType("kaggle.api.kaggle_api_extended")
        extended_module.KaggleApi = FakeKaggleApi

        with patch.dict(
            sys.modules,
            {
                "kaggle": kaggle_module,
                "kaggle.api": api_module,
                "kaggle.api.kaggle_api_extended": extended_module,
            },
        ):
            source = KaggleCompetitionSource(min_request_interval_seconds=0.000001)

        self.assertTrue(source._api.authenticated)

    def test_explicit_token_authentication_restores_the_environment(self) -> None:
        observed_tokens: list[str | None] = []

        class FakeKaggleApi:
            def authenticate(self) -> None:
                observed_tokens.append(os.environ.get("KAGGLE_API_TOKEN"))

        kaggle_module = ModuleType("kaggle")
        kaggle_module.__path__ = []
        api_module = ModuleType("kaggle.api")
        api_module.__path__ = []
        extended_module = ModuleType("kaggle.api.kaggle_api_extended")
        extended_module.KaggleApi = FakeKaggleApi

        with (
            patch.dict(
                sys.modules,
                {
                    "kaggle": kaggle_module,
                    "kaggle.api": api_module,
                    "kaggle.api.kaggle_api_extended": extended_module,
                },
            ),
            patch.dict("os.environ", {"KAGGLE_API_TOKEN": "previous-token"}, clear=True),
        ):
            authenticated_kaggle_api("temporary-token")
            restored_token = os.environ.get("KAGGLE_API_TOKEN")

        self.assertEqual(observed_tokens, ["temporary-token"])
        self.assertEqual(restored_token, "previous-token")

    def test_legacy_authentication_isolated_from_modern_tokens_and_local_config(self) -> None:
        observed: dict[str, object] = {}

        class FakeKaggleApi:
            config = "/home/example/.kaggle/kaggle.json"

            def authenticate(self) -> None:
                access_token_path = Path(os.environ["KAGGLE_API_TOKEN"])
                observed.update(
                    username=os.environ.get("KAGGLE_USERNAME"),
                    key=os.environ.get("KAGGLE_KEY"),
                    access_token_file_exists=access_token_path.is_file(),
                    access_token_file_contents=access_token_path.read_text(encoding="utf-8"),
                    aggregate_tokens=os.environ.get("KAGGLE_API_TOKENS"),
                    aggregate_legacy=os.environ.get("KAGGLE_LEGACY_CREDENTIALS"),
                    config=self.config,
                    config_exists=Path(self.config).exists(),
                )

        kaggle_module = ModuleType("kaggle")
        kaggle_module.__path__ = []
        api_module = ModuleType("kaggle.api")
        api_module.__path__ = []
        extended_module = ModuleType("kaggle.api.kaggle_api_extended")
        extended_module.KaggleApi = FakeKaggleApi
        original_environment = {
            "KAGGLE_API_TOKEN": "previous-token",
            "KAGGLE_API_TOKENS": '["modern-token"]',
            "KAGGLE_LEGACY_CREDENTIALS": "legacy-secret-json",
            "KAGGLE_USERNAME": "previous-user",
            "KAGGLE_KEY": "previous-key",
        }

        with (
            patch.dict(
                sys.modules,
                {
                    "kaggle": kaggle_module,
                    "kaggle.api": api_module,
                    "kaggle.api.kaggle_api_extended": extended_module,
                },
            ),
            patch.dict("os.environ", original_environment, clear=True),
        ):
            authenticated_kaggle_api(
                LegacyKaggleCredential("apostle715", "legacy-key")
            )
            restored_environment = {
                name: os.environ.get(name) for name in original_environment
            }

        self.assertEqual(observed["username"], "apostle715")
        self.assertEqual(observed["key"], "legacy-key")
        self.assertTrue(observed["access_token_file_exists"])
        self.assertEqual(observed["access_token_file_contents"], "")
        self.assertIsNone(observed["aggregate_tokens"])
        self.assertIsNone(observed["aggregate_legacy"])
        self.assertFalse(observed["config_exists"])
        self.assertNotEqual(observed["config"], FakeKaggleApi.config)
        self.assertEqual(restored_environment, original_environment)

    def test_rejected_token_becomes_a_safe_authentication_error(self) -> None:
        class FakeKaggleApi:
            def authenticate(self) -> None:
                print("verbose authentication instructions")
                raise SystemExit(1)

        kaggle_module = ModuleType("kaggle")
        kaggle_module.__path__ = []
        api_module = ModuleType("kaggle.api")
        api_module.__path__ = []
        extended_module = ModuleType("kaggle.api.kaggle_api_extended")
        extended_module.KaggleApi = FakeKaggleApi
        visible_output = io.StringIO()

        with (
            patch.dict(
                sys.modules,
                {
                    "kaggle": kaggle_module,
                    "kaggle.api": api_module,
                    "kaggle.api.kaggle_api_extended": extended_module,
                },
            ),
            patch("sys.stdout", visible_output),
            self.assertRaises(KaggleAuthenticationError),
        ):
            authenticated_kaggle_api("rejected-token")

        self.assertEqual(visible_output.getvalue(), "")

    def test_catalog_only_requests_public_groups(self) -> None:
        calls = []

        class FakeApi:
            def competitions_list(self, **kwargs):
                calls.append(kwargs)
                slug = f'{kwargs["group"]}-competition'
                item = SimpleNamespace(
                    ref=f"https://www.kaggle.com/competitions/{slug}",
                    title=slug,
                    category="Featured",
                    reward="",
                    deadline=datetime(2026, 1, 1),
                    team_count=10,
                    awards_points=True,
                )
                return SimpleNamespace(competitions=[item], next_page_token="")

        competitions = KaggleCompetitionSource(
            FakeApi(), retry_attempts=1, min_request_interval_seconds=0.000001
        ).list_competitions()

        self.assertEqual([item.slug for item in competitions], ["general-competition", "community-competition"])
        self.assertEqual({call["group"] for call in calls}, {"general", "community"})
        self.assertTrue(all(call["group"] not in {"entered", "hosted", "unlaunched"} for call in calls))
        self.assertTrue(all(call["page"] == -1 for call in calls))
        self.assertTrue(all("page_token" in call for call in calls))
        self.assertTrue(all(item.awards_points for item in competitions))

    def test_catalog_follows_page_tokens_and_deduplicates(self) -> None:
        calls = []

        def item(slug):
            return SimpleNamespace(
                ref=f"https://www.kaggle.com/competitions/{slug}",
                title=slug,
                category="Featured",
                reward="",
                deadline=datetime(2026, 1, 1),
                team_count=10,
                awards_points=False,
            )

        class FakeApi:
            def competitions_list(self, **kwargs):
                calls.append(kwargs)
                if kwargs["group"] == "general" and kwargs["page_token"] is None:
                    return SimpleNamespace(
                        competitions=[item("shared"), item("general-first")],
                        next_page_token="general-next",
                    )
                if kwargs["group"] == "general":
                    return SimpleNamespace(
                        competitions=[item("shared"), item("general-second")],
                        next_page_token="",
                    )
                return SimpleNamespace(
                    competitions=[item("shared"), item("community-only")],
                    next_page_token="",
                )

        competitions = KaggleCompetitionSource(
            FakeApi(), retry_attempts=1, min_request_interval_seconds=0.000001
        ).list_competitions()

        self.assertEqual(
            [competition.slug for competition in competitions],
            ["shared", "general-first", "general-second", "community-only"],
        )
        self.assertEqual(len(calls), 3)

    def test_aggregated_source_adds_entered_competitions_and_routes_account_access(self) -> None:
        public_competition = self._competition(
            datetime(2026, 8, 1, tzinfo=timezone.utc)
        )
        entered_competition = Competition(
            slug="ended-entered",
            title="Ended entered competition",
            url="https://www.kaggle.com/competitions/ended-entered",
            category="Featured",
            reward="",
            deadline=datetime(2026, 1, 1, tzinfo=timezone.utc),
            api_team_count=20,
        )
        primary_api = SimpleNamespace(name="primary")
        entered_api = SimpleNamespace(name="entered")
        calls: list[tuple[str, str]] = []

        class FakeCompetitionSource:
            def __init__(self, api, **kwargs):
                self.api = api

            def list_competitions(self, max_competitions=None):
                return [public_competition] if self.api is primary_api else []

            def get_leaderboard(self, competition, normalized_teams):
                calls.append((self.api.name, competition.slug))
                if self.api is primary_api and competition.slug == "ended-entered":
                    raise RuntimeError("primary account cannot access ended competition")
                return LeaderboardSnapshot(team_count=20, kind="private", matches=())

        with patch(
            "agentkaggle_leaderboard.kaggle_source.KaggleCompetitionSource",
            FakeCompetitionSource,
        ):
            source = KaggleAggregatedCompetitionSource(
                primary_api,
                [(entered_competition, entered_api)],
                min_request_interval_seconds=0.000001,
            )
            competitions = source.list_competitions()
            snapshot = source.get_leaderboard(entered_competition, {"alpha": "Alpha"})

        self.assertEqual(
            [competition.slug for competition in competitions],
            ["safe-test", "ended-entered"],
        )
        self.assertEqual(snapshot.kind, "private")
        self.assertEqual(calls, [("entered", "ended-entered")])

    def test_repeated_catalog_page_token_is_rejected(self) -> None:
        class FakeApi:
            def competitions_list(self, **kwargs):
                return SimpleNamespace(competitions=[], next_page_token="repeated")

        source = KaggleCompetitionSource(
            FakeApi(), retry_attempts=1, min_request_interval_seconds=0.000001
        )
        with self.assertRaises(InvalidKaggleResponse):
            source.list_competitions()

    def test_retry_after_updates_the_shared_cooldown(self) -> None:
        source = KaggleCompetitionSource(
            SimpleNamespace(), retry_attempts=2, min_request_interval_seconds=0.000001
        )
        retryable = RuntimeError("rate limited")
        retryable.response = SimpleNamespace(status_code=429, headers={"Retry-After": "3"})
        calls = iter((retryable, "ok"))

        def operation():
            result = next(calls)
            if isinstance(result, BaseException):
                raise result
            return result

        with patch.object(source, "_postpone_requests") as postpone:
            self.assertEqual(source._call_with_retry(operation), "ok")
        postpone.assert_called_once_with(3.0)

    def test_http_date_retry_after_is_supported(self) -> None:
        source = KaggleCompetitionSource(
            SimpleNamespace(), retry_attempts=2, min_request_interval_seconds=0.000001
        )
        retryable = RuntimeError("rate limited")
        retryable.response = SimpleNamespace(
            status_code=429,
            headers={"Retry-After": "Fri, 17 Jul 2026 00:00:07 GMT"},
        )
        calls = iter((retryable, "ok"))

        def operation():
            result = next(calls)
            if isinstance(result, BaseException):
                raise result
            return result

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, tzinfo=tz)

        with (
            patch("agentkaggle_leaderboard.kaggle_source.datetime", FixedDateTime),
            patch.object(source, "_postpone_requests") as postpone,
        ):
            self.assertEqual(source._call_with_retry(operation), "ok")
        postpone.assert_called_once_with(7.0)

    def test_competition_slug_accepts_only_safe_kaggle_refs(self) -> None:
        self.assertEqual(
            competition_slug("https://www.kaggle.com/competitions/house-prices"),
            "house-prices",
        )
        with self.assertRaises(RuntimeError):
            competition_slug("https://www.kaggle.com/competitions/../secret")

    def test_unknown_leaderboard_kind_is_rejected(self) -> None:
        snapshot = LeaderboardSnapshot(team_count=10, kind="unknown", matches=())
        with self.assertRaises(InvalidKaggleResponse):
            validate_leaderboard_visibility(snapshot, self._competition(None))

    def test_active_private_leaderboard_is_rejected(self) -> None:
        snapshot = LeaderboardSnapshot(team_count=10, kind="private", matches=())
        current_time = datetime(2026, 7, 17, tzinfo=timezone.utc)
        with self.assertRaises(UnsafePrivateLeaderboard):
            validate_leaderboard_visibility(
                snapshot,
                self._competition(datetime(2026, 8, 1, tzinfo=timezone.utc)),
                now=current_time,
            )

    def test_ended_private_leaderboard_is_allowed(self) -> None:
        snapshot = LeaderboardSnapshot(team_count=10, kind="private", matches=())
        validate_leaderboard_visibility(
            snapshot,
            self._competition(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            now=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )

    def test_archive_reader_whitelists_fields_and_matches_normalized_names(self) -> None:
        rows = [
            {
                "Rank": "0",
                "TeamId": "0",
                "TeamName": "Alpha",
                "LastSubmissionDate": "2026-01-01T00:00:00Z",
                "Score": "1.000",
                "TeamMemberUserNames": "benchmark-not-exported",
            },
            {
                "Rank": "1",
                "TeamId": "10",
                "TeamName": "Other",
                "LastSubmissionDate": "2026-01-01T00:00:00Z",
                "Score": "0.999",
                "TeamMemberUserNames": "not-exported",
            },
            {
                "Rank": "2",
                "TeamId": "11",
                "TeamName": " ＡLPHA ",
                "LastSubmissionDate": "2026-01-02T00:00:00Z",
                "Score": "0.876543210",
                "TeamMemberUserNames": "private-field",
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir, "board.csv")
            with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            archive_path = Path(temp_dir, "sample.zip")
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(csv_path, "sample-privateleaderboard.csv")

            snapshot = KaggleCompetitionSource._read_archive(archive_path, {"alpha": "Alpha"})

        self.assertEqual(snapshot.team_count, 2)
        self.assertEqual(snapshot.kind, "private")
        self.assertEqual(len(snapshot.matches), 1)
        self.assertEqual(snapshot.matches[0].configured_team_name, "Alpha")
        self.assertEqual(snapshot.matches[0].rank, 2)
        self.assertEqual(snapshot.matches[0].score, "0.876543210")
        self.assertNotIn("private-field", repr(snapshot))

    def test_archive_reader_uses_best_rank_and_counts_each_team_once(self) -> None:
        rows = [
            {
                "Rank": "8",
                "TeamId": "11",
                "TeamName": "Alpha",
                "Score": "0.8",
            },
            {
                "Rank": "2",
                "TeamId": "11",
                "TeamName": "Alpha",
                "Score": "0.2",
            },
            {
                "Rank": "1",
                "TeamId": "12",
                "TeamName": "Other",
                "Score": "0.1",
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir, "board.csv")
            with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            archive_path = Path(temp_dir, "sample.zip")
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(csv_path, "sample-publicleaderboard.csv")

            snapshot = KaggleCompetitionSource._read_archive(
                archive_path, {"alpha": "Alpha"}
            )

        self.assertEqual(snapshot.team_count, 2)
        self.assertEqual(len(snapshot.matches), 1)
        self.assertEqual(snapshot.matches[0].rank, 2)
        self.assertEqual(snapshot.matches[0].score, "0.2")
        self.assertEqual(snapshot.score_order, "lower")


if __name__ == "__main__":
    unittest.main()
