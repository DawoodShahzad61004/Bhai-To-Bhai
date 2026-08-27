"""Git worktree lifecycle for the coding subagents in a wave.

Each subagent in a wave gets its own worktree on its own branch, works there in
isolation, and the merger merges them afterwards. That isolation is the entire
reason agent 4 exists.

Every function here returns a result rather than raising, for the same reason the
adapters do: a merge conflict is an outcome the merger agent is *for*, not an
exception the graph should die on.

Worktrees are created outside the target repository (config.WORKTREE_DIR_NAME,
alongside it) so that `git status` in the target stays clean and an agent working
in one cannot wander into another's.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import (
    ARTIFACT_DIR_NAME,
    ARTIFACT_ROOT,
    GIT_TIMEOUT_SECONDS,
    INTEGRATION_BRANCH_TEMPLATE,
    TASK_BRANCH_TEMPLATE,
    WORKTREE_DIR_NAME,
)
from logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0

    @property
    def output(self) -> str:
        """Whichever stream said something, preferring stdout."""
        return (self.stdout or self.stderr).strip()


@dataclass(frozen=True)
class Worktree:
    """One coding subagent's isolated checkout."""

    task_id: str
    path: Path
    branch: str


def git(repo: Path | str, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> GitResult:
    """Run one git command in `repo`. Returns a result; never raises."""
    argv = [shutil.which("git") or "git", *args]
    logger.debug("git %s (cwd=%s)", " ".join(args), repo)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return GitResult(ok=False, stderr="git was not found on PATH", returncode=127)
    except subprocess.TimeoutExpired:
        return GitResult(
            ok=False, stderr=f"git {args[0]} exceeded {timeout}s", returncode=124
        )

    result = GitResult(
        ok=completed.returncode == 0,
        stdout=(completed.stdout or "").strip(),
        stderr=(completed.stderr or "").strip(),
        returncode=completed.returncode,
    )
    if not result.ok:
        logger.debug("git %s failed (%d): %s", args[0], result.returncode, result.stderr)
    return result


def is_git_repo(path: Path | str) -> bool:
    return git(path, "rev-parse", "--git-dir").ok


def current_branch(repo: Path | str) -> str:
    result = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout if result.ok else ""


def head_sha(repo: Path | str) -> str:
    """The commit a check result or a handoff packet is anchored to.

    A pass or a fail with no commit behind it is not evidence (ADR-005).
    """
    result = git(repo, "rev-parse", "HEAD")
    return result.stdout if result.ok else ""


def has_changes(repo: Path | str) -> bool:
    """Whether the working tree has anything uncommitted."""
    result = git(repo, "status", "--porcelain")
    return bool(result.ok and result.stdout)


def worktree_root(target_repo: Path | str) -> Path:
    """Where this run's worktrees live: a sibling of the target repository."""
    target = Path(target_repo).resolve()
    return target.parent / WORKTREE_DIR_NAME


def artifact_root(target_repo: Path | str) -> Path:
    """Where this target's artifact store lives: a sibling of the target repository.

    Same idiom as `worktree_root()` above, and for the same reason: state that
    is not the target's own product code should not live inside its working
    tree, where an ordinary `git clean` or a target that already owns a
    `runs/` directory can destroy or collide with it (Decisions.md ADR-037).

    Named `<target-name>-<hash8>` rather than the bare target name so two
    different targets that happen to share a basename (two clones of the same
    project under different parents, or two unrelated projects both called
    "app") never resolve to the same store.
    """
    target = Path(target_repo).resolve()
    base = Path(ARTIFACT_ROOT) if ARTIFACT_ROOT else target.parent / ARTIFACT_DIR_NAME
    digest = hashlib.sha256(str(target).casefold().encode("utf-8")).hexdigest()[:8]
    return base / f"{target.name}-{digest}"


def branch_name(run_id: str, task_id: str) -> str:
    return TASK_BRANCH_TEMPLATE.format(run=run_id, task=task_id)


def integration_branch_name(run_id: str) -> str:
    return INTEGRATION_BRANCH_TEMPLATE.format(run=run_id)


def ensure_integration_branch(target_repo: Path | str, run_id: str) -> tuple[str, GitResult]:
    """Create (or reuse) the branch every wave merges into.

    The merger integrates here rather than into the user's checked-out branch, so
    a run that goes wrong is discarded by deleting a branch.
    """
    branch = integration_branch_name(run_id)
    existing = git(target_repo, "rev-parse", "--verify", branch)
    if existing.ok:
        return branch, GitResult(ok=True, stdout=f"reusing {branch}")
    created = git(target_repo, "branch", branch)
    return branch, created


def reset_branch(target_repo: Path | str, branch: str, sha: str) -> GitResult:
    """Move `branch` back to `sha`, discarding everything after it.

    This is what "the worktree's work is reverted" has to mean once a wave has
    already been merged. The reviewer runs *after* the merger, so by the time a
    wave is rejected its work is on the integration branch — deleting the
    worktrees alone would leave the rejected commits in place and the next
    attempt would branch from its own rejected output.

    Nothing is lost that matters: each task's own branch still exists and still
    points at what that agent did, so the rejected attempt remains inspectable.
    """
    if not sha:
        return GitResult(ok=False, stderr="no base commit recorded for this wave")
    checkout = git(target_repo, "checkout", branch)
    if not checkout.ok:
        return checkout
    result = git(target_repo, "reset", "--hard", sha)
    if result.ok:
        logger.info("reset %s back to %s", branch, sha[:8])
    return result


def create(
    target_repo: Path | str,
    *,
    run_id: str,
    task_id: str,
    base: str,
) -> tuple[Worktree | None, GitResult]:
    """Add a worktree for one task, branched from `base`.

    Returns (worktree, result). A failure comes back with the worktree as None
    and git's own message intact, so the caller can report which task could not
    be started rather than losing the whole wave to a traceback.
    """
    target = Path(target_repo).resolve()
    branch = branch_name(run_id, task_id)
    path = worktree_root(target) / f"{run_id}-{task_id}"

    if path.exists():
        # A leftover from an interrupted run. Reconcile against actual git state
        # rather than assuming the previous run did nothing (ADR-005).
        logger.warning("worktree path %s already exists; removing it first", path)
        remove(target, path)

    result = git(target, "worktree", "add", "-B", branch, str(path), base)
    if not result.ok:
        logger.error("could not create worktree for %s: %s", task_id, result.stderr)
        return None, result

    logger.info("worktree ready | task=%s | branch=%s | %s", task_id, branch, path)
    return Worktree(task_id=task_id, path=path, branch=branch), result


def commit_all(worktree: Path | str, message: str) -> GitResult:
    """Commit everything in a worktree.

    An orchestrator-side commit, on top of whatever the agent committed itself.
    ADR-005's premise is that a killed process must still leave a diff, and an
    agent that dies mid-turn has not committed anything.
    """
    staged = git(worktree, "add", "-A")
    if not staged.ok:
        return staged
    status = git(worktree, "status", "--porcelain")
    if status.ok and not status.stdout:
        return GitResult(ok=True, stdout="nothing to commit")
    return git(worktree, "commit", "-m", message)


def merge(
    target_repo: Path | str,
    *,
    branch: str,
    into: str,
    message: str,
) -> GitResult:
    """Merge one task branch into the integration branch.

    Left in a conflicted state on failure, deliberately: that is the state the
    merger agent is dispatched to resolve. Aborting here would throw away the
    work it needs to see.

    Cleaned of untracked/ignored files immediately before the merge, because
    this checkout is long-lived across the whole run and anything left behind
    by an earlier command — a stray `__pycache__/*.pyc`, say — can collide
    with an incoming branch that adds a tracked file at the same path, which
    git refuses to resolve on its own (docs/Bugs.md #51). Safe now that the
    artifact store lives outside the target's working tree (ADR-037): this
    checkout holds only the target's own product code.
    """
    checkout = git(target_repo, "checkout", into)
    if not checkout.ok:
        return checkout
    git(target_repo, "clean", "-xdff")
    return git(target_repo, "merge", "--no-ff", "-m", message, branch)


def conflicted_files(repo: Path | str) -> list[str]:
    """Paths git reports as unmerged. Empty when the merge is clean."""
    result = git(repo, "diff", "--name-only", "--diff-filter=U")
    if not result.ok or not result.stdout:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def abort_merge(repo: Path | str) -> GitResult:
    return git(repo, "merge", "--abort")


def remove(target_repo: Path | str, path: Path | str) -> GitResult:
    """Drop a worktree, forcefully enough to survive an agent leaving it dirty."""
    result = git(target_repo, "worktree", "remove", "--force", str(path))
    if not result.ok and Path(path).exists():
        # git refuses to remove a worktree it has lost track of; the directory
        # still has to go or the next run's create() will collide with it.
        shutil.rmtree(path, ignore_errors=True)
        git(target_repo, "worktree", "prune")
        return GitResult(ok=not Path(path).exists(), stdout=f"force-removed {path}")
    return result


def cleanup(target_repo: Path | str, worktrees: list[Worktree]) -> None:
    """Remove every worktree in a wave. Best effort, and says what it could not do."""
    for worktree in worktrees:
        result = remove(target_repo, worktree.path)
        if not result.ok:
            logger.warning("could not remove worktree %s: %s", worktree.path, result.stderr)
