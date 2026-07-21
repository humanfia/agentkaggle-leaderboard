from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


PUBLIC_KEYS = {
    "top": {
        "schema_version",
        "generated_at",
        "status",
        "summary",
        "teams",
        "competitions",
        "late_submissions",
        "methodology",
    },
    "summary": {
        "tracked_team_count",
        "discovered_competition_count",
        "scanned_competition_count",
        "failed_competition_count",
        "matched_competition_count",
        "participation_count",
        "late_submission_account_count",
        "failed_late_submission_account_count",
        "late_submission_competition_count",
        "late_submission_count",
        "late_submission_error_counts",
        "truncated",
        "error_counts",
    },
    "team": {
        "name",
        "competition_count",
        "best_rank",
        "average_top_percent",
        "medal_candidate_count",
        "late_submission_count",
    },
    "competition": {
        "slug",
        "title",
        "url",
        "category",
        "reward",
        "deadline",
        "state",
        "leaderboard_kind",
        "leaderboard_team_count",
        "api_team_count",
        "awards_points",
        "entries",
    },
    "entry": {"team_name", "rank", "top_percent", "score", "submission_date", "medal_candidate"},
    "late_submission": {
        "competition_slug",
        "competition_title",
        "competition_url",
        "deadline",
        "team_name",
        "public_score",
        "private_score",
        "submission_date",
    },
    "methodology": {"rank", "top_percent", "score", "late_submission", "medal_candidate"},
}
PUBLIC_ERROR_KINDS = {
    "access_denied",
    "not_found",
    "rate_limited",
    "network",
    "unsafe_private_leaderboard",
    "invalid_response",
    "unexpected",
}


def _require_exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    if set(value) != expected:
        raise ValueError(f"Public payload has unexpected or missing fields at {location}")


def validate_public_payload(payload: dict[str, Any]) -> None:
    _require_exact_keys(payload, PUBLIC_KEYS["top"], "root")
    _require_exact_keys(payload["summary"], PUBLIC_KEYS["summary"], "summary")
    error_counts = payload["summary"]["error_counts"]
    if not isinstance(error_counts, dict) or not set(error_counts).issubset(PUBLIC_ERROR_KINDS):
        raise ValueError("Public payload has an unsupported error category")
    if not all(isinstance(count, int) and count > 0 for count in error_counts.values()):
        raise ValueError("Public payload has an invalid error count")
    late_error_counts = payload["summary"]["late_submission_error_counts"]
    if not isinstance(late_error_counts, dict) or not set(late_error_counts).issubset(PUBLIC_ERROR_KINDS):
        raise ValueError("Public payload has an unsupported late submission error category")
    if not all(isinstance(count, int) and count > 0 for count in late_error_counts.values()):
        raise ValueError("Public payload has an invalid late submission error count")
    _require_exact_keys(payload["methodology"], PUBLIC_KEYS["methodology"], "methodology")
    for index, team in enumerate(payload["teams"]):
        _require_exact_keys(team, PUBLIC_KEYS["team"], f"teams[{index}]")
    for competition_index, competition in enumerate(payload["competitions"]):
        _require_exact_keys(
            competition,
            PUBLIC_KEYS["competition"],
            f"competitions[{competition_index}]",
        )
        for entry_index, entry in enumerate(competition["entries"]):
            _require_exact_keys(
                entry,
                PUBLIC_KEYS["entry"],
                f"competitions[{competition_index}].entries[{entry_index}]",
            )
    for index, submission in enumerate(payload["late_submissions"]):
        _require_exact_keys(
            submission,
            PUBLIC_KEYS["late_submission"],
            f"late_submissions[{index}]",
        )


def write_json_atomic(payload: dict[str, Any], output_path: Path) -> None:
    validate_public_payload(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary_file:
        temporary_file.write(encoded)
        temporary_name = temporary_file.name
    try:
        os.replace(temporary_name, output_path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
