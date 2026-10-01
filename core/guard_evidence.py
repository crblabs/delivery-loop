"""The guard map: what a run hashes at every turn end to notice an edit it did not see.

The edit guard checks edit tools before they run. A shell command is not checked,
so every turn end hashes the files a run may not change unseen and compares the
map with the last accepted one. The map holds every carve-out, every
``loop_exact`` file, every file under ``loop_prefixes``, the ``guard_watch``
files, and the config snapshot the run started under. ``carve_out_prefixes``
(``.git/``) are left out: hashing a git directory is unbounded, and the edit
guard denies edits there.

A value is a sha256, or ``missing`` or ``irregular`` for a file that is gone or
is not a regular file: the sentinels ``supervisor_decide`` already reads. The
work is bounded, because the agent controls what it hashes and a Stop hook that
runs past its timeout lets the turn end: a file over ``MAX_FILE_BYTES``, or past
``MAX_TOTAL_BYTES`` in all, is recorded by its size and modification time
instead of its content, and a loop prefix past ``MAX_PREFIX_FILES`` files adds
one ``over-cap`` entry. The worktree's ``.git`` pointer and its git config are in
the map too: the first names the repository, the second where a push goes. The two
project settings files are hashed by the keys that can switch the hooks off
(``SETTINGS_SWITCH_KEYS``), because the harness rewrites the rest of a settings
file whenever a person answers a permission prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

from core import run_index as ri
from core.config import SETTINGS_FILES, SETTINGS_SWITCH_KEYS, LoopConfig

CONFIG_SNAPSHOT = ri.SNAPSHOT_FILE
MAX_FILE_BYTES = 8 << 20
MAX_TOTAL_BYTES = 64 << 20
MAX_PREFIX_FILES = 500
GIT_POINTER = ".git"
GIT_CONFIG = "<git>/config"


def config_hash(run_dir: Path) -> str:
    try:
        return hashlib.sha256((run_dir / CONFIG_SNAPSHOT).read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Budget:
    """The bytes left to hash in one guard map."""

    def __init__(self) -> None:
        self.left = MAX_TOTAL_BYTES


def _hash_file(path: Path, rel: str, budget: _Budget) -> str:
    """The guard value of one file: its hash, or ``missing`` / ``irregular``."""
    try:
        info = path.lstat()
    except OSError:
        # A settings file with none of the switch keys acts as no file at all, so
        # the harness creating one for a permission rule changes nothing here.
        return _switch_hash({}) if rel in SETTINGS_FILES else "missing"
    if not stat.S_ISREG(info.st_mode):
        return "irregular"
    if info.st_size > MAX_FILE_BYTES or info.st_size > budget.left:
        return f"stat:{info.st_size}:{info.st_mtime_ns}"
    budget.left -= info.st_size
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return "irregular"
    if rel in SETTINGS_FILES:
        try:
            settings = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return _hash_bytes(data)
        if isinstance(settings, dict):
            return _switch_hash(settings)
    return _hash_bytes(data)


def _switch_hash(settings: dict) -> str:
    keys = {k: settings[k] for k in SETTINGS_SWITCH_KEYS if k in settings}
    return _hash_bytes(json.dumps(keys, sort_keys=True).encode("utf-8"))


def guard_paths(worktree: Path, config: LoopConfig) -> list[str]:
    """Every repository path in the guard map. ``carve_out_prefixes`` (``.git/``)
    are left out: hashing a git directory is unbounded, and edits there are denied."""
    paths = set(config.carve_outs) | set(config.loop_exact) | set(config.guard_watch)
    paths.discard(GIT_POINTER)
    for prefix in config.loop_prefixes:
        paths |= _prefix_files(worktree, prefix)
    return sorted(paths)


def _prefix_files(worktree: Path, prefix: str) -> set[str]:
    base = worktree / prefix
    if not base.is_dir() or base.is_symlink():
        return set()
    found: set[str] = set()
    for directory, _dirs, files in os.walk(base):
        for name in files:
            if len(found) >= MAX_PREFIX_FILES:
                return found | {f"{prefix}*"}
            found.add((Path(directory) / name).relative_to(worktree).as_posix())
    return found


def _git_config(worktree: Path) -> Path | None:
    """The config file of the repository the worktree's ``.git`` names."""
    pointer = worktree / GIT_POINTER
    if pointer.is_dir() and not pointer.is_symlink():
        return pointer / "config"
    try:
        text = pointer.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip().splitlines()[0])
    gitdir = gitdir if gitdir.is_absolute() else worktree / gitdir
    try:
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return gitdir / "config"
    common_dir = Path(common) if Path(common).is_absolute() else gitdir / common
    return common_dir / "config"


# The git config sections that decide where a push goes, what runs it, or which
# other file to read. ``branch.*`` is left out: ``git push -u`` writes the
# upstream there, and every other worktree of the repository shares this file.
_PUSH_SECTIONS = ("remote", "url", "push", "core", "credential", "include", "includeif")


def _git_config_hash(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError:
        return "missing"
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        return "irregular"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "irregular"
    kept, section = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped[1:].split("]", 1)[0].split(None, 1)[0].split(".", 1)[0].casefold()
        if section in _PUSH_SECTIONS and stripped and not stripped.startswith(("#", ";")):
            kept.append(stripped)
    return _hash_bytes("\n".join(kept).encode("utf-8"))


def guard_map(worktree: Path, config: LoopConfig, run_dir: Path) -> dict[str, str]:
    budget = _Budget()
    values = {}
    for rel in guard_paths(worktree, config):
        values[rel] = "over-cap" if rel.endswith("*") else _hash_file(worktree / rel, rel, budget)
    pointer = worktree / GIT_POINTER
    values[GIT_POINTER] = "dir" if pointer.is_dir() else _hash_file(pointer, GIT_POINTER, budget)
    config_file = _git_config(worktree)
    values[GIT_CONFIG] = "missing" if config_file is None else _git_config_hash(config_file)
    values[f"<run>/{CONFIG_SNAPSHOT}"] = config_hash(run_dir)
    return values


def declared(rel: str, edits: list[str]) -> bool:
    """Whether the approved plan declared ``rel``, exactly or under a directory entry."""
    for entry in edits:
        if entry.endswith("/"):
            prefix = PurePosixPath(entry.rstrip("/")).parts
            parts = PurePosixPath(rel).parts
            if len(parts) > len(prefix) and parts[: len(prefix)] == prefix:
                return True
        elif rel == entry:
            return True
    return False
