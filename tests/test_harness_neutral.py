"""core/ names no harness: every harness value lives in config.py or an adapter."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent / "core"
# The harness, its tools, and its payload and environment names.
HARNESS = re.compile(
    r"claude|\bSkill\b|NotebookEdit|MultiEdit|hookSpecificOutput|stop_hook_active", re.I
)


@pytest.mark.parametrize(
    "path", sorted(p for p in CORE.glob("*.py") if p.name != "config.py"), ids=lambda p: p.name
)
def test_a_core_module_names_no_harness(path: Path) -> None:
    # Value: protects=the core/adapter boundary the README states; fails_when=a harness
    # literal lands in core; why_new=the hooks moved into this repository; seam=none
    hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if HARNESS.search(line)
    ]
    assert hits == []
