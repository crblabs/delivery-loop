#!/usr/bin/env python3
"""The plugin's Stop hook: the end of every agent turn, in every session.

It answers from ``adapters.claude_code.bootstrap`` when no run is active here or
the interpreter is older than the floor, and hands every other turn end to
``adapters.claude_code.hooks.run_stop``. Python 3.8 must still parse this file.
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
        result = boot.old_python_hook("stop", payload or {})
        if result is not None:
            print(boot.block_json(result["block"]))
        return 0
    if payload is not None and not boot.run_found(payload):
        return 0
    from adapters.claude_code.hooks import emit, run_stop

    return emit(run_stop(raw))


if __name__ == "__main__":
    sys.exit(main())
