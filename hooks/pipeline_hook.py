#!/usr/bin/env python3
"""The plugin's hooks, one entry point named by the event in ``argv[1]``.

* ``stop``: the end of every agent turn, in every session.
* ``guard``: every edit, shell, skill and MCP write call (PreToolUse).
* ``prompt``: what a person types (UserPromptSubmit), so a typed
  ``/delivery-loop:pipeline resume`` or ``abort`` is applied here.

It answers from ``adapters.claude_code.bootstrap`` when the event needs no run
or the interpreter is older than the floor, and hands every other event to
``adapters.claude_code.hooks``. Python 3.8 must still parse this file.
"""

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from adapters.claude_code import bootstrap as boot  # noqa: E402
from core import run_index as ri  # noqa: E402

# kind: (whether a parsed payload needs the adapter, the old-Python result key,
# how that result is printed, the adapter function)
KINDS = {
    "stop": (boot.run_found, "block", boot.block_json, "run_stop"),
    "guard": (boot.needs_adapter, "deny", boot.deny_json, "run_guard"),
    "prompt": (
        lambda payload: boot.prompt_command(payload.get("prompt")) is not None,
        "context",
        boot.context_json,
        "run_prompt",
    ),
}


# Seconds before hooks.json's timeout (30 for guard, 60 for stop) at which the
# hook answers on its own: a hook the harness kills lets the call or the turn
# through, so a slow check fails closed here instead.
DEADLINES = {"guard": 25, "stop": 55}


def _arm(kind: str, payload: object) -> None:
    seconds = DEADLINES.get(kind)
    if seconds is None or not hasattr(signal, "SIGALRM"):
        return
    reason = (
        "delivery-loop ran out of time checking this %s, so it %s it to be safe. "
        "Fix: retry; if it repeats, run delivery-loop doctor. See %srunbook"
    )

    def expire(signum, frame):
        if kind == "guard":
            out = boot.deny_json(reason % ("call", "denied", ri.README_URL))
        elif isinstance(payload, dict) and payload.get("stop_hook_active") is True:
            os._exit(0)
        else:
            out = boot.block_json(reason % ("turn end", "kept the turn going", ri.README_URL))
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
        os._exit(0)

    signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)


def main(argv: list) -> int:
    if len(argv) != 1 or argv[0] not in KINDS:
        sys.stderr.write("usage: pipeline_hook.py stop|guard|prompt\n")
        return 0
    kind = argv[0]
    needs, key, render, runner = KINDS[kind]
    raw = sys.stdin.read()
    payload = boot.payload_of(raw)
    _arm(kind, payload)
    if ri.old_python():
        result = boot.old_python_hook(kind, payload or {})
        if result is not None:
            print(render(result[key]))
        return 0
    if payload is None:
        # The adapter reports an unparsed Stop or PreToolUse payload; a prompt
        # that does not parse is not a resume or an abort.
        if kind == "prompt":
            return 0
    elif not needs(payload):
        return 0
    from adapters.claude_code import hooks

    return hooks.emit(getattr(hooks, runner)(raw))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
