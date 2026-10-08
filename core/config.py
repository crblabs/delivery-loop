#!/usr/bin/env python3
"""The one seam between the loop and the harness and tracker that host it.

Every harness name, command name and tracker name the loop needs is a field on
``LoopConfig`` and lives nowhere else, so the rest of ``core/`` holds no harness
literal at all. The defaults reproduce the values the loop ran on before the
seam existed, with three kinds of exception. The values that named one operator's
setup, the session label and the tracker prefix, are neutral, so a repository
with no ``loop.toml`` at all runs on defaults that fit it. ``loop.toml`` and the
worktree's ``.git`` pointer are carve-outs, and the built-in carve-outs stay in
force whatever state directory a file sets. And the hooks, their helpers and the
stage prompts are no longer in the worktree: they ship in the plugin, so the
carve-outs name only what a worktree still holds. The two project settings files
stay carve-outs, written as ``.claude/...`` and not under ``{state_dir}``,
because Claude Code reads them there and either can switch the plugin's hooks
off.

``load_config`` looks in three places, in order: ``loop.toml`` in the
repository, then ``~/.config/delivery-loop/<repo-slug>.toml`` for the operator,
then the built-in defaults. The files are merged key by key, so the repository
file wins over the user file for every key it sets, and a key neither sets keeps
its default. Neither file is required. A key the loop does not read yet is
ignored, so a host may keep the whole template in place, but a key written in
the repository file overrides the user file even when it holds the default. A
key the loop no longer reads is refused, so a host learns that its meaning
changed. What is read is validated: a path may not be absolute and may not
escape its root, a prefix may not be empty, and the tracker pattern must
compile.

Path fields are written with placeholders, ``{state_dir}``, ``{state_file}`` and
``{state_stem}``, substituted once when the object is built. A host that moves
the harness state directory therefore moves the carve-outs, the loop prefixes
and the ledger with it, from one setting. ``{state_file}`` and ``{state_stem}``
expand to the state file's name only: the file itself lives under
``state_root``, so a worktree path built from them names nothing. The tracker
pattern takes ``{tracker_prefix}`` the same way.

The run state does not live in the worktree, so git never sees it and a host
repository needs no ignore rule. It lives under ``state_root``,
``~/.delivery-loop`` by default, one directory per worktree. The
``DELIVERY_LOOP_HOME`` environment variable overrides it at run time, the way
``GSTACK_HOME`` moves gstack's.

The stage list is configuration too. ``stages`` is a tuple of ``StageSpec``, one
per stage, and each spec declares the contract the loop depends on and not only
a name: the command the stage invokes, the prompt it is given, what it emits for
the next stage, whether its finishing token is checked against a clean worktree,
whether it always stops for a person, and whether it needs the agent sandbox
off. The default tuple is the five stages the loop shipped with, so a host that
declares nothing keeps them. A host with another skillset writes its own
``[[stages]]`` array and the loop drives that instead.

The object is built at an entry point and passed down. Without ``--config``,
every supervisor command also builds one per run, from that run's own worktree,
through ``config_for_run``. Nothing here is read at import time by
another module except ``DEFAULTS``, which is the value every public function
falls back to when a caller passes no config.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass, fields
from functools import cached_property
from pathlib import Path, PurePosixPath

CONFIG_FILENAME = "loop.toml"
# The operator's own configuration, one file per repository, under the home
# directory: ``<USER_CONFIG_DIR>/<repo-slug>.toml``.
USER_CONFIG_DIR = ".config/delivery-loop"
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# A working ``git rev-parse`` answers in milliseconds; a stuck one must not stall
# a supervisor call for long, so it times out and the loader reports ConfigError.
GIT_TIMEOUT_S = 3

# Substituted into the path fields when the object is built, so one setting
# moves every path that lives under the harness state directory.
_STATE_TOKENS = ("state_dir", "state_file", "state_stem")
_EXPANDED_STRINGS = ("ledger_dir", "stage_target")
_EXPANDED_TUPLES = (
    "carve_outs",
    "carve_out_prefixes",
    "loop_prefixes",
    "loop_exact",
    "guard_watch",
)
# The project settings files Claude Code reads, whatever state_dir a file sets.
# Either can set disableAllHooks or switch the plugin off, so both are carve-outs.
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")
# The settings keys that can switch the plugin's hooks off. A settings file is
# guarded by these keys only, because the harness rewrites the rest of it, the
# permission rules, whenever a person answers a permission prompt.
SETTINGS_SWITCH_KEYS = ("disableAllHooks", "enabledPlugins", "hooks", "env")
# The harness's user directory: the user settings and the installed plugins.
# An edit tool may not write there during a run, nor in the directory the
# harness reads instead when HARNESS_HOME_ENV names one.
HARNESS_HOME = "~/.claude"
HARNESS_HOME_ENV = "CLAUDE_CONFIG_DIR"
# The user settings file in the harness's user directory. A shell write there can
# switch every hook off, so the turn end hashes its switch keys too, with
# ``enabledPlugins`` narrowed to this plugin's own entries.
HARNESS_SETTINGS = "settings.json"
PLUGIN_ID_PREFIX = "delivery-loop@"
# The user-level git config files git reads before the repository's own.
USER_GIT_CONFIGS = ("~/.gitconfig", "{xdg}/git/config")
XDG_CONFIG_ENV = "XDG_CONFIG_HOME"
# Where the plugin keeps its default stage prompts, from the plugin root.
PLUGIN_STAGES_DIR = "skills/pipeline/stages"


class ConfigError(ValueError):
    """A config file the loop refuses: a bad type, an unsafe path, a bad pattern."""


# A group that holds a quantifier and is itself repeated, the shape behind
# catastrophic backtracking.
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)[+*{]")


def _grouped(prefix: str) -> str:
    """The prefix as one unit inside the pattern, so "a|b" cannot split it."""
    return f"(?:{prefix})" if "|" in prefix else prefix


def _set(target: object, name: str, value: object) -> None:
    # The dataclass is frozen for its callers; only the constructor writes.
    object.__setattr__(target, name, value)


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


GATES = ("none", "approval", "review_batch")

# A stage name is a dict key, a file name component and a token in a message, so
# it may hold no separator and may not read as a relative path.
_STAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_STAGE_STRINGS = ("command", "prompt", "emits", "gate")
_STAGE_BOOLS = ("clean_tree", "sandbox_off", "shell_waits")


@dataclass(frozen=True)
class StageSpec:
    """One stage of the loop, and the contract the rest of the run depends on.

    ``name`` is the only required field and names the stage everywhere: in the
    state file, in the attempts map, and in what a person is shown. ``command``
    is the harness command the stage invokes, empty when the model works
    directly with no skill behind it. ``prompt`` is the stage's prompt file,
    relative to the prompts directory, and defaults to ``<name>.md``. ``emits``
    says in one phrase what the stage must leave behind for the next one, empty
    when it leaves nothing. ``clean_tree`` says the finishing token is only
    accepted over a committed worktree. ``gate`` is one of ``GATES`` and says
    whether the stage stops for a person: ``none`` never, ``approval`` at an
    approval question, ``review_batch`` at a batch of review questions.
    ``sandbox_off`` says the stage cannot run under the agent sandbox.
    ``shell_waits`` lets the stage wait in a foreground shell (``sleep``, a
    polling loop), such as a QA stage that waits for a dev server; every other
    stage is denied one, because a foreground wait holds the turn open.
    """

    name: str
    command: str = ""
    prompt: str = "{name}.md"
    emits: str = ""
    clean_tree: bool = False
    gate: str = "none"
    sandbox_off: bool = False
    shell_waits: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigError(f"a stage needs a name, got {self.name!r}")
        if not _STAGE_NAME_RE.match(self.name):
            raise ConfigError(
                f"stage name {self.name!r} must be safe as a file name component: "
                "letters, digits, underscore, dot and hyphen, and it may not start with a dot"
            )
        for field_name in ("command", "emits"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ConfigError(f"stage {self.name} {field_name} takes a string, got {value!r}")
        _set(self, "prompt", _expand(self.prompt, {"name": self.name}))
        label = f"stage {self.name} prompt"
        _refuse_empty(label, self.prompt)
        _refuse_unsafe(label, self.prompt)
        if self.gate not in GATES:
            raise ConfigError(
                f"stage {self.name} gate must be one of {', '.join(GATES)}, got {self.gate!r}"
            )
        for field_name in _STAGE_BOOLS:
            value = getattr(self, field_name)
            if not isinstance(value, bool):
                raise ConfigError(
                    f"stage {self.name} {field_name} takes true or false, got {value!r}"
                )


# The five stages the loop shipped with. Every value is the stage's own written
# contract, so a host that declares nothing runs exactly what it ran before.
DEFAULT_STAGES = (
    StageSpec(
        name="autoplan",
        command="/autoplan",
        emits="PLAN: the absolute path of the plan file",
    ),
    StageSpec(name="implement", clean_tree=True),
    StageSpec(name="qa", command="/qa", clean_tree=True, sandbox_off=True),
    StageSpec(name="review", command="/review", clean_tree=True),
    StageSpec(
        name="ship",
        command="/ship",
        emits="PR: the pull request url, then the run's done token",
        clean_tree=True,
    ),
)


@dataclass(frozen=True)
class LoopConfig:
    """Every value the loop takes from its host, with neutral defaults.

    ``state_dir`` names the harness directory inside a worktree. ``state_root``
    is the directory every run's state lives under, outside any repository, and
    ``state_file`` names the state file in a run's directory. ``ledger_dir`` and
    ``ledger_name`` place the supervisor's own ledger under the operator's home
    directory. ``carve_outs``, ``carve_out_prefixes``, ``loop_prefixes``,
    ``loop_exact``, ``stage_shorthand`` and ``stage_target`` are the loop-path
    vocabulary. ``guard_watch`` names files that are not carve-outs but whose
    change mid-run pauses the run, such as an in-repo copy of the old hooks.
    ``stages`` is the stage list itself, one ``StageSpec`` per stage, in order.
    ``session_label`` names the session in a card header,
    ``resume_command`` and ``abort_command`` are the operator commands a card
    quotes, and ``tracker_prefix`` with ``tracker_pattern`` recognise an issue
    identifier in a worktree name. ``tracker_prefix`` is a regular expression
    fragment; the default matches any run of letters. ``stop_task_tool`` and
    ``background_flag`` are the harness's names for stopping a background task
    and for running a command in the background, quoted to the agent.
    """

    state_dir: str = ".claude"
    state_root: str = "~/.delivery-loop"
    state_file: str = "state.json"
    ledger_dir: str = "{state_dir}/supervisor"
    ledger_name: str = "{slug}-ledger.local.jsonl"
    carve_outs: tuple[str, ...] = (
        *SETTINGS_FILES,
        # The loop reads loop.toml without being asked, so a run may not edit it.
        CONFIG_FILENAME,
        # The worktree's .git pointer names the repository, and so the user file.
        ".git",
    )
    # The main checkout's git directory names the repository too (commondir).
    carve_out_prefixes: tuple[str, ...] = (".git/",)
    # A host's own stage prompts override the plugin's; they are loop files.
    loop_prefixes: tuple[str, ...] = ("{state_dir}/skills/pipeline/",)
    loop_exact: tuple[str, ...] = ()
    # An in-repo copy of the old hooks would drive the run a second time.
    guard_watch: tuple[str, ...] = (
        "{state_dir}/hooks/pipeline_stop.py",
        "{state_dir}/hooks/pipeline_guard.py",
    )
    stage_shorthand: str = "stages/"
    stage_target: str = "{state_dir}/skills/pipeline/stages/"
    stages: tuple[StageSpec, ...] = DEFAULT_STAGES
    session_label: str = "Session"
    resume_command: str = "/delivery-loop:pipeline resume"
    abort_command: str = "/delivery-loop:pipeline abort"
    tracker_prefix: str = "[A-Za-z]+"
    tracker_pattern: str = r"({tracker_prefix}-\d+)"
    # The harness's names for stopping a background task and for starting a
    # command in the background, quoted in what the agent is told.
    stop_task_tool: str = "TaskStop"
    background_flag: str = "run_in_background"
    # How long a background wait may last, with the session quiet as long,
    # before the supervisor escalates it (waiting_stale); a wait on shell
    # commands alone, such as a dev server left running, gets the shorter one.
    wait_stale_after_s: int = 3600
    shell_wait_stale_after_s: int = 900

    def __post_init__(self) -> None:
        _refuse_empty("state_dir", self.state_dir)
        _refuse_unsafe("state_dir", self.state_dir)
        # One spelling per directory, so "./.harness" and ".harness/" expand to
        # the same carve-outs the path checks compare against.
        _set(self, "state_dir", PurePosixPath(self.state_dir).as_posix())
        if self.state_dir == ".":
            raise ConfigError("state_dir must name a directory inside the worktree")
        _refuse_empty("state_root", self.state_root)
        if not (self.state_root.startswith("/") or self.state_root.startswith("~")):
            raise ConfigError(
                f"state_root must be an absolute path or start with ~: {self.state_root!r}"
            )
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
            _expand(self.tracker_pattern, {"tracker_prefix": _grouped(self.tracker_prefix)}),
        )

    def _validate(self) -> None:
        for name in ("ledger_dir", "stage_target", "stage_shorthand"):
            _refuse_empty(name, getattr(self, name))
            _refuse_unsafe(name, getattr(self, name))
        _refuse_empty("ledger_name", self.ledger_name)
        if "/" in self.ledger_name:
            raise ConfigError(f"ledger_name names one file, not a path: {self.ledger_name!r}")
        # The ledger is appended to under the home directory, so it may only ever
        # be a .jsonl file of its own, never an existing dotfile.
        if not self.ledger_name.endswith(".jsonl"):
            raise ConfigError(f"ledger_name must end in .jsonl: {self.ledger_name!r}")
        for name in _EXPANDED_TUPLES:
            for value in getattr(self, name):
                _refuse_empty(name, value)
                _refuse_unsafe(name, value)
        for name in (
            "session_label",
            "resume_command",
            "abort_command",
            "stop_task_tool",
            "background_flag",
        ):
            _refuse_empty(name, getattr(self, name))
        _refuse_empty("tracker_prefix", self.tracker_prefix)
        for name in ("wait_stale_after_s", "shell_wait_stale_after_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"{name} must be a whole number of seconds, 1 or more")
        try:
            re.compile(self.tracker_pattern)
        except (re.error, OverflowError, RecursionError) as exc:
            raise ConfigError(f"tracker_pattern is not a regular expression: {exc}") from exc
        # A repository file is read without a flag, and a nested quantifier such
        # as "(a+)+" can make one card take minutes to match a worktree name.
        if _NESTED_QUANTIFIER.search(self.tracker_pattern):
            raise ConfigError(
                f"tracker_pattern may not repeat a group that repeats: {self.tracker_pattern!r}"
            )
        self._validate_stages()

    def _validate_stages(self) -> None:
        _set(self, "stages", _as_tuple("stages", self.stages))
        if not self.stages:
            raise ConfigError("stages must name at least one stage")
        seen: set[str] = set()
        for stage in self.stages:
            if not isinstance(stage, StageSpec):
                raise ConfigError(f"every stage must be a StageSpec, got {stage!r}")
            if stage.name in seen:
                raise ConfigError(
                    f"two stages are named {stage.name!r}; every stage name must be unique"
                )
            seen.add(stage.name)

    @cached_property
    def stage_names(self) -> tuple[str, ...]:
        """The configured stage names, in order."""
        return tuple(stage.name for stage in self.stages)

    def stage(self, name: str) -> StageSpec:
        """The spec of one configured stage, by name."""
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise KeyError(name)

    @cached_property
    def carve_outs_cf(self) -> frozenset[str]:
        """The carve-out set, casefolded for a case-insensitive filesystem."""
        return frozenset(c.casefold() for c in self.carve_outs)

    @cached_property
    def carve_out_prefixes_cf(self) -> tuple[str, ...]:
        """The carve-out prefixes, casefolded for a case-insensitive filesystem."""
        return tuple(p.casefold() for p in self.carve_out_prefixes)

    @cached_property
    def tracker_re(self) -> re.Pattern[str]:
        """Matches one issue identifier inside a longer name."""
        return re.compile(self.tracker_pattern)

    def ledger_file(self, repo_slug: str) -> str:
        """The ledger file name for one repository slug."""
        return self.ledger_name.replace("{slug}", repo_slug)


DEFAULTS = LoopConfig()


def to_dict(config: LoopConfig) -> dict:
    """The config as plain JSON values, every placeholder already expanded."""
    data = {f.name: getattr(config, f.name) for f in fields(LoopConfig)}
    data["stages"] = [asdict(stage) for stage in config.stages]
    return {k: list(v) if isinstance(v, tuple) else v for k, v in data.items()}


def from_dict(data: object) -> LoopConfig:
    """The config ``to_dict`` wrote, validated again as it is rebuilt.

    A run records the config it started under, so the hooks judge it by that one
    and not by whatever the repository or the user file says later.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"a config snapshot must be an object, got {data!r}")
    known = {f.name for f in fields(LoopConfig)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"a config snapshot has unknown keys: {sorted(unknown)}")
    values = dict(data)
    stages = values.get("stages", [])
    if not isinstance(stages, list) or any(not isinstance(s, dict) for s in stages):
        raise ConfigError("a config snapshot's stages must be a list of objects")
    try:
        values["stages"] = tuple(StageSpec(**s) for s in stages)
        return LoopConfig(**{k: tuple(v) if isinstance(v, list) else v for k, v in values.items()})
    except TypeError as exc:
        raise ConfigError(f"a config snapshot does not fit LoopConfig: {exc}") from exc


