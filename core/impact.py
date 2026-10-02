"""The impact map: which zones a branch touched, and what those changes reach.

This is the computed half of the impact map; `core/zones.py` holds the declared
half. Given the diff from the merge base to HEAD, it colours each zone of the
host's zone map: a zone with a changed file is direct (new, changed or
removed), a zone a change reaches is indirect, and the rest are untouched.
"Untouched" means no change was detected by the modelled signals. It is not a
proof of no effect; the unclassified list and the declared edges keep that
honest.

THE SIGNALS.
  * Ownership: the zone whose pattern owns a changed path is direct. A deleted
    file, and a rename's source, resolve against the map at the merge base, so a
    branch that deletes a file and its zone entry still names the owner.
  * Declared edges: a changed path marks every zone its own zone reaches along
    the map's edges, transitively. A dependency the code does not show (a job
    that runs a script, a check that reads every table) is declared as an edge.
  * The Python import graph: every tracked `*.py` file at the merge base and at
    HEAD, read from committed blobs. A changed Python file marks the zone of
    every file that imports it, transitively, with the changed file as the via.

MODULE NAMES. A file's module name comes from its path: `src/pkg/mod.py` is
`src.pkg.mod`, and also `pkg.mod` and `mod`, because the import root (a `src/`
layout, a script directory on `sys.path`) is not known. `pkg/__init__.py` is
`pkg`. An absolute import resolves first against the importer's own directory
(a script importing a sibling), then against the longest name that matches.
`from a import b` tries `a.b` (a submodule) before `a`. A relative import
resolves by path from the importer's package. The limits: a short name that
several files share (`utils`) links to all of them, a repo file that shadows a
standard module name links every importer of that module, dynamic imports
(`importlib`, `__import__`) are not seen, and a file that does not parse
contributes no edges. Each limit over-claims or is a declared edge away.

Output: a mermaid flowchart (the same picture on every branch, only the
colours change) and a markdown table of zone, direct, indirect via, untouched.
Deterministic: sorted throughout, no timestamps.

    loop-impact                              # table, diff since the merge base
    loop-impact --format both                # mermaid and table (the ship stage)
    loop-impact --base origin/main           # an explicit base ref
    loop-impact --map docs/zones.toml        # a map other than the default
    loop-impact --explain src/api/app.py     # which zone owns a path, and why
    loop-impact --coverage                   # every tracked file no zone owns

With no map at the default path, it prints one line and exits 0, so a host
without a zone map skips the picture. Exit codes: 0 a clean report; 1 fix the
record (the map is invalid, or a changed file is owned by no zone: add it to
the map or to its `unzoned` list); 2 a usage or environment fault.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from core import zones as zonesmod
from core.config import git_env


class ImpactError(Exception):
    """A usage or environment fault. Maps to exit 2."""


# ------------------------------------------------------------- changes


@dataclass(frozen=True)
class Change:
    status: str
    old: str | None
    new: str


def parse_name_status(raw: str) -> list[Change]:
    """Parse `git diff --name-status`, NUL-delimited (`-z`) or plain text.

    A plain change is `STATUS path`, a rename or copy `STATUS old new`. The
    status carries a similarity score on a rename (`R100`) or a copy (`C085`);
    only the letter is kept. A truncated record is dropped, never a crash.
    """
    if "\0" in raw:
        fields = raw.split("\0")
    else:
        fields = [f for part in raw.splitlines() for f in part.split("\t")]
    fields = [f for f in fields if f != ""]
    changes: list[Change] = []
    i = 0
    while i < len(fields):
        letter = fields[i][0]
        width = 3 if letter in ("R", "C") else 2
        if i + width > len(fields):
            break
        old = fields[i + 1] if width == 3 else None
        changes.append(Change(status=letter, old=old, new=fields[i + width - 1]))
        i += width
    return changes


# ------------------------------------------------------------- import graph


def module_names(path: str) -> list[str]:
    """Every dotted name `path` may be imported by, longest first: each suffix
    of its path whose parts are all identifiers. Not a `.py` file: none."""
    if not path.endswith(".py"):
        return []
    parts = path[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    names = []
    for i in range(len(parts)):
        tail = parts[i:]
        if all(p.isidentifier() for p in tail):
            names.append(".".join(tail))
    return names


class ModuleIndex:
    """The tracked Python files, by path and by every dotted name they carry."""

    def __init__(self, paths: list[str]):
        self.paths = set(paths)
        self.names: dict[str, set[str]] = defaultdict(set)
        for path in paths:
            for name in module_names(path):
                self.names[name].add(path)

    def by_parts(self, parts: list[str]) -> set[str]:
        """The file a package-relative path names: `a/b.py` or `a/b/__init__.py`."""
        if not parts:
            return set()
        stem = "/".join(parts)
        return {p for p in (f"{stem}.py", f"{stem}/__init__.py") if p in self.paths}

    def absolute(self, name: str, importer: str) -> set[str]:
        """The files an absolute import of `name` reaches: a sibling of the
        importer first, then the longest dotted name the index holds."""
        parts = name.split(".")
        here = importer.split("/")[:-1]
        for n in range(len(parts), 0, -1):
            sibling = self.by_parts(here + parts[:n]) if here else set()
            if sibling:
                return sibling
        for n in range(len(parts), 0, -1):
            hit = self.names.get(".".join(parts[:n]))
            if hit:
                return set(hit)
        return set()

    def relative(self, level: int, module: str | None, name: str, importer: str) -> set[str]:
        """The files `from <dots><module> import <name>` reaches, by path."""
        base = importer.split("/")[:-1]
        if level - 1 > len(base):
            return set()
        base = base[: len(base) - (level - 1)]
        mod = module.split(".") if module else []
        if name != "*":
            hit = self.by_parts(base + mod + [name])
            if hit:
                return hit
        return self.by_parts(base + mod)


def _from_imports(node: ast.ImportFrom, path: str, index: ModuleIndex) -> set[str]:
    if node.level:
        return {
            p for a in node.names for p in index.relative(node.level, node.module, a.name, path)
        }
    if not node.module:
        return set()
    return {p for a in node.names for p in index.absolute(f"{node.module}.{a.name}", path)}


def _node_imports(node: ast.AST, path: str, index: ModuleIndex) -> set[str]:
    if isinstance(node, ast.Import):
        return {p for alias in node.names for p in index.absolute(alias.name, path)}
    if isinstance(node, ast.ImportFrom):
        return _from_imports(node, path, index)
    return set()


def imports_of(path: str, source: str, index: ModuleIndex) -> set[str]:
    """The tracked files that `source` (the file at `path`) imports, itself
    excluded. A file that does not parse contributes no edges."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        found |= _node_imports(node, path, index)
    found.discard(path)
    return found


