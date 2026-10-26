"""Summarize how two copies of a Claude Code `settings.local.json` differ.

The person chooses which whole copy to keep, so the summary only has to show enough to
choose: the list items and object keys each side has that the other lacks. Lists such as
`permissions.allow` compare as sets, since Claude Code appends to them and their order
carries no meaning.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_SHOWN_ITEMS = 5
# Marks a file that is not valid JSON, since `null` is valid JSON and parses to None.
INVALID = object()


def load(path: Path) -> object:
    """Parse a settings file, marking anything that is not valid JSON with `INVALID`.

    Args:
        path (Path): The file to read.

    Returns:
        object: The parsed JSON, or `INVALID` when the file is not valid JSON.

    Raises:
        OSError: When the file cannot be read.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return INVALID


def same(checkout: object, vault: object) -> bool:
    """Compare parsed settings as JSON, so `true` and `1` differ and key order does not.

    Args:
        checkout (object): The checkout's parsed settings.
        vault (object): The vault's parsed settings.

    Returns:
        bool: True when both serialize to the same JSON.
    """
    return json.dumps(checkout, sort_keys=True) == json.dumps(vault, sort_keys=True)


def _scalar(value: object) -> bool:
    return not isinstance(value, dict | list)


def _render(item: object) -> str:
    return item if isinstance(item, str) else json.dumps(item, sort_keys=True)


def _items(values: list[str]) -> str:
    shown = ", ".join(values[:_SHOWN_ITEMS])
    return f"{shown}, ..." if len(values) > _SHOWN_ITEMS else shown


def _list_line(key: str, checkout: list[object], vault: list[object]) -> str | None:
    # Keyed by strict JSON so the string "1" and the number 1 stay distinct items.
    ours = {json.dumps(item, sort_keys=True): item for item in checkout}
    theirs = {json.dumps(item, sort_keys=True): item for item in vault}
    parts = [
        f"{len(only)} only in the {side} ({_items(sorted(_render(mine[k]) for k in only))})"
        for side, mine, only in (
            ("checkout", ours, ours.keys() - theirs.keys()),
            ("vault", theirs, theirs.keys() - ours.keys()),
        )
        if only
    ]
    return f"{key}: {', '.join(parts)}" if parts else None


def _object_line(key: str, checkout: dict, vault: dict) -> str | None:
    parts = []
    for side, mine, other in (("checkout", checkout, vault), ("vault", vault, checkout)):
        if only := [name for name in mine if name not in other]:
            parts.append(f"{', '.join(only)} only in the {side}")
    if differs := [
        name for name in checkout if name in vault and not same(checkout[name], vault[name])
    ]:
        parts.append(f"{', '.join(differs)} differs")
    return f"{key}: {'; '.join(parts)}" if parts else None


def _walk(prefix: str, checkout: dict, vault: dict) -> list[str]:
    lines: list[str] = []
    for name in [*checkout, *(name for name in vault if name not in checkout)]:
        key = f"{prefix}{name}"
        if name not in vault or name not in checkout:
            side = "checkout" if name in checkout else "vault"
            lines.append(f"{key}: only in the {side}")
            continue
        ours, theirs = checkout[name], vault[name]
        if same(ours, theirs):
            continue
        if isinstance(ours, list) and isinstance(theirs, list):
            line = _list_line(key, ours, theirs)
        elif isinstance(ours, dict) and isinstance(theirs, dict):
            if all(_scalar(value) for value in [*ours.values(), *theirs.values()]):
                line = _object_line(key, ours, theirs)
            else:
                lines.extend(_walk(f"{key}.", ours, theirs))
                continue
        else:
            line = f"{key}: differs"
        if line:
            lines.append(line)
    return lines


def summarize(checkout: object, vault: object) -> list[str]:
    """Describe what each parsed settings copy has that the other lacks.

    Args:
        checkout (object): The checkout's parsed settings.
        vault (object): The vault's parsed settings.

    Returns:
        list[str]: One line per differing key, empty when the copies are equal.
    """
    if same(checkout, vault):
        return []
    if isinstance(checkout, dict) and isinstance(vault, dict):
        return _walk("", checkout, vault)
    return ["the settings differ"]