# Where each field is written in a loop.toml: the field, its table, its key.
_STRING_KEYS = (
    ("state_dir", "harness", "state_dir"),
    ("state_root", "harness", "state_root"),
    ("state_file", "harness", "state_file"),
    ("session_label", "harness", "session_label"),
    ("stop_task_tool", "harness", "stop_task_tool"),
    ("background_flag", "harness", "background_flag"),
    ("ledger_dir", "ledger", "dir"),
    ("ledger_name", "ledger", "name"),
    ("stage_shorthand", "loop_paths", "stage_shorthand"),
    ("stage_target", "loop_paths", "stage_target"),
    ("resume_command", "commands", "resume"),
    ("abort_command", "commands", "abort"),
    ("tracker_prefix", "tracker", "prefix"),
    ("tracker_pattern", "tracker", "pattern"),
)
_INT_KEYS = (
    ("wait_stale_after_s", "supervisor", "wait_stale_after_seconds"),
    ("shell_wait_stale_after_s", "supervisor", "shell_wait_stale_after_seconds"),
)
_ADDITIVE = ("carve_outs", "carve_out_prefixes", "guard_watch")
_LIST_KEYS = (
    ("carve_outs", "loop_paths", "carve_outs"),
    ("carve_out_prefixes", "loop_paths", "carve_out_prefixes"),
    ("loop_prefixes", "loop_paths", "prefixes"),
    ("loop_exact", "loop_paths", "exact"),
    ("guard_watch", "loop_paths", "guard_watch"),
)


