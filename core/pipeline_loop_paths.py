#!/usr/bin/env python3
"""Shared loop-path classification for the delivery loop.

This is the ONE definition of which repo-relative paths a run may edit while it
is active, so the edit guard, the turn-end routine and the qa route never drift
on the meaning of "authorized". Both hooks import it rather than each holding a
copy.

Three things live here:

  * The carve-out: the enforcement files a declaration can NEVER authorize (the
    two hooks, their shims, this module itself, the two settings files, the state
    file and its siblings, and the archive directory). It is checked AFTER the
    allow-list and always wins, so a task that must edit the guard itself keeps
    the pauses, while a task that edits a stage prompt or a doc runs hands-off.
  * `parse_loop_edits_block` / `parse_loop_edits_rejected`: extract the declared
    paths (and the rejected tokens) from a ```loop-edits fenced block in a task
    brief or an approved plan.
  * `classify_loop_path`: "authorized" | "denied-carveout" | "not-declared" for
    one repo-relative path against a declared set.

Standard library only.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# --- carve-out: never authorizable, checked after the allow-list --------------
# Exact repo-relative paths the enforcement boundary owns.
CARVEOUT = frozenset(
    {
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/hooks/pipeline_guard.py",
        ".claude/hooks/pipeline_stop.py",
        ".claude/hooks/pipeline-guard",
        ".claude/hooks/pipeline-stop",
        ".claude/hooks/pipeline_loop_paths.py",
    }
)
# The archive directory of terminal runs.
_CARVEOUT_PREFIX = (".claude/pipeline-runs.local.d/",)
# The state file `.claude/pipeline.local.json` and its siblings
# (`.blocked`, `.lock`, `.corrupt-*`, `.tmp-*`). Case-insensitive: on a
# case-insensitive filesystem `.claude/PIPELINE.local.json` is the same file.
_CARVEOUT_STATE_RE = re.compile(r"^\.claude/pipeline\.local\b", re.IGNORECASE)
# Casefolded copies of the exact set and the prefix(es), so the carve-out also
# catches a case-variant spelling of an enforcement file
# (`.claude/hooks/PIPELINE_GUARD.py`) that names the same file on a
# case-insensitive filesystem and could otherwise be declared and edited.
_CARVEOUT_CF = frozenset(c.casefold() for c in CARVEOUT)
_CARVEOUT_PREFIX_CF = tuple(p.casefold() for p in _CARVEOUT_PREFIX)

# --- the loop set a declaration MAY name (subject to the carve-out) -----------
_LOOP_PREFIX = (".claude/hooks/", ".claude/skills/pipeline/")
_LOOP_EXACT = frozenset({".claude/settings.json"})

# The `stages/<name>` shorthand resolves to the stage-prompt directory.
_STAGE_SHORTHAND = "stages/"
_STAGE_TARGET = ".claude/skills/pipeline/stages/"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FENCE_OPEN_RE = re.compile(r"^[ \t]*```loop-edits[ \t]*$")
_FENCE_CLOSE_RE = re.compile(r"^[ \t]*```[ \t]*$")


def _normalize(rel: str) -> str:
    """Strip a leading `./` (repeatedly) and surrounding whitespace."""
    rel = (rel or "").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def is_carveout(rel: str) -> bool:
    """Whether `rel` is an enforcement file no declaration can ever authorize.

    Matched case-insensitively: on a case-insensitive filesystem a case-variant
    spelling (`.claude/hooks/PIPELINE_GUARD.py`, `.claude/settings.JSON`) names
    the SAME file, so it must be caught too."""
    rel = _normalize(rel)
    cf = rel.casefold()
    if cf in _CARVEOUT_CF:
        return True
    if cf.startswith(_CARVEOUT_PREFIX_CF):
        return True
    return bool(_CARVEOUT_STATE_RE.match(rel))


def _in_loop_set(rel: str) -> bool:
    """Whether `rel` is inside the region a declaration may name at all."""
    return rel in _LOOP_EXACT or rel.startswith(_LOOP_PREFIX)


def _clean_token(line: str) -> str:
    """One declared line to a bare repo-relative token: strip surrounding
    whitespace and backticks and a trailing list separator."""
    tok = line.strip()
    tok = tok.strip("`").strip()
    tok = tok.rstrip(",;").strip()
    return tok


def _parse(text: str) -> tuple[list[str], list[str]]:
    """(accepted, rejected) from every ```loop-edits fenced block in `text`.

    `accepted` is the sorted, de-duplicated set of authorized repo-relative
    paths (in the loop set, not carve-out, no `..`, no control character).
    `rejected` is every path-looking token dropped for `..`, a control
    character, being out of the loop root, or being carve-out, so a caller can
    echo them and the author can see what did not take.
    """
    accepted: set[str] = set()
    rejected: list[str] = []
    in_block = False
    for raw in (text or "").splitlines():
        if not in_block:
            if _FENCE_OPEN_RE.match(raw):
                in_block = True
            continue
        if _FENCE_CLOSE_RE.match(raw):
            in_block = False
            continue
        tok = _clean_token(raw)
        if not tok:
            continue
        if _CONTROL_RE.search(tok) or ".." in tok:
            rejected.append(tok)
            continue
        rel = _normalize(tok)
        if rel.startswith(_STAGE_SHORTHAND):
            rel = _STAGE_TARGET + rel[len(_STAGE_SHORTHAND) :]
        rel = _normalize(rel)
        if _in_loop_set(rel) and not is_carveout(rel):
            accepted.add(rel)
        else:
            rejected.append(tok)
    return sorted(accepted), rejected


def parse_loop_edits_block(text: str) -> list[str]:
    """The sorted, de-duplicated set of authorized declared loop paths."""
    return _parse(text)[0]


def parse_loop_edits_rejected(text: str) -> list[str]:
    """The path-looking tokens that were dropped (carve-out, out-of-root, `..`)."""
    return _parse(text)[1]


def _is_segment_ancestor(dir_entry: str, rel: str) -> bool:
    """Whether `dir_entry` (a directory declaration) is a segment-boundary
    ancestor of `rel`. Uses path parts, never a raw `startswith`, so
    `.claude/hooks-evil/x` does NOT match a `.claude/hooks/` entry."""
    prefix = PurePosixPath(dir_entry.rstrip("/")).parts
    parts = PurePosixPath(rel).parts
    return len(parts) > len(prefix) and parts[: len(prefix)] == prefix


def classify_loop_path(rel: str, declared) -> str:
    """Classify one repo-relative path against a declared set.

    Returns:
      * "denied-carveout": an enforcement file, never authorizable;
      * "authorized": matched a declared entry (exact for a file entry, a
        segment-boundary ancestor for a directory entry ending in `/`);
      * "not-declared": otherwise.

    The carve-out is checked first, so it always wins over the allow-list.
    """
    rel = _normalize(rel)
    if is_carveout(rel):
        return "denied-carveout"
    for entry in declared or []:
        if not isinstance(entry, str):
            continue
        entry = entry.strip()
        if not entry:
            continue
        if entry.endswith("/"):
            if _is_segment_ancestor(entry, rel):
                return "authorized"
        elif rel == _normalize(entry):
            return "authorized"
    return "not-declared"
