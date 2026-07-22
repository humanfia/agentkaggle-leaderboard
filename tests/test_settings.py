import unittest
from unittest.mock import patch

from agentkaggle_leaderboard.settings import (
    ConfigurationError,
    LegacyKaggleCredential,
    Settings,
    credential_secret_values,
    merge_team_names,
    normalize_team_name,
    parse_api_tokens,
    parse_legacy_credentials,
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

    def test_legacy_credentials_require_username_key_objects(self) -> None:
        credentials = parse_legacy_credentials(
            '[{"username":"apostle715","key":"legacy-key"}]'
        )

        self.assertEqual(
            credentials,
            (LegacyKaggleCredential("apostle715", "legacy-key"),),
        )
        self.assertEqual(credential_secret_values(credentials[0]), ("legacy-key",))
        self.assertNotIn("apostle715", repr(credentials[0]))
        self.assertNotIn("legacy-key", repr(credentials[0]))

    def test_legacy_credentials_reject_extra_fields_without_echoing_values(self) -> None:
        secret = "legacy-key-not-for-errors"
        with self.assertRaises(ConfigurationError) as raised:
            parse_legacy_credentials(
                '[{"username":"apostle715","key":"'
                + secret
                + '","token":"unexpected"}]'
            )

        self.assertNotIn(secret, str(raised.exception))

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

    def test_settings_unify_modern_and_legacy_contributor_credentials(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KAGGLE_TEAMS": '["Alpha"]',
                "KAGGLE_API_TOKEN": "primary-token",
                "KAGGLE_API_TOKENS": '["modern-contributor"]',
                "KAGGLE_LEGACY_CREDENTIALS": (
                    '[{"username":"apostle715","key":"legacy-key"}]'
                ),
            },
            clear=True,
        ):
            settings = Settings.from_environment(load_local_dotenv=False)

        legacy = LegacyKaggleCredential("apostle715", "legacy-key")
        self.assertEqual(
            settings.api_tokens,
            ("primary-token", "modern-contributor", legacy),
        )
        self.assertEqual(
            settings.team_discovery_api_tokens,
            ("modern-contributor", legacy),
        )
        self.assertNotIn("legacy-key", repr(settings))

    def test_legacy_credentials_can_be_the_only_authentication_source(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "KAGGLE_TEAMS": '["Alpha"]',
                "KAGGLE_API_TOKENS": "[]",
                "KAGGLE_LEGACY_CREDENTIALS": (
                    '[{"username":"apostle715","key":"legacy-key"}]'
                ),
            },
            clear=True,
        ):
            settings = Settings.from_environment(load_local_dotenv=False)

        self.assertEqual(
            settings.api_tokens,
            (LegacyKaggleCredential("apostle715", "legacy-key"),),
        )

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
