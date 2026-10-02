#!/usr/bin/env bash
# The checks CI runs, so a person or the pre-push hook can run them first.
#
#   scripts/check.sh                 every step
#   scripts/check.sh lint format     only the named steps
#
# Steps: tests, lint, format, rules. CI calls each step by name, so a step
# changes here, in one place.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2

error() {
    if [ -n "${GITHUB_ACTIONS:-}" ]; then
        echo "::error::$1"
    else
        echo "check: $1" >&2
    fi
}

step_tests() { uv run pytest -q; }

step_lint() { uv run ruff check .; }

step_format() { uv run ruff format --check .; }

step_rules() {
    local fail=0
    # Fixed strings, so the U+0085 in a tracked fixture cannot read as a dash.
    local en em
    en=$(printf '\342\200\223')
    em=$(printf '\342\200\224')
    if git grep -n -F -e "$en" -e "$em" -- .; then
        error "The lines above use an en dash or an em dash. Use a hyphen, a comma, a colon or a new sentence."
        fail=1
    fi
    if git grep -n -E -e 'COD-[0-9]+' -- .; then
        error "The lines above carry a ticket id. The repository history carries ids, so remove it from the file."
        fail=1
    fi
    # A private name must never reach this public repository. The patterns live
    # in the repository variable FORBIDDEN_NAMES, which CI passes as FORBIDDEN.
    if [ -n "${FORBIDDEN:-}" ] && git grep -n -E -e "$FORBIDDEN" -- .; then
        error "The lines above carry a name that must stay private. Remove it."
        fail=1
    fi
    return $fail
}

steps=("$@")
if [ ${#steps[@]} -eq 0 ]; then
    steps=(tests lint format rules)
fi

failed=()
for step in "${steps[@]}"; do
    case "$step" in
        tests | lint | format | rules) "step_$step" || failed+=("$step") ;;
        *)
            error "unknown step: $step (tests, lint, format, rules)"
            exit 2
            ;;
    esac
done

if [ ${#failed[@]} -gt 0 ]; then
    error "failed: ${failed[*]}"
    exit 1
fi
