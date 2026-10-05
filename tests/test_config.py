"""The configuration seam: pinned defaults, and a loop.toml honoured.

Every harness and tracker literal core once carried is now a field on
``LoopConfig``. Three things must hold. The defaults are pinned here, value for
value, and name no one operator's setup. A loop.toml that moves those values
must reach every module that reads them, which is checked here through a real
function from each. And the loader reads the repository file over the user file
over the defaults.
"""

from __future__ import annotations

import json
import os
import subprocess
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
state_root = "@ROOT@"
state_file = "run.json"

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
def moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> cfg.LoopConfig:
    # The environment wins over the config, so the moved root is only seen
    # with the environment variable unset.
    monkeypatch.delenv(ps.HOME_ENV)
    return cfg.load_config(write(tmp_path, MOVED.replace("@ROOT@", str(tmp_path / "moved-home"))))


# --- the defaults are pinned ---------------------------------------------------


def test_the_defaults_are_pinned() -> None:
    d = cfg.DEFAULTS
    assert d.state_dir == ".claude"
    assert d.state_root == "~/.delivery-loop"
    assert d.state_file == "state.json"
    assert d.ledger_dir == ".claude/supervisor"
    assert d.ledger_file("slug") == "slug-ledger.local.jsonl"
    assert d.carve_outs == (
        ".claude/settings.json",
        ".claude/settings.local.json",
        "loop.toml",
        ".git",
    )
    assert d.carve_out_prefixes == (".git/",)
    assert d.loop_prefixes == (".claude/skills/pipeline/",)
    assert d.loop_exact == ()
    assert d.guard_watch == (".claude/hooks/pipeline_stop.py", ".claude/hooks/pipeline_guard.py")
    assert d.stage_shorthand == "stages/"
    assert d.stage_target == ".claude/skills/pipeline/stages/"
    assert d.session_label == "Session"
    assert d.resume_command == "/delivery-loop:pipeline resume"
    assert d.abort_command == "/delivery-loop:pipeline abort"
    assert d.tracker_prefix == "[A-Za-z]+"
    assert d.tracker_pattern == r"([A-Za-z]+-\d+)"


def test_defaults_still_classify_the_paths_they_used_to() -> None:
    assert lp.is_carveout(".claude/settings.json") is True
    assert lp.is_carveout(".claude/settings.local.json") is True
    # The hooks ship in the plugin now, so a worktree copy of them protects nothing.
    assert lp.is_carveout(".claude/hooks/pipeline_stop.py") is False
    # The run state is not in the repository, so no repository path is it.
    assert lp.is_carveout(".claude/pipeline.local.json") is False
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
    # The settings files stay where Claude Code reads them.
    assert config.carve_outs[0] == ".claude/settings.json"
    assert config.loop_prefixes == (".harness/skills/pipeline/",)
    assert config.guard_watch[0] == ".harness/hooks/pipeline_stop.py"
    assert config.ledger_dir == ".harness/supervisor"


def test_the_shipped_template_reproduces_the_defaults() -> None:
    """The template is the documentation of the seam, so it must still say what
    the code does. Filling it in with the values it ships changes nothing."""
    template = Path(__file__).resolve().parent.parent / "templates" / "loop.toml"
    assert cfg.load_config(template) == cfg.DEFAULTS


# --- one loop.toml, honoured by every module that reads it -------------------


def test_moved_state_root_reaches_the_state_reader(moved: cfg.LoopConfig, tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    path = ps.state_path(worktree, "crblabs/host", moved)
    assert path.parent.parent == tmp_path / "moved-home" / "runs" / "crblabs-host"
    assert path.name == "run.json"


def test_moved_state_dir_reaches_the_loop_paths(moved: cfg.LoopConfig) -> None:
    assert lp.is_carveout(".harness/hooks/stop.py", moved) is True
    assert lp.is_carveout(".harness/archive/run.json", moved) is True
    # A file that moves the directory cannot move the built-in carve-outs away.
    assert lp.is_carveout(".claude/settings.json", moved) is True
    assert lp.is_carveout("loop.toml", moved) is True
    declared = "```loop-edits\nstages/ship.md\n```"
    assert lp.parse_loop_edits_block(declared, moved) == [".harness/stages/ship.md"]
    assert lp.classify_loop_path(".harness/hooks/stop.py", [], moved) == "denied-carveout"


def test_moved_state_dir_reaches_the_ledger(moved: cfg.LoopConfig, tmp_path: Path) -> None:
    path = sl.ledger_path("slug", home=tmp_path, config=moved)
    assert path == tmp_path / ".harness" / "watch" / "slug-ledger.local.jsonl"
    assert sl.acquire_lock("slug", home=tmp_path, config=moved) is not None
    assert (tmp_path / ".harness" / "watch" / "slug-ledger.local.lock").exists()


def test_moved_state_root_reaches_the_scanner(moved: cfg.LoopConfig, tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    path = ps.prepare_run_dir(worktree, "crblabs/host", moved)
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
    path.write_text(json.dumps(state), encoding="utf-8")
    records = ss.scan(None, NOW, 600, moved)
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
    assert card.split("\n")[0].endswith("Session abcdef12 (task-9)")
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
        '[harness]\nstate_root = "relative/home"\n',
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
        '[harness]\nstate_root = ""\n',
        '[commands]\nabort = ""\n',
    ],
)
def test_an_empty_value_is_refused(tmp_path: Path, table: str) -> None:
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, table))


