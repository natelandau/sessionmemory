"""Secret redaction for the sweep's write backstop.

The sweep runs skip-permissions work over an untrusted transcript whose
proposed writes could carry a leaked credential. `Sweep._validate_writes` is
the one caller, and it runs every write, tool-reported or diff-derived,
through this scrubber before trusting it.
"""

from __future__ import annotations

import re

REDACTED = "«redacted-secret»"

# (pattern, replacement). Token-shaped secrets replace the whole match; the
# key:value form preserves the label and redacts only the value.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), REDACTED),
    # Fine-grained PATs use a github_pat_ prefix the gh[pousr]_ form misses.
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), REDACTED),
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
