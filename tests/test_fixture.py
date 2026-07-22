from __future__ import annotations

import json
import unittest
from pathlib import Path
from statistics import fmean

from agentkaggle_leaderboard.medals import medal_candidate
from agentkaggle_leaderboard.output import validate_public_payload


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "leaderboard.json"


class FixtureConsistencyTests(unittest.TestCase):
    def test_fixture_metrics_are_internally_consistent(self) -> None:
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        validate_public_payload(payload)

        competitions = payload["competitions"]
        entries = [entry for competition in competitions for entry in competition["entries"]]
        late_submissions = payload["late_submissions"]
        summary = payload["summary"]
        self.assertEqual(summary["matched_competition_count"], len(competitions))
        self.assertEqual(summary["participation_count"], len(entries))
        self.assertEqual(summary["tracked_team_count"], len(payload["teams"]))
        self.assertEqual(summary["late_submission_count"], len(late_submissions))
        self.assertEqual(
            summary["late_submission_competition_count"],
            len({entry["competition_slug"] for entry in late_submissions}),
        )
        self.assertTrue(all(competition["entries"] for competition in competitions))

        for competition in competitions:
            team_count = competition["leaderboard_team_count"]
            for entry in competition["entries"]:
                self.assertEqual(entry["top_percent"], round(entry["rank"] / team_count * 100, 4))
                expected_medal = (
                    medal_candidate(entry["rank"], team_count)
                    if competition["awards_points"]
                    else "not_eligible"
                )
                self.assertEqual(entry["medal_candidate"], expected_medal)

        for team in payload["teams"]:
            team_entries = [entry for entry in entries if entry["team_name"] == team["name"]]
            team_late_submissions = [
                entry for entry in late_submissions if entry["team_name"] == team["name"]
            ]
            official_slugs = {
                competition["slug"]
                for competition in competitions
                if any(entry["team_name"] == team["name"] for entry in competition["entries"])
            }
            late_slugs = {
                entry["competition_slug"] for entry in team_late_submissions
            }
            self.assertEqual(
                team["competition_count"],
                len(official_slugs | late_slugs),
            )
            self.assertEqual(
                team["best_rank"],
                min((entry["rank"] for entry in team_entries), default=None),
            )
            self.assertEqual(
                team["average_top_percent"],
                round(fmean(entry["top_percent"] for entry in team_entries), 4)
                if team_entries
                else None,
            )
            self.assertEqual(
                team["medal_candidate_count"],
                sum(
                    entry["medal_candidate"] in {"gold", "silver", "bronze"}
                    for entry in team_entries
                ),
            )
            self.assertEqual(team["late_submission_count"], len(team_late_submissions))


if __name__ == "__main__":
    unittest.main()
