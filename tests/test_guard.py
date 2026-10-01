"""The pre-tool guard: which edits and pushes a run's agent may make."""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.claude_code import bootstrap as boot
from core import guard
from core import run_index as ri
from core import run_state as rs
from core.config import LoopConfig, StageSpec


def _edit(run: rs.Run, path: str, cwd: Path | None = None, tool: str = "Edit"):
    call = guard.PreToolCall("s1", "edit", tool, str(cwd or run.worktree), None, path)
    return guard.handle_pre_tool(call, run)


def _bash(run: rs.Run, command: str):
    call = guard.PreToolCall("s1", "shell", "Bash", None, command, None)
    return guard.handle_pre_tool(call, run)


@pytest.mark.parametrize("tool", boot.EDIT_TOOLS)
def test_ordinary_source_is_allowed_to_every_edit_tool(start_run, tool: str) -> None:
    run, worktree = start_run()
    assert _edit(run, "src/app.py", tool=tool).allow
    assert _edit(run, str(worktree / "README.md"), tool=tool).allow


@pytest.mark.parametrize(
    "rel", ["loop.toml", ".claude/settings.json", ".claude/settings.local.json", ".git/config"]
)
def test_a_carve_out_is_denied_with_a_fix(start_run, rel: str) -> None:
    run, _ = start_run()
    verdict = _edit(run, rel)
    assert not verdict.allow
    assert "Cause:" in verdict.reason and "Fix:" in verdict.reason
    assert "#what-the-guard-covers" in verdict.reason


def test_a_loop_file_needs_the_plan_to_declare_it(start_run) -> None:
    run, _ = start_run()
    rel = ".claude/skills/pipeline/stages/qa.md"
    verdict = _edit(run, rel)
    assert not verdict.allow and "did not declare" in verdict.reason
    rs.update(run, lambda s: s.__setitem__("declared_loop_edits", [rel]))
    assert _edit(run, rel).allow


def test_a_settings_file_stays_denied_even_when_declared(start_run) -> None:
    run, _ = start_run()
    rs.update(run, lambda s: s.__setitem__("declared_loop_edits", [".claude/settings.json"]))
    assert not _edit(run, ".claude/settings.json").allow


def test_an_edit_from_outside_the_worktree_is_still_judged(start_run, tmp_path: Path) -> None:
    # Value: protects=cd elsewhere does not switch the guard off; fails_when=the worktree
    # is read from cwd only; why_new=eng E2; seam=none
    run, worktree = start_run()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert not _edit(run, str(worktree / "loop.toml"), cwd=elsewhere).allow


def test_a_symlink_cannot_lead_an_edit_to_a_carve_out(start_run) -> None:
    run, worktree = start_run()
    (worktree / "loop.toml").write_text("", encoding="utf-8")
    (worktree / "innocent.toml").symlink_to(worktree / "loop.toml")
    assert not _edit(run, "innocent.toml").allow


def test_the_loops_own_state_and_the_harness_directory_are_denied(start_run) -> None:
    run, _ = start_run()
    for target in (
        str(Path(ri.state_home()) / "index" / run.key),
        str(run.state_path),
        str(Path.home() / ".claude" / "settings.json"),
        str(rs.PLUGIN_ROOT / "hooks" / "pipeline_guard.py"),
        "~/.claude/plugins/cache/x",
    ):
        verdict = _edit(run, target)
        assert not verdict.allow, target
        assert "holds the loop's own rules or state" in verdict.reason


def test_other_paths_outside_the_worktree_are_allowed(start_run, tmp_path: Path) -> None:
    # Value: protects=a stage's skill can keep its files outside the repo (a plan file);
    # fails_when=every outside write is denied; why_new=guard scope; seam=none
    run, _ = start_run()
    assert _edit(run, str(tmp_path / "plans" / "plan.md")).allow


def test_a_finished_run_guards_nothing(start_run) -> None:
    run, _ = start_run()
    ri.abort_run(run.key, run.entry, "person")
    assert _edit(run, "loop.toml").allow


def _ship_run(start_run, emits: str = "PR: the pull request url"):
    config = LoopConfig(stages=(StageSpec(name="implement"), StageSpec(name="ship", emits=emits)))
    run, worktree = start_run(config=config)
    return run


def _at_ship(run: rs.Run) -> None:
    def move(state: dict) -> None:
        state["current"] = 1
        state["current_stage"] = "ship"

    rs.update(run, move)


@pytest.mark.parametrize(
    "command",
    ["git push -u origin HEAD", "git push origin main", "git push", "git push -u origin main"],
)
def test_the_publishing_stage_pushes_its_own_branch(start_run, command: str) -> None:
    run = _ship_run(start_run)
    assert not _bash(run, command).allow
    _at_ship(run)
    assert _bash(run, command).allow


@pytest.mark.parametrize(
    "command",
    [
        "git push --force origin main",
        "git push -f",
        "git push --force-with-lease origin main",
        "git push origin +main",
        "git push --all origin",
        "git push --mirror origin",
        "git push --delete origin main",
        "git push origin other",
        "git push origin HEAD:release",
        "git push upstream main",
        "cd x && git -c core.x=1 push --force",
    ],
)
def test_a_rewriting_or_foreign_push_is_denied(start_run, command: str) -> None:
    run = _ship_run(start_run)
    _at_ship(run)
    verdict = _bash(run, command)
    assert not verdict.allow, command
    assert "Fix:" in verdict.reason


