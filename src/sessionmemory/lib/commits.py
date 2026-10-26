"""Ask git and GitHub where a commit landed.

`Repository` is the only place reconcile and doctor talk to git or `gh`. Every
subprocess runs without `GIT_`-prefixed variables other than the ssh settings, never
prompts, and has a timeout, and the only write to the repository is `git fetch origin`.

`pulls` separates two kinds of `gh` failure. HTTP 422 is GitHub's "No commit found for
SHA": the commit was never pushed there, which is an answer (`PullState.NONE`). Any other
failure, such as a missing `gh`, a timeout, a 401, 403, or 404, or output that does not
parse, means we could not ask, and is `PullState.UNANSWERED`. Callers must treat
`UNANSWERED` as "leave the commit alone", never as "not merged".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from sessionmemory.lib import gitinfo, registry

if TYPE_CHECKING:
    from collections.abc import Iterable

GH_TIMEOUT_SECONDS = 10
FETCH_TIMEOUT_SECONDS = 30
# GitHub's commit-to-pulls listing carries no commit count, so the search below a rebase
# merge is bounded instead. A copy deeper than this falls back to the merge commit.
REBASE_SEARCH_DEPTH = 100

_CAT_FILE = "git cat-file --batch-check"
_HEX = re.compile(r"[0-9a-fA-F]{4,64}")
_GITHUB_PREFIX = "github.com/"
_ALL_BRANCHES = "refs/heads/*:refs/remotes/origin/*"


class PullState(StrEnum):
    """What GitHub says about a commit's pull requests."""

    MERGED = "merged"
    OPEN = "open"
    NONE = "none"
    UNANSWERED = "unanswered"


@dataclass(frozen=True)
class PullAnswer:
    """GitHub's answer for one commit."""

    state: PullState
    merge_sha: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Expansion:
    """The result of expanding abbreviated shas.

    A sha in neither field is unknown: the repository has no such object, or it is not a
    commit. `ambiguous` is a different answer, since the commit may well exist and sit on
    trunk, so a caller must not treat it as missing.

    Attributes:
        full (dict[str, str]): Each resolved input mapped to its full commit sha.
        ambiguous (frozenset[str]): Inputs that match more than one object.
    """

    full: dict[str, str]
    ambiguous: frozenset[str] = frozenset()


class CommitsError(RuntimeError):
    """Git could not answer a question, so an empty result would be a lie.

    Attributes:
        command: The git command line that failed.
        detail: Its first stderr line, or why git gave no answer.
    """

    def __init__(self, command: str, detail: str) -> None:
        """Record the failing command and why it failed."""
        super().__init__(f"{command}: {detail}")
        self.command = command
        self.detail = detail


_KEPT_GIT_VARIABLES = frozenset({"GIT_SSH_COMMAND", "GIT_SSH", "GIT_SSH_VARIANT"})