def importer_graph(sources: dict[str, str]) -> dict[str, set[str]]:
    """The reverse import graph over `sources` (path to source): each file
    mapped to the files that import it."""
    index = ModuleIndex(sorted(sources))
    importers: dict[str, set[str]] = {p: set() for p in sources}
    for importer, source in sources.items():
        for imported in imports_of(importer, source, index):
            importers[imported].add(importer)
    return importers


def transitive_importers(path: str, importers: dict[str, set[str]]) -> set[str]:
    """Every file that transitively imports `path`, itself excluded."""
    seen: set[str] = set()
    queue = deque(importers.get(path, set()))
    while queue:
        p = queue.popleft()
        if p in seen or p == path:
            continue
        seen.add(p)
        queue.extend(importers.get(p, set()))
    return seen


# ------------------------------------------------------------- report

_STATUS_CLASS = {"A": "new", "M": "changed", "T": "changed", "R": "changed", "D": "removed"}
_SALIENCE = ["removed", "new", "changed"]


@dataclass
class Report:
    base_ref: str = ""
    base_sha: str = ""
    head_sha: str = ""
    direct: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))
    indirect: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    unclassified: list[str] = field(default_factory=list)

    def any_changes(self) -> bool:
        return bool(self.direct or self.indirect or self.unclassified)


def downstream(zone_key: str, zs: zonesmod.Zones) -> set[str]:
    """Zones reachable from `zone_key` along declared edges, itself excluded."""
    seen: set[str] = set()
    queue = deque(zs.by_key[zone_key].edges)
    while queue:
        z = queue.popleft()
        if z not in seen:
            seen.add(z)
            queue.extend(zs.by_key[z].edges)
    seen.discard(zone_key)
    return seen