def _str(table: dict, key: str) -> str:
    value = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{key} takes a string, got {value!r}")
    return value


def _int(table: dict, key: str) -> int:
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} takes a whole number, got {value!r}")
    return value


def _strs(table: dict, key: str) -> tuple[str, ...]:
    value = table[key]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{key} takes a list of strings, got {value!r}")
    return tuple(value)


def _bool(table: dict, key: str, label: str) -> bool:
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{label} {key} takes true or false, got {value!r}")
    return value


def _stages(value: object) -> tuple[StageSpec, ...]:
    """The stage list from an array of ``[[stages]]`` tables, in file order."""
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise ConfigError(f"stages takes an array of [[stages]] tables, got {value!r}")
    if not value:
        raise ConfigError("stages must name at least one stage")
    specs = []
    for index, entry in enumerate(value, start=1):
        if "name" not in entry:
            raise ConfigError(f"stage {index} has no name; every [[stages]] table needs one")
        label = f"stage {entry['name']!r}"
        fields: dict[str, object] = {"name": _str(entry, "name")}
        for key in _STAGE_STRINGS:
            if key in entry:
                fields[key] = _str(entry, key)
        for key in _STAGE_BOOLS:
            if key in entry:
                fields[key] = _bool(entry, key, label)
        specs.append(StageSpec(**fields))
    return tuple(specs)


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table, got {value!r}")
    return value


