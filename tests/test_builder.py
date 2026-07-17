from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from agentkaggle_leaderboard.builder import build_leaderboard
from agentkaggle_leaderboard.models import Competition, LeaderboardEntry, LeaderboardSnapshot
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
