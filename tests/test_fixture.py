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
        self.assertEqual(
            {team["name"] for team in payload["teams"]},
            {team["name"] for team in payload["late_teams"]},
        )
        self.assertEqual(
            {team["name"] for team in payload["teams"]},
            {team["name"] for team in payload["ongoing_teams"]},
        )
        self.assertEqual(summary["late_submission_count"], len(late_submissions))
        self.assertEqual(
            summary["late_submission_competition_count"],
            len({entry["competition_slug"] for entry in late_submissions}),
        )
        self.assertTrue(all(competition["entries"] for competition in competitions))

        for competition in competitions:
            team_count = competition["leaderboard_team_count"]
            for entry in competition["entries"]:
                if entry["late_rank"] is not None:
                    self.assertEqual(
                        entry["late_top_percent"],
                        round(
                            entry["late_rank"] / entry["late_rank_team_count"] * 100,
                            4,
                        ),
                    )
                if entry["rank"] is None:
                    self.assertIsNone(entry["top_percent"])
                    self.assertEqual(entry["score"], "")
                    self.assertEqual(entry["submission_date"], "")
                    self.assertEqual(entry["medal_candidate"], "unavailable")
                    continue
                self.assertEqual(entry["top_percent"], round(entry["rank"] / team_count * 100, 4))
                expected_medal = (
                    medal_candidate(entry["rank"], team_count)
                    if competition["awards_points"]
                    else "not_eligible"
                )
                self.assertEqual(entry["medal_candidate"], expected_medal)

        for leaderboard_name, mode in (
            ("teams", "overall"),
            ("late_teams", "late"),
            ("ongoing_teams", "ongoing"),
        ):
            leaderboard = payload[leaderboard_name]
            positions = [team["position"] for team in leaderboard if team["position"] is not None]
            self.assertEqual(positions, list(range(1, len(positions) + 1)))
            for team in leaderboard:
                selected_results = []
                competition_count = 0
                official_medals = 0
                for competition in competitions:
                    entry = next(
                        (
                            item
                            for item in competition["entries"]
                            if item["team_name"] == team["name"]
                        ),
                        None,
                    )
                    if entry is None:
                        continue
                    official_result = (
                        (entry["rank"], entry["top_percent"])
                        if entry["rank"] is not None
                        else None
                    )
                    late_result = (
                        (entry["late_rank"], entry["late_top_percent"])
                        if entry["late_rank"] is not None
                        else None
                    )
                    official_medals += entry["medal_candidate"] in {
                        "gold",
                        "silver",
                        "bronze",
                    }
                    if mode == "ongoing":
                        selected = official_result
                        has_result = official_result is not None
                    elif mode == "late":
                        selected = late_result
                        has_result = bool(entry["late_submission_date"])
                    else:
                        selected = min(
                            [
                                result
                                for result in (official_result, late_result)
                                if result is not None
                            ],
                            key=lambda result: (result[1], result[0]),
                            default=None,
                        )
                        has_result = official_result is not None or bool(
                            entry["late_submission_date"]
                        )
                    competition_count += has_result
                    if selected is not None:
                        selected_results.append(selected)

                team_late_submissions = [
                    entry
                    for entry in late_submissions
                    if entry["team_name"] == team["name"]
                ]
                self.assertEqual(team["competition_count"], competition_count)
                self.assertEqual(
                    team["best_rank"],
                    min((result[0] for result in selected_results), default=None),
                )
                self.assertEqual(
                    team["average_top_percent"],
                    round(fmean(result[1] for result in selected_results), 4)
                    if selected_results
                    else None,
                )
                self.assertEqual(
                    team["medal_candidate_count"],
                    official_medals if mode in {"overall", "ongoing"} else 0,
                )
                self.assertEqual(
                    team["late_submission_count"],
                    len(team_late_submissions) if mode in {"overall", "late"} else 0,
                )

        for late_submission in late_submissions:
            competition = next(
                item
                for item in competitions
                if item["slug"] == late_submission["competition_slug"]
            )
            entry = next(
                item
                for item in competition["entries"]
                if item["team_name"] == late_submission["team_name"]
            )
            self.assertEqual(entry["late_public_score"], late_submission["public_score"])
            self.assertEqual(entry["late_private_score"], late_submission["private_score"])
            self.assertEqual(entry["late_submission_date"], late_submission["submission_date"])


if __name__ == "__main__":
    unittest.main()
