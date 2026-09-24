"""The stage list is configuration, and the five defaults are today's loop.

Two things must hold. The defaults must reproduce the stages the loop ran on
when they were a module constant, each with the contract its own stage prompt
states, so a host that declares nothing sees no change. And a host that declares
another skillset must be driven by that list end to end, with the state reader
refusing a run recorded under a different one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import config as cfg
from core import pipeline_state as ps
from core import supervisor_scan as ss

NOW = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)

# Another team's skillset: three stages, one of them with no command at all.
THREE = """
[[stages]]
name = "triage"
command = "/triage"
emits = "TICKET: the identifier the fix is filed under"
gate = "approval"

[[stages]]
name = "fix"
clean_tree = true

[[stages]]
name = "release"
command = "/release"
prompt = "release/publish.md"
emits = "RELEASE: the tag that was pushed"
clean_tree = true
"""


def write(tmp_path: Path, text: str, name: str = "loop.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def three(tmp_path: Path) -> cfg.LoopConfig:
    return cfg.load_config(write(tmp_path, THREE))


def make_state(names, current: int = 0, **over) -> dict:
    state = {
        "version": 1,
        "run_id": "9f1c2e7a-3b4d-4e5f-8a6b-7c8d9e0f1a2b",
        "revision": 1,
        "stages": list(names),
        "current": current,
        "current_stage": names[current],
        "status": "running",
        "attempts": dict.fromkeys(names, 0),
        "total_attempts": 0,
        "session_id": "abc",
        "repo": "crblabs/delivery-loop",
        "updated_at": "2026-09-16T09:59:30+00:00",
        "history": [],
    }
    state.update(over)
    return state


def write_state(worktree: Path, state: dict, config: cfg.LoopConfig = cfg.DEFAULTS) -> Path:
    path = ps.state_path(worktree, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


# --- the defaults are today's stages -----------------------------------------


def test_the_defaults_are_todays_five_stages_in_order() -> None:
    assert cfg.DEFAULTS.stage_names == ("autoplan", "implement", "qa", "review", "ship")
    assert cfg.DEFAULTS.stage_names == ps.STAGES
    assert ps.stage_names() == cfg.DEFAULTS.stage_names


def test_each_default_stage_declares_the_contract_its_prompt_states() -> None:
    commands = {s.name: s.command for s in cfg.DEFAULTS.stages}
    assert commands == {
        "autoplan": "/autoplan",
        # The model builds the plan itself here, with no skill behind it.
        "implement": "",
        "qa": "/qa",
        "review": "/review",
        "ship": "/ship",
    }
    assert [s.prompt for s in cfg.DEFAULTS.stages] == [
        "autoplan.md",
        "implement.md",
        "qa.md",
        "review.md",
        "ship.md",
    ]
    assert cfg.DEFAULTS.stage("autoplan").emits.startswith("PLAN:")
    assert cfg.DEFAULTS.stage("ship").emits.startswith("PR:")
    assert [s.emits for s in cfg.DEFAULTS.stages if s.emits] == [
        cfg.DEFAULTS.stage("autoplan").emits,
        cfg.DEFAULTS.stage("ship").emits,
    ]
    # The planner writes outside the worktree, so only it skips the tree check.
    assert [s.name for s in cfg.DEFAULTS.stages if not s.clean_tree] == ["autoplan"]
    assert {s.name: s.gate for s in cfg.DEFAULTS.stages} == {
        "autoplan": "approval",
        "implement": "none",
        "qa": "none",
        "review": "review_batch",
        "ship": "none",
    }
    assert [s.name for s in cfg.DEFAULTS.stages if s.sandbox_off] == ["qa"]


def test_an_empty_command_is_valid() -> None:
    stage = cfg.StageSpec(name="implement")
    assert stage.command == ""
    assert cfg.LoopConfig(stages=(stage,)).stage_names == ("implement",)


def test_a_minimal_entry_names_its_own_prompt(tmp_path: Path) -> None:
    config = cfg.load_config(write(tmp_path, '[[stages]]\nname = "fix"\n'))
    (stage,) = config.stages
    assert stage.prompt == "fix.md"
    assert (stage.command, stage.emits, stage.gate) == ("", "", "none")
    assert (stage.clean_tree, stage.sandbox_off) == (False, False)


def test_an_unknown_stage_name_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        cfg.DEFAULTS.stage("triage")


# --- another skillset, honoured end to end -----------------------------------


def test_three_declared_stages_replace_the_five(three: cfg.LoopConfig) -> None:
    assert three.stage_names == ("triage", "fix", "release")
    assert three.stage("fix").command == ""
    assert three.stage("release").prompt == "release/publish.md"
    assert three.stage("triage").gate == "approval"
    # The rest of the config is untouched by a declared stage list.
    assert three.state_dir == cfg.DEFAULTS.state_dir


def test_the_state_reader_reads_a_run_of_the_declared_stages(
    three: cfg.LoopConfig, tmp_path: Path
) -> None:
    worktree = tmp_path / "repo" / "task"
    path = write_state(worktree, make_state(three.stage_names, current=1), three)
    condition, state = ps.read_state(path, three)
    assert condition == "ok"
    assert state["current_stage"] == "fix"


def test_the_scanner_reports_a_run_of_the_declared_stages(
    three: cfg.LoopConfig, tmp_path: Path
) -> None:
    worktree = tmp_path / "repo" / "task"
    write_state(worktree, make_state(three.stage_names, current=2), three)
    pattern = str(tmp_path / "*" / "*" / ".claude" / "pipeline.local.json")
    (record,) = ss.scan(pattern, None, NOW, 600, three)
    assert record["condition"] == "ok"
    # The count a person is shown is read off the list, not written as five.
    assert (record["stage_num"], record["stage_count"]) == (3, 3)


# --- a run of one stage list is never read as a run of another ---------------


def test_a_state_recorded_under_another_stage_list_is_refused(
    three: cfg.LoopConfig, tmp_path: Path
) -> None:
    worktree = tmp_path / "repo" / "task"
    path = write_state(worktree, make_state(cfg.DEFAULTS.stage_names))
    assert ps.read_state(path, cfg.DEFAULTS)[0] == "ok"
    assert ps.read_state(path, three)[0] == "corrupt"


def test_a_state_of_the_declared_stages_is_refused_under_the_defaults(
    three: cfg.LoopConfig, tmp_path: Path
) -> None:
    worktree = tmp_path / "repo" / "task"
    path = write_state(worktree, make_state(three.stage_names), three)
    assert ps.read_state(path, three)[0] == "ok"
    assert ps.read_state(path)[0] == "corrupt"


def test_the_same_names_in_another_order_are_refused(tmp_path: Path) -> None:
    reversed_names = tuple(reversed(cfg.DEFAULTS.stage_names))
    path = write_state(tmp_path / "w", make_state(reversed_names))
    assert ps.read_state(path)[0] == "corrupt"


def test_an_attempts_map_of_another_stage_list_is_refused(
    three: cfg.LoopConfig, tmp_path: Path
) -> None:
    state = make_state(three.stage_names, attempts=dict.fromkeys(cfg.DEFAULTS.stage_names, 0))
    path = write_state(tmp_path / "w", state, three)
    assert ps.read_state(path, three)[0] == "corrupt"


# --- what a stage list may not say -------------------------------------------


@pytest.mark.parametrize(
    "text,rule",
    [
        ("stages = []\n", "at least one stage"),
        ('[[stages]]\ncommand = "/fix"\n', "no name"),
        ('[[stages]]\nname = "fix"\n\n[[stages]]\nname = "fix"\n', "unique"),
        ('[[stages]]\nname = ""\n', "needs a name"),
        ('[[stages]]\nname = "a/b"\n', "file name component"),
        ('[[stages]]\nname = "../escape"\n', "file name component"),
        ('[[stages]]\nname = ".hidden"\n', "file name component"),
        ('[[stages]]\nname = "fix"\ngate = "sometimes"\n', "gate must be one of"),
        ('[[stages]]\nname = "fix"\nprompt = "/etc/passwd"\n', "repository-relative"),
        ('[[stages]]\nname = "fix"\nprompt = "../outside.md"\n', "escape"),
        ('[[stages]]\nname = "fix"\nprompt = ""\n', "may not be empty"),
        ("[[stages]]\nname = 3\n", "takes a string"),
        ('[[stages]]\nname = "fix"\nclean_tree = "yes"\n', "true or false"),
        ('[[stages]]\nname = "fix"\nsandbox_off = 1\n', "true or false"),
        ('[[stages]]\nname = "fix"\ncommand = 7\n', "takes a string"),
        ('stages = ["fix"]\n', "array of [[stages]] tables"),
        ('[stages]\nname = "fix"\n', "array of [[stages]] tables"),
    ],
)
def test_each_stage_rule_refuses_and_names_itself(tmp_path: Path, text: str, rule: str) -> None:
    with pytest.raises(cfg.ConfigError) as caught:
        cfg.load_config(write(tmp_path, text))
    assert rule in str(caught.value)


def test_a_duplicate_name_key_in_one_table_is_refused(tmp_path: Path) -> None:
    # TOML refuses the second key outright, so the first is never silently kept.
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, '[[stages]]\nname = "fix"\nname = "ship"\n'))


def test_an_empty_stage_tuple_is_refused() -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.LoopConfig(stages=())


def test_a_stage_list_of_something_other_than_specs_is_refused() -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.LoopConfig(stages=({"name": "fix"},))
    with pytest.raises(cfg.ConfigError):
        cfg.LoopConfig(stages="fix")


def test_a_stage_spec_is_frozen() -> None:
    with pytest.raises(AttributeError):
        cfg.DEFAULTS.stages[0].name = "other"
