"""The zone map: load it, ask which zone owns a path, check coverage.

This is the declared half of the impact map; `core/impact.py` is the computed
half. The map is one TOML file in the host repository, by default
`docs/architecture.zones.toml`. It names the zones of the system, the paths each
zone owns, and the data-flow edges between zones. An edge `a -> b` says a change
in `a` can reach `b`; any dependency the import graph cannot see (a job that
runs a script, a check that reads every table) is declared as an edge.

    schema = 1
    unzoned = ["LICENSE"]          # optional: paths deliberately owned by no zone

    [zones.api]
    title = "API"
    paths = ["src/api/**", "openapi.yaml"]
    edges = ["web"]

THE PATTERNS. Kept small, so ownership and overlap are decidable:
  * an exact file path, no glob: `openapi.yaml`;
  * a directory prefix ending `/**`: `src/api/**` owns that directory and
    everything under it.
The most specific pattern wins: an exact path beats a prefix, a longer prefix
beats a shorter one. Two zones declaring the same pattern is the only way a tie
can arise, and that is an error at load.

THE KEYS. A zone key is letters, digits, `_` and `-`, so it is safe as a diagram
node. Two keys that differ only in `-` against `_` are refused, because the
diagram folds them into one node.

Standard library only, and no file read at import. A bad map raises
`ZoneError` with a message a maintainer can act on; the caller turns it into
exit 1 (fix the record).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

DEFAULT_MAP = "docs/architecture.zones.toml"

_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_ZONE_KEYS = {"title", "paths", "edges"}
_TOP_KEYS = {"schema", "zones", "unzoned"}


class ZoneError(ValueError):
    """The zone map is invalid. The message says what to fix."""


@dataclass(frozen=True)
class Zone:
    key: str
    title: str
    paths: tuple[str, ...]
    edges: tuple[str, ...]


class Zones:
    """An ordered map of zone key to Zone, with a pattern index for lookup."""

    def __init__(self, by_key: dict[str, Zone], unzoned: tuple[str, ...] = ()):
        self.by_key = by_key
        self.unzoned = unzoned
        self._patterns = [
            (pattern, zone.key, specificity(pattern))
            for zone in by_key.values()
            for pattern in zone.paths
        ]

    def __iter__(self):
        return iter(self.by_key.values())

    def __len__(self):
        return len(self.by_key)


# ------------------------------------------------------------- patterns


def _literal(pattern: str) -> str:
    return pattern[:-3] if pattern.endswith("/**") else pattern


def specificity(pattern: str) -> tuple[int, int]:
    """Higher is more specific: an exact path (kind 2) beats any prefix (kind 1),
    and within a kind the longer literal wins."""
    return (1 if pattern.endswith("/**") else 2, len(_literal(pattern)))


def matches(pattern: str, path: str) -> bool:
    """True when `pattern` owns the repo-relative `path`."""
    lit = _literal(pattern)
    if pattern.endswith("/**"):
        return path == lit or path.startswith(lit + "/")
    return path == lit


def _validate_pattern(pattern: str, where: str) -> None:
    if "*" in _literal(pattern) or "?" in pattern or "[" in pattern:
        raise ZoneError(
            f"{where}: path {pattern!r} uses an unsupported glob. "
            "A pattern is an exact file path or a directory prefix ending '/**'."
        )
    if not pattern or pattern.startswith("/") or ".." in pattern.split("/"):
        raise ZoneError(f"{where}: path {pattern!r} must be a non-empty repo-relative path.")


# ------------------------------------------------------------- load


def load(path: Path | str) -> Zones:
    """Read the zone map from a file and validate it. Raises ZoneError."""
    map_path = Path(path)
    try:
        text = map_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ZoneError(f"{map_path}: cannot read the zone map ({type(exc).__name__})") from exc
    return load_text(text, str(map_path))


def _str_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _zone_fields(key: str, body: object) -> tuple[str, list[str], list[str]]:
    if not _KEY.match(key):
        raise ZoneError(f"zone {key!r}: a key is letters, digits, '_' and '-' only.")
    if not isinstance(body, dict):
        raise ZoneError(f"zone {key!r}: must be a table.")
    unknown = set(body) - _ZONE_KEYS
    if unknown:
        raise ZoneError(f"zone {key!r}: unknown key(s) {sorted(unknown)}; a typo hides a hole.")
    title = body.get("title")
    if not isinstance(title, str) or not title:
        raise ZoneError(f"zone {key!r}: missing a non-empty 'title'.")
    paths = body.get("paths")
    if not _str_list(paths) or not paths:
        raise ZoneError(f"zone {key!r}: 'paths' must be a non-empty list of strings.")
    edges = body.get("edges", [])
    if not _str_list(edges):
        raise ZoneError(f"zone {key!r}: 'edges' must be a list of zone keys.")
    return title, paths, edges


def _parse_zone(key: str, body: object, owner: dict[str, str]) -> Zone:
    title, paths, edges = _zone_fields(key, body)
    for pattern in paths:
        _validate_pattern(pattern, f"zone {key!r}")
        if pattern in owner:
            raise ZoneError(
                f"path {pattern!r} is declared by both zone {owner[pattern]!r} "
                f"and zone {key!r}. A path belongs to exactly one zone."
            )
        owner[pattern] = key
    return Zone(key=key, title=title, paths=tuple(paths), edges=tuple(edges))


def _check_node_ids(by_key: dict[str, Zone]) -> None:
    seen: dict[str, str] = {}
    for key in by_key:
        nid = node_id(key)
        if nid in seen:
            raise ZoneError(
                f"zones {seen[nid]!r} and {key!r} both map to diagram node {nid!r} "
                "(a '-' against '_' clash); rename one."
            )
        seen[nid] = key


def _check_edges(by_key: dict[str, Zone]) -> None:
    for zone in by_key.values():
        for target in zone.edges:
            if target not in by_key:
                valid = ", ".join(sorted(by_key))
                raise ZoneError(
                    f"zone {zone.key!r} has edge -> {target!r}, which is not a defined zone "
                    f"(add [zones.{target}], or fix the edge). Valid zones: {valid}."
                )


def _unzoned(raw: dict, source: str) -> tuple[str, ...]:
    values = raw.get("unzoned", [])
    if not _str_list(values):
        raise ZoneError(f"{source}: 'unzoned' must be a list of paths.")
    for pattern in values:
        _validate_pattern(pattern, f"{source}: unzoned")
    return tuple(values)


def load_text(text: str, source: str = "<zone map>") -> Zones:
    """Parse and validate a zone map from a TOML string. `source` names it in
    errors; the impact tool loads the base revision's map from a git blob."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ZoneError(f"{source}: not valid TOML ({exc})") from exc
    if raw.get("schema") != 1:
        raise ZoneError(f"{source}: unknown schema {raw.get('schema')!r}; this reader knows 1.")
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise ZoneError(f"{source}: unknown top-level key(s) {sorted(unknown)}.")
    table = raw.get("zones")
    if not isinstance(table, dict) or not table:
        raise ZoneError(f"{source}: no [zones.*] tables defined.")
    owner: dict[str, str] = {}
    by_key = {key: _parse_zone(key, body, owner) for key, body in table.items()}
    _check_node_ids(by_key)
    _check_edges(by_key)
    return Zones(by_key, _unzoned(raw, source))


# ------------------------------------------------------------- lookup


def zone_of(path: str, zones: Zones) -> str | None:
    """The zone that owns `path` (repo-relative, forward slashes), or None."""
    best: tuple[tuple[int, int], str] | None = None
    for pattern, key, spec in zones._patterns:
        if matches(pattern, path) and (best is None or spec > best[0]):
            best = (spec, key)
    return best[1] if best else None


def is_unzoned(path: str, zones: Zones) -> bool:
    """True when the map lists `path` as deliberately owned by no zone."""
    return any(matches(pattern, path) for pattern in zones.unzoned)


def coverage(zones: Zones, tracked_paths: list[str]) -> list[str]:
    """The tracked paths owned by no zone and not listed as unzoned, sorted.
    Run over every tracked file, it names a path no zone has claimed."""
    return sorted(
        p for p in tracked_paths if not is_unzoned(p, zones) and zone_of(p, zones) is None
    )


def node_id(key: str) -> str:
    """A diagram-safe node id. The prefix keeps a key like `end`, a mermaid
    keyword, from breaking the flowchart."""
    return "z_" + key.replace("-", "_")
