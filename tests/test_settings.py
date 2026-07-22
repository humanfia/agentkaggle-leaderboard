import unittest
from unittest.mock import patch

from agentkaggle_leaderboard.settings import (
    ConfigurationError,
    Settings,
    merge_team_names,
    normalize_team_name,
    parse_api_tokens,
    parse_team_names,
)


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

    def test_api_tokens_merge_single_and_json_values_without_duplicates(self) -> None:
        self.assertEqual(
            parse_api_tokens(" primary-token ", '["secondary-token", "primary-token"]'),
            ("primary-token", "secondary-token"),
        )

    def test_api_tokens_require_a_json_string_array(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "array of strings"):
            parse_api_tokens(None, '{"token": "secret"}')

    def test_settings_load_multiple_api_tokens_from_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KAGGLE_TEAMS": '["Alpha"]',
                "KAGGLE_API_TOKENS": '["token-a", "token-b"]',
            },
            clear=True,
        ):
            settings = Settings.from_environment(load_local_dotenv=False)

        self.assertEqual(settings.api_tokens, ("token-a", "token-b"))
        self.assertEqual(settings.team_discovery_api_tokens, ("token-a", "token-b"))
        self.assertNotIn("token-a", repr(settings))

    def test_auto_discovery_allows_an_empty_manual_team_list(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KAGGLE_API_TOKEN": "primary-token",
                "KAGGLE_API_TOKENS": '["contributor-token"]',
                "KAGGLE_AUTO_DISCOVER_TEAMS": "true",
            },
            clear=True,
        ):
            settings = Settings.from_environment(load_local_dotenv=False)

        self.assertEqual(settings.teams, ())
        self.assertTrue(settings.auto_discover_teams)
        self.assertEqual(settings.api_tokens, ("primary-token", "contributor-token"))
        self.assertEqual(settings.team_discovery_api_tokens, ("contributor-token",))

    def test_empty_manual_team_list_requires_auto_discovery(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"KAGGLE_API_TOKEN": "primary-token"},
                clear=True,
            ),
            self.assertRaisesRegex(ConfigurationError, "AUTO_DISCOVER"),
        ):
            Settings.from_environment(load_local_dotenv=False)

    def test_team_merge_preserves_configured_spelling_and_adds_discovered_names(self) -> None:
        self.assertEqual(
            merge_team_names(("Alpha",), [" ＡLPHA ", "Dynamic Team", "dynamic team"]),
            ("Alpha", "Dynamic Team"),
        )


if __name__ == "__main__":
    unittest.main()
