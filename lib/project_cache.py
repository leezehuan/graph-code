"""Project-scoped cache paths shared by local services."""

import hashlib
import os
from pathlib import Path


def project_cache(root: Path, cache_dir: Path | None = None) -> Path:
    base = Path(cache_dir or os.getenv("LANGCODE_CACHE_DIR") or Path.home() / ".langcode")
    identity = os.path.normcase(str(root.resolve()))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    directory = base / "projects" / digest
    directory.mkdir(parents=True, exist_ok=True)
    return directory
