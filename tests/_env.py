"""Shared environment scrubbing for subprocess-based hook tests.

The hooks' `project_root` resolves a project by running `git rev-parse` with the
env it is handed, so any git-prefixed env var present in that env can retarget
git at a different repository than the test's own cwd (GIT_DIR and GIT_WORK_TREE
most directly, but also GIT_NAMESPACE and the GIT_CONFIG_PARAMETERS /
GIT_CONFIG_COUNT / GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> family, through
which a config override such as core.worktree has the same effect). The indexed
GIT_CONFIG_KEY_<n>/GIT_CONFIG_VALUE_<n> names can't be enumerated as a fixed
set, so a prefix match is the only rule that actually covers the hazard. That
makes the rule deliberately broader than "location": every GIT_-prefixed var
goes, including ones that only affect behavior, because there is no way to keep
those and still catch the indexed family.

When the suite runs under a git hook (pre-commit) or from a worktree, git
exports these vars and they leak into every test-spawned process, making a tmp
project resolve to the outer checkout instead. Strip them so resolution falls
through to the test's own cwd / CLAUDE_PROJECT_DIR, matching production where a
hook runs with no git-hook env.
"""

from __future__ import annotations

import os

# Prefix for every env var git uses to locate or override a repository. Mirrors
# `sessionhooks.store.GIT_VAR_PREFIX`, spelled out rather than imported: `tests/_env`
# is imported at collection time, before any fixture has put the hooks dir on
# sys.path.
_GIT_VAR_PREFIX = "GIT_"


def clean_environ(*, also_drop: frozenset[str] = frozenset()) -> dict[str, str]:
    """Copy os.environ with every GIT_ var and every `also_drop` key removed.

    Use for any env handed to a hook subprocess or to `Store.for_cwd` so git
    resolution targets the test's tmp project rather than the checkout the suite
    happens to run from.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_GIT_VAR_PREFIX) and k not in also_drop
    }