def test_the_worktree_git_pointer_is_a_carve_out() -> None:
    # Value: protects=a run cannot rewrite the .git pointer that names its repository and so
    # its user file; fails_when=.git is dropped from the built-in carve-outs;
    # why_new=CRB-20 removed the old .git carve-out with the in-repo state; seam=none
    assert lp.is_carveout(".git", cfg.DEFAULTS) is True
    assert lp.is_carveout(".GIT", cfg.DEFAULTS) is True


def test_a_found_repo_file_cannot_move_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Value: protects=a committed loop.toml read without a flag cannot move the run state;
    # fails_when=the machine-wide keys are allowed in a file the loader finds itself;
    # why_new=only a file named with --config may set them; seam=none
    repo = git_repo(tmp_path / "mine")
    write(repo, '[harness]\nstate_root = "/elsewhere"\n')
    monkeypatch.chdir(repo)
    with pytest.raises(cfg.ConfigError, match="state_root is read only from"):
        cfg.load_config(home=tmp_path / "home")
    assert cfg.load_config(repo / "loop.toml", home=tmp_path / "home").state_root == "/elsewhere"


def test_an_orphaned_run_is_escalated_as_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=decide escalates an orphaned run as orphaned, naming loop-prune;
    # fails_when=decide judges it, or labels it config_invalid; why_new=CRB-20 orphaned runs
    # met the per-run lookup here; seam=none
    record = {"worktree": str(tmp_path / "gone"), "condition": "ok", "orphaned": True}
    record_file = tmp_path / "record.json"
    record_file.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert sd.main(["--record-file", str(record_file)]) == 0
    out, err = capsys.readouterr()
    decision = json.loads(out)
    assert (decision["action"], decision["reason"]) == ("escalate", "orphaned")
    assert "loop-prune" in decision["notify"]
    assert "CONFIG_INVALID" not in err
    assert sc.main(["--record-file", str(record_file)]) == 0
    out, err = capsys.readouterr()
    assert "Worktree is gone" in out
    assert "CONFIG_INVALID" not in err


def test_an_orphaned_run_of_a_custom_stage_list_is_still_orphaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=an orphaned run that reads as corrupt under the default stages is still
    # escalated as orphaned; fails_when=decide checks the condition before orphaned;
    # why_new=the red team reproduced an "unobservable" escalation for it; seam=none
    record = {"worktree": str(tmp_path / "gone"), "condition": "corrupt", "orphaned": True}
    record_file = tmp_path / "record.json"
    record_file.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert sd.main(["--record-file", str(record_file)]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "orphaned"


def test_a_repo_file_cannot_move_a_user_carve_out_away(tmp_path: Path) -> None:
    # Value: protects=carve-outs the user file implies stay protected when the repository file
    # moves state_dir; fails_when=carve-outs expand only under the final state_dir;
    # why_new=two Codex passes reproduced an auto_accept this way; seam=none
    home = tmp_path / "home"
    user_file(
        home,
        "mine",
        '[harness]\nstate_dir = ".agent"\n'
        '[loop_paths]\ncarve_outs = ["{state_dir}/private.json"]\n',
    )
    repo = git_repo(tmp_path / "mine")
    write(repo, '[harness]\nstate_dir = "elsewhere"\n')
    config = cfg.load_config(repo, home=home)
    for path in (".agent/private.json", ".claude/settings.json", "elsewhere/private.json"):
        assert lp.is_carveout(path, config) is True, path


def test_a_symlinked_repo_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Value: protects=a committed loop.toml cannot point at an unprotected file a run may edit;
    # fails_when=the loader follows a symlinked loop.toml it found itself;
    # why_new=Codex found the carve-out bypass through the link target; seam=none
    repo = git_repo(tmp_path / "mine")
    (repo / "settings.toml").write_text('[tracker]\nprefix = "eng"\n', encoding="utf-8")
    (repo / "loop.toml").symlink_to(repo / "settings.toml")
    monkeypatch.chdir(repo)
    with pytest.raises(cfg.ConfigError, match="symbolic link"):
        cfg.load_config(home=tmp_path / "home")
    # A link the operator names with --config is the operator's choice.
    assert cfg.load_config(repo / "loop.toml", home=tmp_path / "home").tracker_prefix == "eng"


def test_the_main_checkout_git_directory_is_a_carve_out() -> None:
    # Value: protects=a run cannot rewrite .git/commondir to change the repository and so the
    # user file; fails_when=.git/ is dropped from the built-in carve-out prefixes;
    # why_new=Codex found .git/commondir authorizable; seam=none
    assert lp.is_carveout(".git/commondir", cfg.DEFAULTS) is True


def test_a_mistyped_config_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Value: protects=a --config that names no file stops the command instead of judging every
    # run by the defaults; fails_when=load_cli_config falls back to DEFAULTS for a missing
    # file; why_new=the native adversarial pass found the silent fallback; seam=none
    assert cfg.load_cli_config(tmp_path / "typo-loop.toml") is None
    assert "--config names no file" in capsys.readouterr().err
    assert cfg.load_cli_config(tmp_path) is None


def test_a_bare_layout_repo_is_named_after_its_checkout(tmp_path: Path) -> None:
    # Value: protects=repositories in the .bare layout each get their own user file;
    # fails_when=repo_slug names the repository after the .bare directory;
    # why_new=the native adversarial pass found every such repo sharing .bare.toml; seam=none
    source = git_repo(tmp_path / "source")
    commit(source)
    project = tmp_path / "projA"
    project.mkdir()
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(project / ".bare")], check=True
    )
    (project / ".git").write_text("gitdir: ./.bare\n", encoding="utf-8")
    tree = tmp_path / "trees" / "fix"
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "-q", "-b", "fix", str(tree)], check=True
    )
    assert cfg.repo_slug(tree) == "projA"


