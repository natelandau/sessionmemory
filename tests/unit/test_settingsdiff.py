"""Tests for the summary of how two settings.local.json copies differ."""

from __future__ import annotations

import pytest

from sessionmemory.lib import settingsdiff


def test_summarize_lists_objects_and_one_sided_keys():
    """Verify lists, scalar objects, and one-sided keys each get their own lines."""
    checkout = {"permissions": {"allow": ["A", "B", "C"]}, "enabledPlugins": {"x@y": True}}
    vault = {
        "permissions": {"allow": ["A", "D"]},
        "enabledPlugins": {"x@y": False, "z@y": True},
        "env": {"K": "1"},
    }

    assert settingsdiff.summarize(checkout, vault) == [
        "permissions.allow: 2 only in the checkout (B, C), 1 only in the vault (D)",
        "enabledPlugins: z@y only in the vault",
        "enabledPlugins.x@y: true in the checkout, false in the vault",
        "env: only in the vault",
    ]


def test_summarize_caps_listed_items():
    """Verify a long list shows five items and an ellipsis."""
    checkout = {"permissions": {"allow": [str(n) for n in range(7)]}}
    vault = {"permissions": {"allow": []}}

    assert settingsdiff.summarize(checkout, vault) == [
        "permissions.allow: 7 only in the checkout (0, 1, 2, 3, 4, ...)"
    ]


def test_summarize_shows_both_values_of_a_differing_scalar():
    """Verify a scalar that differs shows each side's value."""
    assert settingsdiff.summarize({"model": "a"}, {"model": "b"}) == [
        "model: a in the checkout, b in the vault"
    ]


def test_summarize_names_a_differing_mixed_value():
    """Verify a value whose type differs between the copies is named, not described."""
    assert settingsdiff.summarize({"model": "a"}, {"model": ["a"]}) == ["model: differs"]


def test_summarize_keeps_list_items_of_different_types_apart():
    """Verify the string "1" and the number 1 are different list items."""
    assert settingsdiff.summarize({"x": ["1"]}, {"x": [1]}) == [
        "x: 1 only in the checkout (1), 1 only in the vault (1)"
    ]


def test_summarize_equal_settings_is_empty():
    """Verify identical settings produce no lines."""
    data = {"permissions": {"allow": ["A"]}}

    assert settingsdiff.summarize(data, data) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [('{"a": 1}', {"a": 1}), ("null", None), ("{not json", settingsdiff.INVALID)],
)
def test_load(tmp_path, text, expected):
    """Verify load parses JSON, null included, and marks invalid JSON with INVALID."""
    path = tmp_path / "settings.local.json"
    path.write_text(text, encoding="utf-8")

    assert settingsdiff.load(path) == expected


def test_summarize_never_shows_env_values():
    """Verify a differing env value is named without printing either side's value."""
    checkout = {"env": {"API_KEY": "secret-one", "MODE": "a"}}
    vault = {"env": {"API_KEY": "secret-two", "MODE": "a"}}

    assert settingsdiff.summarize(checkout, vault) == ["env.API_KEY: differs"]
