#!/usr/bin/env bash
# Claude Code PostToolUse hook: after an edit tool writes a Python file in this
# repository, format it and lint it. Exit 2 hands ruff's findings back to
# Claude, so it fixes them in the same turn instead of CI finding them later.
file=$(python3 -c 'import json, sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))' 2>/dev/null)
case "$file" in
    *.py) ;;
    *) exit 0 ;;
esac
[ -f "$file" ] || exit 0
root=$(cd "${CLAUDE_PROJECT_DIR:-.}" && pwd -P) || exit 0
case "$(cd "$(dirname "$file")" && pwd -P)/" in
    "$root"/*) ;;
    *) exit 0 ;;
esac
cd "$root" || exit 0
uv run --quiet ruff format --quiet "$file" >/dev/null 2>&1
if ! out=$(uv run --quiet ruff check --output-format concise "$file" 2>&1); then
    echo "ruff found problems in the file just written; fix them:" >&2
    echo "$out" >&2
    exit 2
fi
