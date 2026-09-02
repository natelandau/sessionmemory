"""Verify the shared secret-scrub redacts token- and key:value-shaped secrets."""

from __future__ import annotations

from sessionhooks.safety import REDACTED, scrub  # ty: ignore[unresolved-import]


def test_scrub_redacts_aws_key_and_reports_change() -> None:
    """Redact an AWS access key id and report the text as changed."""
    # Given text containing an AWS access key id
    text = "use AKIAIOSFODNN7EXAMPLE here"
    # When scrubbed
    out, changed = scrub(text)
    # Then the token is replaced and the change is reported
    assert "AKIA" not in out
    assert REDACTED in out
    assert changed is True


def test_scrub_preserves_label_redacts_value() -> None:
    """Redact a key:value secret's value while leaving the label readable."""
    # Given a key:value secret
    text = "api_key = 'abcdefghijklmnopqrstuvwxyz0123'"
    # When scrubbed
    out, changed = scrub(text)
    # Then the label remains and only the value is redacted
    assert out.startswith("api_key = '")
    assert REDACTED in out
    assert changed is True


def test_scrub_redacts_fine_grained_github_pat() -> None:
    """Redact a fine-grained GitHub PAT, whose github_pat_ prefix the gh[pousr]_ pattern alone would miss."""
    # Given text containing a fine-grained GitHub PAT (github_pat_ prefix)
    text = "token " + "github_pat_" + "11ABCDE" + "fghij" * 8
    # When scrubbed
    out, changed = scrub(text)
    # Then the PAT is redacted (the gh[pousr]_ form alone would miss this prefix)
    assert "github_pat_11" not in out
    assert REDACTED in out
    assert changed is True


def test_scrub_clean_text_unchanged() -> None:
    """Leave text with no secrets untouched and report no change."""
    # Given text with no secrets
    text = "just a normal learning about how the parser works"
    # When scrubbed
    out, changed = scrub(text)
    # Then nothing changes
    assert out == text
    assert changed is False