def test_a_tracker_prefix_alternation_stays_one_unit() -> None:
    # Value: protects=a prefix such as "cod|eng" matches whole identifiers; fails_when=the
    # prefix is substituted without a group; why_new=the native pass found wrong task ids;
    # seam=none
    config = cfg.LoopConfig(tracker_prefix="cod|eng")
    assert config.tracker_re.search("decode-eng-5").group(1) == "eng-5"


@pytest.mark.parametrize("pattern", ["((a+)+-\\\\d+)", "((\\\\w*)*)"])
def test_a_backtracking_tracker_pattern_is_refused(tmp_path: Path, pattern: str) -> None:
    # Value: protects=a repository file cannot hang loop-card with a catastrophic pattern;
    # fails_when=the nested-quantifier check is removed; why_new=Codex reproduced a hang;
    # seam=none
    with pytest.raises(cfg.ConfigError, match="may not repeat a group"):
        cfg.load_config(write(tmp_path, f'[tracker]\npattern = "{pattern}"\n'))


def test_tables_that_nest_too_deeply_across_files_are_refused(tmp_path: Path) -> None:
    # Value: protects=two valid but deeply nested files are a ConfigError, not a crash;
    # fails_when=_merge recursion escapes load_config; why_new=Codex reproduced the crash;
    # seam=none
    deep = "[" + ".".join(["a"] * 3000) + "]\nx = 1\n"
    home = tmp_path / "home"
    user_file(home, "mine", deep)
    repo = git_repo(tmp_path / "mine")
    write(repo, deep)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(repo, home=home)


def test_a_git_that_answers_two_lines_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Value: protects=an old git that echoes --path-format cannot make the slug unreadable and
    # silently drop the user file; fails_when=_git accepts multi-line output;
    # why_new=the native pass found git before 2.31 does this; seam=none
    def old_git(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "--path-format=absolute\n.git\n", "")

    monkeypatch.setattr(cfg.subprocess, "run", old_git)
    with pytest.raises(cfg.ConfigError, match="git 2.31"):
        cfg.repo_slug(tmp_path)


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


# Value: protects=a loop.toml that still sets worktree_glob fails loudly and names state_root;
#   fails_when=the removed key is silently ignored again;
#   why_new=only unknown future keys were tested;
#   seam=none
def test_a_removed_worktree_glob_is_refused_with_its_replacement(tmp_path: Path) -> None:
    with pytest.raises(cfg.ConfigError, match="state_root"):
        cfg.load_config(write(tmp_path, '[harness]\nworktree_glob = "~/trees/*/*"\n'))


USER_ADDS = '[loop_paths]\ncarve_outs = ["secret.txt"]\n'


# --- where the config is read from, and per-run config -------------------------


@pytest.mark.parametrize(
    ("name", "identifier"),
    [
        ("operator-cod-14-match-existing", "cod-14"),
        ("operator-abc-21-neutral-defaults", "abc-21"),
        ("ENG-1234-fix-login", "ENG-1234"),
    ],
)
def test_the_default_tracker_pattern_fits_any_tracker(name: str, identifier: str) -> None:
    match = cfg.DEFAULTS.tracker_re.search(name)
    assert match is not None and match.group(1) == identifier


def user_file(home: Path, slug: str, text: str) -> Path:
    path = home / cfg.USER_CONFIG_DIR / f"{slug}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def commit(repo: Path) -> None:
    identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t"}
    identity |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env={**os.environ, **identity},
    )


def test_a_repo_with_no_file_anywhere_runs_on_the_defaults(tmp_path: Path) -> None:
    repo = git_repo(tmp_path / "plain")
    assert cfg.load_config(repo, home=tmp_path / "home") is cfg.DEFAULTS


