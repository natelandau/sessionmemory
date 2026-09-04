"""Tests for the backlog item writer."""

from __future__ import annotations

import pytest

from sessionmemory.lib import backlog

TODAY = "2026-09-03"


def _add(path, **overrides) -> str:
    options = {
        "kind": "feat",
        "size": "S",
        "description": "cache the model",
        "topic": "index",
        "today": TODAY,
    }
    options.update(overrides)
    return backlog.add_item(path, **options)


def test_add_item_creates_the_file_with_the_heading_when_missing(tmp_path):
    """Verify a missing file is created with the `# Backlog` heading and the kind section."""
    path = tmp_path / "backlog.md"

    line = _add(path)

    assert line == "- [ ] [S] cache the model - 2026-09-03 [#index]"
    assert path.read_text(encoding="utf-8") == (
        "# Backlog\n\n## feat\n\n- [ ] [S] cache the model - 2026-09-03 [#index]\n"
    )


def test_add_item_appends_after_the_last_item_of_its_section(tmp_path):
    """Verify the line lands at the end of its section, and the following section is untouched."""
    path = tmp_path / "backlog.md"
    path.write_text(
        "# Backlog\n\n## feat\n\n- [ ] [M] first - 2026-09-01 [#a]\n\n## docs\n\n- [x] [S] done - 2026-09-01 [#b]\n",
        encoding="utf-8",
    )

    _add(path)

    assert path.read_text(encoding="utf-8") == (
        "# Backlog\n\n## feat\n\n- [ ] [M] first - 2026-09-01 [#a]\n"
        "- [ ] [S] cache the model - 2026-09-03 [#index]\n\n"
        "## docs\n\n- [x] [S] done - 2026-09-01 [#b]\n"
    )


def test_add_item_fills_an_empty_section(tmp_path):
    """Verify a heading with no items gets a blank line and then the item."""
    path = tmp_path / "backlog.md"
    path.write_text(
        "# Backlog\n\n## feat\n\n## docs\n\n- [ ] [S] d - 2026-09-01\n", encoding="utf-8"
    )

    _add(path)

    assert path.read_text(encoding="utf-8") == (
        "# Backlog\n\n## feat\n\n- [ ] [S] cache the model - 2026-09-03 [#index]\n\n"
        "## docs\n\n- [ ] [S] d - 2026-09-01\n"
    )


def test_add_item_appends_a_missing_section_at_the_end(tmp_path):
    """Verify an absent kind heading is added last, leaving the existing order alone."""
    path = tmp_path / "backlog.md"
    path.write_text("# Backlog\n\n## docs\n\n- [ ] [S] d - 2026-09-01 [#b]\n", encoding="utf-8")

    _add(path, kind="fix")

    assert path.read_text(encoding="utf-8") == (
        "# Backlog\n\n## docs\n\n- [ ] [S] d - 2026-09-01 [#b]\n\n"
        "## fix\n\n- [ ] [S] cache the model - 2026-09-03 [#index]\n"
    )


def test_add_item_matches_the_heading_exactly(tmp_path):
    """Verify `## fix` is not mistaken for `## fixtures`."""
    path = tmp_path / "backlog.md"
    path.write_text("# Backlog\n\n## fixtures\n\n- [ ] [S] x - 2026-09-01\n", encoding="utf-8")

    _add(path, kind="fix")

    assert path.read_text(encoding="utf-8").endswith(
        "## fixtures\n\n- [ ] [S] x - 2026-09-01\n\n"
        "## fix\n\n- [ ] [S] cache the model - 2026-09-03 [#index]\n"
    )


def test_add_item_normalizes_a_file_with_no_trailing_newline(tmp_path):
    """Verify an item appended to a file lacking a final newline still lands on its own line."""
    path = tmp_path / "backlog.md"
    path.write_text("# Backlog\n\n## feat\n\n- [ ] [S] a - 2026-09-01", encoding="utf-8")

    _add(path)

    assert path.read_text(encoding="utf-8") == (
        "# Backlog\n\n## feat\n\n- [ ] [S] a - 2026-09-01\n"
        "- [ ] [S] cache the model - 2026-09-03 [#index]\n"
    )


def test_add_item_leaves_a_file_without_the_backlog_heading_alone(tmp_path):
    """Verify the `# Backlog` heading is never inserted into a file someone else structured."""
    path = tmp_path / "backlog.md"
    path.write_text("Some notes.\n", encoding="utf-8")

    _add(path)

    assert path.read_text(encoding="utf-8") == (
        "Some notes.\n\n## feat\n\n- [ ] [S] cache the model - 2026-09-03 [#index]\n"
    )


def test_add_item_omits_the_topic_when_none_is_given(tmp_path):
    """Verify the trailing bracket is dropped rather than rendered empty."""
    line = _add(tmp_path / "backlog.md", topic=None)

    assert line == "- [ ] [S] cache the model - 2026-09-03"


def test_add_item_strips_a_leading_hash_from_the_topic(tmp_path):
    """Verify `#index` and `index` write the same tag."""
    line = _add(tmp_path / "backlog.md", topic=" #index ")

    assert line.endswith("[#index]")


def test_add_item_strips_the_description(tmp_path):
    """Verify surrounding whitespace never reaches the line."""
    line = _add(tmp_path / "backlog.md", description="  trim me  ")

    assert line == "- [ ] [S] trim me - 2026-09-03 [#index]"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"description": "   "}, id="empty-description"),
        pytest.param({"description": "two\nlines"}, id="multiline-description"),
        pytest.param({"topic": "#"}, id="empty-topic"),
        pytest.param({"topic": "two words"}, id="topic-with-whitespace"),
        pytest.param({"kind": "chore"}, id="unknown-kind"),
        pytest.param({"size": "XL"}, id="unknown-size"),
    ],
)
def test_add_item_rejects_a_malformed_item_and_writes_nothing(tmp_path, overrides):
    """Verify a bad field raises before anything touches the file."""
    path = tmp_path / "backlog.md"

    with pytest.raises(backlog.BacklogError):
        _add(path, **overrides)

    assert not path.exists()