# Keys the loop once read and no longer does. Ignoring one would silently change
# what the loop does, so each is refused with what replaced it.
_REMOVED_KEYS = (
    (
        "harness",
        "worktree_glob",
        "every run now lives under [harness] state_root, ~/.delivery-loop by default, "
        "and loop-scan reads it there",
    ),
)


def from_mapping(data: dict) -> LoopConfig:
    """One config from a parsed loop.toml, defaulting every key the file omits.

    A key the file leaves out keeps the field's own default, placeholders and
    all, so setting only the state directory moves every path that names it, and
    setting only the tracker prefix reshapes the tracker pattern. A key the loop
    does not read yet is ignored, but a key it no longer reads is refused, with
    what replaced it. An array of ``[[stages]]`` tables replaces the whole stage
    list; a file with no such array keeps the default five. The carve-out lists
    are the exception to replacement: a file's entries are added to the defaults,
    and the defaults stay in force at the default state directory too, so no
    file can take a carve-out away.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"a loop.toml must be a table, got {data!r}")
    for table_name, key, replacement in _REMOVED_KEYS:
        if key in _table(data, table_name):
            raise ConfigError(f"[{table_name}] {key} is no longer read: {replacement}")
    values: dict[str, object] = {}
    for field_name, table_name, key in _STRING_KEYS:
        table = _table(data, table_name)
        if key in table:
            values[field_name] = _str(table, key)
    for field_name, table_name, key in _LIST_KEYS:
        table = _table(data, table_name)
        if key in table:
            values[field_name] = _strs(table, key)
    for field_name, table_name, key in _INT_KEYS:
        table = _table(data, table_name)
        if key in table:
            values[field_name] = _int(table, key)
    if "stages" in data:
        values["stages"] = _stages(data["stages"])
    # A file may add carve-outs but never remove one: the carve-outs guard the
    # loop's own enforcement files, and a repository file is read without a flag.
    for name in _ADDITIVE:
        if name in values:
            default = LoopConfig.__dataclass_fields__[name].default
            values[name] = tuple(dict.fromkeys((*default, *values[name])))
    config = LoopConfig(**values)
    # The built-in carve-outs also stay in force at the default state
    # directory, so a file that moves state_dir cannot move them away.
    for name in _ADDITIVE:
        _set(config, name, tuple(dict.fromkeys((*getattr(config, name), *getattr(DEFAULTS, name)))))
    return config


# Variables that make git answer for another repository than the one in cwd, as
# they do inside a git hook. They are dropped, so cwd alone picks the repository.
_GIT_REPOSITORY_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


def harness_home() -> Path:
    """The harness's user directory, as the harness itself resolves it."""
    return Path(os.environ.get(HARNESS_HOME_ENV) or HARNESS_HOME).expanduser()


