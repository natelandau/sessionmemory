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


# Split so a secret scanner does not flag this file as holding a real key.
_PGP_BEGIN = "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----"
_PGP_END = "-----END PGP " + "PRIVATE KEY BLOCK-----"
_PGP_ARMOR = ["Version: GnuPG v2", "Comment: exported for backup"]


@pytest.mark.parametrize("prefix", ["", "    ", "> "])
def test_scrub_redacts_a_whole_pgp_private_key_block(prefix: str) -> None:
    """Verify a PGP private key block is redacted through its armor headers and checksum."""
    # Given a PGP block with Version and Comment headers, a blank line, and a checksum
    lines = [_PGP_BEGIN, *_PGP_ARMOR, "", *_PEM_BODY, "=Ab3d", _PGP_END]
    text = "Key:\n" + "".join(f"{prefix}{line}\n" for line in lines) + "after\n"
    # When scrubbed
    out, changed = scrub(text)
    # Then no armor, body, or checksum line survives and the text around the block is kept
    assert out == f"Key:\n{REDACTED}\nafter\n"
    assert changed is True


def test_scrub_redacts_an_unterminated_pgp_block_through_its_checksum() -> None:
    """Verify a PGP block cut off before its END line is redacted through its short checksum."""
    # Given a PGP block with no END line, ending in its checksum line
    lines = [_PGP_BEGIN, *_PGP_ARMOR, "", *_PEM_BODY, "=Ab3d"]
    text = "".join(f"{line}\n" for line in lines)
    # When scrubbed
    out, changed = scrub(text)
    # Then nothing of the block survives, not even the checksum
    assert out == REDACTED + "\n"
    assert "Ab3d" not in out
    assert changed is True


def test_scrub_leaves_a_bare_pgp_header_alone() -> None:
    """Verify a bare PGP header in an example, followed by an ellipsis, is untouched."""
    # Given a setup example showing only the PGP header
    text = "Paste it here:\n" + _PGP_BEGIN + "\n...\n"
    # When scrubbed
    out, changed = scrub(text)
    # Then the text is unchanged
    assert out == text
    assert changed is False


def test_scrub_redacts_the_short_last_line_of_an_unterminated_pem_block() -> None:
    """Verify an unterminated PEM block loses its final short base64 fragment too."""
    # Given a PEM block cut off after a short final line of key material
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"
    text = header + "\n" + "".join(f"{line}\n" for line in _PEM_BODY) + "xYz0+/==\n"
    # When scrubbed
    out, changed = scrub(text)
    # Then no fragment of the key survives
    assert out == REDACTED + "\n"
    assert changed is True


def test_scrub_redacts_a_multi_line_short_tail_of_an_indented_block() -> None:
    """Verify every short trailing line that carries the block's prefix is consumed."""
    # Given an indented unterminated block ending in two short base64 lines
    header = "-----BEGIN " + "PRIVATE KEY-----"
    lines = [header, *_PEM_BODY, "abcd", "ef=="]
    text = "".join(f"    {line}\n" for line in lines) + "after\n"
    # When scrubbed
    out, changed = scrub(text)
    # Then both short lines are gone and the unprefixed prose after the block is kept
    assert out == f"{REDACTED}\nafter\n"
    assert changed is True


@pytest.mark.parametrize(
    ("begin", "end"),
    [
        ("-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"),
        (_PGP_BEGIN, _PGP_END),
    ],
)
def test_scrub_redacts_a_json_escaped_block(begin: str, end: str) -> None:
    """Verify a key block escaped onto one JSON line is redacted from BEGIN through END."""
    # Given a JSON string holding a key block whose newlines are the two characters \n
    escaped = "\\n".join([begin, *_PEM_BODY, "=Ab3d", end])
    text = '{"key": "' + escaped + '\\n", "other": 1}\n'
    # When scrubbed
    out, changed = scrub(text)
    # Then the whole block is one redaction and the JSON around it is intact
    assert out == '{"key": "' + REDACTED + '\\n", "other": 1}\n'
    assert changed is True


def test_scrub_redacts_a_json_escaped_pgp_block_with_armor_headers() -> None:
    """Verify a JSON-escaped PGP block is redacted through its armor headers and blank line."""
    # Given a JSON string holding a PGP block with headers and a blank separator
    escaped = "\\r\\n".join([_PGP_BEGIN, *_PGP_ARMOR, "", *_PEM_BODY, _PGP_END])
    text = '{"key": "' + escaped + '"}'
    # When scrubbed
    out, changed = scrub(text)
    # Then nothing of the block survives
    assert out == '{"key": "' + REDACTED + '"}'
    assert changed is True


