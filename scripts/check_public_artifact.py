from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


TEXT_SUFFIXES = {".html", ".css", ".js", ".json", ".txt", ".xml", ".svg"}
FORBIDDEN_SUFFIXES = {".csv", ".env", ".key", ".log", ".zip"}
FORBIDDEN_MARKERS = (
    b"KAGGLE_API_TOKEN",
    b"KAGGLE_API_TOKENS",
    b"KAGGLE_LEGACY_CREDENTIALS",
    b"KAGGLE_USERNAME",
    b"KAGGLE_KEY",
    b"TeamMemberUserNames",
    b"Authorization: Bearer",
    b"/.kaggle/",
    b"\\.kaggle\\",
)


def scan(
    paths: list[Path],
    api_tokens: str | list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    findings: list[str] = []
    if isinstance(api_tokens, str):
        token_values = (api_tokens,)
    else:
        token_values = tuple(api_tokens or ())
    token_bytes = tuple(
        token.encode("utf-8") for token in token_values if len(token) >= 8
    )

    files: list[Path] = []
    for path in paths:
        if path.is_symlink():
            findings.append(f"symbolic link is not allowed: {path}")
        elif path.is_dir():
            for item in path.rglob("*"):
                if item.is_symlink():
                    findings.append(f"symbolic link is not allowed: {item}")
                elif item.is_file():
                    files.append(item)
        elif path.is_file():
            files.append(path)
        else:
            findings.append(f"missing path: {path}")

    for file_path in files:
        suffix = file_path.suffix.casefold()
        if suffix in FORBIDDEN_SUFFIXES or file_path.name.casefold().startswith(".env"):
            findings.append(f"forbidden artifact type: {file_path}")
            continue
        if suffix not in TEXT_SUFFIXES:
            findings.append(f"unsupported artifact type: {file_path}")
            continue
        content = file_path.read_bytes()
        if any(token in content for token in token_bytes):
            findings.append(f"credential value found: {file_path}")
        lowered_content = content.lower()
        for marker in FORBIDDEN_MARKERS:
            if marker.lower() in lowered_content:
                findings.append(f"forbidden field marker found: {file_path}")
                break
    return findings


def credential_values_from_environment() -> tuple[str, ...]:
    credentials: list[str] = []
    single_token = os.environ.get("KAGGLE_API_TOKEN")
    if single_token and single_token.strip():
        credentials.append(single_token.strip())

    raw_tokens = os.environ.get("KAGGLE_API_TOKENS")
    if raw_tokens and raw_tokens.strip():
        try:
            parsed_tokens = json.loads(raw_tokens)
        except json.JSONDecodeError as exc:
            raise ValueError("KAGGLE_API_TOKENS is not a valid JSON array") from exc
        if not isinstance(parsed_tokens, list) or not all(
            isinstance(item, str) for item in parsed_tokens
        ):
            raise ValueError("KAGGLE_API_TOKENS JSON must be an array of strings")
        credentials.extend(item.strip() for item in parsed_tokens if item.strip())

    raw_legacy_credentials = os.environ.get("KAGGLE_LEGACY_CREDENTIALS")
    if raw_legacy_credentials and raw_legacy_credentials.strip():
        try:
            parsed_legacy_credentials = json.loads(raw_legacy_credentials)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "KAGGLE_LEGACY_CREDENTIALS is not a valid JSON array"
            ) from exc
        if not isinstance(parsed_legacy_credentials, list):
            raise ValueError(
                "KAGGLE_LEGACY_CREDENTIALS JSON must be an array of username/key objects"
            )
        for item in parsed_legacy_credentials:
            if not isinstance(item, dict) or set(item) != {"username", "key"}:
                raise ValueError(
                    "KAGGLE_LEGACY_CREDENTIALS JSON must be an array of "
                    "username/key objects"
                )
            username = item["username"]
            key = item["key"]
            if (
                not isinstance(username, str)
                or not isinstance(key, str)
                or not username.strip()
                or not key.strip()
            ):
                raise ValueError(
                    "KAGGLE_LEGACY_CREDENTIALS username and key values must be "
                    "non-empty strings"
                )
            credentials.append(key.strip())

    return tuple(dict.fromkeys(credentials))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Pages artifact for credential or raw-field leaks")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        credential_values = credential_values_from_environment()
    except ValueError as exc:
        print(f"ERROR {exc}")
        return 1
    findings = scan(args.paths, credential_values)
    if findings:
        for finding in findings:
            print(f"ERROR {finding}")
        return 1
    print(f"Public artifact safety check passed for {len(args.paths)} path(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
