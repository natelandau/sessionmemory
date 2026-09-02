"""Verify the headless claude runner seam.

`run()` never spawns a real `claude` process here: every case that exercises it
replaces `subprocess.run` with a controlled fake, so the try/except mapping and
the RunResult it builds are tested without an external dependency.
"""

from __future__ import annotations

import json
import subprocess

from sessionhooks import runner as runner_mod  # ty: ignore[unresolved-import]
from sessionhooks.runner import (  # ty: ignore[unresolved-import]
    ClaudeRunner,
    RunResult,
    build_args,
    build_env,
    changed_files,
    parse_stream_json,
)

# ---------------------------------------------------------------------------
# build_env (recursion guard + parent-process isolation)
# ---------------------------------------------------------------------------


def test_build_env_sets_headless_guard() -> None:
    """Verify build_env sets SESSIONMEMORY_HEADLESS=1 in the returned dict."""
    # Given an empty base / When building the env
    result = build_env(base={})
    # Then the headless guard is set
    assert result["SESSIONMEMORY_HEADLESS"] == "1"


def test_build_env_strips_parent_process_vars() -> None:
    """Verify build_env drops CLAUDECODE and CLAUDE_CODE_ENTRYPOINT but keeps the rest."""
    # Given a base carrying the parent Claude Code markers
    base = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "HOME": "/home/user"}
    # When building the env
    result = build_env(base=base)
    # Then the markers are gone and unrelated keys survive
    assert "CLAUDECODE" not in result
    assert "CLAUDE_CODE_ENTRYPOINT" not in result
    assert result["HOME"] == "/home/user"


def test_build_env_pins_extra_variables_over_the_base() -> None:
    """Verify a resolved setting overrides whatever the parent environment carried."""
    # Given a base whose vault root points somewhere stale
    base = {"PROJECT_MEMORY_ROOT": "/stale", "HOME": "/home/user"}
    # When building the env with a resolved root pinned on top
    result = build_env(base=base, extra={"PROJECT_MEMORY_ROOT": "/resolved"})
    # Then the resolved value wins
    assert result["PROJECT_MEMORY_ROOT"] == "/resolved"
    assert result["HOME"] == "/home/user"


def test_runner_carries_its_extra_env_into_the_subprocess_environment() -> None:
    """Verify the vault root the sweep resolved reaches the agent's own CLI calls."""
    # Given a runner constructed with a pinned vault root
    runner = ClaudeRunner(extra_env={"PROJECT_MEMORY_ROOT": "/v"})
    # When building the env it would hand the subprocess
    result = build_env(base={}, extra=runner.extra_env)
    # Then the root is present
    assert result["PROJECT_MEMORY_ROOT"] == "/v"


def test_build_env_does_not_mutate_base() -> None:
    """Verify build_env never modifies the caller's base mapping."""
    # Given a base dict with both vars present
    base = {"CLAUDECODE": "1", "X": "y"}
    original = dict(base)
    # When building the env
    build_env(base=base)
    # Then the base mapping is unchanged
    assert base == original


# ---------------------------------------------------------------------------
# build_args
# ---------------------------------------------------------------------------


def test_build_args_restricts_tools_and_skips_permissions() -> None:
    """Verify build_args restricts tools, skips permissions, and streams json."""
    # Given a model / When building args
    args = build_args(model="claude-sonnet-4-6")
    # Then the safety-relevant flags are present
    assert args[args.index("--allowedTools") + 1] == "Read,Write,Edit"
    assert "--dangerously-skip-permissions" in args
    assert args[args.index("--output-format") + 1] == "stream-json"


def test_build_args_saves_transcript_by_default() -> None:
    """Verify build_args omits --no-session-persistence so the sweep session is saved."""
    # Given the default save_transcript / When building args
    args = build_args(model="claude-sonnet-4-6")
    # Then persistence is left on (the flag that disables it is absent)
    assert "--no-session-persistence" not in args


def test_build_args_disables_persistence_when_not_saving() -> None:
    """Verify save_transcript=False adds --no-session-persistence to discard the session."""
    # Given save_transcript disabled / When building args
    args = build_args(model="claude-sonnet-4-6", save_transcript=False)
    # Then the persistence-disabling flag is present
    assert "--no-session-persistence" in args


def test_runner_threads_save_transcript_into_args() -> None:
    """Verify ClaudeRunner passes its save_transcript setting through to the built args."""
    # Given a runner that should not save its transcript
    args = ClaudeRunner(model="m", save_transcript=False)._build_args()
    # Then the persistence-disabling flag is present
    assert "--no-session-persistence" in args


