from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from agentkaggle_leaderboard.builder import _safe_failure_kind, build_leaderboard
from agentkaggle_leaderboard.kaggle_source import (
    InvalidKaggleResponse,
    KaggleAuthenticationError,
)
from agentkaggle_leaderboard.models import (
    Competition,
    LateSubmissionEntry,
    LeaderboardEntry,
    LeaderboardSnapshot,
)
from agentkaggle_leaderboard.output import validate_public_payload
from agentkaggle_leaderboard.settings import Settings


class FakeSource:
    def __init__(self) -> None:
        self.competitions = [
            Competition(
                slug="active-comp",
                title="Active competition",
                url="https://www.kaggle.com/competitions/active-comp",
                category="Featured",
                reward="$10,000",
                deadline=datetime(2026, 8, 1, tzinfo=timezone.utc),
                api_team_count=500,
                awards_points=True,
            ),
            Competition(
                slug="no-match",
                title="No match",
                url="https://www.kaggle.com/competitions/no-match",
                category="Playground",
                reward="Swag",
                deadline=datetime(2026, 1, 1, tzinfo=timezone.utc),
                api_team_count=20,
            ),
            Competition(
                slug="blocked",
                title="Blocked",
                url="https://www.kaggle.com/competitions/blocked",
                category="Research",
                reward="",
                deadline=None,
                api_team_count=100,
            ),
        ]

    def list_competitions(self, max_competitions: int | None = None) -> list[Competition]:
        return self.competitions[:max_competitions]

    def get_leaderboard(self, competition, normalized_teams):
        if competition.slug == "blocked":
            error = RuntimeError("sensitive upstream text")
            error.response = type("Response", (), {"status_code": 403})()
            raise error
        if competition.slug == "no-match":
            return LeaderboardSnapshot(team_count=20, kind="private", matches=())
        return LeaderboardSnapshot(
            team_count=500,
            kind="public",
            matches=(
                LeaderboardEntry("Alpha", 11, "0.123456789", "2026-07-01T00:00:00Z"),
            ),
        )


