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
import re
import stat
from pathlib import Path, PurePosixPath

from core import run_index as ri
from core.config import (
    HARNESS_SETTINGS,
    PLUGIN_ID_PREFIX,
    SETTINGS_FILES,
    SETTINGS_SWITCH_KEYS,
    LoopConfig,
    harness_home,
    user_git_configs,
)

CONFIG_SNAPSHOT = ri.SNAPSHOT_FILE
MAX_FILE_BYTES = 8 << 20
MAX_TOTAL_BYTES = 64 << 20
MAX_PREFIX_FILES = 500
GIT_POINTER = ".git"
GIT_CONFIG = "<git>/config"
GIT_WORKTREE_CONFIG = "<git>/config.worktree"
GIT_INCLUDES = "<git>/includes"
USER_GIT_CONFIG = "<user>/gitconfig"
USER_SETTINGS = "<user>/settings"


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


def _switch_hash(settings: dict, plugin_only: bool = False) -> str:
    keys = {k: settings[k] for k in SETTINGS_SWITCH_KEYS if k in settings}
    if plugin_only and isinstance(keys.get("enabledPlugins"), dict):
        # In the user settings, other plugins come and go; only this one's
        # entries can switch its hooks off.
        own = {
            k: v for k, v in keys["enabledPlugins"].items() if str(k).startswith(PLUGIN_ID_PREFIX)
        }
        if own:
            keys["enabledPlugins"] = own
        else:
            del keys["enabledPlugins"]
    return _hash_bytes(json.dumps(keys, sort_keys=True).encode("utf-8"))


def _user_settings_hash(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError:
        return _switch_hash({})
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        return "irregular"
    try:
        settings = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return "irregular"
    return _switch_hash(settings, plugin_only=True) if isinstance(settings, dict) else "irregular"


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


def _git_dirs(worktree: Path) -> tuple[Path, Path] | None:
    """(the worktree's own git directory, the repository's common one)."""
    pointer = worktree / GIT_POINTER
    if pointer.is_dir() and not pointer.is_symlink():
        return pointer, pointer
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
        return gitdir, gitdir
    return gitdir, (Path(common) if Path(common).is_absolute() else gitdir / common)


def _git_config(worktree: Path) -> Path | None:
    """The config file of the repository the worktree's ``.git`` names."""
    dirs = _git_dirs(worktree)
    return None if dirs is None else dirs[1] / "config"


# The git config sections that decide where a push goes, what runs it, or which
# other file to read. ``branch.*`` is left out: ``git push -u`` writes the
# upstream there, and every other worktree of the repository shares this file.
_PUSH_SECTIONS = ("remote", "url", "push", "core", "credential", "include", "includeif")


def _push_lines(path: Path) -> list[str] | None:
    """The push-relevant lines of one git config file; None when it is not a
    small regular file (a missing file is an empty list)."""
    try:
        info = path.lstat()
    except OSError:
        return []
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    kept, section = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped[1:].split("]", 1)[0].split(None, 1)[0].split(".", 1)[0].casefold()
        if section in _PUSH_SECTIONS and stripped and not stripped.startswith(("#", ";")):
            kept.append(stripped)
    return kept


def _git_config_hash(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "missing"
    kept = _push_lines(path)
    return "irregular" if kept is None else _hash_bytes("\n".join(kept).encode("utf-8"))


_INCLUDE_RE = re.compile(r"^path\s*=\s*(.+)$", re.I)


def _includes_hash(configs: list[Path]) -> str:
    """The push-relevant lines of every file an ``include.path`` names, one level
    deep: the include line alone is hashed with its config, not what it points to."""
    parts = []
    for config in configs:
        for line in _push_lines(config) or []:
            match = _INCLUDE_RE.match(line)
            if not match:
                continue
            target = Path(match.group(1).strip().strip('"')).expanduser()
            target = target if target.is_absolute() else config.parent / target
            kept = _push_lines(target)
            parts.append(f"{target}\n" + ("irregular" if kept is None else "\n".join(kept)))
    return _hash_bytes("\n\n".join(parts).encode("utf-8"))


def guard_map(worktree: Path, config: LoopConfig, run_dir: Path) -> dict[str, str]:
    budget = _Budget()
    values = {}
    for rel in guard_paths(worktree, config):
        values[rel] = "over-cap" if rel.endswith("*") else _hash_file(worktree / rel, rel, budget)
    pointer = worktree / GIT_POINTER
    values[GIT_POINTER] = "dir" if pointer.is_dir() else _hash_file(pointer, GIT_POINTER, budget)
    dirs = _git_dirs(worktree)
    git_files = [] if dirs is None else [dirs[1] / "config", dirs[0] / "config.worktree"]
    values[GIT_CONFIG] = "missing" if dirs is None else _git_config_hash(git_files[0])
    values[GIT_WORKTREE_CONFIG] = "missing" if dirs is None else _git_config_hash(git_files[1])
    user_git = user_git_configs()
    values[USER_GIT_CONFIG] = _hash_bytes(
        "\n".join(f"{p}:{_git_config_hash(p)}" for p in user_git).encode("utf-8")
    )
    values[GIT_INCLUDES] = _includes_hash(user_git + git_files)
    values[USER_SETTINGS] = _user_settings_hash(harness_home() / HARNESS_SETTINGS)
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
