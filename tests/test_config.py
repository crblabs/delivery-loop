"""The configuration seam: today's values as defaults, and a loop.toml honoured.

Every harness and tracker literal core once carried is now a field on
``LoopConfig``. Two things must hold. The defaults must reproduce, character for
character, the values the modules held inline, so an absent or empty loop.toml
changes nothing. And a loop.toml that moves those values must reach every module
that reads them, which is checked here through a real function from each.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import config as cfg
from core import pipeline_loop_paths as lp
from core import pipeline_state as ps
from core import supervisor_card as sc
from core import supervisor_decide as sd
from core import supervisor_ledger as sl
from core import supervisor_pause_stats as sps
from core import supervisor_scan as ss

NOW = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)

MOVED = """
[harness]
state_dir = ".harness"
state_file = "run.local.json"
worktree_glob = "~/trees/*/*/{state_dir}/{state_file}"

[loop_paths]
carve_outs = ["{state_dir}/settings.json", "{state_dir}/hooks/stop.py"]
carve_out_prefixes = ["{state_dir}/archive/"]
prefixes = ["{state_dir}/hooks/", "{state_dir}/stages/"]
exact = ["{state_dir}/settings.json"]
stage_shorthand = "stages/"
stage_target = "{state_dir}/stages/"

[ledger]
dir = "{state_dir}/watch"

[commands]
resume = "/loop go"
abort = "/loop stop"

