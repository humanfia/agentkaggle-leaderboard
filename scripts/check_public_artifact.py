from __future__ import annotations

import argparse
import os
from pathlib import Path


TEXT_SUFFIXES = {".html", ".css", ".js", ".json", ".txt", ".xml", ".svg"}
FORBIDDEN_SUFFIXES = {".csv", ".env", ".key", ".log", ".zip"}
FORBIDDEN_MARKERS = (
    b"KAGGLE_API_TOKEN",
    b"KAGGLE_USERNAME",
    b"KAGGLE_KEY",
    b"TeamMemberUserNames",
    b"Authorization: Bearer",
    b"/.kaggle/",
    b"\\.kaggle\\",
)


def scan(paths: list[Path], api_token: str | None = None) -> list[str]:
    findings: list[str] = []
    token_bytes = api_token.encode("utf-8") if api_token and len(api_token) >= 8 else None

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
        if token_bytes and token_bytes in content:
            findings.append(f"credential value found: {file_path}")
        lowered_content = content.lower()
        for marker in FORBIDDEN_MARKERS:
            if marker.lower() in lowered_content:
                findings.append(f"forbidden field marker found: {file_path}")
                break
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Pages artifact for credential or raw-field leaks")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    findings = scan(args.paths, os.environ.get("KAGGLE_API_TOKEN"))
    if findings:
        for finding in findings:
            print(f"ERROR {finding}")
        return 1
    print(f"Public artifact safety check passed for {len(args.paths)} path(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
