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


def test_scrub_redacts_a_whole_pem_block() -> None:
    """Redact an entire PEM private key block, header and body, so no key material survives."""
    # Given a complete PEM private key block
    body = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    # Split so a secret scanner does not flag this file as holding a real key.
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"
    footer = "-----END RSA " + "PRIVATE KEY-----"
    text = header + "\n" + "".join(f"{body}\n" for _ in range(4)) + footer
    # When scrubbed
    out, changed = scrub(text)
    # Then the header and every body line are gone and one redaction marks the block
    assert header not in out
    assert body not in out
    assert out.count(REDACTED) == 1
    assert changed is True


def test_scrub_leaves_a_header_alone_in_an_example() -> None:
    """Leave a bare PEM header in prose untouched, since a setup example names the header without a key."""
    # Given a setup example that shows only the header line
    header = "-----BEGIN " + "PRIVATE KEY-----"
    text = "Paste it here:\n" + header + "\n...\n"
    # When scrubbed
    out, changed = scrub(text)
    # Then the text is unchanged and no change is reported
    assert out == text
    assert changed is False
