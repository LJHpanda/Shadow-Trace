"""Validate that the tracked source snapshot is safe to publish."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = {
    "LICENSE",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "requirements.txt",
    "requirements-dev.txt",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/security.yml",
}
FORBIDDEN_TRACKED_NAMES = {
    "cookies.txt", "tasks.json", "groups.json", "detect_records.json",
    "instance.json", "server.pid", "server-stdout.log", "server_run.log",
    "config.json", "launcher.log",
}
FORBIDDEN_PARTS = {
    ".telegram_bot", ".venv", "Runtime", "downloads", "logs",
    "docs/archive", "static/legacy", "static/historical-yingji",
}
SECRET_PATTERNS = {
    "telegram_bot_token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{20,}\b"),
    "openai_api_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
PERSONAL_PATTERNS = {
    "windows_user_path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I),
    "internal_workspace": re.compile(r"workbuddy|codex_space|D:\\secure", re.I),
    "known_local_identity": re.compile(r"98191|黎总", re.I),
}
TEXT_SUFFIXES = {
    "", ".bat", ".css", ".html", ".ini", ".js", ".json", ".md",
    ".nsi", ".ps1", ".py", ".spec", ".txt", ".yml", ".yaml",
}


def _tracked_files(root: Path) -> list[Path]:
    try:
        top_level = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if Path(top_level.stdout.strip()).resolve() != root.resolve():
            raise RuntimeError("source directory is nested inside a different Git repository")
        completed = subprocess.run(
            [
                "git", "-C", str(root), "ls-files", "-z",
                "--cached", "--others", "--exclude-standard",
            ],
            check=True,
            capture_output=True,
        )
        paths = [root / item.decode("utf-8") for item in completed.stdout.split(b"\0") if item]
        return [path for path in paths if path.is_file()]
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        excluded = {".git", ".venv", "Runtime", "release", "build", "dist"}
        return [
            path for path in root.rglob("*")
            if path.is_file() and not any(part in excluded for part in path.parts)
        ]


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_forbidden_path(relative: str) -> bool:
    path = Path(relative)
    if path.name in FORBIDDEN_TRACKED_NAMES:
        return True
    return any(
        relative == part or relative.startswith(part.rstrip("/") + "/")
        for part in FORBIDDEN_PARTS
    )


def check_public_source(root: Path = ROOT) -> dict:
    root = root.resolve()
    files = _tracked_files(root)
    tracked = {_relative(path, root) for path in files}
    missing = sorted(REQUIRED_FILES - tracked)
    forbidden_paths = sorted(relative for relative in tracked if _is_forbidden_path(relative))
    findings: list[dict] = []

    for path in files:
        relative = _relative(path, root)
        if relative == "scripts/public_release_check.py":
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for category, pattern in {**SECRET_PATTERNS, **PERSONAL_PATTERNS}.items():
            for match in pattern.finditer(text):
                value = match.group(0)
                if "TEST_ONLY" in value or "example" in value.lower():
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append({"type": category, "file": relative, "line": line})

    root_license = root / "LICENSE"
    bundled_license = root / "legal" / "COPYING.GPLv3.txt"
    license_match = (
        root_license.is_file()
        and bundled_license.is_file()
        and root_license.read_bytes() == bundled_license.read_bytes()
    )
    result = {
        "ok": not missing and not forbidden_paths and not findings and license_match,
        "root": str(root),
        "tracked_files": len(tracked),
        "missing_required": missing,
        "forbidden_tracked_paths": forbidden_paths,
        "sensitive_findings": findings,
        "license_copies_match": license_match,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = check_public_source(args.root)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
