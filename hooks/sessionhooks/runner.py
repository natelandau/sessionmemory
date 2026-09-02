"""The `claude -p` client for the headless sweep, plus its injectable seam.

`ClaudeRunner.run` builds the isolated environment (the recursion guard), the
restricted CLI args, runs the subprocess, parses its stream-json into a
structured `RunResult`, and never raises. `Sweep` depends on the `Runner`
protocol, so its tests pass a duck-typed fake and never spawn a real agent.
The deterministic helpers (`build_env`, `build_args`, `parse_stream_json`) are
module-level so they stay unit-testable without a subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

HEADLESS_ENV = "SESSIONMEMORY_HEADLESS"
# Tools that alter a file. Only these produce a path the containment backstop
# may revert.
MUTATING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT = 180


@dataclass(frozen=True, slots=True)
class RunResult:
    """The structured outcome of one headless sweep run."""

    success: bool
    exit_code: int
    changed_files: list[str]
    text: str
    stderr: str


class Runner(Protocol):
    """The seam `Sweep` depends on: anything that runs a prompt and returns a result."""

    def run(self, prompt: str, *, cwd: str) -> RunResult:
        """Run `prompt` in `cwd` and return a structured result."""
        ...  # pragma: no cover - a Protocol stub body, never executed


def build_env(*, base: Mapping[str, str], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build an isolated environment for the `claude -p` subprocess.

    Sets the headless guard so the spawned agent's own session hooks no-op, and
    drops CLAUDECODE / CLAUDE_CODE_ENTRYPOINT so the child is not treated as
    nested in the parent Claude Code process.

    Args:
        base: The environment to derive from, normally `os.environ`.
        extra: Variables to pin on top, for tooling the agent must be able to
            run whose configuration this process resolved rather than inherited.
    """
    env = dict(base)
    env[HEADLESS_ENV] = "1"
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    if extra:
        env.update(extra)
    return env


def build_args(*, model: str, save_transcript: bool = True) -> list[str]:
    """Construct the `claude -p` CLI args for the sweep.

    `--allowedTools` pre-approves rather than restricts, so the agent still
    reaches Bash and must: every note it writes is created by the vault CLI, not
    by a file tool. Narrowing the tool set with `--tools` would leave the sweep
    able to edit notes it can no longer create.

    Args:
        model: The model to run the sweep under.
        save_transcript: When False, add --no-session-persistence so the sweep's
            own session is not written to ~/.claude/projects. Defaults to True so
            the sweep's API-token usage stays auditable in the transcript.
    """
    args = [
        "claude",
        "-p",
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        "Read,Write,Edit",
        "--dangerously-skip-permissions",
    ]
    if not save_transcript:
        args.append("--no-session-persistence")
    return args


def _extract_tool_entries(content: list[object]) -> list[dict[str, str]]:
    """Extract tool_use blocks from a stream-json message content array."""
    entries: list[dict[str, str]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_entry: dict[str, str] = {"tool": str(block.get("name", "unknown"))}
        tool_input = block.get("input")
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path") or tool_input.get("path") or ""
            if file_path:
                tool_entry["file"] = str(file_path)
        entries.append(tool_entry)
    return entries


def changed_files(tools: list[dict[str, str]]) -> list[str]:
    """Return the paths the agent actually modified, ignoring the ones it only read.

    Read carries a `file_path` exactly as Write does, so a tool list filtered on
    the path alone reports reads as writes. The containment backstop reverts
    what this returns, and reverting a file the agent merely opened would delete
    it from the user's repository.
    """
    return [t["file"] for t in tools if "file" in t and t.get("tool") in MUTATING_TOOLS]


def parse_stream_json(stdout: str) -> tuple[list[dict[str, str]], str]:
    """Parse stream-json output into (tools_used, result_text).

    tools_used collects one dict per assistant `tool_use` block ({"tool": name}
    plus "file" when the input carries a file_path/path). result_text is the
    final `result` entry's text. Malformed lines are skipped.
    """
    tools_used: list[dict[str, str]] = []
    result_text = ""
    for line in stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            tools_used.extend(_extract_tool_entries(content))
        if entry.get("type") == "result":
            r = entry.get("result", "")
            if isinstance(r, str) and r:
                result_text = r
    return tools_used, result_text


class ClaudeRunner:
    """Runs `claude -p` for the sweep and returns a structured, never-raising result."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
        save_transcript: bool = True,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.save_transcript = save_transcript
        self.extra_env = dict(extra_env) if extra_env else {}

    def _build_args(self) -> list[str]:
        """Build the CLI args for this runner's configured model and persistence."""
        return build_args(model=self.model, save_transcript=self.save_transcript)

    def run(self, prompt: str, *, cwd: str) -> RunResult:
        """Execute `claude -p` and return a RunResult, never raising.

        Failures map to negative exit codes: timeout -2, claude-not-found -3,
        OSError -5. On any failure `changed_files` is empty and `success` is False.
        """
        args = self._build_args()
        env = build_env(base=os.environ, extra=self.extra_env)
        try:
            proc = subprocess.run(  # noqa: S603 - args are build_args, not user input
                args,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=cwd,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                success=False,
                exit_code=-2,
                changed_files=[],
                text="",
                stderr=f"timed out after {self.timeout}s",
            )
        except FileNotFoundError:
            return RunResult(
                success=False,
                exit_code=-3,
                changed_files=[],
                text="",
                stderr="claude CLI not found",
            )
        except OSError as exc:
            return RunResult(
                success=False, exit_code=-5, changed_files=[], text="", stderr=f"OSError: {exc}"
            )
        tools, text = parse_stream_json(proc.stdout or "")
        return RunResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            changed_files=changed_files(tools),
            text=text,
            stderr=proc.stderr or "",
        )