def test_build_args_model_reflects_argument() -> None:
    """Verify the --model flag reflects the model argument."""
    # Given a custom model / When building args
    args = build_args(model="claude-opus-4-5")
    # Then the model flag matches
    assert args[args.index("--model") + 1] == "claude-opus-4-5"


def test_runner_uses_its_model() -> None:
    """Verify ClaudeRunner threads its configured model into the built args."""
    # Given a runner configured with a model
    args = ClaudeRunner(model="claude-test-model")._build_args()
    # Then its args carry that model
    assert args[args.index("--model") + 1] == "claude-test-model"


# ---------------------------------------------------------------------------
# parse_stream_json -> RunResult fields
# ---------------------------------------------------------------------------


def _stream(file_path: str) -> str:
    """Build sample stream-json reporting a Write to file_path plus a result line."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Write", "input": {"file_path": file_path}}
                    ]
                },
            }
        ),
        "",
        "not-json{{{",
        json.dumps({"type": "result", "result": "done"}),
    ]
    return "\n".join(lines)


def test_parse_stream_json_extracts_tool_and_result() -> None:
    """Verify parse_stream_json captures the written file and the final result text."""
    # Given sample stream-json output with one Write and a result
    tools, text = parse_stream_json(_stream("/home/u/out.md"))
    # Then the Write file and result text are recovered, junk lines skipped
    assert tools == [{"tool": "Write", "file": "/home/u/out.md"}]
    assert text == "done"


def test_parse_stream_json_path_fallback() -> None:
    """Verify parse_stream_json uses the 'path' input key when 'file_path' is absent."""
    # Given a tool_use block using 'path'
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"path": "/etc/hosts"}}]
            },
        }
    )
    # When parsing
    tools, _ = parse_stream_json(line)
    # Then the file key is set from 'path'
    assert tools[0]["file"] == "/etc/hosts"


def test_parse_stream_json_skips_junk() -> None:
    """Verify parse_stream_json returns empties on all-junk input without raising."""
    # Given only blank and malformed lines
    tools, text = parse_stream_json("\n\nnot-json{{{\n")
    # Then both outputs are empty
    assert tools == []
    assert text == ""


# ---------------------------------------------------------------------------
# RunResult is a structured value the sweep consumes
# ---------------------------------------------------------------------------


def test_run_result_holds_changed_files() -> None:
    """Verify RunResult exposes the structured fields Sweep reads."""
    # Given a constructed result
    result = RunResult(success=True, exit_code=0, changed_files=["a.md"], text="ok", stderr="")
    # Then its fields are accessible as a typed object
    assert result.changed_files == ["a.md"]
    assert result.success is True


def test_changed_files_excludes_a_file_the_agent_only_read() -> None:
    """Verify a read is never reported as a write, so the backstop cannot revert it."""
    # Given a run that read a repository file and wrote one note
    tools = [
        {"tool": "Read", "file": "/repo/src/main.py"},
        {"tool": "Write", "file": "/vault/projects/p/note.md"},
    ]
    # When collecting the files the agent changed
    result = changed_files(tools)
    # Then only the written note is reported
    assert result == ["/vault/projects/p/note.md"]


def test_changed_files_counts_every_mutating_tool() -> None:
    """Verify edits are reported alongside writes, so the backstop still sees them."""
    # Given a run that edited one note and wrote another
    tools = [
        {"tool": "Edit", "file": "/vault/a.md"},
        {"tool": "Write", "file": "/vault/b.md"},
    ]
    # When collecting the files the agent changed
    result = changed_files(tools)
    # Then both are reported
    assert result == ["/vault/a.md", "/vault/b.md"]


def test_changed_files_ignores_a_tool_entry_with_no_file() -> None:
    """Verify a tool call carrying no path contributes nothing rather than raising."""
    # Given a Bash call, which carries no file path
    tools = [{"tool": "Bash"}, {"tool": "Write", "file": "/vault/a.md"}]
    # When collecting the files the agent changed
    result = changed_files(tools)
    # Then only the write is reported
    assert result == ["/vault/a.md"]


# ---------------------------------------------------------------------------
# parse_stream_json: content-block and result-line edge cases
# ---------------------------------------------------------------------------


def test_parse_stream_json_skips_a_non_tool_use_content_block() -> None:
    """Verify a text content block alongside a tool_use is ignored, not misparsed."""
    # Given a content list mixing a text block with a tool_use block
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "thinking out loud"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.md"}},
                ]
            },
        }
    )
    # When parsing
    tools, _ = parse_stream_json(line)
    # Then only the tool_use block is recorded
    assert tools == [{"tool": "Read", "file": "/a.md"}]


def test_parse_stream_json_tool_use_without_an_input_dict() -> None:
    """Verify a tool_use with no input dict is recorded without a file key."""
    # Given a tool_use block with no "input" key
    line = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}
    )
    # When parsing
    tools, _ = parse_stream_json(line)
    # Then the tool is recorded with no file key
    assert tools == [{"tool": "Bash"}]


def test_parse_stream_json_tool_use_input_with_no_file_path() -> None:
    """Verify a tool_use whose input names neither file_path nor path omits the file key."""
    # Given a tool_use whose input carries neither "file_path" nor "path"
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
            },
        }
    )
    # When parsing
    tools, _ = parse_stream_json(line)
    # Then the tool is recorded with no file key
    assert tools == [{"tool": "Bash"}]


def test_parse_stream_json_skips_a_json_line_that_is_not_an_object() -> None:
    """Verify a line that parses to something other than a JSON object is skipped."""
    # Given a line that parses to a JSON array, followed by a valid result line
    lines = "\n".join(["[1, 2, 3]", json.dumps({"type": "result", "result": "ok"})])
    # When parsing
    tools, text = parse_stream_json(lines)
    # Then the non-object line is skipped and the result line still lands
    assert tools == []
    assert text == "ok"


def test_parse_stream_json_ignores_an_empty_result_string() -> None:
    """Verify an empty result value is not adopted as the final text."""
    # Given a result entry whose "result" value is an empty string
    lines = "\n".join(
        [
            json.dumps({"type": "result", "result": ""}),
            json.dumps({"type": "assistant", "message": {"content": []}}),
        ]
    )
    # When parsing / Then the empty result is not adopted as the final text
    _, text = parse_stream_json(lines)
    assert text == ""


# ---------------------------------------------------------------------------
# ClaudeRunner.run: the try/except mapping, with subprocess.run replaced
# ---------------------------------------------------------------------------


def test_run_maps_a_timeout_to_a_negative_exit_code(monkeypatch) -> None:
    """Verify a subprocess timeout is reported as a structured failure, not raised."""

    # Given subprocess.run replaced with one that always times out
    def _raise(*_args, **_kwargs) -> None:
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=180)

    monkeypatch.setattr(runner_mod.subprocess, "run", _raise)

    # When running the prompt
    result = ClaudeRunner(timeout=180).run("prompt", cwd="/repo")

    # Then the timeout is reported as a structured failure, not raised
    assert result.success is False
    assert result.exit_code == -2
    assert result.changed_files == []
    assert "timed out" in result.stderr


def test_run_maps_a_missing_claude_binary_to_a_negative_exit_code(monkeypatch) -> None:
    """Verify a missing `claude` executable is reported as a structured failure, not raised."""
    # Given subprocess.run replaced with one that raises FileNotFoundError
    monkeypatch.setattr(
        runner_mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError)
    )

    # When running the prompt
    result = ClaudeRunner().run("prompt", cwd="/repo")

    # Then the missing binary is reported as a structured failure, not raised
    assert result.success is False
    assert result.exit_code == -3
    assert "not found" in result.stderr


def test_run_maps_an_os_error_to_a_negative_exit_code(monkeypatch) -> None:
    """Verify any other OSError is reported as a structured failure, not raised."""
    # Given subprocess.run replaced with one that raises a generic OSError
    monkeypatch.setattr(
        runner_mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no proc"))
    )

    # When running the prompt
    result = ClaudeRunner().run("prompt", cwd="/repo")

    # Then the error is reported as a structured failure, not raised
    assert result.success is False
    assert result.exit_code == -5
    assert "no proc" in result.stderr


def test_run_builds_a_result_from_a_successful_subprocess(monkeypatch) -> None:
    """Verify a clean run parses stream-json into tools_used and the final text."""
    # Given subprocess.run replaced with one returning stream-json for a clean run
    stdout = _stream("/vault/note.md")

    def _fake_run(*_args, **_kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    # When running the prompt
    result = ClaudeRunner().run("prompt", cwd="/repo")

    # Then the stream-json output is parsed into a successful RunResult
    assert result.success is True
    assert result.exit_code == 0
    assert result.changed_files == ["/vault/note.md"]
    assert result.text == "done"
    assert result.stderr == ""