def _add_direct(report: Report, path: str, status: str, zmap: zonesmod.Zones) -> None:
    zone = zonesmod.zone_of(path, zmap)
    if zone is None:
        report.unclassified.append(path)
    else:
        report.direct[zone].append((status, path))


def _classify(change: Change, zs: zonesmod.Zones, base_map: zonesmod.Zones, report: Report):
    """Record one change's direct impact. Return its (path, removed) pairs for
    the indirect signals; a removed path resolves against the base map."""
    if change.status == "D":
        _add_direct(report, change.new, "removed", base_map)
        return [(change.new, True)]
    if change.status not in ("R", "C") or change.old is None:
        _add_direct(report, change.new, _STATUS_CLASS.get(change.status, "changed"), zs)
        return [(change.new, False)]
    if change.status == "C":
        _add_direct(report, change.new, "new", zs)
        return [(change.new, False)]
    if zonesmod.zone_of(change.old, base_map) != zonesmod.zone_of(change.new, zs):
        _add_direct(report, change.old, "removed", base_map)
        _add_direct(report, change.new, "new", zs)
    else:
        _add_direct(report, change.new, "changed", zs)
    return [(change.new, False), (change.old, True)]


def _indirect(
    touched: list[tuple[str, bool]],
    zs: zonesmod.Zones,
    base_map: zonesmod.Zones,
    importers: dict[str, set[str]],
    report: Report,
) -> None:
    for path, removed in touched:
        zone = zonesmod.zone_of(path, base_map if removed else zs)
        if zone in zs.by_key:
            for target in downstream(zone, zs):
                report.indirect[target].add(zone)
        if path.endswith(".py"):
            for importer in transitive_importers(path, importers):
                owner = zonesmod.zone_of(importer, zs)
                if owner is not None:
                    report.indirect[owner].add(path)


def compute(
    changes: list[Change],
    zs: zonesmod.Zones,
    importers: dict[str, set[str]],
    base_zs: zonesmod.Zones | None = None,
    report: Report | None = None,
) -> Report:
    """Build the impact report. Pure: no git, no filesystem; every input is
    given. `base_zs` is the map at the merge base, `zs` when absent there."""
    report = report or Report()
    base_map = base_zs or zs
    touched: list[tuple[str, bool]] = []
    for change in changes:
        touched.extend(_classify(change, zs, base_map, report))
    _indirect(touched, zs, base_map, importers, report)
    report.unclassified = sorted(set(report.unclassified))
    for zone in report.direct:
        report.direct[zone] = sorted(set(report.direct[zone]))
    return report


def zone_class(zone_key: str, report: Report) -> str:
    """The diagram class of a zone: its most salient direct status, else
    indirect, else untouched. Direct wins over indirect."""
    if zone_key in report.direct:
        classes = {c for c, _ in report.direct[zone_key]}
        return next((s for s in _SALIENCE if s in classes), "changed")
    if zone_key in report.indirect:
        return "indirect"
    return "untouched"


# ------------------------------------------------------------- rendering

_CLASSDEFS = {
    "new": "fill:#dcfce7,stroke:#16a34a,color:#14532d",
    "changed": "fill:#fef3c7,stroke:#d97706,color:#78350f",
    "removed": "fill:#fee2e2,stroke:#dc2626,color:#7f1d1d",
    "indirect": "fill:#dbeafe,stroke:#2563eb,color:#1e3a8a",
    "untouched": "fill:#f3f4f6,stroke:#9ca3af,color:#374151",
}


def to_mermaid(report: Report, zs: zonesmod.Zones) -> str:
    """A complete fenced mermaid block: the zone flowchart, coloured."""
    lines = ["```mermaid", "flowchart TD"]
    lines += [f"    classDef {cls} {style}" for cls, style in _CLASSDEFS.items()]
    keys = sorted(zs.by_key)
    for key in keys:
        title = zs.by_key[key].title.replace('"', "'")
        lines.append(f'    {zonesmod.node_id(key)}["{title}"]')
    for key in keys:
        for target in sorted(zs.by_key[key].edges):
            lines.append(f"    {zonesmod.node_id(key)} --> {zonesmod.node_id(target)}")
    for key in keys:
        lines.append(f"    class {zonesmod.node_id(key)} {zone_class(key, report)}")
    lines.append("```")
    return "\n".join(lines)


