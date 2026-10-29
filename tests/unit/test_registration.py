"""Tests for recording a directory as a project."""

from __future__ import annotations

from pathlib import Path

import pytest

from sessionmemory.lib import registry
from sessionmemory.lib.registration import (
    Candidate,
    RegistrationError,
    candidate,
    check_slug,
    derive_slug,
    record,
)


@pytest.mark.parametrize(
    ("remotes", "root", "expected"),
    [
        (("github.com/nate/My.Repo",), "/work/elsewhere", "my-repo"),
        ((), "/work/Plain Dir", "plain-dir"),
        (("github.com/nate/...",), "/work/fallback", "fallback"),
    ],
)
def test_derive_slug_prefers_the_remote_then_the_directory(remotes, root, expected):
    """Verify a slug comes from the first remote's name, then the directory name."""
    assert derive_slug(Candidate(root=Path(root), remotes=remotes)) == expected


def test_derive_slug_refuses_when_nothing_slugifies():
    """Verify a directory with no usable name asks for an explicit slug."""
    with pytest.raises(RegistrationError, match="cannot derive a slug"):
        derive_slug(Candidate(root=Path("/"), remotes=()))


@pytest.mark.parametrize(
    ("slug", "details"),
    [(" ", []), ("My-Repo", ["try: --slug my-repo"]), ("../x", ["try: --slug x"])],
)
def test_check_slug_refuses_anything_but_its_own_slug(slug, details):
    """Verify an explicit slug is refused rather than corrected, with the corrected form offered."""
    with pytest.raises(RegistrationError) as caught:
        check_slug(slug)
    assert caught.value.details == details


def test_candidate_refuses_a_missing_directory(tmp_path):
    """Verify a path that is not a directory is never registered."""
    with pytest.raises(RegistrationError, match="is not a directory"):
        candidate(tmp_path / "absent")


def test_record_files_the_project_and_creates_its_fields(vault, tmp_path):
    """Verify recording saves the entry and creates the field folders."""
    root = tmp_path / "plain"
    root.mkdir()

    registered = record(vault, candidate(root), None)

    assert registered.slug == "plain"
    assert registered.enclosing is None
    assert registry.load(vault)["plain"].root == str(root)
    assert (vault / "projects" / "plain" / "learnings").is_dir()


def test_record_refuses_a_directory_already_registered(vault, tmp_path):
    """Verify a second registration of the same root names the slug it already has."""
    root = tmp_path / "plain"
    root.mkdir()
    record(vault, candidate(root), None)

    with pytest.raises(RegistrationError, match="already registered as 'plain'"):
        record(vault, candidate(root), "other")


def test_record_reports_the_project_it_sits_inside(vault, tmp_path):
    """Verify a directory nested in a registered one names its enclosing project."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    record(vault, candidate(outer), None)

    registered = record(vault, candidate(inner), None)

    assert registered.enclosing is not None
    assert registered.enclosing.slug == "outer"
