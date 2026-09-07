"""Fail closed when a YingJi release directory contains unsafe payloads."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


FORBIDDEN_NAMES = {
    "cookies.txt",
    "detect_records.json",
    "groups.json",
    "instance.json",
    "launcher.log",
    "server-stdout.log",
    "server_run.log",
    "tasks.json",
}
FORBIDDEN_DIRS = {"Data", "Downloads", "downloads", "logs", ".trash"}
MEDIA_SUFFIXES = {
    ".aac",
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ts",
    ".wav",
    ".webm",
}
REQUIRED_FILES = {
    "YingJi.exe",
    "Runtime/ffmpeg.exe",
    "Runtime/ffprobe.exe",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "legal/THIRD_PARTY_NOTICES.txt",
    "legal/COPYING.GPLv3.txt",
    "legal/FFMPEG_SOURCE.txt",
}


def verify_release(root: Path) -> dict:
    root = root.resolve()
    missing = sorted(
        relative for relative in REQUIRED_FILES if not (root / relative).is_file()
    )
    forbidden: list[str] = []

    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_dir() and path.name in FORBIDDEN_DIRS:
            forbidden.append(relative + "/")
            continue
        if not path.is_file():
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in MEDIA_SUFFIXES:
            forbidden.append(relative)

    invalid_legal: list[str] = []
    for relative in ("LICENSE", "legal/COPYING.GPLv3.txt"):
        gpl_license = root / relative
        if not gpl_license.is_file():
            continue
        opening = gpl_license.read_text(encoding="utf-8", errors="replace")[:300]
        if "PLACEHOLDER" in opening or "GNU GENERAL PUBLIC LICENSE" not in opening:
            invalid_legal.append(relative)

    result = {
        "ok": not missing and not forbidden and not invalid_legal,
        "root": str(root),
        "missing_required": missing,
        "forbidden_payloads": sorted(forbidden),
        "invalid_legal_files": invalid_legal,
        "file_count": sum(1 for path in root.rglob("*") if path.is_file()),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    result = verify_release(args.release_dir)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