class BuilderTests(unittest.TestCase):
    def test_builds_sanitized_partial_dashboard_payload(self) -> None:
        payload = build_leaderboard(
            FakeSource(),
            Settings(("Alpha", "Beta"), workers=2),
            generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )

        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["summary"]["discovered_competition_count"], 3)
        self.assertEqual(payload["summary"]["scanned_competition_count"], 2)
        self.assertEqual(payload["summary"]["matched_competition_count"], 1)
        self.assertEqual(payload["summary"]["error_counts"], {"access_denied": 1})

        competition = payload["competitions"][0]
        entry = competition["entries"][0]
        self.assertEqual(competition["leaderboard_team_count"], 500)
        self.assertEqual(entry["rank"], 11)
        self.assertEqual(entry["top_percent"], 2.2)
        self.assertEqual(entry["score"], "0.123456789")
        self.assertEqual(entry["medal_candidate"], "gold")

        serialized = json.dumps(payload)
        self.assertNotIn("sensitive upstream text", serialized)
        self.assertNotIn("TeamMemberUserNames", serialized)
        self.assertEqual(payload["teams"][1]["competition_count"], 0)
        validate_public_payload(payload)

    def test_public_schema_rejects_raw_fields(self) -> None:
        payload = build_leaderboard(
            FakeSource(),
            Settings(("Alpha",), workers=1),
            max_competitions=1,
            generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )
        payload["competitions"][0]["TeamMemberUserNames"] = "must never be published"
        with self.assertRaisesRegex(ValueError, "unexpected or missing fields"):
            validate_public_payload(payload)

    def test_each_competition_uses_only_the_best_rank_per_team(self) -> None:
        class DuplicateTeamSource(FakeSource):
            def list_competitions(self, max_competitions=None):
                return self.competitions[:1]

            def get_leaderboard(self, competition, normalized_teams):
                return LeaderboardSnapshot(
                    team_count=100,
                    kind="public",
                    matches=(
                        LeaderboardEntry("Alpha", 25, "0.80", "2026-07-01T00:00:00Z"),
                        LeaderboardEntry("Alpha", 5, "0.95", "2026-07-02T00:00:00Z"),
                    ),
                )

        payload = build_leaderboard(
            DuplicateTeamSource(),
            Settings(("Alpha",), workers=1),
            generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )

        entries = payload["competitions"][0]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["rank"], 5)
        self.assertEqual(entries[0]["top_percent"], 5.0)
        self.assertEqual(payload["teams"][0]["competition_count"], 1)
        self.assertEqual(payload["teams"][0]["average_top_percent"], 5.0)

    def test_late_submissions_are_sanitized_deduplicated_and_counted(self) -> None:
        late_entry = LateSubmissionEntry(
            competition_slug="ended-comp",
            competition_title="Ended competition",
            competition_url="https://www.kaggle.com/competitions/ended-comp",
            deadline=datetime(2026, 6, 1, tzinfo=timezone.utc),
            configured_team_name="Beta",
            public_score="0.91",
            private_score="0.87",
            submission_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        payload = build_leaderboard(
            FakeSource(),
            Settings(("Alpha", "Beta"), workers=2),
            generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            late_submissions=(late_entry, late_entry),
            late_submission_account_count=2,
            late_submission_failure_kinds=("access_denied",),
        )

        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["summary"]["late_submission_account_count"], 2)
        self.assertEqual(payload["summary"]["failed_late_submission_account_count"], 1)
        self.assertEqual(payload["summary"]["late_submission_competition_count"], 1)
        self.assertEqual(payload["summary"]["late_submission_count"], 1)
        self.assertEqual(
            payload["summary"]["late_submission_error_counts"],
            {"access_denied": 1},
        )
        beta_summary = next(team for team in payload["teams"] if team["name"] == "Beta")
        self.assertEqual(beta_summary["competition_count"], 1)
        self.assertEqual(beta_summary["late_submission_count"], 1)
        late_competition = next(
            competition
            for competition in payload["competitions"]
            if competition["slug"] == "ended-comp"
        )
        self.assertEqual(late_competition["leaderboard_kind"], "unavailable")
        self.assertEqual(late_competition["leaderboard_team_count"], 0)
        self.assertEqual(len(late_competition["entries"]), 1)
        late_competition_entry = late_competition["entries"][0]
        self.assertIsNone(late_competition_entry["rank"])
        self.assertIsNone(late_competition_entry["top_percent"])
        self.assertEqual(late_competition_entry["late_public_score"], "0.91")
        self.assertEqual(late_competition_entry["late_private_score"], "0.87")
        self.assertEqual(
            set(payload["late_submissions"][0]),
            {
                "competition_slug",
                "competition_title",
                "competition_url",
                "deadline",
                "team_name",
                "public_score",
                "private_score",
                "submission_date",
            },
        )
        validate_public_payload(payload)

    def test_late_submissions_keep_the_best_score_per_team_and_competition(self) -> None:
        class LowerIsBetterSource:
            competition = Competition(
                slug="ended-comp",
                title="Ended competition",
                url="https://www.kaggle.com/competitions/ended-comp",
                category="Featured",
                reward="",
                deadline=datetime(2026, 6, 1, tzinfo=timezone.utc),
                api_team_count=100,
            )

            def list_competitions(self, max_competitions=None):
                return [self.competition]

            def get_leaderboard(self, competition, normalized_teams):
                return LeaderboardSnapshot(
                    team_count=100,
                    kind="private",
                    matches=(
                        LeaderboardEntry(
                            "Alpha", 10, "0.20", "2026-06-01T00:00:00Z"
                        ),
                    ),
                    score_order="lower",
                )

        better_older = LateSubmissionEntry(
            competition_slug="ended-comp",
            competition_title="Ended competition",
            competition_url="https://www.kaggle.com/competitions/ended-comp",
            deadline=datetime(2026, 6, 1, tzinfo=timezone.utc),
            configured_team_name="Alpha",
            public_score="0.15",
            private_score="0.10",
            submission_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        worse_newer = LateSubmissionEntry(
            competition_slug="ended-comp",
            competition_title="Ended competition",
            competition_url="https://www.kaggle.com/competitions/ended-comp",
            deadline=datetime(2026, 6, 1, tzinfo=timezone.utc),
            configured_team_name="Alpha",
            public_score="0.25",
            private_score="0.30",
            submission_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        )

        payload = build_leaderboard(
            LowerIsBetterSource(),
            Settings(("Alpha",), workers=1),
            generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            late_submissions=(better_older, worse_newer),
            late_submission_account_count=1,
        )

        self.assertEqual(payload["summary"]["late_submission_count"], 1)
        self.assertEqual(payload["late_submissions"][0]["private_score"], "0.10")
        competition_entry = payload["competitions"][0]["entries"][0]
        self.assertEqual(competition_entry["rank"], 10)
        self.assertEqual(competition_entry["score"], "0.20")
        self.assertEqual(competition_entry["late_public_score"], "0.15")
        self.assertEqual(competition_entry["late_private_score"], "0.10")
        self.assertEqual(
            competition_entry["late_submission_date"],
            "2026-07-01T00:00:00Z",
        )
        self.assertEqual(payload["teams"][0]["competition_count"], 1)

    def test_late_only_team_result_keeps_known_competition_metadata(self) -> None:
        late_entry = LateSubmissionEntry(
            competition_slug="no-match",
            competition_title="No match",
            competition_url="https://www.kaggle.com/competitions/no-match",
            deadline=datetime(2026, 1, 1, tzinfo=timezone.utc),
            configured_team_name="Beta",
            public_score="114302",
            private_score="114302",
            submission_date=datetime(2026, 7, 11, tzinfo=timezone.utc),
        )

        payload = build_leaderboard(
            FakeSource(),
            Settings(("Alpha", "Beta"), workers=2),
            generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            late_submissions=(late_entry,),
            late_submission_account_count=1,
        )

        competition = next(
            item for item in payload["competitions"] if item["slug"] == "no-match"
        )
        self.assertEqual(competition["category"], "Playground")
        self.assertEqual(competition["leaderboard_kind"], "private")
        self.assertEqual(competition["leaderboard_team_count"], 20)
        self.assertEqual(competition["entries"][0]["team_name"], "Beta")
        self.assertIsNone(competition["entries"][0]["rank"])
        self.assertEqual(competition["entries"][0]["late_public_score"], "114302")

    def test_public_schema_rejects_unapproved_error_categories(self) -> None:
        payload = build_leaderboard(
            FakeSource(),
            Settings(("Alpha",), workers=1),
            generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )
        payload["summary"]["error_counts"] = {"SensitiveInternalException": 1}
        with self.assertRaisesRegex(ValueError, "unsupported error category"):
            validate_public_payload(payload)

    def test_failure_kinds_are_fixed_public_categories(self) -> None:
        self.assertEqual(
            _safe_failure_kind(InvalidKaggleResponse("raw upstream detail")),
            "invalid_response",
        )
        sensitive_exception = type("SensitiveInternalException", (RuntimeError,), {})
        self.assertEqual(_safe_failure_kind(sensitive_exception("raw detail")), "unexpected")
        self.assertEqual(
            _safe_failure_kind(KaggleAuthenticationError("raw auth detail")),
            "access_denied",
        )

    def test_max_competitions_marks_result_truncated(self) -> None:
        payload = build_leaderboard(
            FakeSource(),
            Settings(("Alpha",), workers=1),
            max_competitions=1,
            generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )
        self.assertEqual(payload["status"], "partial")
        self.assertTrue(payload["summary"]["truncated"])

    def test_severely_degraded_scan_is_rejected(self) -> None:
        class MostlyFailingSource:
            def list_competitions(self, max_competitions=None):
                return [
                    Competition(
                        slug=f"competition-{index}",
                        title=f"Competition {index}",
                        url=f"https://www.kaggle.com/competitions/competition-{index}",
                        category="Featured",
                        reward="",
                        deadline=None,
                        api_team_count=10,
                    )
                    for index in range(4)
                ]

            def get_leaderboard(self, competition, normalized_teams):
                if competition.slug != "competition-0":
                    raise RuntimeError("upstream failure")
                return LeaderboardSnapshot(team_count=10, kind="public", matches=())

        with self.assertRaisesRegex(RuntimeError, "too degraded"):
            build_leaderboard(
                MostlyFailingSource(),
                Settings(("Alpha",), workers=2),
                generated_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
