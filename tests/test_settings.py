import unittest

from agentkaggle_leaderboard.settings import ConfigurationError, normalize_team_name, parse_team_names


class SettingsTests(unittest.TestCase):
    def test_json_array_preserves_commas_in_names(self) -> None:
        self.assertEqual(parse_team_names('["Alpha, Inc.", "Beta"]'), ("Alpha, Inc.", "Beta"))

    def test_comma_and_newline_formats(self) -> None:
        self.assertEqual(parse_team_names(" Alpha,\nBeta "), ("Alpha", "Beta"))

    def test_normalization_is_unicode_and_case_insensitive(self) -> None:
        self.assertEqual(normalize_team_name("  Ａlpha  "), "alpha")

    def test_duplicate_normalized_names_are_rejected_without_values(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "duplicate names"):
            parse_team_names('["Alpha", " alpha "]')

    def test_empty_configuration_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            parse_team_names("  ")


if __name__ == "__main__":
    unittest.main()

