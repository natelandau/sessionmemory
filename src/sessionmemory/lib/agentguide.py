"""Text that teaches an AI agent to manage the vault on a person's behalf.

The guide is a markdown file shipped inside the package rather than a plugin skill, so an
agent with no plugin installed, or a different agent entirely, reads the same text.
"""

from __future__ import annotations

from importlib import resources

PROMPT = """\
Help me manage my sessionmemory vault.

1. Run `sessionmemory --skill` and read all of it. It explains how the vault works and how to manage it.
2. Run `sessionmemory doctor --json`.
3. Give me a brief overview of the vault's state: its projects, and what doctor found in each.
4. Ask me what I want help with, and offer a few suggestions based on what you found.

Ask me before you delete, move, or overwrite anything."""


def skill() -> str:
    """Return the guide an agent reads before it manages the vault.

    Returns:
        str: The guide as markdown.
    """
    guide = resources.files(__package__).joinpath("agent_skill.md").read_text(encoding="utf-8")
    return guide.rstrip("\n")