def test_the_user_file_configures_its_own_repo(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user_file(home, "mine", '[tracker]\nprefix = "task"\n')
    config = cfg.load_config(git_repo(tmp_path / "mine"), home=home)
    assert config.tracker_pattern == r"(task-\d+)"


def test_the_user_file_does_not_reach_another_repo(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user_file(home, "mine", '[tracker]\nprefix = "task"\n')
    assert cfg.load_config(git_repo(tmp_path / "other"), home=home) is cfg.DEFAULTS


def test_the_repo_file_wins_key_by_key_over_the_user_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user_file(
        home,
        "mine",
        '[harness]\nsession_label = "Desk"\n[tracker]\nprefix = "task"\n',
    )
    repo = git_repo(tmp_path / "mine")
    write(repo, '[tracker]\nprefix = "eng"\n')
    config = cfg.load_config(repo, home=home)
    assert config.tracker_pattern == r"(eng-\d+)"
    # A key the repository file leaves out still comes from the user file.
    assert config.session_label == "Desk"


def test_the_two_files_merge_inside_one_table(tmp_path: Path) -> None:
    # Value: protects=a repo file key does not drop a user file key in the same table;
    # fails_when=the merge replaces a whole table instead of merging it key by key;
    # why_new=the key by key test above sets its keys in two different tables; seam=none
    home = tmp_path / "home"
    user_file(home, "mine", '[harness]\nsession_label = "Mine"\n')
    repo = git_repo(tmp_path / "mine")
    write(repo, '[harness]\nstate_dir = ".harness"\n')
    config = cfg.load_config(repo, home=home)
    assert config.state_dir == ".harness"
    assert config.session_label == "Mine"


def test_a_subdirectory_of_the_repo_finds_the_root_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Value: protects=a command run in a subdirectory reads loop.toml at the repository root;
    # fails_when=config_paths stops asking git for the top level of the current directory;
    # why_new=every other test passes a path; seam=none
    repo = git_repo(tmp_path / "mine")
    write(repo, '[tracker]\nprefix = "eng"\n')
    inner = repo / "core" / "deep"
    inner.mkdir(parents=True)
    monkeypatch.chdir(inner)
    assert cfg.load_config(home=tmp_path / "home").tracker_pattern == r"(eng-\d+)"


def test_an_explicit_directory_is_read_as_given(tmp_path: Path) -> None:
    # Value: protects=--config <dir> reads the loop.toml in that directory, not the repo root one;
    # fails_when=an explicit directory is swapped for the repository top level;
    # why_new=the subdirectory test only covers the current directory; seam=none
    repo = git_repo(tmp_path / "mine")
    write(repo, '[tracker]\nprefix = "eng"\n')
    named = repo / "configs"
    named.mkdir()
    write(named, '[tracker]\nprefix = "ops"\n')
    assert cfg.load_config(named, home=tmp_path / "home").tracker_pattern == r"(ops-\d+)"


def test_a_worktree_of_a_bare_repo_drops_the_git_suffix(tmp_path: Path) -> None:
    # Value: protects=a worktree of a bare mine.git clone reads the user file mine.toml;
    # fails_when=repo_slug keeps the .git suffix and looks for mine.git.toml;
    # why_new=the worktree test above uses a repository with a work tree; seam=none
    home = tmp_path / "home"
    user_file(home, "mine", '[tracker]\nprefix = "task"\n')
    source = git_repo(tmp_path / "source")
    commit(source)
    bare = tmp_path / "mine.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(bare)], check=True)
    tree = tmp_path / "trees" / "task-3"
    subprocess.run(
        ["git", "-C", str(bare), "worktree", "add", "-q", "-b", "task-3", str(tree)], check=True
    )
    assert cfg.repo_slug(tree) == "mine"
    assert cfg.load_config(tree, home=home).tracker_pattern == r"(task-\d+)"


def test_a_repo_stage_list_replaces_the_user_one_whole(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user_file(home, "mine", '[[stages]]\nname = "a"\n[[stages]]\nname = "b"\n')
    repo = git_repo(tmp_path / "mine")
    write(repo, '[[stages]]\nname = "fix"\n')
    assert cfg.load_config(repo, home=home).stage_names == ("fix",)


def test_every_worktree_of_a_repo_shares_its_user_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    user_file(home, "mine", '[tracker]\nprefix = "task"\n')
    repo = git_repo(tmp_path / "mine")
    commit(repo)
    tree = tmp_path / "trees" / "task-7-anything"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "task-7", str(tree)], check=True
    )
    assert cfg.repo_slug(tree) == "mine"
    assert cfg.load_config(tree, home=home).tracker_pattern == r"(task-\d+)"


def test_outside_a_repository_there_is_no_user_file(tmp_path: Path) -> None:
    assert cfg.config_paths(tmp_path, home=tmp_path)[1] is None


def test_an_explicit_slug_picks_the_user_file(tmp_path: Path) -> None:
    user_file(tmp_path, "named", '[tracker]\nprefix = "task"\n')
    config = cfg.load_config(tmp_path, slug="named", home=tmp_path)
    assert config.tracker_pattern == r"(task-\d+)"


def test_a_bad_user_file_is_refused(tmp_path: Path) -> None:
    user_file(tmp_path, "named", "[harness\n")
    with pytest.raises(cfg.ConfigError, match="not valid TOML"):
        cfg.load_config(tmp_path, slug="named", home=tmp_path)


def test_a_user_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    # Value: protects=a user file with bytes that are not UTF-8 is refused as a ConfigError;
    # fails_when=UnicodeDecodeError is dropped from the guard in _read and escapes load_cli_config;
    # why_new=the bad user file test covers broken TOML only; seam=none
    user_file(tmp_path, "named", "").write_bytes(b'[harness]\nsession_label = "\xff"\n')
    with pytest.raises(cfg.ConfigError, match="cannot read"):
        cfg.load_config(tmp_path, slug="named", home=tmp_path)


@pytest.mark.parametrize("failure", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 3)])
def test_a_git_that_cannot_run_is_refused_not_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    # Value: protects=a missing or stuck git cannot silently drop the user file and its
    # carve-outs; fails_when=_git turns a failure into "no repository" again;
    # why_new=two reviewers reproduced an auto-accept through this path; seam=none
    def broken_git(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(cfg.subprocess, "run", broken_git)
    write(tmp_path, '[tracker]\nprefix = "eng"\n')
    with pytest.raises(cfg.ConfigError, match="cannot run git"):
        cfg.load_config(tmp_path)


def test_a_translated_git_still_reads_as_no_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Value: protects=an operator with a non-English locale can run outside a repository;
    # fails_when=_git stops forcing English messages before it reads git's error text;
    # why_new=git translates "not a git repository" and the suite ran in English; seam=none
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert cfg.config_paths(tmp_path, home=tmp_path)[1] is None
    # The host may lack the French locale, so the forced environment is checked too.
    seen: dict[str, str] = {}
    real_run = cfg.subprocess.run

    def spy(*args: object, **kwargs: object) -> object:
        seen.update(kwargs["env"])  # type: ignore[arg-type]
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cfg.subprocess, "run", spy)
    cfg.config_paths(tmp_path, home=tmp_path)
    assert (seen["LC_ALL"], seen["LANGUAGE"]) == ("C", "C")


