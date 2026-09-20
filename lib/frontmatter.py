"""Safe YAML frontmatter shared by skill discovery and writes."""

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class FrontmatterResult:
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def parse_frontmatter(content: str) -> FrontmatterResult:
    lines = content.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(body=content)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("Unclosed YAML frontmatter")
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML frontmatter") from exc
    if not isinstance(meta, dict) or any(not isinstance(key, str) for key in meta):
        raise ValueError("Frontmatter must be a mapping with string keys")
    return FrontmatterResult(meta, "\n".join(lines[end + 1:]).strip())


def format_frontmatter(meta: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{header}\n---\n\n{body}"
