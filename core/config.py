#!/usr/bin/env python3
"""The one seam between the loop and the harness and tracker that host it.

Every harness name, command name and tracker name the loop needs is a field on
``LoopConfig`` and lives nowhere else, so the rest of ``core/`` holds no harness
literal at all. The defaults reproduce the values the loop ran on before the
seam existed, so an absent, empty or partial ``loop.toml`` behaves exactly as
the code behaved with the literals inline.

``load_config`` reads one ``loop.toml`` with ``tomllib`` and falls back to the
default for every key the file does not set. A key the loop does not read yet is
ignored, so a host may keep the whole template in place. What is read is
validated: a path may not be absolute and may not escape its root, a prefix may
not be empty, and the tracker pattern must compile.

Path fields are written with placeholders, ``{state_dir}``, ``{state_file}`` and
``{state_stem}``, substituted once when the object is built. A host that moves
the harness state directory therefore moves the carve-outs, the loop prefixes,
the worktree glob and the ledger with it, from one setting. The tracker pattern
takes ``{tracker_prefix}`` the same way.

The object is built once and passed down. Nothing here is read at import time by
another module except ``DEFAULTS``, which is the value every public function
falls back to when a caller passes no config.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path, PurePosixPath

CONFIG_FILENAME = "loop.toml"

# Substituted into the path fields when the object is built, so one setting
# moves every path that lives under the harness state directory.
_STATE_TOKENS = ("state_dir", "state_file", "state_stem")
_EXPANDED_STRINGS = ("ledger_dir", "stage_target", "worktree_glob")
_EXPANDED_TUPLES = ("carve_outs", "carve_out_prefixes", "loop_prefixes", "loop_exact")


class ConfigError(ValueError):
    """A loop.toml the loop refuses: a bad type, an unsafe path, a bad pattern."""


def _set(config: LoopConfig, name: str, value: object) -> None:
    # The dataclass is frozen for its callers; only the constructor writes.
    object.__setattr__(config, name, value)


def _expand(value: object, tokens: dict[str, str]) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"expected a string, got {value!r}")
    for token, replacement in tokens.items():
        value = value.replace("{" + token + "}", replacement)
    return value


def _as_tuple(name: str, value: object) -> tuple:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    raise ConfigError(f"{name} takes a list of strings, got {value!r}")


def _refuse_empty(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise ConfigError(f"{name} takes a string, got {value!r}")
    if not value.strip():
        raise ConfigError(f"{name} may not be empty")


def _refuse_unsafe(name: str, value: str) -> None:
    """Refuse an absolute path and one that climbs out of the repository root."""
    if value.startswith("/") or value.startswith("~"):
        raise ConfigError(f"{name} must be a repository-relative path: {value!r}")
    if ".." in PurePosixPath(value).parts:
        raise ConfigError(f"{name} may not escape the repository root: {value!r}")


@dataclass(frozen=True)
class LoopConfig:
    """Every value the loop takes from its host, with today's values as defaults.

    ``state_dir`` and ``state_file`` name the harness directory and the run state
    file inside a worktree. ``ledger_dir`` and ``ledger_name`` place the
    supervisor's own ledger under the operator's home directory.
    ``carve_outs``, ``carve_out_prefixes``, ``loop_prefixes``, ``loop_exact``,
    ``stage_shorthand`` and ``stage_target`` are the loop-path vocabulary.
    ``worktree_glob`` finds runs, ``session_label`` names the session in a card
    header, ``resume_command`` and ``abort_command`` are the operator commands a
    card quotes, and ``tracker_prefix`` with ``tracker_pattern`` recognise an
    issue identifier in a worktree name.
    """

    state_dir: str = ".claude"
    state_file: str = "pipeline.local.json"
    ledger_dir: str = "{state_dir}/supervisor"
    ledger_name: str = "{slug}-ledger.local.jsonl"
    carve_outs: tuple[str, ...] = (
        "{state_dir}/settings.json",
        "{state_dir}/settings.local.json",
        "{state_dir}/hooks/pipeline_guard.py",
        "{state_dir}/hooks/pipeline_stop.py",
        "{state_dir}/hooks/pipeline-guard",
        "{state_dir}/hooks/pipeline-stop",
        "{state_dir}/hooks/pipeline_loop_paths.py",
    )
    carve_out_prefixes: tuple[str, ...] = ("{state_dir}/pipeline-runs.local.d/",)
    loop_prefixes: tuple[str, ...] = ("{state_dir}/hooks/", "{state_dir}/skills/pipeline/")
    loop_exact: tuple[str, ...] = ("{state_dir}/settings.json",)
    stage_shorthand: str = "stages/"
    stage_target: str = "{state_dir}/skills/pipeline/stages/"
    worktree_glob: str = "~/emdash/worktrees/*/*/{state_dir}/{state_file}"
    session_label: str = "Emdash session"
    resume_command: str = "/pipeline resume"
    abort_command: str = "/pipeline abort"
    tracker_prefix: str = "cod"
    tracker_pattern: str = r"({tracker_prefix}-\d+)"

    def __post_init__(self) -> None:
        _refuse_empty("state_dir", self.state_dir)
        _refuse_unsafe("state_dir", self.state_dir)
        _refuse_empty("state_file", self.state_file)
        if "/" in self.state_file or self.state_file in (".", ".."):
            raise ConfigError(f"state_file names one file, not a path: {self.state_file!r}")
        self._expand()
        self._validate()

    def _expand(self) -> None:
        stem = PurePosixPath(self.state_file).stem
        tokens = dict(zip(_STATE_TOKENS, (self.state_dir, self.state_file, stem), strict=True))
        for name in _EXPANDED_STRINGS:
            _set(self, name, _expand(getattr(self, name), tokens))
        for name in _EXPANDED_TUPLES:
            _set(
                self, name, tuple(_expand(v, tokens) for v in _as_tuple(name, getattr(self, name)))
            )
        _set(
            self,
            "tracker_pattern",
            _expand(self.tracker_pattern, {"tracker_prefix": self.tracker_prefix}),
        )

    def _validate(self) -> None:
        for name in ("ledger_dir", "stage_target", "stage_shorthand"):
            _refuse_empty(name, getattr(self, name))
            _refuse_unsafe(name, getattr(self, name))
        _refuse_empty("ledger_name", self.ledger_name)
        if "/" in self.ledger_name:
            raise ConfigError(f"ledger_name names one file, not a path: {self.ledger_name!r}")
        for name in _EXPANDED_TUPLES:
            for value in getattr(self, name):
                _refuse_empty(name, value)
                _refuse_unsafe(name, value)
        for name in ("worktree_glob", "session_label", "resume_command", "abort_command"):
            _refuse_empty(name, getattr(self, name))
        _refuse_empty("tracker_prefix", self.tracker_prefix)
        try:
            re.compile(self.tracker_pattern)
        except re.error as exc:
            raise ConfigError(f"tracker_pattern is not a regular expression: {exc}") from exc

    @cached_property
    def state_depth(self) -> int:
        """How many path segments separate a worktree root from its state file."""
        return len(PurePosixPath(self.state_dir).parts) + 1

    @cached_property
    def carve_outs_cf(self) -> frozenset[str]:
        """The carve-out set, casefolded for a case-insensitive filesystem."""
        return frozenset(c.casefold() for c in self.carve_outs)

    @cached_property
    def carve_out_prefixes_cf(self) -> tuple[str, ...]:
        """The carve-out prefixes, casefolded for a case-insensitive filesystem."""
        return tuple(p.casefold() for p in self.carve_out_prefixes)

    @cached_property
    def state_file_re(self) -> re.Pattern[str]:
        """Matches the state file and its siblings, whatever suffix they carry."""
        stem = PurePosixPath(self.state_file).stem
        return re.compile("^" + re.escape(f"{self.state_dir}/{stem}") + r"\b", re.IGNORECASE)

    @cached_property
    def tracker_re(self) -> re.Pattern[str]:
        """Matches one issue identifier inside a longer name."""
        return re.compile(self.tracker_pattern)

    def ledger_file(self, repo_slug: str) -> str:
        """The ledger file name for one repository slug."""
        return self.ledger_name.replace("{slug}", repo_slug)


DEFAULTS = LoopConfig()


# Where each field is written in a loop.toml: the field, its table, its key.
_STRING_KEYS = (
    ("state_dir", "harness", "state_dir"),
    ("state_file", "harness", "state_file"),
    ("worktree_glob", "harness", "worktree_glob"),
    ("session_label", "harness", "session_label"),
    ("ledger_dir", "ledger", "dir"),
    ("ledger_name", "ledger", "name"),
    ("stage_shorthand", "loop_paths", "stage_shorthand"),
    ("stage_target", "loop_paths", "stage_target"),
    ("resume_command", "commands", "resume"),
    ("abort_command", "commands", "abort"),
    ("tracker_prefix", "tracker", "prefix"),
    ("tracker_pattern", "tracker", "pattern"),
)
_LIST_KEYS = (
    ("carve_outs", "loop_paths", "carve_outs"),
    ("carve_out_prefixes", "loop_paths", "carve_out_prefixes"),
    ("loop_prefixes", "loop_paths", "prefixes"),
    ("loop_exact", "loop_paths", "exact"),
)


def _str(table: dict, key: str) -> str:
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{key} takes a string, got {value!r}")
    return value


def _strs(table: dict, key: str) -> tuple[str, ...]:
    value = table[key]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{key} takes a list of strings, got {value!r}")
    return tuple(value)


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table, got {value!r}")
    return value


def from_mapping(data: dict) -> LoopConfig:
    """One config from a parsed loop.toml, defaulting every key the file omits.

    A key the file leaves out keeps the field's own default, placeholders and
    all, so setting only the state directory moves every path that names it, and
    setting only the tracker prefix reshapes the tracker pattern. A key the loop
    does not read is ignored.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"a loop.toml must be a table, got {data!r}")
    values: dict[str, object] = {}
    for field_name, table_name, key in _STRING_KEYS:
        table = _table(data, table_name)
        if key in table:
            values[field_name] = _str(table, key)
    for field_name, table_name, key in _LIST_KEYS:
        table = _table(data, table_name)
        if key in table:
            values[field_name] = _strs(table, key)
    return LoopConfig(**values)


def load_config(path: str | Path | None = None) -> LoopConfig:
    """Read one loop.toml, or return the defaults when there is no file to read.

    ``path`` may name the file or the directory that holds it. A missing file is
    not an error: the loop has a default for every value. A file that is present
    but unreadable, unparseable or invalid is an error, because a host that
    wrote one meant it to be read.
    """
    if path is None:
        return DEFAULTS
    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / CONFIG_FILENAME
    if not candidate.is_file():
        return DEFAULTS
    try:
        data = tomllib.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {candidate}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{candidate} is not valid TOML: {exc}") from exc
    return from_mapping(data)