def test_other_shell_commands_pass(start_run) -> None:
    run = _ship_run(start_run)
    assert _bash(run, "pytest -q && git commit -am wip").allow
    assert _bash(run, "git status").allow


def test_a_letter_case_variant_of_a_protected_root_is_denied(start_run) -> None:
    # Value: protects=on a case-insensitive file system ~/.CLAUDE is ~/.claude;
    # fails_when=the protected roots compare case-sensitively; why_new=review security;
    # seam=none
    run, _ = start_run()
    for target in ("~/.CLAUDE/settings.json", "~/.Claude/plugins/x", str(run.state_path).upper()):
        assert not _edit(run, target).allow, target


def test_the_harness_config_dir_from_the_environment_is_protected(
    start_run, tmp_path, monkeypatch
) -> None:
    run, _ = start_run()
    moved = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    assert not _edit(run, str(moved / "settings.json")).allow


def test_a_settings_file_outside_the_worktree_is_denied(start_run, tmp_path) -> None:
    # Value: protects=the settings the harness reads when launched from a parent directory;
    # fails_when=only the worktree's own settings are carve-outs; why_new=review adversarial;
    # seam=none
    run, _ = start_run()
    for name in ("settings.json", "settings.local.json"):
        assert not _edit(run, str(tmp_path / ".claude" / name)).allow


def test_a_case_variant_loop_file_still_needs_a_declaration(start_run) -> None:
    run, _ = start_run()
    assert not _edit(run, ".Claude/Skills/pipeline/stages/ship.md").allow


@pytest.mark.parametrize(
    "command",
    [
        "git push -uf origin main",
        "git push -fu",
        "git push origin :main",
        "git push --delete=main origin",
        "git push --repo=elsewhere",
        "git -c remote.origin.pushurl=x push origin main",
        "git -c url.x.insteadOf=y push",
    ],
)
def test_a_bundled_force_a_delete_refspec_or_a_redirect_is_denied(start_run, command) -> None:
    # Value: protects=the publishing stage pushes its own branch plainly;
    # fails_when=-uf, :branch or -c remote.* slips through; why_new=review testing; seam=none
    run = _ship_run(start_run)
    _at_ship(run)
    assert not _bash(run, command).allow, command


def test_a_push_option_value_is_not_read_as_a_remote(start_run) -> None:
    run = _ship_run(start_run)
    _at_ship(run)
    assert _bash(run, "git push -o ci.skip origin main").allow


@pytest.mark.parametrize(
    "command",
    [
        "git push --del origin main",
        "git push --mirr origin",
        "git push --al origin",
        "git push --tags origin",
        "git --config-env=remote.origin.url=VAR push origin main",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.url git push origin main",
        "git -C ../other push origin HEAD",
    ],
)
def test_an_abbreviated_option_or_an_outside_setting_is_denied(start_run, command) -> None:
    # Value: protects=the push policy against git's own spellings; fails_when=--del or
    # --config-env slips through; why_new=re-review; seam=none
    run = _ship_run(start_run)
    _at_ship(run)
    assert not _bash(run, command).allow, command


def test_a_push_from_another_checked_out_branch_is_denied(start_run, git) -> None:
    run = _ship_run(start_run)
    _at_ship(run)
    git(run.worktree, "checkout", "-q", "-b", "other")
    assert not _bash(run, "git push origin HEAD").allow
    assert not _bash(run, "git push").allow
    git(run.worktree, "checkout", "-q", "main")
    assert _bash(run, "git push origin HEAD").allow


# Regression: ISSUE-001 - a run whose worktree sits inside a protected directory had every
# edit denied
# Found by /qa on 2026-10-01
# Report: .gstack/qa-reports/run-20261001T130243Z/qa-report-delivery-loop-cli-2026-10-01.md
def test_a_worktree_inside_a_protected_directory_can_still_edit_its_own_files(
    make_repo, tmp_path: Path
) -> None:
    # Value: protects=a repository kept under ~/.claude runs the loop like any other;
    # fails_when=a protected root that contains the worktree denies its ordinary files;
    # why_new=test_guard only places worktrees outside every protected root; seam=none
    from core.config import DEFAULTS

    worktree = make_repo(Path.home() / ".claude" / "dotfiles")
    run, _ = rs.start(worktree, "CRB-1", DEFAULTS, "s1")
    assert _edit(run, "notes.md").allow
    assert _edit(run, str(worktree / "src" / "app.py")).allow
    # Its own enforcement files stay guarded, and so does the rest of the directory.
    assert not _edit(run, "loop.toml").allow
    assert not _edit(run, ".claude/settings.json").allow
    assert not _edit(run, str(Path.home() / ".claude" / "settings.json")).allow


def test_a_worktree_that_is_the_plugin_itself_cannot_edit_the_running_plugin(
    start_run, monkeypatch
) -> None:
    # Value: protects=a run on the plugin's own checkout (loaded with --plugin-dir) cannot
    # rewrite the hooks that guard it; fails_when=the plugin root is skipped when it holds
    # the worktree; why_new=pairs with the ISSUE-001 fix; seam=none
    run, worktree = start_run()
    monkeypatch.setattr(rs, "PLUGIN_ROOT", worktree)
    assert not _edit(run, "hooks/pipeline_guard.py").allow