[tracker]
prefix = "task"
pattern = "(task-\\\\d+)"
"""


def write(tmp_path: Path, text: str, name: str = "loop.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def moved(tmp_path: Path) -> cfg.LoopConfig:
    return cfg.load_config(write(tmp_path, MOVED))


# --- the defaults are today's values -----------------------------------------


def test_defaults_are_the_values_the_modules_held_inline() -> None:
    d = cfg.DEFAULTS
    assert d.state_dir == ".claude"
    assert d.state_file == "pipeline.local.json"
    assert d.ledger_dir == ".claude/supervisor"
    assert d.ledger_file("slug") == "slug-ledger.local.jsonl"
    assert d.carve_outs == (
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/hooks/pipeline_guard.py",
        ".claude/hooks/pipeline_stop.py",
        ".claude/hooks/pipeline-guard",
        ".claude/hooks/pipeline-stop",
        ".claude/hooks/pipeline_loop_paths.py",
    )
    assert d.carve_out_prefixes == (".claude/pipeline-runs.local.d/",)
    assert d.loop_prefixes == (".claude/hooks/", ".claude/skills/pipeline/")
    assert d.loop_exact == (".claude/settings.json",)
    assert d.stage_shorthand == "stages/"
    assert d.stage_target == ".claude/skills/pipeline/stages/"
    assert d.worktree_glob == "~/emdash/worktrees/*/*/.claude/pipeline.local.json"
    assert d.session_label == "Emdash session"
    assert d.resume_command == "/pipeline resume"
    assert d.abort_command == "/pipeline abort"
    assert d.tracker_prefix == "cod"
    assert d.tracker_pattern == r"(cod-\d+)"


def test_defaults_still_classify_the_paths_they_used_to() -> None:
    assert lp.is_carveout(".claude/hooks/pipeline_stop.py") is True
    assert lp.is_carveout(".claude/PIPELINE.local.json") is True
    assert lp.is_carveout(".claude/pipeline-runs.local.d/x.json") is True
    assert lp.is_carveout(".claude/skills/pipeline/stages/ship.md") is False
    declared = "```loop-edits\nstages/ship.md\n```"
    assert lp.parse_loop_edits_block(declared) == [".claude/skills/pipeline/stages/ship.md"]


def test_a_missing_file_yields_the_defaults(tmp_path: Path) -> None:
    assert cfg.load_config(tmp_path / "absent.toml") is cfg.DEFAULTS
    assert cfg.load_config(tmp_path) is cfg.DEFAULTS
    assert cfg.load_config(None) is cfg.DEFAULTS


def test_an_empty_file_yields_the_defaults(tmp_path: Path) -> None:
    assert cfg.load_config(write(tmp_path, "")) == cfg.DEFAULTS


def test_a_partial_file_keeps_every_other_default(tmp_path: Path) -> None:
    config = cfg.load_config(write(tmp_path, '[tracker]\nprefix = "task"\n'))
    assert config.tracker_pattern == r"(task-\d+)"
    assert config.state_dir == cfg.DEFAULTS.state_dir
    assert config.carve_outs == cfg.DEFAULTS.carve_outs


def test_a_directory_argument_finds_the_file(tmp_path: Path) -> None:
    write(tmp_path, '[harness]\nstate_dir = ".harness"\n')
    assert cfg.load_config(tmp_path).state_dir == ".harness"


def test_moving_only_the_state_dir_moves_every_path_that_names_it(tmp_path: Path) -> None:
    config = cfg.load_config(write(tmp_path, '[harness]\nstate_dir = ".harness"\n'))
    assert config.carve_outs[0] == ".harness/settings.json"
    assert config.carve_out_prefixes == (".harness/pipeline-runs.local.d/",)
    assert config.loop_prefixes == (".harness/hooks/", ".harness/skills/pipeline/")
    assert config.ledger_dir == ".harness/supervisor"
    assert config.worktree_glob.endswith(".harness/pipeline.local.json")


def test_the_shipped_template_reproduces_the_defaults() -> None:
    """The template is the documentation of the seam, so it must still say what
    the code does. Filling it in with the values it ships changes nothing."""
    template = Path(__file__).resolve().parent.parent / "templates" / "loop.toml"
    assert cfg.load_config(template) == cfg.DEFAULTS


# --- one loop.toml, honoured by every module that reads it -------------------


def test_moved_state_dir_reaches_the_state_reader(moved: cfg.LoopConfig, tmp_path: Path) -> None:
    assert ps.state_path(tmp_path, moved) == tmp_path / ".harness" / "run.local.json"


def test_moved_state_dir_reaches_the_loop_paths(moved: cfg.LoopConfig) -> None:
    assert lp.is_carveout(".harness/hooks/stop.py", moved) is True
    assert lp.is_carveout(".harness/run.local.lock", moved) is True
    assert lp.is_carveout(".harness/archive/run.json", moved) is True
    # The old spelling is nobody's enforcement file once the directory moved.
    assert lp.is_carveout(".claude/hooks/pipeline_stop.py", moved) is False
    declared = "```loop-edits\nstages/ship.md\n```"
    assert lp.parse_loop_edits_block(declared, moved) == [".harness/stages/ship.md"]
    assert lp.classify_loop_path(".harness/hooks/stop.py", [], moved) == "denied-carveout"


def test_moved_state_dir_reaches_the_ledger(moved: cfg.LoopConfig, tmp_path: Path) -> None:
    path = sl.ledger_path("slug", home=tmp_path, config=moved)
    assert path == tmp_path / ".harness" / "watch" / "slug-ledger.local.jsonl"
    assert sl.acquire_lock("slug", home=tmp_path, config=moved) is not None
    assert (tmp_path / ".harness" / "watch" / "slug-ledger.local.lock").exists()


def test_moved_state_dir_reaches_the_scanner(moved: cfg.LoopConfig, tmp_path: Path) -> None:
    worktree = tmp_path / "repo" / "task"
    (worktree / ".harness").mkdir(parents=True)
    state = {
        "version": 1,
        "run_id": "9f1c2e7a-3b4d-4e5f-8a6b-7c8d9e0f1a2b",
        "revision": 1,
        "stages": list(ps.STAGES),
        "current": 0,
        "current_stage": "autoplan",
        "status": "running",
        "attempts": {s: 0 for s in ps.STAGES},
        "total_attempts": 0,
        "session_id": "abc",
        "updated_at": "2026-09-16T09:59:30+00:00",
        "history": [],
    }
    ps.state_path(worktree, moved).write_text(json.dumps(state), encoding="utf-8")
    pattern = str(tmp_path / "*" / "*" / ".harness" / "run.local.json")
    records = ss.scan(pattern, None, NOW, 600, moved)
    assert [r["condition"] for r in records] == ["ok"]
    assert records[0]["worktree"] == str(worktree)


def test_moved_commands_and_tracker_reach_the_card(moved: cfg.LoopConfig) -> None:
    record = {
        "task": "TASK-9",
        "current_stage": "ship",
        "status": "failed",
        "session_id": "abcdef12-0000",
        "worktree": "/w/operator-task-9-read",
        "updated_at": "2026-09-22T08:05:25Z",
        "paused_reason": "cap_total",
    }
    card = sc.render(record, None, moved)
    assert card.split("\n")[0].endswith("Emdash session abcdef12 (task-9)")
    assert "1. Fix the cause, then /loop go" in card
    assert "2. /loop stop" in card


def test_moved_carve_outs_reach_the_decision_and_the_pause_stats(moved: cfg.LoopConfig) -> None:
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{".harness/hooks/stop.py": "aaa"}],
        "guard_pending": {".harness/hooks/stop.py": "bbb"},
    }
    allowlist = [".harness/hooks/stop.py"]
    decision = sd.decide(record, allowlist, None, False, moved)
    # The moved carve-out wins over the allowlist, so this can never auto-accept.
    assert decision["action"] == "escalate"
    assert sps.classify_pause(record, allowlist, moved) == "carveout"
    # The same record under the defaults is off the carve-out list entirely.
    assert sd.decide(record, allowlist, None, False)["action"] == "auto_accept"
    assert sps.classify_pause(record, allowlist) == "removable_auto_accept"


def test_moved_tracker_pattern_is_the_only_one_recognised(moved: cfg.LoopConfig) -> None:
    record = {"task": "T", "status": "running", "worktree": "/w/operator-cod-9-read"}
    header = sc.header(record, "running", moved)
    assert "(operator-cod-9-read)" in header
    assert "(cod-9)" in sc.header(record, "running")


# --- what a loop.toml may not say --------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        '[loop_paths]\ncarve_outs = ["/etc/passwd"]\n',
        '[loop_paths]\ncarve_outs = ["../outside/settings.json"]\n',
        '[loop_paths]\ncarve_outs = ["a/../../outside"]\n',
        '[loop_paths]\nexact = ["/absolute"]\n',
        '[loop_paths]\ncarve_out_prefixes = ["/absolute/"]\n',
    ],
)
def test_an_unsafe_path_is_refused(tmp_path: Path, table: str) -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, table))


@pytest.mark.parametrize(
    "table",
    [
        '[loop_paths]\nprefixes = [""]\n',
        '[loop_paths]\ncarve_out_prefixes = ["   "]\n',
        '[loop_paths]\ncarve_outs = [""]\n',
        '[harness]\nstate_dir = ""\n',
        '[harness]\nstate_file = ""\n',
        '[commands]\nabort = ""\n',
    ],
)
def test_an_empty_value_is_refused(tmp_path: Path, table: str) -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, table))


def test_a_tracker_pattern_that_does_not_compile_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, '[tracker]\npattern = "(unclosed"\n'))


def test_a_state_file_that_is_a_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, '[harness]\nstate_file = "a/b.json"\n'))


def test_a_wrong_type_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, "[harness]\nstate_dir = 3\n"))
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, '[loop_paths]\ncarve_outs = "one-string"\n'))


def test_broken_toml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, "[harness\n"))


def test_the_config_is_frozen() -> None:
    with pytest.raises(AttributeError):
        cfg.DEFAULTS.state_dir = ".other"
