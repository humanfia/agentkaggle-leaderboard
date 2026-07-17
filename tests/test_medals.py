import unittest

from agentkaggle_leaderboard.medals import medal_candidate, medal_cutoffs


class MedalTests(unittest.TestCase):
    def test_cutoffs_follow_all_four_team_count_bands(self) -> None:
        self.assertEqual(medal_cutoffs(9), {"gold": 0, "silver": 1, "bronze": 3})
        self.assertEqual(medal_cutoffs(50), {"gold": 5, "silver": 10, "bronze": 20})
        self.assertEqual(medal_cutoffs(100), {"gold": 10, "silver": 20, "bronze": 40})
        self.assertEqual(medal_cutoffs(500), {"gold": 11, "silver": 50, "bronze": 100})
        self.assertEqual(medal_cutoffs(1234), {"gold": 12, "silver": 61, "bronze": 123})

    def test_candidate_uses_exclusive_best_medal(self) -> None:
        self.assertEqual(medal_candidate(11, 500), "gold")
        self.assertEqual(medal_candidate(12, 500), "silver")
        self.assertEqual(medal_candidate(100, 500), "bronze")
        self.assertEqual(medal_candidate(101, 500), "none")

    def test_band_boundaries_are_explicit(self) -> None:
        self.assertEqual(medal_cutoffs(99), {"gold": 9, "silver": 19, "bronze": 39})
        self.assertEqual(medal_cutoffs(100), {"gold": 10, "silver": 20, "bronze": 40})
        self.assertEqual(medal_cutoffs(249), {"gold": 10, "silver": 49, "bronze": 99})
        self.assertEqual(medal_cutoffs(250), {"gold": 10, "silver": 50, "bronze": 100})
        self.assertEqual(medal_cutoffs(999), {"gold": 11, "silver": 50, "bronze": 100})
        self.assertEqual(medal_cutoffs(1000), {"gold": 12, "silver": 50, "bronze": 100})

    def test_zero_teams_is_invalid(self) -> None:
        with self.assertRaises(ValueError):
            medal_cutoffs(0)

    def test_small_competition_can_have_no_gold_zone(self) -> None:
        self.assertEqual(medal_candidate(1, 9), "silver")


if __name__ == "__main__":
    unittest.main()