def test_a_rewritten_git_pointer_is_refused_not_ignored(tmp_path: Path) -> None:
    # Value: protects=a run that rewrites its worktree .git file cannot drop the user file;
    # fails_when=_git reads a broken .git as "no repository"; why_new=the security review
    # reproduced the user carve-outs disappearing this way; seam=none
    home = tmp_path / "home"
    user_file(home, "mine", '[loop_paths]\ncarve_outs = ["secret.txt"]\n')
    wt = tmp_path / "mine"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /nonexistent/x\n", encoding="utf-8")
    with pytest.raises(cfg.ConfigError, match="git cannot read"):
        cfg.load_config(wt, home=home)


def test_a_dangling_git_symlink_is_refused_not_ignored(tmp_path: Path) -> None:
    # Value: protects=a run that swaps its .git for a dangling symlink cannot drop the user
    # file; fails_when=_has_git_entry drops its is_symlink() check; why_new=the pointer test
    # writes a .git file only; seam=none
    home = tmp_path / "home"
    user_file(home, "mine", USER_ADDS)
    wt = tmp_path / "mine"
    wt.mkdir()
    (wt / ".git").symlink_to(tmp_path / "nowhere")
    with pytest.raises(cfg.ConfigError, match="git cannot read"):
        cfg.load_config(wt, home=home)


@pytest.mark.parametrize(
    ("code", "out", "err"),
    [(128, "", "fatal: detected dubious ownership in repository"), (0, "", "")],
)
def test_a_git_that_fails_otherwise_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, out: str, err: str
) -> None:
    # Value: protects=any git answer other than "not a repository" fails closed;
    # fails_when=the final raise in _git turns back into "no repository";
    # why_new=the other git tests raise before git answers; seam=none
    def answer(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, code, out, err)

    monkeypatch.setattr(cfg.subprocess, "run", answer)
    with pytest.raises(cfg.ConfigError, match="git cannot read"):
        cfg.load_config(tmp_path)


def test_an_inherited_git_dir_does_not_pick_another_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Value: protects=a command run from a git hook reads the user file of the repo in cwd;
    # fails_when=_git passes GIT_DIR and friends through to git;
    # why_new=conftest clears these for every other test; seam=none
    home = tmp_path / "home"
    user_file(home, "mine", '[loop_paths]\ncarve_outs = ["secret.txt"]\n')
    mine = git_repo(tmp_path / "mine")
    other = git_repo(tmp_path / "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert "secret.txt" in cfg.load_config(mine, home=home).carve_outs


def test_a_checkout_with_an_unsafe_name_has_no_user_file(tmp_path: Path) -> None:
    # Value: protects=a checkout whose name is unsafe as a file name reads no user file;
    # fails_when=repo_slug stops checking the name it reads from git;
    # why_new=the unsafe slug tests pass an explicit slug only; seam=none
    repo = git_repo(tmp_path / "My Repo")
    assert cfg.repo_slug(repo) is None
    assert cfg.config_paths(repo, home=tmp_path)[1] is None


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".."])
def test_an_unsafe_slug_is_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(cfg.ConfigError, match="unsafe repo slug"):
        cfg.load_config(tmp_path, slug=bad, home=tmp_path)


