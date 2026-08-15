"""Source-repository .env reader with process environment precedence."""

from __future__ import annotations

import os
import re
from pathlib import Path

_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_path() -> Path:
    override = os.environ.get("MINI_CLAUDE_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / ".env"


def _parse_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def dotenv_values() -> dict[str, str]:
    """Read the Mini Claude source repository's ``.env`` file."""
    path = _env_path()
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


def load_dotenv() -> None:
    """Load source-repository values without overriding process variables."""
    for key, value in dotenv_values().items():
        os.environ.setdefault(key, value)
