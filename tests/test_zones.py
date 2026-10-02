"""The zone map loader, the declared half of the impact map.

Each test names the failure it prevents: a bad map must raise a message a
maintainer can act on, and the lookup must be deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import zones

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "zones.toml"

VALID = """
schema = 1
[zones.alpha]
title = "Alpha"
paths = ["src/a.py", "data/alpha/**"]
edges = ["beta"]
[zones.beta]
title = "Beta"
paths = ["src/b.py"]
edges = []
"""


def _load(body: str) -> zones.Zones:
    return zones.load_text(body, "<t>")


def test_valid_map_loads() -> None:
    z = _load(VALID)
    assert set(z.by_key) == {"alpha", "beta"}
    assert z.by_key["alpha"].edges == ("beta",)
    assert z.unzoned == ()


def test_the_shipped_template_loads() -> None:
    # Value: protects=a host copies a map that loads; fails_when=the template
    # drifts from the loader's rules; why_new=the template ships with the tool; seam=none
    z = zones.load(TEMPLATE)
    assert len(z) >= 3
    assert zones.zone_of("src/api/app.py", z) == "api"


def test_load_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "zones.toml"
    path.write_text(VALID, encoding="utf-8")
    assert len(zones.load(path)) == 2


def test_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(zones.ZoneError, match="cannot read"):
        zones.load(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("schema = 1\n[zones.x\n", "not valid TOML"),
        ('schema=2\n[zones.x]\ntitle="X"\npaths=["a"]\n', "schema"),
        ('schema=1\n[zones.x]\npaths=["a"]\n', "title"),
        ('schema=1\n[zones.x]\ntitle="X"\n', "paths"),
        ('schema=1\n[zones.x]\ntitle="X"\npaths=[]\n', "paths"),
        ('schema=1\n[zones.x]\ntitle="X"\npaths=["a"]\nedgs=[]\n', "unknown key"),
        ('schema=1\n[zones.x]\ntitle="X"\npaths=["a"]\nedges="y"\n', "edges"),
        ('schema=1\nzone=1\n[zones.x]\ntitle="X"\npaths=["a"]\n', "unknown top-level"),
        ("schema=1\n", "no \\[zones"),
        ('schema=1\n[zones."a b"]\ntitle="X"\npaths=["a"]\n', "letters, digits"),
        ('schema=1\nunzoned="a"\n[zones.x]\ntitle="X"\npaths=["a"]\n', "unzoned"),
    ],
)
def test_a_bad_map_raises(body: str, match: str) -> None:
    with pytest.raises(zones.ZoneError, match=match):
        _load(body)


def test_dangling_edge_raises_with_both_names() -> None:
    body = 'schema=1\n[zones.x]\ntitle="X"\npaths=["a"]\nedges=["y"]\n'
    with pytest.raises(zones.ZoneError, match="x.*->.*y"):
        _load(body)


def test_duplicate_pattern_raises() -> None:
    body = (
        "schema=1\n"
        '[zones.x]\ntitle="X"\npaths=["src/a.py"]\n'
        '[zones.y]\ntitle="Y"\npaths=["src/a.py"]\n'
    )
    with pytest.raises(zones.ZoneError, match="declared by both"):
        _load(body)


@pytest.mark.parametrize("pattern", ["src/*.py", "**", "src/a?.py", "src/[ab].py"])
def test_bad_glob_raises(pattern: str) -> None:
    body = f'schema=1\n[zones.x]\ntitle="X"\npaths=["{pattern}"]\n'
    with pytest.raises(zones.ZoneError, match="unsupported glob"):
        _load(body)


@pytest.mark.parametrize("pattern", ["/abs/path", "../out/**", ""])
def test_a_path_outside_the_repository_raises(pattern: str) -> None:
    body = f'schema=1\n[zones.x]\ntitle="X"\npaths=["{pattern}"]\n'
    with pytest.raises(zones.ZoneError, match="repo-relative"):
        _load(body)


def test_node_id_collision_raises() -> None:
    body = (
        "schema=1\n"
        '[zones."a-b"]\ntitle="A"\npaths=["src/a.py"]\n'
        '[zones.a_b]\ntitle="B"\npaths=["src/b.py"]\n'
    )
    with pytest.raises(zones.ZoneError, match="diagram node"):
        _load(body)


def test_node_id_cannot_be_a_diagram_keyword() -> None:
    assert zones.node_id("end") != "end"
    assert zones.node_id("my-zone") == "z_my_zone"


def test_zone_of_prefix_recurses() -> None:
    assert zones.zone_of("data/alpha/deep/nested/file.csv", _load(VALID)) == "alpha"


def test_zone_of_prefix_owns_its_own_directory_name_only() -> None:
    assert zones.zone_of("data/alphabet.csv", _load(VALID)) is None


def test_zone_of_exact_beats_prefix() -> None:
    body = (
        "schema=1\n"
        '[zones.broad]\ntitle="Broad"\npaths=["data/**"]\n'
        '[zones.narrow]\ntitle="Narrow"\npaths=["data/x/one.csv"]\nedges=["broad"]\n'
    )
    z = _load(body)
    assert zones.zone_of("data/x/one.csv", z) == "narrow"
    assert zones.zone_of("data/x/two.csv", z) == "broad"


def test_zone_of_longer_prefix_wins() -> None:
    body = (
        "schema=1\n"
        '[zones.broad]\ntitle="Broad"\npaths=["data/**"]\n'
        '[zones.deep]\ntitle="Deep"\npaths=["data/x/**"]\nedges=["broad"]\n'
    )
    z = _load(body)
    assert zones.zone_of("data/x/y.csv", z) == "deep"
    assert zones.zone_of("data/z.csv", z) == "broad"


def test_zone_of_unowned_is_none() -> None:
    assert zones.zone_of("nowhere/file.txt", _load(VALID)) is None


def test_coverage_finds_unowned() -> None:
    tracked = ["src/a.py", "src/b.py", "surprise/new.py"]
    assert zones.coverage(_load(VALID), tracked) == ["surprise/new.py"]


def test_coverage_respects_the_unzoned_list() -> None:
    z = _load('unzoned = ["LICENSE", "vendor/**"]\n' + VALID)
    tracked = ["src/a.py", "LICENSE", "vendor/lib/x.js", "surprise/new.py"]
    assert zones.coverage(z, tracked) == ["surprise/new.py"]
    assert zones.is_unzoned("vendor/lib/x.js", z)