def test_a_file_can_add_a_carve_out_but_never_remove_one(tmp_path: Path) -> None:
    # Value: protects=the guard hooks stay carve-outs whatever a loop.toml says;
    # fails_when=carve_outs from a file replace the defaults instead of adding to them;
    # why_new=a repository file is now read without a flag; seam=none
    empty = cfg.load_config(write(tmp_path, "[loop_paths]\ncarve_outs = []\n"))
    assert empty.carve_outs == cfg.DEFAULTS.carve_outs
    added = cfg.load_config(write(tmp_path, '[loop_paths]\ncarve_outs = ["Makefile"]\n'))
    assert added.carve_outs == (*cfg.DEFAULTS.carve_outs, "Makefile")
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{".claude/settings.json": "aaa"}],
        "guard_pending": {".claude/settings.json": "bbb"},
    }
    decision = sd.decide(record, [".claude/settings.json"], None, False, empty)
    assert decision["action"] == "escalate"


@pytest.mark.parametrize("key", ["carve_outs", "carve_out_prefixes"])
def test_an_empty_carve_out_list_keeps_the_defaults(tmp_path: Path, key: str) -> None:
    # Value: protects=both carve-out lists only ever add to the defaults;
    # fails_when=a list is dropped from _ADDITIVE and replaces the defaults again;
    # why_new=the test above covers carve_outs alone; seam=none
    config = cfg.load_config(write(tmp_path, f"[loop_paths]\n{key} = []\n"))
    assert getattr(config, key) == getattr(cfg.DEFAULTS, key)


def test_a_repo_file_cannot_drop_a_user_carve_out(tmp_path: Path) -> None:
    # Value: protects=a carve-out the operator adds in the user file survives any repo file;
    # fails_when=the repository file's list replaces the user file's list in the merge;
    # why_new=the other carve-out tests use one file; seam=none
    home = tmp_path / "home"
    user_file(home, "mine", '[loop_paths]\ncarve_outs = ["secret.txt"]\n')
    repo = git_repo(tmp_path / "mine")
    write(repo, '[loop_paths]\ncarve_outs = ["Makefile"]\n')
    carve_outs = cfg.load_config(repo, home=home).carve_outs
    assert "secret.txt" in carve_outs
    assert "Makefile" in carve_outs


def test_moving_the_state_dir_keeps_the_guard_files_carved_out(tmp_path: Path) -> None:
    # Value: protects=a loop.toml that moves state_dir cannot free the real guard files;
    # fails_when=the built-in carve-outs are only expanded against the configured state_dir;
    # why_new=a repository file is read without a flag and could set state_dir; seam=none
    config = cfg.load_config(write(tmp_path, '[harness]\nstate_dir = "zz"\n'))
    for path in (".claude/settings.json", ".claude/settings.local.json", "loop.toml"):
        assert lp.is_carveout(path, config) is True


@pytest.mark.parametrize(
    ("user_text", "repo_text"),
    [
        (USER_ADDS, 'loop_paths = "oops"\n'),
        (USER_ADDS, '[loop_paths]\ncarve_outs = "Makefile"\n'),
        # Value: protects=a bad carve-out list in the user file is refused when the repo file
        # sets a good one; fails_when=_collect_carve_outs stops checking types and spreads a
        # string into letters; why_new=the rows above put the bad value in the repo file; seam=none
        ('[loop_paths]\ncarve_outs = "Makefile"\n', '[loop_paths]\ncarve_outs = ["x"]\n'),
    ],
)
def test_a_bad_repo_file_is_refused_beside_user_carve_outs(
    tmp_path: Path, user_text: str, repo_text: str
) -> None:
    # Value: protects=a bad repository file is refused even when the user file adds carve-outs;
    # fails_when=the cross-file carve-out union raises TypeError or hides the bad value;
    # why_new=the refusal tests read one file; seam=none
    home = tmp_path / "home"
    user_file(home, "mine", user_text)
    repo = git_repo(tmp_path / "mine")
    write(repo, repo_text)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(repo, home=home)


