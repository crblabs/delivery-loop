#!/usr/bin/env python3
"""The plugin's PreToolUse hook: every edit, shell, skill and MCP write call.

It answers from ``adapters.claude_code.bootstrap`` when the call needs no run or
the interpreter is older than the floor, and hands every other call to
``adapters.claude_code.hooks.run_guard``. Python 3.8 must still parse this file.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from adapters.claude_code import bootstrap as boot  # noqa: E402
from core import run_index as ri  # noqa: E402


def main() -> int:
    raw = sys.stdin.read()
    payload = boot.payload_of(raw)
    if ri.old_python():
        result = boot.old_python_hook("guard", payload or {})
        if result is not None:
            print(boot.deny_json(result["deny"]))
        return 0
    if payload is not None and not boot.needs_adapter(payload):
        return 0
    from adapters.claude_code.hooks import emit, run_guard

    return emit(run_guard(raw))


if __name__ == "__main__":
    sys.exit(main())