def md_path(path: str) -> str:
    """A filename made safe for a markdown table cell: a backtick, a pipe or a
    newline must not break the table or inject structure into a PR body."""
    return path.replace("`", "'").replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def to_table(report: Report, zs: zonesmod.Zones, map_name: str = zonesmod.DEFAULT_MAP) -> str:
    """A markdown table: zone, direct, indirect via, untouched."""
    lines = ["| Zone | Direct | Indirect via | Untouched |", "|---|---|---|---|"]
    for key in sorted(zs.by_key):
        direct = "<br>".join(f"{c}: `{md_path(p)}`" for c, p in report.direct.get(key, []))
        via = ", ".join(md_path(v) for v in sorted(report.indirect.get(key, set())))
        untouched = "yes" if zone_class(key, report) == "untouched" else ""
        lines.append(f"| {key} | {direct or '-'} | {via or '-'} | {untouched or '-'} |")
    out = "\n".join(lines)
    if report.unclassified:
        out += f"\n\n**Unclassified (owned by no zone; add to `{md_path(map_name)}`):**\n"
        out += "\n".join(f"- `{md_path(p)}`" for p in report.unclassified)
    out += (
        "\n\n_Untouched means no change detected by the modelled signals "
        "(declared paths, the declared edges, the Python import graph); "
        "it is not a proof of no effect._"
    )
    return out


def render(report: Report, fmt: str, zs: zonesmod.Zones, map_name: str) -> str:
    """The report as `fmt` (mermaid, table or both), under a one-line header."""
    head = f"Impact of {report.base_sha[:8]}..{report.head_sha[:8]} (base {report.base_ref})\n"
    if fmt == "mermaid":
        return head + to_mermaid(report, zs)
    if fmt == "table":
        return head + to_table(report, zs, map_name)
    return head + to_mermaid(report, zs) + "\n\n" + to_table(report, zs, map_name)


