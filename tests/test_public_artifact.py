from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_public_artifact import scan


class PublicArtifactTests(unittest.TestCase):
    def test_clean_artifact_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir, "index.html")
            artifact.write_text("<h1>Public team leaderboard</h1>", encoding="utf-8")
            self.assertEqual(scan([Path(temp_dir)], "test-token-123456"), [])

    def test_token_and_raw_kaggle_fields_are_detected_without_echoing_token(self) -> None:
        token = "test-token-123456"
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir, "index.html")
            artifact.write_text(f"{token} TeamMemberUserNames", encoding="utf-8")
            findings = scan([artifact], token)
        rendered = "\n".join(findings)
        self.assertIn("credential value found", rendered)
        self.assertIn("forbidden field marker found", rendered)
        self.assertNotIn(token, rendered)

    def test_every_token_in_a_multi_account_secret_is_detected(self) -> None:
        tokens = ("first-token-123456", "second-token-123456")
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir, "index.html")
            artifact.write_text(f"public {tokens[1]}", encoding="utf-8")
            findings = scan([artifact], tokens)

        rendered = "\n".join(findings)
        self.assertIn("credential value found", rendered)
        self.assertNotIn(tokens[0], rendered)
        self.assertNotIn(tokens[1], rendered)

    def test_unknown_file_type_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir, "renamed-archive.bin")
            artifact.write_bytes(b"not a public text artifact")
            findings = scan([Path(temp_dir)])
        self.assertTrue(any("unsupported artifact type" in finding for finding in findings))

    def test_symbolic_link_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir, "index.html")
            target.write_text("safe", encoding="utf-8")
            link = Path(temp_dir, "linked.html")
            link.symlink_to(target)
            findings = scan([Path(temp_dir)])
        self.assertTrue(any("symbolic link is not allowed" in finding for finding in findings))


if __name__ == "__main__":
    unittest.main()