def test_scrub_redacts_an_unterminated_json_escaped_block_through_its_run() -> None:
    """Verify a JSON-escaped block with no END is redacted through the end of its base64 run."""
    # Given a JSON string holding a key block cut off after a short final fragment
    header = "-----BEGIN " + "PRIVATE KEY-----"
    escaped = "\\n".join([header, *_PEM_BODY, "xYz=="])
    text = '{"key": "' + escaped + '", "n": 2}'
    # When scrubbed
    out, changed = scrub(text)
    # Then the header, body, and fragment are gone and the JSON around them is kept
    assert out == '{"key": "' + REDACTED + '", "n": 2}'
    assert changed is True


def test_scrub_leaves_a_json_escaped_bare_header_alone() -> None:
    """Verify a JSON-escaped header followed by an ellipsis is untouched."""
    # Given a JSON example naming only the header
    text = '{"hint": "-----BEGIN ' + 'PRIVATE KEY-----\\n...\\n"}'
    # When scrubbed
    out, changed = scrub(text)
    # Then the text is unchanged
    assert out == text
    assert changed is False


@pytest.mark.parametrize("unit", ["Aa: b\\n", "ab\\n", "\\n"])
def test_scrub_json_escaped_header_with_many_short_units_is_linear(unit: str) -> None:
    """Verify a JSON-escaped header followed by many non-key units is unchanged and quick."""
    # Given a JSON-escaped header followed by 50,000 units that never form a key
    text = "-----BEGIN " + "PRIVATE KEY-----\\n" + unit * 50_000
    # When scrubbed
    started = time.perf_counter()
    out, changed = scrub(text)
    elapsed = time.perf_counter() - started
    # Then the text is unchanged and the scrub finishes well under a second
    assert out == text
    assert changed is False
    assert elapsed < 1.0


def test_scrub_many_short_body_lines_without_a_long_line_is_linear() -> None:
    """Verify a header followed by many short base64 lines and no long line scrubs quickly."""
    # Given a header followed by 50,000 short lines and no END line
    text = "-----BEGIN " + "PRIVATE KEY-----\n" + "ab\n" * 50_000
    # When scrubbed
    started = time.perf_counter()
    out, changed = scrub(text)
    elapsed = time.perf_counter() - started
    # Then the text is unchanged and the scrub finishes well under a second
    assert out == text
    assert changed is False
    assert elapsed < 1.0


def test_scrub_redacts_a_short_tail_after_a_long_line_with_trailing_spaces() -> None:
    """Verify a short final fragment goes even when the long line above it ends in spaces."""
    # Given an unterminated block whose long and short lines both carry trailing spaces
    header = "-----BEGIN " + "PRIVATE KEY-----"
    text = header + "\n" + "A" * 64 + "  \nZm9vYmFy  \n"
    # When scrubbed
    out, changed = scrub(text)
    # Then no fragment of the key survives
    assert "Zm9vYmFy" not in out
    assert out == REDACTED + "  \n"
    assert changed is True


@pytest.mark.parametrize(
    ("after", "kept"),
    [("\\nThe end", "\\nThe end"), ("\\nhello-world", "\\nhello-world")],
)
def test_scrub_json_tail_never_takes_part_of_a_prose_word(after: str, kept: str) -> None:
    """Verify an unterminated JSON-escaped block stops before a following prose unit."""
    # Given an unterminated escaped block, a short fragment, then prose in the same string
    header = "-----BEGIN " + "PRIVATE KEY-----"
    escaped = "\\n".join([header, *_PEM_BODY, "abc="]) + after
    text = '{"key": "' + escaped + '"}'
    # When scrubbed
    out, changed = scrub(text)
    # Then the fragment goes and the prose unit is kept whole
    assert out == '{"key": "' + REDACTED + kept + '"}'
    assert changed is True


def test_scrub_redacts_a_json_block_that_escapes_slashes() -> None:
    """Verify a JSON-escaped block whose encoder escaped every slash is redacted in full."""
    # Given a key block serialized by an encoder that escapes forward slashes
    begin = "-----BEGIN RSA " + "PRIVATE KEY-----"
    end = "-----END RSA " + "PRIVATE KEY-----"
    body = [line.replace("/", "\\/") for line in _PEM_BODY]
    text = '{"key": "' + "\\n".join([begin, *body, end]) + '\\n"}'
    # When scrubbed
    out, changed = scrub(text)
    # Then the whole block is one redaction
    assert out == '{"key": "' + REDACTED + '\\n"}'
    assert changed is True


def test_scrub_redacts_an_unterminated_json_block_that_escapes_slashes() -> None:
    """Verify an unterminated block with escaped slashes is redacted through its run."""
    header = "-----BEGIN " + "PRIVATE KEY-----"
    body = [line.replace("/", "\\/") for line in _PEM_BODY]
    text = '{"key": "' + "\\n".join([header, *body, "x\\/y=="]) + '"}'
    out, changed = scrub(text)
    assert out == '{"key": "' + REDACTED + '"}'
    assert changed is True
