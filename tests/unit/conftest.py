"""Fixtures shared by the git-backed unit tests."""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING

import pytest

from tests.unit.gitfixtures import Fixture, commit, git

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo(tmp_path: Path) -> Fixture:
    """Build a bare origin plus a clone holding trunk, `feat`, and a deleted `gone`."""
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "-b", "main")
    main_head = commit(clone, "base")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "remote", "set-head", "origin", "-a")

    git(clone, "checkout", "-q", "-b", "feat")
    feat = commit(clone, "feat")
    git(clone, "push", "-q", "origin", "feat")

    git(clone, "checkout", "-q", "main")
    git(clone, "checkout", "-q", "-b", "gone")
    gone = commit(clone, "gone")
    git(clone, "checkout", "-q", "main")
    git(clone, "branch", "-q", "-D", "gone")
    git(clone, "reflog", "expire", "--expire=now", "--all")
    return Fixture(clone=clone, main_head=main_head, feat=feat, gone=gone)


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Put a fake `gh` first on PATH and return a function that sets its responses.

    The script answers `gh api repos/<slug>/commits/<sha>/pulls` from the JSON file named
    by `GH_FAKE_RESPONSES`, which maps a sha to `{stdout, stderr, code}`.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(
        f"""#!{sys.executable}
import json, os, sys, time
sha = sys.argv[-1].split("/")[-2]
with open(os.environ["GH_FAKE_RESPONSES"], encoding="utf-8") as handle:
    entry = json.load(handle).get(sha)
if entry is None:
    sys.stderr.write("gh: server error (HTTP 500)\\n")
    sys.exit(1)
time.sleep(entry.get("sleep", 0))
sys.stdout.write(entry.get("stdout", ""))
sys.stderr.write(entry.get("stderr", ""))
sys.exit(entry.get("code", 0))
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    responses = tmp_path / "responses.json"
    responses.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_FAKE_RESPONSES", str(responses))

    def respond(
        sha: str, *, stdout: str = "", stderr: str = "", code: int = 0, sleep: float = 0
    ) -> None:
        data = json.loads(responses.read_text(encoding="utf-8"))
        data[sha] = {"stdout": stdout, "stderr": stderr, "code": code, "sleep": sleep}
        responses.write_text(json.dumps(data), encoding="utf-8")

    return respond
