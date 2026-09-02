"""The plugin manifests and the CLI shim, checked as shipped."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_marketplace_declares_one_plugin_at_the_repo_root():
    """The marketplace lists exactly the one plugin, sourced from the repo root."""
    data = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())

    assert [p["source"] for p in data["plugins"]] == ["./"]
    assert [p["name"] for p in data["plugins"]] == ["sessionmemory"]


def test_plugin_manifest_names_the_plugin():
    """The plugin manifest carries the name the marketplace entry refers to."""
    data = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())

    assert data["name"] == "sessionmemory"
    assert data["version"]


def test_the_shim_is_executable_and_reaches_the_cli():
    """The shim runs from any working directory, since hooks run in the caller's repo."""
    shim = REPO / "bin" / "sessionmemory"

    assert os.access(shim, os.X_OK)

    proc = subprocess.run(
        [str(shim), "--help"], capture_output=True, text=True, cwd="/", check=False
    )

    assert proc.returncode == 0
    assert "Usage:" in proc.stdout


def test_the_shim_resolves_its_project_root_through_a_symlink(tmp_path):
    """The shim finds its project even when reached through a symlink on PATH."""
    shim = REPO / "bin" / "sessionmemory"
    link = tmp_path / "vault-link"
    link.symlink_to(shim)

    # `uv run pytest` puts VIRTUAL_ENV and this repo's own .venv/bin on PATH for this
    # very process. Either one lets `uv run` reach the CLI directly regardless of what
    # ROOT the shim resolved, masking the bug, so both are stripped here.
    venv_bin = str(REPO / ".venv" / "bin")
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p != venv_bin)
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["PATH"] = path

    proc = subprocess.run(
        [str(link), "--help"], capture_output=True, text=True, cwd="/", env=env, check=False
    )

    assert proc.returncode == 0
    assert "Usage:" in proc.stdout
