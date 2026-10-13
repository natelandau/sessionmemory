"""Secret redaction for the sweep's write backstop.

The sweep runs skip-permissions work over an untrusted transcript whose
proposed writes could carry a leaked credential. `Sweep._validate_writes` is
the one caller, and it runs every write, tool-reported or diff-derived,
through this scrubber before trusting it.
"""

from __future__ import annotations

import re

REDACTED = "«redacted-secret»"

_B64 = r"[A-Za-z0-9+/=]"
# A PEM label (`RSA PRIVATE KEY`, `PRIVATE KEY`) or OpenPGP's `PGP PRIVATE KEY BLOCK`.
_KIND = r"(?P<kind>[A-Z ]*PRIVATE KEY(?: BLOCK)?)"
# RFC 1421 header lines (Proc-Type, DEK-Info) and OpenPGP armor headers (Version,
# Comment) may sit between BEGIN and the body. Each line repeats the block's own
# prefix, captured as `p` from the BEGIN line.
_PEM_HEADER_LINE = r"(?P=p)[A-Za-z][A-Za-z0-9-]*:[^\r\n]*\r?\n"
_PEM_BODY_LINE = rf"(?P=p){_B64}+[ \t]*\r?\n"
# The indentation or `> ` blockquote marker a block pasted into markdown carries on every
# line. `^` lets a prefix start only at a line start, so a long run of whitespace is
# scanned once rather than from every position inside it.
_PEM_PREFIX = r"(?P<p>(?:^[ \t]*(?:>[ \t]*)?)?)"
# A line break as a JSON string spells it: the two characters backslash and n.
_ESCAPED_BREAK = r"(?:\\r)?\\n"
# Base64 inside a JSON string, where an encoder may escape `/` as `\/`.
_JSON_B64 = r"(?:[A-Za-z0-9+/=]|\\/)"
# A unit of the tail ends at an escaped break, the string's closing quote, or the text's
# end, so a prose word after the key is never cut in two.
_JSON_UNIT_END = r"(?=\\[rn]|\"|\Z)"

# (pattern, replacement). Token-shaped secrets replace the whole match; the
# key:value form preserves the label and redacts only the value.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), REDACTED),
    # Fine-grained PATs use a github_pat_ prefix the gh[pousr]_ form misses.
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    # A bare header appears in setup examples, but a body carries key material whether or
    # not its END footer survived. An unterminated body runs to its last long line, then
    # takes every following line of nothing but base64, since a short final line is key
    # material too. Every body line ends in a newline, and every tail line starts with
    # one after any trailing blanks, which base64 never holds, so no repetition can split
    # one run of characters and backtracking stays linear.
    (
        re.compile(
            rf"(?m){_PEM_PREFIX}-----BEGIN {_KIND}-----[ \t]*\r?\n"
            rf"(?:{_PEM_HEADER_LINE})*"
            r"(?:[ \t]*(?:>[ \t]*)?\r?\n)?"
            rf"(?:(?:{_PEM_BODY_LINE})*(?P=p)-----END (?P=kind)-----"
            rf"|(?:{_PEM_BODY_LINE})*(?P=p){_B64}{{40,}}"
            rf"(?:[ \t]*\r?\n(?P=p){_B64}+(?=[ \t]*(?:\r?\n|\Z)))*)"
        ),
        REDACTED,
    ),
    # The same block serialized into one JSON string. A backslash appears only in an
    # escaped break or an escaped slash, so each escaped break bounds a unit just as a
    # newline does.
    (
        re.compile(
            rf"-----BEGIN {_KIND}-----{_ESCAPED_BREAK}"
            rf"(?:[A-Za-z][A-Za-z0-9-]*:[^\\\r\n\"]*{_ESCAPED_BREAK})*"
            rf"(?:{_ESCAPED_BREAK})?"
            rf"(?:(?:{_JSON_B64}+{_ESCAPED_BREAK})*-----END (?P=kind)-----"
            rf"|(?:{_JSON_B64}+{_ESCAPED_BREAK})*{_JSON_B64}{{40,}}"
            rf"(?:{_ESCAPED_BREAK}{_JSON_B64}+{_JSON_UNIT_END})*)"
        ),
        REDACTED,
    ),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?)([A-Za-z0-9/+_\-]{20,})"
        ),
        r"\1" + REDACTED,
    ),
)


def scrub(text: str) -> tuple[str, bool]:
    """Redact any secret-shaped content; return (scrubbed_text, changed)."""
    changed = False
    for pattern, repl in SECRET_PATTERNS:
        text, n = pattern.subn(repl, text)
        if n:
            changed = True
    return text, changed
