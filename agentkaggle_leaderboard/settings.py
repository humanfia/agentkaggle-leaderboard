from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised for invalid configuration without echoing secret values."""


def normalize_team_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def parse_team_names(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value or not raw_value.strip():
        raise ConfigurationError("KAGGLE_TEAMS must contain at least one team name")

    raw_value = raw_value.strip()
    if raw_value.startswith("["):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("KAGGLE_TEAMS is not a valid JSON array") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ConfigurationError("KAGGLE_TEAMS JSON must be an array of strings")
        candidates = parsed
    else:
        candidates = raw_value.replace("\r", "\n").replace("\n", ",").split(",")

    names = tuple(name.strip() for name in candidates if name.strip())
    if not names:
        raise ConfigurationError("KAGGLE_TEAMS must contain at least one team name")

    normalized: dict[str, str] = {}
    for name in names:
        key = normalize_team_name(name)
        if key in normalized:
            raise ConfigurationError("KAGGLE_TEAMS contains duplicate names after normalization")
        normalized[key] = name
    return names


def parse_api_token_array(raw_tokens: str | None) -> tuple[str, ...]:
    if not raw_tokens or not raw_tokens.strip():
        return ()
    try:
        parsed = json.loads(raw_tokens)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("KAGGLE_API_TOKENS is not a valid JSON array") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ConfigurationError("KAGGLE_API_TOKENS JSON must be an array of strings")
    return tuple(dict.fromkeys(item.strip() for item in parsed if item.strip()))


def parse_api_tokens(single_token: str | None, raw_tokens: str | None) -> tuple[str, ...]:
    candidates: list[str] = []
    if single_token and single_token.strip():
        candidates.append(single_token.strip())
    candidates.extend(parse_api_token_array(raw_tokens))

    tokens = tuple(dict.fromkeys(candidates))
    if not tokens:
        raise ConfigurationError("KAGGLE_API_TOKEN or KAGGLE_API_TOKENS is required")
    return tokens


def merge_team_names(
    configured_names: tuple[str, ...],
    discovered_names: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    merged = {normalize_team_name(name): name for name in configured_names}
    for name in discovered_names:
        clean_name = name.strip()
        if clean_name:
            merged.setdefault(normalize_team_name(clean_name), clean_name)
    return tuple(merged.values())


def _parse_bool(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _parse_positive_int(name: str, default: int, maximum: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 1 or value > maximum:
        raise ConfigurationError(f"{name} must be between 1 and {maximum}")
    return value


def _parse_positive_float(name: str, default: float, maximum: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if value <= 0 or value > maximum:
        raise ConfigurationError(f"{name} must be greater than 0 and at most {maximum:g}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    teams: tuple[str, ...]
    workers: int = 2
    request_interval_seconds: float = 2.0
    api_tokens: tuple[str, ...] = field(default=(), repr=False, compare=False)
    team_discovery_api_tokens: tuple[str, ...] = field(default=(), repr=False, compare=False)
    auto_discover_teams: bool = False

    @property
    def normalized_teams(self) -> dict[str, str]:
        return {normalize_team_name(name): name for name in self.teams}

    @classmethod
    def from_environment(cls, *, load_local_dotenv: bool = True) -> "Settings":
        if load_local_dotenv:
            load_dotenv(override=False)
        auto_discover_teams = _parse_bool("KAGGLE_AUTO_DISCOVER_TEAMS", default=False)
        raw_teams = os.environ.get("KAGGLE_TEAMS")
        teams = parse_team_names(raw_teams) if raw_teams and raw_teams.strip() else ()
        if not teams and not auto_discover_teams:
            raise ConfigurationError(
                "KAGGLE_TEAMS is required unless KAGGLE_AUTO_DISCOVER_TEAMS is true"
            )
        raw_api_tokens = os.environ.get("KAGGLE_API_TOKENS")
        api_tokens = parse_api_tokens(
            os.environ.get("KAGGLE_API_TOKEN"),
            raw_api_tokens,
        )
        contributor_tokens = parse_api_token_array(raw_api_tokens)
        team_discovery_api_tokens = contributor_tokens or api_tokens
        workers = _parse_positive_int("KAGGLE_SCAN_WORKERS", default=2, maximum=16)
        request_interval_seconds = _parse_positive_float(
            "KAGGLE_REQUEST_INTERVAL_SECONDS", default=2.0, maximum=10
        )
        return cls(
            teams=teams,
            workers=workers,
            request_interval_seconds=request_interval_seconds,
            api_tokens=api_tokens,
            team_discovery_api_tokens=team_discovery_api_tokens,
            auto_discover_teams=auto_discover_teams,
        )
