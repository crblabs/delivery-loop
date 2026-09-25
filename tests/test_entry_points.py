"""The console entry points, pinned against the table that declares them.

A runbook calls these tools by name, so a name that does not resolve fails in
production rather than in review. The table is read from `pyproject.toml`, never
copied here: a script added later without a working target fails this suite.
"""

from __future__ import annotations

import inspect
import tomllib
from importlib import import_module
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
PREFIX = "loop-"


def scripts() -> dict[str, str]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["scripts"]


def test_the_table_is_not_empty() -> None:
    assert scripts()


@pytest.mark.parametrize("name", sorted(scripts()))
def test_every_script_resolves_to_a_callable_main(name: str) -> None:
    target = scripts()[name]
    module_path, _, attribute = target.partition(":")
    main = getattr(import_module(module_path), attribute)
    assert callable(main), f"{name} points at {target}, which is not callable"
    assert attribute == "main", f"{name} points at {attribute}, not main"


@pytest.mark.parametrize("name", sorted(scripts()))
def test_every_main_takes_no_required_argument(name: str) -> None:
    module_path, _, attribute = scripts()[name].partition(":")
    signature = inspect.signature(getattr(import_module(module_path), attribute))
    required = [
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    assert not required, f"{name} requires {required}, so the entry point cannot call it"


@pytest.mark.parametrize("name", sorted(scripts()))
def test_every_name_shares_one_prefix(name: str) -> None:
    assert name.startswith(PREFIX), f"{name} does not group with the others under {PREFIX}"