def _safe_env() -> dict[str, str]:
    """Return the process environment for git and `gh`: few `GIT_` variables, never prompting, C locale.

    A git hook exports `GIT_DIR` and `GIT_INDEX_FILE`, which would retarget these calls
    at the repository running the hook.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        # The ssh settings only choose how fetch authenticates, never which repository.
        if not k.startswith("GIT_") or k in _KEPT_GIT_VARIABLES
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    # The C locale keeps git's messages in the wording the callers match on.
    env["LC_ALL"] = "C"
    env["LANGUAGE"] = ""
    return env


class Repository:
    """Read-only questions about one repository's commits, plus `fetch`."""

    def __init__(self, root: Path) -> None:
        """Bind to the repository working tree at `root`.

        Args:
            root (Path): The repository's working tree.
        """
        self.root = root
        self._windows: dict[tuple[str, str], dict[str, list[str]]] = {}

    def _git(
        self,
        *args: str,
        timeout: float = gitinfo.GIT_TIMEOUT_SECONDS,
        input_text: str = "",
    ) -> subprocess.CompletedProcess[str] | None:
        """Run git in the repository, or return None when it could not run or timed out.

        Standard input is always a pipe, empty unless `input_text` is given, so git never
        reads the terminal.
        """
        return gitinfo.run_git(
            self.root, *args, env=_safe_env(), timeout=timeout, input_text=input_text
        )

    def is_repository(self) -> bool:
        """Return whether the root is the top-level directory of a git working tree.

        A plain subdirectory of some other repository answers `rev-parse` too, so the
        root must be the working tree's top level, not merely inside one.
        """
        if not self.root.is_dir():
            return False
        top = (self._git_ok("rev-parse", "--show-toplevel") or "").strip()
        if not top:
            return False
        # samefile, not path equality, so a symlink or another letter case on a
        # case-insensitive disk still names the same directory.
        try:
            return Path(top).samefile(self.root)
        except OSError:
            return False

    def _git_ok(self, *args: str) -> str | None:
        """Return git's stdout on exit 0, else None."""
        done = self._git(*args)
        if done is None or done.returncode != 0:
            return None
        return done.stdout

    def _git_strict(
        self, *args: str, ok: tuple[int, ...] = (0,), input_text: str = ""
    ) -> subprocess.CompletedProcess[str]:
        """Run git and raise `CommitsError` unless it exits with a code in `ok`."""
        done = self._git(*args, input_text=input_text)
        command = "git " + " ".join(args)
        if done is None:
            msg = "timed out or could not run"
            raise CommitsError(command, msg)
        if done.returncode not in ok:
            lines = done.stderr.strip().splitlines()
            raise CommitsError(command, lines[0] if lines else f"exit {done.returncode}")
        return done

    @cached_property
    def trunk(self) -> str:
        """The trunk branch name: what `origin/HEAD` points at, else `main`, else `master`."""
        head = self._git_ok("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if head and head.strip().startswith("origin/"):
            return head.strip().removeprefix("origin/")
        if self._ref_exists("refs/heads/main"):
            return "main"
        return "master"

    def _ref_exists(self, ref: str) -> bool:
        """Report whether `ref` exists; raise `CommitsError` when git cannot say."""
        done = self._git_strict("show-ref", "--verify", "--quiet", ref, ok=(0, 1))
        return done.returncode == 0

    def fetch(self) -> bool:
        """Run `git fetch origin`, the only write this class makes.

        Returns:
            bool: True when the fetch exited 0, False when there is no origin, git could
                not run, or the fetch failed or timed out.
        """
        done = self._git("fetch", "origin", timeout=FETCH_TIMEOUT_SECONDS)
        return done is not None and done.returncode == 0

    def expand(self, shas: Iterable[str]) -> Expansion:
        """Resolve each abbreviated sha to its full commit sha.

        Args:
            shas (Iterable[str]): Abbreviated or full shas.

        Returns:
            Expansion: The resolved shas and the ambiguous ones. An input in neither is
                unknown to the repository, or is not a commit.

        Raises:
            CommitsError: When git fails or times out.
        """
        wanted = list(dict.fromkeys(shas))
        valid = [sha for sha in wanted if _HEX.fullmatch(sha)]
        if not valid:
            return Expansion(full={})

        # A `^{commit}` suffix would report an ambiguous prefix as missing, so the type
        # is read from the output instead: only a `commit` line resolves.
        done = self._git_strict(
            "cat-file", "--batch-check", input_text="".join(f"{sha}\n" for sha in valid)
        )
        lines = done.stdout.splitlines()
        if len(lines) != len(valid):
            raise CommitsError(_CAT_FILE, "unexpected output")
        full: dict[str, str] = {}
        ambiguous: set[str] = set()
        for sha, line in zip(valid, lines, strict=True):
            fields = line.split()
            if len(fields) == 3 and fields[1] == "commit":  # noqa: PLR2004
                full[sha] = fields[0]
            elif len(fields) == 2 and fields[1] == "ambiguous":  # noqa: PLR2004
                ambiguous.add(sha)
        return Expansion(full=full, ambiguous=frozenset(ambiguous))

    def trunk_commits(self) -> set[str]:
        """Return the full shas reachable from the local trunk and `origin/<trunk>`.

        Returns:
            set[str]: Empty when neither ref exists.

        Raises:
            CommitsError: When git fails or times out.
        """
        refs = [
            ref
            for ref in (f"refs/heads/{self.trunk}", f"refs/remotes/origin/{self.trunk}")
            if self._ref_exists(ref)
        ]
        if not refs:
            return set()
        return set(self._git_strict("rev-list", *refs).stdout.split())

    def ref_commits(self) -> set[str]:
        """Return the full shas reachable from any branch, remote-tracking ref, or tag.

        Raises:
            CommitsError: When git fails or times out.
        """
        done = self._git_strict("rev-list", "--branches", "--remotes", "--tags")
        return set(done.stdout.split())

    def in_branch(self, sha: str, *, off_trunk: bool = False) -> bool:
        """Report whether a live ref other than trunk holds `sha`.

        Tags count, as they do in `ref_commits`, so reconcile never drops a commit that
        doctor reports as held.

        Args:
            sha (str): A commit sha.
            off_trunk (bool): The caller already knows `sha` is a full commit sha that is
                not on trunk, so skip verifying both.

        Returns:
            bool: True when a ref under `refs/heads`, `refs/remotes`, or `refs/tags`
                contains the commit, ignoring the trunk, `origin/<trunk>`, and
                `origin/HEAD`. False for an unknown sha, and for a commit already on
                trunk, which every branch cut from trunk contains too.

        Raises:
            CommitsError: When git fails or times out.
        """
        if not off_trunk:
            known = self._git_strict(
                "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", ok=(0, 1)
            )
            if known.returncode != 0 or self._on_trunk(sha):
                return False
        out = self._git_strict(
            "for-each-ref",
            "--contains",
            sha,
            "--format=%(refname)",
            "refs/heads",
            "refs/remotes",
            "refs/tags",
        ).stdout
        ignored = {
            f"refs/heads/{self.trunk}",
            f"refs/remotes/origin/{self.trunk}",
            "refs/remotes/origin/HEAD",
        }
        return any(ref and ref not in ignored for ref in out.splitlines())

    def _on_trunk(self, sha: str) -> bool:
        """Report whether `sha` is an ancestor of the local trunk or `origin/<trunk>`.

        Raises:
            CommitsError: When git fails or times out.
        """
        for ref in (f"refs/heads/{self.trunk}", f"refs/remotes/origin/{self.trunk}"):
            if not self._ref_exists(ref):
                continue
            done = self._git_strict("merge-base", "--is-ancestor", sha, ref, ok=(0, 1))
            if done.returncode == 0:
                return True
        return False

    def describe(self, sha: str) -> str | None:
        """Return `<short sha> <subject>` for a commit, or None when git cannot show it."""
        out = self._git_ok("log", "-1", "--format=%h %s", sha)
        return out.strip() if out and out.strip() else None

    def rebased_copy(self, sha: str, merge_sha: str) -> str | None:
        """Return the trunk commit a rebase merge made from `sha`, if there is exactly one.

        For a rebase merge GitHub reports the last rebased commit as the merge commit, so
        each original's own copy is found by its `git patch-id --stable` among the
        first-parent commits at and below `merge_sha` that the original does not already
        contain. Two copies with the same patch, or none, as after a squash merge, give
        None so the caller keeps the merge commit.

        Args:
            sha (str): The full sha of the original, off-trunk commit.
            merge_sha (str): The merge commit GitHub reported, already on trunk.

        Returns:
            str | None: The copy's full sha, or None when no single copy matches.

        Raises:
            CommitsError: When git fails or times out.
        """
        own = self._patch_ids("-n", "1", sha)
        if len(own) != 1:
            return None
        # Exit 1 means no common ancestor, so there is no shared history to leave out.
        done = self._git_strict("merge-base", sha, merge_sha, ok=(0, 1))
        base = done.stdout.strip() if done.returncode == 0 else ""
        key = (merge_sha, base)
        if key not in self._windows:
            self._windows[key] = self._patch_ids(
                "--first-parent",
                "--no-merges",
                "-n",
                str(REBASE_SEARCH_DEPTH),
                merge_sha,
                *([f"^{base}"] if base else []),
            )
        matches = self._windows[key].get(next(iter(own)), [])
        return matches[0] if len(matches) == 1 else None

    def _patch_ids(self, *revs: str) -> dict[str, list[str]]:
        """Map each patch id in `git log -p <revs>` to the commits that carry it.

        Raises:
            CommitsError: When git fails or times out.
        """
        log = self._git_strict(
            "log", "-p", "--no-color", "--no-ext-diff", "--format=commit %H", *revs
        ).stdout
        if not log.strip():
            return {}
        out = self._git_strict("patch-id", "--stable", input_text=log).stdout
        found: dict[str, list[str]] = {}
        for line in out.splitlines():
            fields = line.split()
            if len(fields) == 2:  # noqa: PLR2004
                found.setdefault(fields[0], []).append(fields[1])
        return found

    def is_shallow(self) -> bool:
        """Report whether the repository is a shallow clone.

        Raises:
            CommitsError: When git cannot answer, since "not shallow" would let a caller
                treat commits below the cut as gone.
        """
        out = self._git_strict("rev-parse", "--is-shallow-repository").stdout.strip()
        if out not in {"true", "false"}:
            command = "git rev-parse --is-shallow-repository"
            detail = f"unexpected output {out!r}"
            raise CommitsError(command, detail)
        return out == "true"

    def fetches_all_branches(self) -> bool:
        """Report whether `git fetch origin` brings every branch of origin.

        True only when every `remote.origin.fetch` value is the default all-branches
        refspec. A single-branch clone, a missing refspec, or any extra refspec, which
        may be a negative one, counts as narrowed.

        Raises:
            CommitsError: When git cannot answer, since "narrowed" and "not narrowed"
                would both be guesses.
        """
        # Exit 1 means the key is unset, which fetches nothing by default.
        done = self._git_strict("config", "--get-all", "remote.origin.fetch", ok=(0, 1))
        values = done.stdout.split()
        return (
            done.returncode == 0
            and bool(values)
            and all(value.removeprefix("+") == _ALL_BRANCHES for value in values)
        )

    def origin_url(self) -> str | None:
        """Return the configured `remote.origin.url`, or None when it is unset.

        Raises:
            CommitsError: When git cannot run, since a hiccup must not make a project
                look like it has no origin.
        """
        # Exit 1 means the key is unset, which is a project without an origin.
        done = self._git_strict("config", "--get", "remote.origin.url", ok=(0, 1))
        url = done.stdout.strip()
        return url if done.returncode == 0 and url else None

    def github_slug(self) -> str | None:
        """Return `owner/repo` when origin is on github.com, else None.

        Raises:
            CommitsError: When git cannot run, since a hiccup must not make a GitHub
                project look like it has no GitHub.
        """
        url = self.origin_url()
        if url is None:
            return None
        key = registry.normalize_remote(url)
        if not key.startswith(_GITHUB_PREFIX):
            return None
        slug = key.removeprefix(_GITHUB_PREFIX)
        return slug if slug.count("/") == 1 and all(slug.split("/")) else None

    def pulls(self, sha: str, *, slug: str | None = None) -> PullAnswer:
        """Ask GitHub which pull requests contain a commit.

        Args:
            sha (str): The full commit sha.
            slug (str | None): The `owner/repo` from `github_slug`, when the caller
                already has it, which saves a git call per commit.

        Returns:
            PullAnswer: MERGED when a PR into trunk merged it, else OPEN when a PR is
                open, else NONE when GitHub has no live PR for it, else UNANSWERED with a
                reason.

        Raises:
            CommitsError: When git cannot run to read the origin URL.
        """
        slug = slug or self.github_slug()
        if slug is None:
            return PullAnswer(PullState.UNANSWERED, reason="origin is not on GitHub")
        fetched = self._gh_pulls(slug, sha)
        if isinstance(fetched, PullAnswer):
            return fetched
        return self._classify(fetched)

    def _gh_pulls(self, slug: str, sha: str) -> PullAnswer | list[dict]:
        """Call `gh` and return the parsed pull list, or the answer that ends the query."""
        gh = shutil.which("gh")
        if gh is None:
            return PullAnswer(PullState.UNANSWERED, reason="gh is not installed")

        try:
            done = subprocess.run(  # noqa: S603
                [gh, "api", f"repos/{slug}/commits/{sha}/pulls"],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
                timeout=GH_TIMEOUT_SECONDS,
                env=_safe_env(),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return PullAnswer(PullState.UNANSWERED, reason="gh timed out")
        except OSError as error:
            return PullAnswer(PullState.UNANSWERED, reason=f"gh failed: {error}")

        if done.returncode != 0:
            if "HTTP 422" in done.stderr and "No commit found" in done.stderr:
                return PullAnswer(PullState.NONE)
            lines = done.stderr.strip().splitlines()
            return PullAnswer(
                PullState.UNANSWERED, reason=f"gh failed: {lines[0] if lines else 'no output'}"
            )

        return self._parse_pulls(done.stdout)

    @staticmethod
    def _parse_pulls(stdout: str) -> PullAnswer | list[dict]:
        """Parse `gh` output into a pull list, or an UNANSWERED answer when it is not one."""
        try:
            pulls = json.loads(stdout)
        except json.JSONDecodeError:
            return PullAnswer(PullState.UNANSWERED, reason="gh returned unparsable output")
        if not isinstance(pulls, list) or not all(isinstance(p, dict) for p in pulls):
            return PullAnswer(PullState.UNANSWERED, reason="gh returned unexpected output")
        return pulls

    def _classify(self, pulls: list[dict]) -> PullAnswer:
        """Reduce GitHub's pull request list to one answer."""
        for pull in pulls:
            base = pull.get("base")
            base_ref = base.get("ref") if isinstance(base, dict) else None
            if pull.get("merged_at") and base_ref == self.trunk:
                return PullAnswer(
                    PullState.MERGED, merge_sha=str(pull.get("merge_commit_sha") or "")
                )
        if any(pull.get("state") == "open" for pull in pulls):
            return PullAnswer(PullState.OPEN)
        return PullAnswer(PullState.NONE)
