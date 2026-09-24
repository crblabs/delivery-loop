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

Which paths those are is not written here. The carve-out set, the carve-out
prefixes, the loop prefixes and the stage-prompt target are fields on
``LoopConfig``, so this module names no harness directory of its own. Every
public function takes the config and falls back to the built-in defaults.

Standard library only.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from core.config import DEFAULTS, LoopConfig

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FENCE_OPEN_RE = re.compile(r"^[ \t]*```loop-edits[ \t]*$")
_FENCE_CLOSE_RE = re.compile(r"^[ \t]*```[ \t]*$")


def _normalize(rel: str) -> str:
    """Strip a leading `./` (repeatedly) and surrounding whitespace."""
    rel = (rel or "").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def is_carveout(rel: str, config: LoopConfig = DEFAULTS) -> bool:
    """Whether `rel` is an enforcement file no declaration can ever authorize.

    Matched case-insensitively: on a case-insensitive filesystem a case-variant
    spelling names the SAME file, so it must be caught too."""
    rel = _normalize(rel)
    cf = rel.casefold()
    if cf in config.carve_outs_cf:
        return True
    if cf.startswith(config.carve_out_prefixes_cf):
        return True
    return bool(config.state_file_re.match(rel))


def _in_loop_set(rel: str, config: LoopConfig) -> bool:
    """Whether `rel` is inside the region a declaration may name at all."""
    return rel in config.loop_exact or rel.startswith(config.loop_prefixes)


def _clean_token(line: str) -> str:
    """One declared line to a bare repo-relative token: strip surrounding
    whitespace and backticks and a trailing list separator."""
    tok = line.strip()
    tok = tok.strip("`").strip()
    tok = tok.rstrip(",;").strip()
    return tok


def _parse(text: str, config: LoopConfig) -> tuple[list[str], list[str]]:
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
        if rel.startswith(config.stage_shorthand):
            rel = config.stage_target + rel[len(config.stage_shorthand) :]
        rel = _normalize(rel)
        if _in_loop_set(rel, config) and not is_carveout(rel, config):
            accepted.add(rel)
        else:
            rejected.append(tok)
    return sorted(accepted), rejected


def parse_loop_edits_block(text: str, config: LoopConfig = DEFAULTS) -> list[str]:
    """The sorted, de-duplicated set of authorized declared loop paths."""
    return _parse(text, config)[0]


def parse_loop_edits_rejected(text: str, config: LoopConfig = DEFAULTS) -> list[str]:
    """The path-looking tokens that were dropped (carve-out, out-of-root, `..`)."""
    return _parse(text, config)[1]


def _is_segment_ancestor(dir_entry: str, rel: str) -> bool:
    """Whether `dir_entry` (a directory declaration) is a segment-boundary
    ancestor of `rel`. Uses path parts, never a raw `startswith`, so a sibling
    directory whose name merely begins with the same letters does NOT match."""
    prefix = PurePosixPath(dir_entry.rstrip("/")).parts
    parts = PurePosixPath(rel).parts
    return len(parts) > len(prefix) and parts[: len(prefix)] == prefix


def classify_loop_path(rel: str, declared, config: LoopConfig = DEFAULTS) -> str:
    """Classify one repo-relative path against a declared set.

    Returns:
      * "denied-carveout": an enforcement file, never authorizable;
      * "authorized": matched a declared entry (exact for a file entry, a
        segment-boundary ancestor for a directory entry ending in `/`);
      * "not-declared": otherwise.

    The carve-out is checked first, so it always wins over the allow-list.
    """
    rel = _normalize(rel)
    if is_carveout(rel, config):
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
