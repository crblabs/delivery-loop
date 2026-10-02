"""The impact map, the computed half: the diff parser, the import resolver, the
report and the command.

`compute()` runs against a synthetic map and a synthetic import graph, so the
report logic is tested apart from git. The command runs in a real repository
built per test.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from core import impact
from core import zones as zonesmod

PLUGIN = Path(__file__).resolve().parent.parent

MAP = """
schema = 1
unzoned = ["LICENSE"]
[zones.data]
title = "Data"
paths = ["data/**"]
edges = ["api", "ci"]
[zones.api]
title = "API"
paths = ["src/api/**"]
edges = ["web"]
[zones.web]
title = "Web app"
paths = ["src/web/**"]
[zones.ci]
title = "CI"
paths = ["tests/**", ".github/**"]
[zones.docs]
title = "Docs"
paths = ["docs/**"]
"""


@pytest.fixture
def zs() -> zonesmod.Zones:
    return zonesmod.load_text(MAP)


# ------------------------------------------------------------- diff parsing


def test_parse_plain_text() -> None:
    changes = impact.parse_name_status("M\tsrc/a.py\nA\tdata/x.csv\n")
    assert changes == [
        impact.Change("M", None, "src/a.py"),
        impact.Change("A", None, "data/x.csv"),
    ]


def test_parse_rename_strips_score() -> None:
    (change,) = impact.parse_name_status("R100\told/a.py\tnew/a.py\n")
    assert change == impact.Change("R", "old/a.py", "new/a.py")


def test_parse_nul_delimited() -> None:
    raw = "M\0src/a.py\0R096\0old.py\0new.py\0"
    assert impact.parse_name_status(raw) == [
        impact.Change("M", None, "src/a.py"),
        impact.Change("R", "old.py", "new.py"),
    ]


def test_parse_truncated_does_not_crash() -> None:
    assert impact.parse_name_status("M") == []
    assert impact.parse_name_status("R100\0old.py") == []


def test_md_path_neutralises_injection() -> None:
    out = impact.md_path("docs/a`b|c\n## Findings")
    assert "`" not in out
    assert "\n" not in out
    assert r"\|" in out


# ------------------------------------------------------------- import resolver


def test_module_names_cover_every_import_root() -> None:
    assert impact.module_names("src/pkg/mod.py") == ["src.pkg.mod", "pkg.mod", "mod"]
    assert impact.module_names("src/pkg/__init__.py") == ["src.pkg", "pkg"]
    assert impact.module_names("my-tools/run.py") == ["run"]
    assert impact.module_names("README.md") == []


def _edges(sources: dict[str, str]) -> dict[str, set[str]]:
    index = impact.ModuleIndex(sorted(sources))
    return {path: impact.imports_of(path, src, index) for path, src in sources.items()}


def test_absolute_imports_resolve_by_dotted_name() -> None:
    sources = {
        "app/main.py": "import pkg.util as u\nfrom pkg import models\nimport os\n",
        "pkg/__init__.py": "",
        "pkg/util.py": "",
        "pkg/models.py": "",
    }
    assert _edges(sources)["app/main.py"] == {"pkg/util.py", "pkg/models.py"}


def test_from_import_of_a_name_falls_back_to_the_module() -> None:
    sources = {"a.py": "from pkg.util import helper\n", "pkg/util.py": ""}
    assert _edges(sources)["a.py"] == {"pkg/util.py"}


def test_from_import_of_a_package_name_reaches_its_init() -> None:
    sources = {"a.py": "from pkg import thing\n", "pkg/__init__.py": ""}
    assert _edges(sources)["a.py"] == {"pkg/__init__.py"}


def test_a_src_layout_resolves_without_the_src_prefix() -> None:
    sources = {"src/pkg/a.py": "from pkg.b import x\n", "src/pkg/b.py": ""}
    assert _edges(sources)["src/pkg/a.py"] == {"src/pkg/b.py"}


def test_a_script_resolves_a_sibling_by_bare_name() -> None:
    # Value: protects=the bare-name convention of a scripts directory; fails_when=a
    # sibling import resolves to a same-named file elsewhere; why_new=generic graph; seam=none
    sources = {
        "tools/run.py": "import helper\n",
        "tools/helper.py": "",
        "other/helper.py": "",
    }
    assert _edges(sources)["tools/run.py"] == {"tools/helper.py"}


def test_relative_imports_resolve_by_path() -> None:
    sources = {
        "pkg/sub/a.py": (
            "from . import b\nfrom .c import y\nfrom .. import top\nfrom ..d import *\n"
        ),
        "pkg/sub/b.py": "",
        "pkg/sub/c.py": "",
        "pkg/top.py": "",
        "pkg/d/__init__.py": "",
    }
    assert _edges(sources)["pkg/sub/a.py"] == {
        "pkg/sub/b.py",
        "pkg/sub/c.py",
        "pkg/top.py",
        "pkg/d/__init__.py",
    }


def test_a_relative_import_inside_an_init_is_its_own_package() -> None:
    sources = {"pkg/__init__.py": "from .core import run\n", "pkg/core.py": ""}
    assert _edges(sources)["pkg/__init__.py"] == {"pkg/core.py"}


def test_a_relative_import_past_the_root_is_dropped() -> None:
    assert _edges({"a.py": "from ... import x\n"})["a.py"] == set()


def test_imports_inside_functions_and_branches_count() -> None:
    sources = {
        "a.py": "def f():\n    import b\nif True:\n    from c import d\n",
        "b.py": "",
        "c.py": "",
    }
    assert _edges(sources)["a.py"] == {"b.py", "c.py"}


def test_an_unparseable_file_contributes_no_edges() -> None:
    assert _edges({"a.py": "def (:\n", "b.py": ""})["a.py"] == set()


def test_importer_graph_is_the_reverse_graph() -> None:
    graph = impact.importer_graph({"a.py": "import b\n", "b.py": "import c\n", "c.py": ""})
    assert graph == {"a.py": set(), "b.py": {"a.py"}, "c.py": {"b.py"}}


def test_transitive_importers_multi_hop() -> None:
    importers = {"c.py": {"b.py"}, "b.py": {"a.py"}}
    assert impact.transitive_importers("c.py", importers) == {"a.py", "b.py"}


def test_transitive_importers_cycle_terminates() -> None:
    importers = {"a.py": {"b.py"}, "b.py": {"a.py"}}
    assert impact.transitive_importers("a.py", importers) == {"b.py"}


# ------------------------------------------------------------- compute


def test_a_code_change_reaches_its_importers_zones(zs: zonesmod.Zones) -> None:
    changes = impact.parse_name_status("M\tsrc/api/models.py\nA\tdocs/new.md\nD\tsrc/web/old.py\n")
    importers = {"src/api/models.py": {"tests/test_models.py"}}
    report = impact.compute(changes, zs, importers)
    assert impact.zone_class("api", report) == "changed"
    assert impact.zone_class("docs", report) == "new"
    assert impact.zone_class("web", report) == "removed"
    assert report.indirect["ci"] == {"src/api/models.py"}
    assert impact.zone_class("data", report) == "untouched"


def test_a_declared_edge_reaches_downstream_transitively(zs: zonesmod.Zones) -> None:
    # Value: protects=a dependency the import graph cannot see is a declared edge, not
    # code; fails_when=edges stop propagating; why_new=the special case left the code; seam=none
    report = impact.compute([impact.Change("M", None, "data/sources.csv")], zs, {})
    assert impact.zone_class("data", report) == "changed"
    for consumer in ("api", "web", "ci"):
        assert report.indirect[consumer] == {"data"}, consumer
    assert "docs" not in report.indirect


def test_direct_wins_over_indirect(zs: zonesmod.Zones) -> None:
    changes = [impact.Change("M", None, "data/a.csv"), impact.Change("M", None, "src/api/x.py")]
    report = impact.compute(changes, zs, {})
    assert impact.zone_class("api", report) == "changed"
    assert "data" in report.indirect["api"]


def test_cross_zone_rename(zs: zonesmod.Zones) -> None:
    changes = impact.parse_name_status("R100\tsrc/api/note.md\tdocs/moved-note.md\n")
    report = impact.compute(changes, zs, {})
    assert ("removed", "src/api/note.md") in report.direct["api"]
    assert ("new", "docs/moved-note.md") in report.direct["docs"]


def test_same_zone_rename_is_a_change(zs: zonesmod.Zones) -> None:
    changes = impact.parse_name_status("R090\tsrc/api/a.py\tsrc/api/b.py\n")
    report = impact.compute(changes, zs, {})
    assert report.direct["api"] == [("changed", "src/api/b.py")]


def test_a_removed_module_still_reaches_its_old_importers(zs: zonesmod.Zones) -> None:
    importers = {"src/api/gone.py": {"src/web/view.py"}}
    report = impact.compute([impact.Change("D", None, "src/api/gone.py")], zs, importers)
    assert "src/api/gone.py" in report.indirect["web"]


def test_deleted_file_resolves_via_base_map() -> None:
    base = zonesmod.load_text('schema=1\n[zones.x]\ntitle="X"\npaths=["src/gone.py", "src/k.py"]\n')
    head = zonesmod.load_text('schema=1\n[zones.x]\ntitle="X"\npaths=["src/k.py"]\n')
    changes = [impact.Change("D", None, "src/gone.py")]
    report = impact.compute(changes, head, {}, base_zs=base)
    assert report.unclassified == []
    assert ("removed", "src/gone.py") in report.direct["x"]


def test_deleted_file_without_base_map_is_unclassified() -> None:
    head = zonesmod.load_text('schema=1\n[zones.x]\ntitle="X"\npaths=["src/k.py"]\n')
    report = impact.compute([impact.Change("D", None, "src/gone.py")], head, {})
    assert report.unclassified == ["src/gone.py"]


def test_unclassified_changed_file(zs: zonesmod.Zones) -> None:
    report = impact.compute([impact.Change("A", None, "brand/new.txt")], zs, {})
    assert report.unclassified == ["brand/new.txt"]


def test_mixed_status_salience_is_removed(zs: zonesmod.Zones) -> None:
    changes = [
        impact.Change("A", None, "src/api/a.py"),
        impact.Change("M", None, "src/api/b.py"),
        impact.Change("D", None, "src/api/c.py"),
    ]
    assert impact.zone_class("api", impact.compute(changes, zs, {})) == "removed"


def test_mermaid_and_table_shape(zs: zonesmod.Zones) -> None:
    report = impact.compute([impact.Change("M", None, "data/a.csv")], zs, {})
    mermaid = impact.to_mermaid(report, zs)
    assert mermaid.startswith("```mermaid\nflowchart TD\n")
    assert "    z_data --> z_api" in mermaid
    assert "    class z_data changed" in mermaid
    assert "    class z_docs untouched" in mermaid
    table = impact.to_table(report, zs)
    assert "| data | changed: `data/a.csv` | - | - |" in table
    assert "| docs | - | - | yes |" in table


def test_output_deterministic_across_hashseed() -> None:
    snippet = (
        "from core import impact, zones\n"
        "import sys\n"
        "z = zones.load('templates/zones.toml')\n"
        "ch = impact.parse_name_status('M\\tdata/a.csv\\nA\\tsrc/api/x.py\\nD\\tsrc/web/y.py\\n')\n"
        "r = impact.compute(ch, z, {'src/api/x.py': {'tests/t.py', 'src/web/v.py'}})\n"
        "sys.stdout.write(impact.to_mermaid(r, z) + impact.to_table(r, z))\n"
    )

    def run(seed: str) -> str:
        env = dict(os.environ, PYTHONHASHSEED=seed)
        return subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=PLUGIN,
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout

    assert run("0") == run("1")


# ------------------------------------------------------------- the command


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(
    make_repo: Callable[[Path], Path],
    git: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """A host repository with a zone map and a small package, on a feature branch."""
    root = make_repo(tmp_path / "host")
    _write(root, "docs/architecture.zones.toml", MAP)
    _write(root, "src/api/__init__.py", "")
    _write(root, "src/api/models.py", "X = 1\n")
    _write(root, "src/web/view.py", "from api.models import X\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(root)
    return root


def _commit(git: Callable[..., str], root: Path, name: str, text: str) -> None:
    _write(root, name, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", name)


def test_no_map_prints_one_line_and_exits_zero(
    make_repo: Callable[[Path], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Value: protects=a host without a map ships with no Picture; fails_when=a missing
    # map is a fault; why_new=the map is optional in a generic host; seam=none
    monkeypatch.chdir(make_repo(tmp_path / "bare"))
    assert impact.main(["--base", "main"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert "no zone map is configured" in out


def test_a_named_map_that_is_missing_is_a_usage_fault(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert impact.main(["--map", "nope.toml"]) == 2
    assert "nope.toml" in capsys.readouterr().err


def test_an_invalid_map_exits_one(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(
        repo,
        "docs/architecture.zones.toml",
        'schema=1\n[zones.x]\ntitle="X"\npaths=["a"]\nedges=["y"]\n',
    )
    assert impact.main(["--base", "main"]) == 1
    assert "error" in capsys.readouterr().err


def test_explain_owned_and_unowned(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert impact.main(["--explain", "src/api/models.py"]) == 0
    assert "zone 'api'" in capsys.readouterr().out
    assert impact.main(["--explain", "no/such/path.txt"]) == 0
    assert "owned by no zone" in capsys.readouterr().out


def test_a_bad_base_exits_two_with_a_remedy(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert impact.main(["--base", "definitely-not-a-ref"]) == 2
    assert "--base" in capsys.readouterr().err


def test_an_empty_diff_reports_no_changes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert impact.main(["--base", "HEAD"]) == 0
    assert "no changes against HEAD" in capsys.readouterr().out


def test_a_branch_report_follows_the_import_graph(
    repo: Path, git: Callable[..., str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=the import signal covers every tracked Python file, not one
    # directory; fails_when=the graph misses a package import; why_new=generic graph; seam=git
    _commit(git, repo, "src/api/models.py", "X = 2\n")
    assert impact.main(["--base", "main", "--format", "both"]) == 0
    out = capsys.readouterr().out
    assert "```mermaid" in out
    assert "    class z_api changed" in out
    assert "| web | - | api, src/api/models.py | - |" in out


def test_an_unowned_changed_file_exits_one(
    repo: Path, git: Callable[..., str], capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(git, repo, "LICENSE", "x\n")
    assert impact.main(["--base", "main"]) == 0
    capsys.readouterr()
    _commit(git, repo, "stray/file.txt", "x\n")
    assert impact.main(["--base", "main"]) == 1
    captured = capsys.readouterr()
    assert "- `stray/file.txt`" in captured.out
    assert "LICENSE" not in captured.out
    assert "owned by no zone" in captured.err


def test_a_deleted_file_and_its_zone_entry_resolve_via_the_base_map(
    repo: Path, git: Callable[..., str], capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "rm", "-q", "-r", "src/web")
    text = MAP.replace('[zones.web]\ntitle = "Web app"\npaths = ["src/web/**"]\n', "")
    _commit(
        git, repo, "docs/architecture.zones.toml", text.replace('edges = ["web"]', "edges = []")
    )
    assert impact.main(["--base", "main"]) == 0
    assert "unclassified" not in capsys.readouterr().out.lower()


def test_coverage_lists_unowned_tracked_files(
    repo: Path, git: Callable[..., str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert impact.main(["--coverage"]) == 1
    assert "README.md" in capsys.readouterr().err
    _commit(
        git,
        repo,
        "docs/architecture.zones.toml",
        MAP.replace('"LICENSE"', '"LICENSE", "README.md"'),
    )
    assert impact.main(["--coverage"]) == 0


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        impact.main(["--help"])
    assert exc.value.code == 0
    assert "impact" in capsys.readouterr().out


# ------------------------------------------------------------- the launcher


def test_the_launcher_parses_on_3_8() -> None:
    text = (PLUGIN / "bin" / "loop-impact").read_text(encoding="utf-8")
    ast.parse(text, feature_version=(3, 8))


def test_the_launcher_runs_from_any_directory(repo: Path) -> None:
    run = subprocess.run(
        [PLUGIN / "bin" / "loop-impact", "--explain", "docs/x.md"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert "zone 'docs'" in run.stdout


def test_the_ship_stage_runs_the_impact_map() -> None:
    text = (PLUGIN / "skills" / "pipeline" / "stages" / "ship.md").read_text("utf-8")
    assert "loop-impact --base origin/<base branch> --format both" in text
    assert "## Picture" in text