# ------------------------------------------------------------- git


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """git's raw stdout. Bytes, so an odd filename is neither mangled nor a crash."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, env=git_env(), input=stdin, capture_output=True
        )
    except FileNotFoundError as exc:
        raise ImpactError("git is not on PATH") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise ImpactError(f"git {' '.join(args[:3])} failed: {err}")
    return proc.stdout


def repo_root(cwd: Path) -> Path:
    """The top of the repository `cwd` is in. Outside one is a usage fault."""
    return Path(_git(cwd, "rev-parse", "--show-toplevel").decode().strip())


def _parse_batch(out: bytes, paths: list[str]) -> dict[str, str]:
    """Split `git cat-file --batch` output into one source per requested path."""
    sources: dict[str, str] = {}
    pos = 0
    for path in paths:
        end = out.index(b"\n", pos)
        header = out[pos:end].split(b" ")
        pos = end + 1
        if len(header) != 3 or header[1] != b"blob":
            continue
        size = int(header[2])
        sources[path] = out[pos : pos + size].decode("utf-8", "replace")
        pos += size + 1
    return sources


def python_sources(root: Path, ref: str) -> dict[str, str]:
    """Every tracked `*.py` file at `ref`, from committed blobs in one batch, so
    a symlink or a moving working tree cannot change the graph."""
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", ref)
    paths = [
        p
        for p in listing.decode("utf-8", "surrogateescape").split("\0")
        if p.endswith(".py") and "\n" not in p
    ]
    if not paths:
        return {}
    request = "".join(f"{ref}:{p}\n" for p in paths).encode("utf-8", "surrogateescape")
    return _parse_batch(_git(root, "cat-file", "--batch", stdin=request), paths)


def build_importers(root: Path, merge_base: str) -> dict[str, set[str]]:
    """The reverse import graph over the base and HEAD sources together, so a
    file deleted in the diff keeps the importers it had at the base."""
    sources = python_sources(root, merge_base)
    sources.update(python_sources(root, "HEAD"))
    return importer_graph(sources)


def resolve(root: Path, base: str) -> tuple[str, str]:
    """(merge base, head sha). Raises ImpactError with a remedy."""
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    try:
        merge_base = _git(root, "merge-base", base, head).decode().strip()
    except ImpactError as exc:
        raise ImpactError(
            f"cannot resolve the merge base of {base!r} and HEAD; fetch the base "
            f"or pass --base <ref> ({exc})"
        ) from exc
    return merge_base, head


def base_zones(root: Path, merge_base: str, rel_map: str | None) -> zonesmod.Zones | None:
    """The map at the merge base, or None when it was absent or invalid there:
    a branch may introduce the map, and an old broken map must not fail it."""
    if rel_map is None:
        return None
    try:
        text = _git(root, "show", f"{merge_base}:{rel_map}").decode("utf-8", "replace")
        return zonesmod.load_text(text, f"{merge_base}:{rel_map}")
    except (ImpactError, zonesmod.ZoneError):
        return None


# ------------------------------------------------------------- cli


def _explain(zs: zonesmod.Zones, path: str) -> int:
    zone = zonesmod.zone_of(path, zs)
    if zone is None:
        note = " It is listed as unzoned." if zonesmod.is_unzoned(path, zs) else ""
        print(f"{path}: owned by no zone.{note}")
        return 0
    patterns = [p for p in zs.by_key[zone].paths if zonesmod.matches(p, path)]
    best = max(patterns, key=zonesmod.specificity)
    print(f"{path}: zone {zone!r} (matched pattern {best!r}).")
    return 0


def _coverage(root: Path, zs: zonesmod.Zones, map_name: str) -> int:
    tracked = _git(root, "ls-files", "-z").decode("utf-8", "surrogateescape").split("\0")
    unowned = zonesmod.coverage(zs, [p for p in tracked if p])
    if not unowned:
        print("impact: every tracked file is owned by a zone or listed as unzoned.")
        return 0
    for path in unowned:
        print(f"  {path}", file=sys.stderr)
    print(f"error: tracked file(s) owned by no zone; add them to {map_name}.", file=sys.stderr)
    return 1


def _report(root: Path, args: argparse.Namespace, zs: zonesmod.Zones, rel: str | None) -> int:
    merge_base, head = resolve(root, args.base)
    diff = _git(root, "diff", "--name-status", "-z", "-M", f"{merge_base}..{head}")
    changes = parse_name_status(diff.decode("utf-8", "surrogateescape"))
    report = Report(base_ref=args.base, base_sha=merge_base, head_sha=head)
    importers = build_importers(root, merge_base)
    compute(changes, zs, importers, base_zones(root, merge_base, rel), report)
    report.unclassified = [p for p in report.unclassified if not zonesmod.is_unzoned(p, zs)]
    if not report.any_changes():
        print(f"impact: no changes against {args.base} ({merge_base[:8]}..{head[:8]}).")
        return 0
    print(render(report, args.format, zs, args.map))
    if report.unclassified:
        msg = f"error: changed file(s) owned by no zone; add them to {args.map}."
        print(msg, file=sys.stderr)
        return 1
    return 0


def _relative(root: Path, map_path: Path) -> str | None:
    try:
        return map_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _run(args: argparse.Namespace, map_given: bool) -> int:
    root = repo_root(Path.cwd())
    map_path = root / args.map
    if not map_path.is_file():
        if map_given:
            raise ImpactError(f"{args.map}: no zone map at that path")
        print(f"impact: no zone map is configured ({args.map} is absent); nothing to draw.")
        return 0
    try:
        zs = zonesmod.load(map_path)
    except zonesmod.ZoneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.explain is not None:
        return _explain(zs, args.explain)
    if args.coverage:
        return _coverage(root, zs, args.map)
    return _report(root, args, zs, _relative(root, map_path))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-impact",
        description="The impact map: which zones a branch touched, and what it reaches.",
    )
    parser.add_argument("--base", default="origin/main", help="base ref (default origin/main)")
    parser.add_argument(
        "--format", choices=("mermaid", "table", "both"), default="table", help="default table"
    )
    parser.add_argument(
        "--map", default=None, help=f"repo-relative zone map (default {zonesmod.DEFAULT_MAP})"
    )
    parser.add_argument("--explain", metavar="PATH", help="print the zone that owns PATH, and why")
    parser.add_argument(
        "--coverage", action="store_true", help="list every tracked file no zone owns"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    map_given = args.map is not None
    if not map_given:
        args.map = zonesmod.DEFAULT_MAP
    try:
        return _run(args, map_given)
    except ImpactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
