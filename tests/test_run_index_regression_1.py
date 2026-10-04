"""Regression: ISSUE-001 — a huge quoted word stalls the shell lexer past the guard's timeout.

Found by /qa on 2026-10-04
Report: .gstack/qa-reports/run-20261004T142012Z/qa-report-delivery-loop-hooks-2026-10-04.md
"""

from __future__ import annotations

import shlex

from core import run_index as ri


def test_a_huge_quoted_word_is_not_fed_whole_to_the_lexer(monkeypatch) -> None:
    # Value: protects=the guard answers a call with a megabyte PR body inside its timeout;
    # fails_when=shlex reads the whole quoted word (40 s at 1 MB, quadratic);
    # why_new=no test bounds the lexer's input; seam=monkeypatch shlex
    seen: list[int] = []
    real = shlex.shlex

    def spy(text, *args, **kwargs):
        seen.append(len(text))
        return real(text, *args, **kwargs)

    monkeypatch.setattr(ri.shlex, "shlex", spy)
    ri._lex.cache_clear()
    body = "wait " * 200_000
    calls = ri.gh_calls(f'gh pr create --body "{body}"\ngh pr merge 3')
    assert calls[0][:3] == ["pr", "create", "--body"]
    assert calls[1] == ["pr", "merge", "3"]
    assert max(seen) < 2 * ri.LEX_WORD_MAX
