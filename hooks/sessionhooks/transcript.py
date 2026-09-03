"""Transcript reading helpers for the sweep.

Provides a windowed, noise-filtered view of the session transcript so the
sweep only processes content recorded since the last compaction boundary and
skips system/hook injections that are not meaningful user/assistant turns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NOISE_MARKERS: frozenset[str] = frozenset(
    {
        "<system-reminder>",
        "<recall-memory>",
        "<local-command-caveat>",
        "<local-command-stdout>",
        "<command-name>",
        "<command-message>",
        "<command-args>",
        "<task-notification>",
        "Stop hook feedback:",
        "SessionStart hook additional context:",
        "Base directory for this skill:",
    }
)


def read_entries(transcript_path: str) -> list[dict[str, Any]]:
    """Parse a JSONL transcript into its object entries, skipping junk lines.

    Blank lines, non-JSON lines, and non-object JSON values are dropped so a
    truncated or partially-flushed transcript never raises. Returns [] when
    the file cannot be read, so callers fail open.
    """
    try:
        raw = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


# A bridged session's `bridgeSessionId` is `cse_<id>`, and the same id opens the
# session at claude.ai under a `session_` prefix.
_BRIDGE_ID_PREFIX = "cse_"
_SESSION_URL = "https://claude.ai/code/session_{}"


def session_url(entries: list[dict[str, Any]]) -> str:
    """Return where this session can be opened online, or '' when it was never bridged."""
    for entry in entries:
        if entry.get("type") != "bridge-session":
            continue
        bridge_id = entry.get("bridgeSessionId")
        if isinstance(bridge_id, str) and bridge_id.startswith(_BRIDGE_ID_PREFIX):
            suffix = bridge_id.removeprefix(_BRIDGE_ID_PREFIX)
            if suffix:
                return _SESSION_URL.format(suffix)
    return ""


def _is_compact_boundary(entry: dict[str, Any]) -> bool:
    """Return whether an entry marks a compaction checkpoint."""
    return bool(
        entry.get("type") == "compact_boundary"
        or entry.get("isCompactSummary")
        or entry.get("compact_boundary")
    )


def window_since_compact(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return entries recorded after the most recent compaction boundary.

    Avoids re-processing content an earlier sweep already saw before a compact.
    With no boundary present, returns all entries.
    """
    start = 0
    for idx, entry in enumerate(entries):
        if _is_compact_boundary(entry):
            start = idx + 1
    return entries[start:]


def _assistant_content_blocks(entry: dict[str, Any]) -> list[Any]:
    """Return the `message.content` block list of an assistant entry, else [].

    Guards every assistant-turn reader: a non-assistant entry, a non-dict
    `message`, or a non-list `content` all yield [] so callers iterate without
    re-checking the entry shape.
    """
    if entry.get("type") != "assistant":
        return []
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _entry_text(entry: dict[str, Any]) -> str:
    """Concatenate the text of every `text` block in one transcript entry.

    For assistant entries, walks the content block list and joins `text`-type
    blocks. For user entries whose content is a plain string, returns that
    string. Tool-result user lines (content is a list) and pure
    thinking/tool_use assistant lines (no text block) contribute nothing and
    return "".
    """
    entry_type = entry.get("type")
    if entry_type == "assistant":
        return "".join(
            block.get("text", "")
            for block in _assistant_content_blocks(entry)
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if entry_type == "user":
        message = entry.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def _has_noise(text: str) -> bool:
    """Return True if the text contains any NOISE_MARKERS substring."""
    return any(marker in text for marker in NOISE_MARKERS)


def meaningful_messages(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return user/assistant entries with non-empty text and no noise markers.

    Drops tool_result user lines (content is a list), pure-thinking/tool_use
    assistant lines (no text block), and any entry whose text contains a
    NOISE_MARKERS substring, these are system injections, hook feedback, or
    skill scaffolding, not real conversation.
    """
    result: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") not in {"user", "assistant"}:
            continue
        text = _entry_text(entry)
        if not text.strip():
            continue
        if _has_noise(text):
            continue
        result.append(entry)
    return result


@dataclass(frozen=True, slots=True)
class Substance:
    """How much the human actually said in one window."""

    messages: int
    characters: int


def user_substance(entries: list[dict[str, Any]]) -> Substance:
    """Measure the human's half of a window, ignoring the agent's entirely.

    The gate needs to tell a session that went somewhere from one where the user
    typed "update my plugins" and left. Agent output is no evidence either way:
    a one-line instruction can draw thousands of words in reply, so counting
    both halves lets verbosity alone clear the bar.
    """
    texts = [
        _entry_text(entry).strip()
        for entry in meaningful_messages(entries)
        if entry.get("type") == "user"
    ]
    return Substance(messages=len(texts), characters=sum(len(t) for t in texts))


def meaningful_text(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return `{"role", "text"}` for each meaningful user/assistant message.

    The prompt-ready view of the transcript: the human's messages and the
    agent's user-facing text only. `meaningful_messages` already strips noise,
    tool_result lines, and thinking/tool-only turns; reducing each surviving
    entry to its `_entry_text` then drops the thinking and tool_use blocks that
    still ride along inside a mixed assistant turn, so nothing but real
    conversation reaches the sweep.
    """
    return [
        {"role": str(entry.get("type", "")), "text": _entry_text(entry)}
        for entry in meaningful_messages(entries)
    ]