def user_git_configs() -> list[Path]:
    """The user-level git config files, in the order git reads them."""
    xdg = os.environ.get(XDG_CONFIG_ENV) or str(Path("~/.config").expanduser())
    return [Path(p.format(xdg=xdg)).expanduser() for p in USER_GIT_CONFIGS]


def user_config_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / USER_CONFIG_DIR


def git_env() -> dict[str, str]:
    """The environment git runs in: no variable that points it at another
    repository, and English messages, so cwd alone picks the repository."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPOSITORY_VARS}
    return env | {"LC_ALL": "C", "LANGUAGE": "C"}


def _git(cwd: Path, *args: str) -> str | None:
    """One line of git output, or None when cwd is in no repository.

    Any other failure raises ``ConfigError``: a missing or stuck git, or a
    repository git refuses to read. Treating those as "no repository" would drop
    the user file, and with it the carve-outs the operator added there.
    """
    # git translates its messages, and the "not a git repository" check below
    # reads one, so the messages are kept in English.
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=git_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"cannot run git in {cwd} to find the repository: {exc}") from exc
    out = done.stdout.strip()
    if done.returncode == 0 and out and "\n" in out:
        # git before 2.31 echoes --path-format back, which would read as a name.
        raise ConfigError(f"git in {cwd} answered more than one line; git 2.31 or later is needed")
    if done.returncode == 0 and out:
        return out
    if "not a git repository" in done.stderr and not _has_git_entry(cwd):
        return None
    # A .git that git cannot read, for example a worktree pointer a run rewrote,
    # is a failure, not "no repository": that would drop the user file.
    raise ConfigError(f"git cannot read the repository in {cwd}: {done.stderr.strip()}")


def _has_git_entry(start: Path) -> bool:
    """Whether ``start`` or a parent holds a ``.git`` file or directory."""
    path = Path(os.path.abspath(start))
    # lexists, so a dangling .git symlink counts as an entry too.
    return any(os.path.lexists(p / ".git") for p in (path, *path.parents))


def repo_root(start: Path) -> Path:
    """The top level of the repository ``start`` is in, or ``start`` outside one."""
    out = _git(start, "rev-parse", "--show-toplevel")
    return Path(out) if out else start


def repo_slug(start: Path) -> str | None:
    """The repository's name: the directory of its main checkout.

    Every worktree of one repository shares one git directory, so the slug is
    read from there and not from the worktree's own directory name, which a
    worktree manager is free to choose. Outside a repository there is no slug.
    """
    common = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return None
    common_dir = Path(common)
    # A dot-named git directory, .git or the .bare of a bare-clone layout, is
    # named after the checkout that holds it.
    slug = (common_dir.parent if common_dir.name.startswith(".") else common_dir).name
    slug = slug.removesuffix(".git")
    return slug if _SLUG_RE.fullmatch(slug) and slug not in (".", "..") else None


def _read(candidate: Path) -> dict:
    try:
        data = tomllib.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {candidate}: {exc}") from exc
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ConfigError(f"{candidate} is not valid TOML: {exc}") from exc
    return data


def _merge(base: dict, over: dict) -> dict:
    """``over`` on top of ``base``, table by table; any other value is replaced whole."""
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def config_paths(
    path: str | Path | None = None, slug: str | None = None, home: Path | None = None
) -> tuple[Path, Path | None]:
    """The repository file and the user file, strongest first, present or not.

    ``path`` names the repository's loop.toml or the directory that holds it.
    When omitted, the file is the one at the root of the repository the current
    directory is in, or in the current directory outside a repository. ``slug``
    names the repository for the user file and is read from git when omitted;
    with no slug there is no user file.

    Two repositories whose main checkouts share a directory name share a slug,
    and so share one user file.
    """
    if path is None:
        candidate = repo_root(Path.cwd()) / CONFIG_FILENAME
    else:
        candidate = Path(path)
        if candidate.is_dir():
            candidate = candidate / CONFIG_FILENAME
    if slug is None:
        slug = repo_slug(candidate.parent)
    elif not _SLUG_RE.match(slug) or slug in (".", ".."):
        raise ConfigError(f"unsafe repo slug: {slug!r}")
    if slug is None:
        return candidate, None
    return candidate, (home or Path.home()) / USER_CONFIG_DIR / f"{slug}.toml"


def load_config(
    path: str | Path | None = None,
    slug: str | None = None,
    home: Path | None = None,
    implicit: bool | None = None,
) -> LoopConfig:
    """The config from the repository file, the user file and the defaults, in that order.

    ``path``, ``slug`` and ``home`` are as ``config_paths`` takes them. Either
    file may be missing: the loop has a default for every value. A file that is
    present but unreadable, unparseable or invalid is an error, because whoever
    wrote one meant it to be read.

    A file read without being named, the user file always and the repository
    file unless ``path`` names it, may not set the machine-wide keys: the hook
    and every command must agree on where the run state lives. ``implicit``
    overrides that test for a caller that passes a path it found itself.
    """
    repo_file, user_file = config_paths(path, slug, home)
    implicit = path is None if implicit is None else implicit
    data: dict = {}
    added: dict[str, list] = {}
    layers: list[LoopConfig] = []
    for candidate in (user_file, repo_file):
        if candidate is not None and candidate.is_file():
            if candidate == repo_file and implicit and candidate.is_symlink():
                # The carve-out names loop.toml, not the file it points to, so a
                # link would let a run edit the config through its target.
                raise ConfigError(f"{candidate} is a symbolic link; commit the file itself")
            one = _read(candidate)
            if candidate == user_file or implicit:
                _refuse_machine_keys(candidate, one)
            # Each file is checked on its own, so a bad key is refused even when
            # the other file sets the same key over it.
            try:
                layers.append(from_mapping(one))
                data = _merge(data, one)
            except ConfigError as exc:
                raise ConfigError(f"{candidate}: {exc}") from exc
            except RecursionError as exc:
                raise ConfigError(f"{candidate}: its tables nest too deeply") from exc
            _collect_carve_outs(one, added)
    if added and isinstance(data.get("loop_paths"), dict):
        # The carve-out lists add up across the files, so the repository file
        # cannot drop a carve-out the user file adds. A loop_paths that is not a
        # table is left for from_mapping to refuse.
        data["loop_paths"] = {**data["loop_paths"], **added}
    if not data:
        return DEFAULTS
    config = from_mapping(data)
    # Each file's carve-outs stay in force as that file expanded them, so a file
    # that moves state_dir cannot carry another file's protections away.
    for name in _ADDITIVE:
        kept = (path for layer in layers for path in getattr(layer, name))
        _set(config, name, tuple(dict.fromkeys((*getattr(config, name), *kept))))
    return config


# Keys that say where the run state lives. Every worktree's hook and every
# supervisor command must read the same value, so they come only from
# DELIVERY_LOOP_HOME or a file named with --config.
_MACHINE_KEYS = ("state_root", "state_file")


def _refuse_machine_keys(candidate: Path, data: dict) -> None:
    table = data.get("harness")
    if not isinstance(table, dict):
        return
    for key in _MACHINE_KEYS:
        if key in table:
            raise ConfigError(
                f"{candidate}: [harness] {key} is read only from DELIVERY_LOOP_HOME or a file"
                " named with --config, so the hook and every command use one state location"
            )


def _collect_carve_outs(data: dict, added: dict[str, list]) -> None:
    table = data.get("loop_paths")
    if not isinstance(table, dict):
        return
    for field_name, _, key in _LIST_KEYS:
        if field_name in _ADDITIVE and key in table:
            # Checked here, because the list from another file would otherwise
            # replace a bad value before from_mapping could refuse it.
            added[key] = [*added.get(key, []), *_strs(table, key)]


def config_for_run(worktree: object, config: LoopConfig, explicit: bool) -> LoopConfig:
    """The config one run is judged by: its own worktree's, unless one was named.

    With no ``--config``, a command reads a run under the config of the worktree
    the run lives in, as ``loop-scan`` does, so every command applies the same
    carve-outs, stages and commands to it. A record that names a worktree that is
    gone raises ``ConfigError``, as a bad file does: judging it under another
    config could loosen its carve-outs.
    """
    if explicit or not isinstance(worktree, str):
        return config
    if not Path(worktree).is_dir():
        raise ConfigError(f"the worktree {worktree} is gone, so its config cannot be read")
    return load_config(Path(worktree), implicit=True)


def load_cli_config(path: str | Path | None, label: str = "CONFIG_INVALID") -> LoopConfig | None:
    """``load_config`` for a command's entry point: None after reporting a bad file.

    A command reads the repository file and the user file without being asked
    to, so a broken one is reported on stderr under ``label`` (``CONFIG_INVALID``
    unless the command names its own, as loop-scan and loop-prune do) and the
    command exits 2, the code for "cannot observe", and not with a traceback.
    """
    try:
        if path is not None:
            named = Path(path) / CONFIG_FILENAME if Path(path).is_dir() else Path(path)
            if not named.is_file():
                # A mistyped --config would otherwise judge every run by the defaults.
                raise ConfigError(f"--config names no file: {named}")
        return load_config(path)
    except ConfigError as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        return None