def test_a_bad_file_makes_a_command_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=a broken loop.toml read without a flag stops a command with exit 2;
    # fails_when=a ConfigError escapes main as a traceback and exit 1;
    # why_new=the refusal tests call load_config directly; seam=none
    write(tmp_path, "[harness\n")
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"condition": "ok", "status": "running"}), encoding="utf-8")
    scan = tmp_path / "scan.json"
    scan.write_text("[]", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    commands = [
        (ss.main, []),
        (sc.main, ["--record-file", str(record)]),
        (sd.main, ["--record-file", str(record)]),
        (sps.main, ["--scan-file", str(scan)]),
    ]
    for main, argv in commands:
        assert main(argv) == 2, main.__module__
        assert "CONFIG_INVALID" in capsys.readouterr().err


def test_the_card_command_reads_the_user_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=loop-card with no --config prints the session label from the user file;
    # fails_when=the card command stops loading config for the current repository;
    # why_new=only loop-scan had a command test for the new lookup; seam=none
    user_file(Path.home(), "mine", '[harness]\nsession_label = "Desk"\n')
    repo = git_repo(tmp_path / "mine")
    record = tmp_path / "record.json"
    record.write_text(
        json.dumps({"task": "T", "status": "running", "session_id": "abcdef12-0"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    assert sc.main(["--record-file", str(record), "--first-line"]) == 0
    assert "Desk abcdef12" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["authorized_keys", "{slug}.json"])
def test_a_ledger_that_is_not_jsonl_is_refused(tmp_path: Path, name: str) -> None:
    # Value: protects=a config file cannot point the ledger at an existing home dotfile;
    # fails_when=the .jsonl suffix check on ledger_name is removed;
    # why_new=the other ledger checks only refuse a path in the name; seam=none
    with pytest.raises(cfg.ConfigError, match="jsonl"):
        cfg.load_config(write(tmp_path, f'[ledger]\ndir = ".ssh"\nname = "{name}"\n'))


@pytest.mark.parametrize("spelling", ["./.harness", ".harness/", ".harness//"])
def test_a_state_dir_spelling_still_carves_out_its_files(tmp_path: Path, spelling: str) -> None:
    # Value: protects=the guard files under a moved state_dir stay carve-outs however it is
    # spelled; fails_when=state_dir is not put in canonical form before the carve-outs expand;
    # why_new=the moved-state tests spell the directory one way only; seam=none
    config = cfg.load_config(write(tmp_path, f'[harness]\nstate_dir = "{spelling}"\n'))
    assert config.state_dir == ".harness"
    # guard_watch adds up like the carve-outs, so the defaults under .claude stay too.
    assert config.guard_watch[:2] == (
        ".harness/hooks/pipeline_stop.py",
        ".harness/hooks/pipeline_guard.py",
    )
    assert config.loop_prefixes == (".harness/skills/pipeline/",)


def test_a_state_dir_of_dot_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cfg.ConfigError, match="state_dir"):
        cfg.load_config(write(tmp_path, '[harness]\nstate_dir = "./"\n'))


def test_the_other_commands_judge_a_run_under_its_own_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=loop-decide, loop-pause-stats and loop-card read a run under its own
    # worktree config, as loop-scan does; fails_when=they judge every run by the cwd config;
    # why_new=only loop-scan had per-worktree tests; seam=none
    wt = tmp_path / "run"
    wt.mkdir()
    write(
        wt,
        '[harness]\nstate_dir = ".agent"\nsession_label = "Desk"\n'
        '[loop_paths]\ncarve_outs = ["{state_dir}/settings.json"]\n',
    )
    record = {
        "worktree": str(wt),
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "session_id": "abcdef12-0",
        "guard_files_seen": [{".agent/settings.json": "aaa"}],
        "guard_pending": {".agent/settings.json": "bbb"},
    }
    record_file = tmp_path / "record.json"
    record_file.write_text(json.dumps(record), encoding="utf-8")
    scan_file = tmp_path / "scan.json"
    scan_file.write_text(json.dumps([record]), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    allow = ["--allow-path", ".agent/settings.json"]
    assert sd.main(["--record-file", str(record_file), *allow]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "escalate"
    assert sps.main(["--scan-file", str(scan_file), *allow]) == 0
    assert json.loads(capsys.readouterr().out)["by_category"]["carveout"] == 1
    assert sc.main(["--record-file", str(record_file), "--first-line"]) == 0
    assert "Desk abcdef12" in capsys.readouterr().out
    # A named file applies to every run instead of the run's own.
    named = write(tmp_path, '[harness]\nsession_label = "Named"\n', "named.toml")
    assert sd.main(["--record-file", str(record_file), *allow, "--config", str(named)]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "auto_accept"
    assert sps.main(["--scan-file", str(scan_file), *allow, "--config", str(named)]) == 0
    assert json.loads(capsys.readouterr().out)["by_category"]["carveout"] == 0
    argv = ["--record-file", str(record_file), "--first-line", "--config", str(named)]
    assert sc.main(argv) == 0
    assert "Named abcdef12" in capsys.readouterr().out


def test_a_run_with_a_broken_worktree_config_is_still_escalated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=a run whose own loop.toml is broken still reaches a person through
    # decide, card and pause-stats; fails_when=a per-run ConfigError stops the command with
    # exit 2; why_new=the per-run lookup is new; seam=none
    wt = tmp_path / "run"
    wt.mkdir()
    write(wt, "[harness\n")
    record = {
        "worktree": str(wt),
        "condition": "config_invalid",
        "session_id": "abcdef12-0",
        "task": "T",
    }
    record_file = tmp_path / "record.json"
    record_file.write_text(json.dumps(record), encoding="utf-8")
    good = {"condition": "ok", "status": "running"}
    scan_file = tmp_path / "scan.json"
    scan_file.write_text(
        json.dumps([record, {**record, "condition": "ok"}, good]), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert sd.main(["--record-file", str(record_file)]) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["action"] == "escalate"
    ok_record = tmp_path / "ok.json"
    ok_record.write_text(json.dumps({**record, "condition": "ok"}), encoding="utf-8")
    assert sd.main(["--record-file", str(ok_record)]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["reason"] == "unobservable"
    # The operator is told which file to repair.
    assert "CONFIG_INVALID" in err and str(wt / "loop.toml") in err
    assert sc.main(["--record-file", str(ok_record), "--first-line"]) == 0
    first = capsys.readouterr().out
    assert "Session abcdef12" in first and "Cannot read this run" in first
    assert "unreadable" in first.split("::")[0]
    assert sps.main(["--scan-file", str(scan_file)]) == 0
    by_category = json.loads(capsys.readouterr().out)["by_category"]
    assert by_category["unobservable"] == 2
    assert by_category["not_paused"] == 1


def test_a_record_whose_worktree_is_gone_is_escalated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=a run whose worktree vanished is escalated, not judged under the cwd
    # config; fails_when=config_for_run falls back to the caller's config for a named but
    # missing worktree; why_new=Codex reproduced an auto-accept through this path; seam=none
    record = {
        "worktree": str(tmp_path / "gone"),
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{".agent/settings.json": "aaa"}],
        "guard_pending": {".agent/settings.json": "bbb"},
    }
    record_file = tmp_path / "record.json"
    record_file.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert sd.main(["--record-file", str(record_file), "--allow-path", ".agent/settings.json"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "unobservable"


def test_each_file_is_checked_on_its_own(tmp_path: Path) -> None:
    # Value: protects=a bad key in the user file is refused even when the repo file sets it;
    # fails_when=the files are only validated after the merge;
    # why_new=the merge tests use valid files; seam=none
    home = tmp_path / "home"
    user_file(home, "mine", '[tracker]\npattern = "(unclosed"\n')
    repo = git_repo(tmp_path / "mine")
    write(repo, '[tracker]\npattern = "(ok-\\\\d+)"\n')
    with pytest.raises(cfg.ConfigError, match="mine.toml"):
        cfg.load_config(repo, home=home)


@pytest.mark.parametrize(
    "text",
    ["a = " + "[" * 2000 + "]" * 2000 + "\n", '[tracker]\nprefix = "a{99999999999}"\n'],
)
def test_a_file_that_breaks_the_parser_is_refused(tmp_path: Path, text: str) -> None:
    # Value: protects=a deeply nested or huge-repeat file is a ConfigError, not a crash;
    # fails_when=RecursionError or OverflowError escapes the loader;
    # why_new=the native adversarial pass crashed loop-scan this way; seam=none
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(write(tmp_path, text))


def test_a_stage_may_allow_shell_waits() -> None:
    # Value: protects=the per-stage escape hatch for a dev server check; fails_when=the key
    # is dropped or accepts a non-boolean; why_new=CRB-28 DX #57; seam=none
    config = cfg.from_mapping({"stages": [{"name": "qa", "shell_waits": True}, {"name": "x"}]})
    assert config.stage("qa").shell_waits is True
    assert config.stage("x").shell_waits is False
    assert cfg.from_dict(cfg.to_dict(config)) == config
    with pytest.raises(cfg.ConfigError, match="shell_waits takes true or false"):
        cfg.from_mapping({"stages": [{"name": "qa", "shell_waits": "yes"}]})


def test_the_harness_tool_names_have_defaults_and_can_be_set() -> None:
    assert cfg.DEFAULTS.stop_task_tool == "TaskStop"
    assert cfg.DEFAULTS.background_flag == "run_in_background"
    config = cfg.from_mapping({"harness": {"stop_task_tool": "KillTask"}})
    assert config.stop_task_tool == "KillTask"
    with pytest.raises(cfg.ConfigError):
        cfg.from_mapping({"harness": {"background_flag": ""}})


def test_the_supervisor_wait_limits_have_defaults_and_can_be_set() -> None:
    # Value: protects=a host sets how long a wait may last before it is escalated, in
    # loop.toml; fails_when=the [supervisor] keys are ignored or a bad value is accepted;
    # why_new=CRB-28 wait time limit TODO; seam=none
    assert cfg.DEFAULTS.wait_stale_after_s == 3600 and cfg.DEFAULTS.shell_wait_stale_after_s == 900
    table = {"wait_stale_after_seconds": 1800, "shell_wait_stale_after_seconds": 300}
    config = cfg.from_mapping({"supervisor": table})
    assert (config.wait_stale_after_s, config.shell_wait_stale_after_s) == (1800, 300)
    assert cfg.from_dict(cfg.to_dict(config)) == config
    for bad in ("10", 0, True, 1.5):
        with pytest.raises(cfg.ConfigError):
            cfg.from_mapping({"supervisor": {"wait_stale_after_seconds": bad}})


def test_loop_scan_reads_each_run_s_wait_limit(tmp_path: Path) -> None:
    from datetime import timedelta

    config = cfg.from_mapping({"supervisor": {"wait_stale_after_seconds": 600}})
    now = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)
    at = (now - timedelta(minutes=20)).isoformat()
    state = {"status": "running", "waiting_on": ["a1"], "waiting_since": at, "updated_at": at}
    assert ss._wait_stale(state, now, config) is True
    assert ss._wait_stale(state, now, cfg.DEFAULTS) is False
    assert ss._wait_stale(state, now, cfg.DEFAULTS, wait_stale_after=600) is True
