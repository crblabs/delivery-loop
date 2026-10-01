#!/usr/bin/env python3
"""The plugin's UserPromptSubmit hook: where a person resumes or aborts a run.

Only a person types a prompt; the agent's own calls never reach this hook. So a
typed ``/delivery-loop:pipeline resume`` or ``abort`` is applied here, with the
session id the harness supplies, and its result reaches the agent as context.
Every other prompt returns at once. Python 3.8 must still parse this file.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from adapters.claude_code import bootstrap as boot  # noqa: E402
from core import run_index as ri  # noqa: E402


def main() -> int:
    raw = sys.stdin.read()
    payload = boot.payload_of(raw)
    if payload is None or boot.prompt_command(payload.get("prompt")) is None:
        return 0
    if ri.old_python():
        result = boot.old_python_hook("prompt", payload)
        if result is not None:
            print(boot.context_json(result["context"]))
        return 0
    from adapters.claude_code.hooks import emit, run_prompt

    return emit(run_prompt(raw))


if __name__ == "__main__":
    sys.exit(main())
