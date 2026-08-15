"""Small project-local .env reader with process environment precedence."""

from __future__ import annotations

import os
import re
from pathlib import Path

_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _project_root(start: Path | None = None) -> Path:
    directory = (start or Path.cwd()).resolve()
    for candidate_directory in (directory, *directory.parents):
        if (candidate_directory / ".git").exists():
            return candidate_directory
    return directory


def _parse_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def dotenv_values(start: Path | None = None) -> dict[str, str]:
    """Read only the current project's ``.env`` file."""
    path = _project_root(start) / ".env"
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return {}
    values: dict[str, str] = {}
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        key, separator, raw_value = entry.partition("=")
        key = key.strip()
        if not separator or not _KEY_PATTERN.fullmatch(key):
            continue
        values[key] = _parse_value(raw_value)
    return values


def load_dotenv(start: Path | None = None) -> None:
    """Load the current project's values without overriding process variables."""
    for key, value in dotenv_values(start).items():
        os.environ.setdefault(key, value)
