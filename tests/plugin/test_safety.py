"""Verify the shared secret-scrub redacts token- and key:value-shaped secrets."""

from __future__ import annotations

import time

import pytest
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


_PEM_BODY = [
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
    "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
]


def test_scrub_redacts_encrypted_legacy_key_with_headers() -> None:
    """Verify an encrypted legacy PEM key is redacted through its Proc-Type and DEK-Info headers."""
    # Given an encrypted legacy key whose RFC 1421 headers sit between BEGIN and the body
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"
    footer = "-----END RSA " + "PRIVATE KEY-----"
    text = (
        header
        + "\nProc-Type: 4,ENCRYPTED\n"
        + "DEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF\n"
        + "\n"
        + "".join(f"{line}\n" for line in _PEM_BODY)
        + footer
    )
    # When scrubbed
    out, changed = scrub(text)
    # Then no header or body line survives and one redaction marks the block
    assert out == REDACTED
    assert changed is True


def test_scrub_redacts_unterminated_pem_body() -> None:
    """Verify a truncated PEM block with no END footer is redacted through its body."""
    # Given a PEM block whose output was cut off before the END footer
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"
    text = header + "\n" + "".join(f"{line}\n" for line in _PEM_BODY)
    # When scrubbed
    out, changed = scrub(text)
    # Then the header and body are redacted and the trailing newline is kept
    assert out == REDACTED + "\n"
    assert changed is True


def test_scrub_redacts_crlf_pem_block() -> None:
    """Verify a PEM block with CRLF line endings is redacted in full."""
    # Given a complete PEM block written with CRLF line endings
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"
    footer = "-----END RSA " + "PRIVATE KEY-----"
    text = header + "\r\n" + "".join(f"{line}\r\n" for line in _PEM_BODY) + footer
    # When scrubbed
    out, changed = scrub(text)
    # Then the whole block is one redaction
    assert out == REDACTED
    assert changed is True


def test_scrub_header_followed_by_long_whitespace_is_linear() -> None:
    """Verify a bare header followed by a long whitespace run is unchanged and scrubs quickly."""
    # Given a bare header followed by 200,000 spaces with no newline
    text = "-----BEGIN " + "PRIVATE KEY-----" + " " * 200_000
    # When scrubbed
    started = time.perf_counter()
    out, changed = scrub(text)
    elapsed = time.perf_counter() - started
    # Then the text is unchanged and the scrub finishes well under a second
    assert out == text
    assert changed is False
    assert elapsed < 1.0


def test_scrub_many_header_lines_without_body_is_linear() -> None:
    """Verify a bare header followed by many RFC 1421-shaped lines is unchanged and scrubs quickly."""
    # Given a bare header followed by 20,000 header-shaped lines and no body
    text = "-----BEGIN " + "PRIVATE KEY-----\n" + "Aa: b\n" * 20_000
    # When scrubbed
    started = time.perf_counter()
    out, changed = scrub(text)
    elapsed = time.perf_counter() - started
    # Then the text is unchanged and the scrub finishes well under a second
    assert out == text
    assert changed is False
    assert elapsed < 1.0


@pytest.mark.parametrize("prefix", ["    ", "\t", "> ", "  > "])
def test_scrub_redacts_an_indented_pem_block(prefix: str) -> None:
    """Verify a PEM block indented or quoted as a whole is redacted through its footer."""
    # Given a complete PEM block whose every line carries the same prefix
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"
    footer = "-----END RSA " + "PRIVATE KEY-----"
    lines = [header, *_PEM_BODY, footer]
    text = "Key:\n" + "".join(f"{prefix}{line}\n" for line in lines) + "after\n"
    # When scrubbed
    out, changed = scrub(text)
    # Then no key line survives and the text around the block is kept
    assert out == f"Key:\n{REDACTED}\nafter\n"
    assert changed is True


def test_scrub_redacts_an_indented_unterminated_pem_body() -> None:
    """Verify an indented PEM block cut off before its footer is redacted through its body."""
    # Given an indented PEM block with no footer
    header = "-----BEGIN " + "PRIVATE KEY-----"
    text = "".join(f"    {line}\n" for line in [header, *_PEM_BODY])
    # When scrubbed
    out, changed = scrub(text)
    # Then the block is redacted
    assert out == REDACTED + "\n"
    assert changed is True


def test_scrub_long_indented_line_without_a_header_is_linear() -> None:
    """Verify a long run of indentation with no PEM header is unchanged and scrubs quickly."""
    # Given 200,000 spaces and tabs before ordinary text
    text = " \t" * 100_000 + "x\n"
    # When scrubbed
    started = time.perf_counter()
    out, changed = scrub(text)
    elapsed = time.perf_counter() - started
    # Then the text is unchanged and the scrub finishes well under a second
    assert out == text
    assert changed is False
    assert elapsed < 1.0
