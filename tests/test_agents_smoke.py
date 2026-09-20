from __future__ import annotations

from pathlib import Path
import py_compile

import pytest


ROOT = Path(__file__).resolve().parents[1]
# The runtime entry points replaced the legacy agents/ tutorial scripts.
AGENT_FILES = [ROOT / "cli.py", ROOT / "worker.py"]
AGENT_IDS = [path.name for path in AGENT_FILES]


@pytest.mark.parametrize("agent_path", AGENT_FILES, ids=AGENT_IDS)
def test_agent_scripts_compile(agent_path: Path) -> None:
    _ = py_compile.compile(str(agent_path), doraise=True)


def test_agent_scripts_exist() -> None:
    assert all(path.is_file() for path in AGENT_FILES), "runtime entry points must exist"
