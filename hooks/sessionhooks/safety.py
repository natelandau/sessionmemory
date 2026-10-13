"""Secret redaction for the sweep's write backstop.

The sweep runs skip-permissions work over an untrusted transcript whose
proposed writes could carry a leaked credential. `Sweep._validate_writes` is
the one caller, and it runs every write, tool-reported or diff-derived,
through this scrubber before trusting it.
"""

from __future__ import annotations

import re

REDACTED = "«redacted-secret»"

# RFC 1421 header lines (Proc-Type, DEK-Info) may sit between BEGIN and the body. Each
# line repeats the block's own prefix, captured as `p` from the BEGIN line.
_PEM_HEADER_LINE = r"(?P=p)[A-Za-z][A-Za-z0-9-]*:[^\r\n]*\r?\n"
_PEM_BODY_LINE = r"(?P=p)[A-Za-z0-9+/=]+[ \t]*\r?\n"
# The indentation or `> ` blockquote marker a block pasted into markdown carries on every
# line. `^` lets a prefix start only at a line start, so a long run of whitespace is
# scanned once rather than from every position inside it.
_PEM_PREFIX = r"(?P<p>(?:^[ \t]*(?:>[ \t]*)?)?)"

# (pattern, replacement). Token-shaped secrets replace the whole match; the
# key:value form preserves the label and redacts only the value.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), REDACTED),
    # Fine-grained PATs use a github_pat_ prefix the gh[pousr]_ form misses.
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    # A bare header appears in setup examples, but a body carries key material whether or
    # not its END footer survived. Every body line ends in a newline, so the repetitions
    # cannot split one run of characters between them and backtracking stays linear.
    (
        re.compile(
            rf"(?m){_PEM_PREFIX}-----BEGIN (?P<kind>[A-Z ]*)PRIVATE KEY-----[ \t]*\r?\n"
            rf"(?:{_PEM_HEADER_LINE})*"
            r"(?:[ \t]*(?:>[ \t]*)?\r?\n)?"
            rf"(?:(?:{_PEM_BODY_LINE})*(?P=p)-----END (?P=kind)PRIVATE KEY-----"
            rf"|(?:{_PEM_BODY_LINE})*(?P=p)[A-Za-z0-9+/=]{{40,}})"
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
