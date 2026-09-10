"""Compact command handler for processing episodic memory artifacts in parallel.

Also the auto-compact trigger `artifacts.py` calls after every write to a
configured artifact: `should_auto_compact()`/`maybe_auto_compact()` gate on
`config.ENABLE_PRUNING`/`MIN_PRUNE_BUDGET` and compact synchronously in place
when both pass, sharing `_compact_and_swap()`'s writer-locked, lockless-read
write path with the manual `--compact` command below.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timezone
from pathlib import Path
from typing import Any

import artifacts as art
import config
from mem_manager.compact import compact_markdown_file
from mem_manager.importance import parse_episodic_md
from mem_manager.memory import DurableMemory
from logging_config import setup_logging, get_logger

logger = get_logger(__name__)


class NoOpEmbedder:
    """Fallback embedder when numpy/sentence-transformers unavailable.

    Disables duplicate grouping (each memory is treated as unique), but allows
    the rest of the pipeline to run.
    """
    def generate_embedding(self, texts: list[str]) -> list[list[float]]:
        """Return dummy embeddings."""
        return [[0.0] * 384 for _ in texts]

    def cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """Return 0.0 similarity, so no duplicates are found."""
        return 0.0


def _memories_to_markdown(memories: list) -> str:
    """Reconstruct episodic markdown from consolidated memories."""
    if not memories:
        return ""

    # Sort by created_at (oldest first)
    sorted_memories = sorted(memories, key=lambda m: m.created_at)

    lines = []
    for m in sorted_memories:
        # Format: ## YYYY-MM-DD HH:MM:SSZ - TAG. Converted to UTC first, not
        # just stamped "Z" as-is - a non-UTC created_at would otherwise be
        # mislabeled as UTC without actually being shifted to it.
        timestamp = m.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        if m.run_id:
            # Reconstructs both the user_choices.md header shape and the
            # literal '<!-- run:ID -->' sentinel append_user_choices()
            # searches for verbatim - without it, a compacted user_choices.md
            # can no longer recognize an already-recorded run on replay and a
            # user's explicit choice gets silently duplicated.
            lines.append(config.USER_CHOICES_RUN_MARKER_TEMPLATE.format(
                run_id=art.safe_id(m.run_id, fallback="run")
            ))
            lines.append(f"## Run `{m.run_id}` — {timestamp}")
        else:
            # Matches append_learning()'s own header format (em dash, not a
            # plain hyphen) - a peer reader's header regex only recognizes
            # that exact separator, and a rewritten learnings.md using a
            # different one becomes invisible to peer_entries_since().
            lines.append(f"## {timestamp} — {m.tag}")
        # Directly under the header, because this function's output OVERWRITES
        # the real artifact file: metadata dropped here is destroyed on the
        # first /compact, not merely missing from this rendering.
        if m.session:
            lines.append(config.SESSION_MARKER_TEMPLATE.format(session=m.session))
        # Re-stamped every pass so a later /compact recognizes this as the
        # SAME memory instead of re-deriving a fresh (and, after tag
        # renumbering, different) id for it - see config.DURABLE_ID_MARKER_TEMPLATE.
        lines.append(config.DURABLE_ID_MARKER_TEMPLATE.format(id=m.id))
        lines.append(config.PROVENANCE_MARKER_TEMPLATE.format(provenance=m.provenance))
        last_accessed = m.last_accessed_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        lines.append(config.LAST_ACCESSED_AT_MARKER_TEMPLATE.format(last_accessed_at=last_accessed))
        lines.append(config.MERGED_FROM_MARKER_TEMPLATE.format(merged_from=",".join(m.merged_from)))
        lines.append("")
        lines.append(m.content)
        lines.append("")

    return "\n".join(lines)


def _compact_with_fallback(
    file_path: Path,
    use_llm: bool = True,
    embedder=None,
    _retry_count: int = 0,
) -> list[DurableMemory]:
    """Run the parse->score->dedup/merge->consolidate->prune pipeline once.

    Retries exactly once, with a `NoOpEmbedder` and `use_llm=False`, when the
    failure is a missing optional dependency (`numpy`/`sentence_transformers`/
    `torch`/`groq`/`openai`/`anthropic`) - the same fallback both
    `compact_file_async` and the auto-compact trigger need, factored out so
    there is exactly one place that knows the retry rule.
    """
    kwargs = {"use_llm": use_llm}
    if embedder is not None:
        kwargs["embedder"] = embedder
    try:
        return compact_markdown_file(file_path, **kwargs)
    except ModuleNotFoundError as exc:
        error_str = str(exc)
        if _retry_count == 0 and any(
            dep in error_str for dep in ["numpy", "sentence_transformers", "torch", "groq", "openai", "anthropic"]
        ):
            logger.warning(f"[COMPACT] {file_path.name}: missing dependencies, using fallback without LLM")
            return _compact_with_fallback(
                file_path, use_llm=False, embedder=NoOpEmbedder(), _retry_count=1
            )
        raise


def _lock_target_for(path: Path) -> Path:
    """The file whose sidecar `.lock` guards writes to `path`.

    Mirrors `artifacts.append_user_choices()`, which coordinates its writes
    through `learnings.md`'s lock instead of its own, to avoid a second lock
    artifact in the flat `shared/` layout - compacting `user_choices.md` has
    to exclude that same writer, so it takes the same lock. Every other file,
    `learnings.md` included, locks on itself, matching `append_learning()`.
    """
    if path.name == art.USER_CHOICES_FILE:
        return path.with_name(art.LEARNINGS_FILE)
    return path


def _invalidate_peer_state(file_path: Path) -> None:
    """After rewriting `learnings.md`, refresh what peer readers rely on.

    `read_learnings_stamp()`/`peer_entries_since()` compare against the
    file's byte size to answer "has anything changed since I last looked",
    and per-task cursors are byte offsets into the pre-compaction file - a
    full-file rewrite (ADR-045) invalidates both: the old size is stale, and
    an old offset can now land past the end of a shrunk file, or mid-record
    of a differently-laid-out one. The stamp gets refreshed; cursors, which
    have no meaningful post-rewrite position to refresh *to*, are simply
    cleared so the next read starts over from the top rather than trusting a
    number that no longer means anything.
    """
    if file_path.name != art.LEARNINGS_FILE:
        return
    stamp_path = file_path.with_name(art.LEARNINGS_STAMP_FILE)
    stamp_path.write_text(str(file_path.stat().st_size), encoding="utf-8")
    cursors_dir = file_path.with_name(art.LEARNINGS_CURSORS_DIRNAME)
    if cursors_dir.is_dir():
        for cursor_file in cursors_dir.iterdir():
            cursor_file.unlink(missing_ok=True)


def _compact_and_swap(file_path: Path, *, use_llm: bool = True, embedder=None) -> list[DurableMemory]:
    """Compact `file_path` in place, excluding peer writers but never readers.

    The pipeline (read, dedup/merge, prune) runs unlocked, same as a plain
    read - it can take a while (real embedding/LLM calls), and holding the
    write lock across all of it would make every `append_learning()`/
    `append_user_choices()` call on this file wait out the whole pipeline
    for no reason a writer-vs-writer lock needs to justify. Only the file
    lock's own scope - assembling the final content and swapping it in -
    runs inside `_lock_target_for(file_path)`'s sidecar lock, the same one
    `artifacts.py`'s writers already take.

    That gap between the unlocked read and the locked write is exactly where
    a peer's append can land, and this file's output unconditionally
    overwrites the whole file (ADR-045) - so the write step re-reads the
    file *inside the lock* and diffs it against the pre-compaction snapshot.
    Bytes appended in that gap are, by `learnings.md`/`user_choices.md`'s
    append-only contract, a clean suffix past the snapshot's length; that
    suffix is spliced onto the compacted markdown verbatim rather than
    discarded, so the peer's entry survives this round uncompacted and gets
    folded in properly on the next pass. If the file changed in some way
    that is *not* a clean suffix append - not possible through this
    module's own writers, but not assumed - the write is abandoned rather
    than risking a corrupt merge; the original file is untouched and the
    caller reports the failure.

    A file whose content parses into 0 *raw* episodic records (garbled or
    unrecognized content, not a legitimate corpus that the pipeline itself
    then pruned down to nothing) is also treated as a failure rather than a
    license to overwrite it with nothing - `_memories_to_markdown([])` is
    `""`, and an empty file is not a safe stand-in for "I couldn't make
    sense of this."

    Never blocks a reader. The write itself keeps this file's pre-write
    bytes in hand and restores them if the write fails partway - a write
    interrupted by a real disk error leaves this file exactly as it was,
    never holding whatever partial bytes made it out before the failure.
    """
    pre_image = file_path.read_bytes()
    if pre_image.strip() and not parse_episodic_md(pre_image.decode("utf-8-sig", errors="replace")):
        raise RuntimeError(
            f"{file_path.name}: 0 episodic records parsed from non-empty input; "
            "aborting rather than overwriting it with an empty file"
        )
    memories = _compact_with_fallback(file_path, use_llm=use_llm, embedder=embedder)
    markdown_content = _memories_to_markdown(memories)

    lock_target = _lock_target_for(file_path)
    with art._exclusive_lock(lock_target):
        current = file_path.read_bytes()
        if not current.startswith(pre_image):
            raise RuntimeError(
                f"{file_path.name} changed in a way that was not a plain append during "
                "compaction; aborting this pass rather than risking a lost update"
            )
        concurrent_tail = current[len(pre_image):].decode("utf-8", errors="replace")
        try:
            file_path.write_text(markdown_content + concurrent_tail, encoding="utf-8")
        except OSError:
            # write_text() truncates before writing, so a failure partway
            # through can leave this file holding an incomplete fragment of
            # the new content - write_bytes() restores exactly what was here
            # before this write attempt started.
            file_path.write_bytes(current)
            raise
        _invalidate_peer_state(file_path)
    return memories


def _success_result(file_path: Path, memories: list[DurableMemory]) -> dict[str, Any]:
    return {
        "file": str(file_path),
        "status": "success",
        "memory_count": len(memories),
        "memories": [
            {
                "id": m.id,
                "tag": m.tag,
                "importance": float(m.importance),
                "content": m.content,
                "created_at": m.created_at.isoformat(),
                "provenance": m.provenance,
                "session": m.session,
                "merged_from_count": len(m.merged_from),
            }
            for m in memories
        ],
    }


def _error_result(file_path: Path, error: str) -> dict[str, Any]:
    return {"file": str(file_path), "status": "error", "error": error, "memories": []}


async def compact_file_async(
    file_path: str | Path,
    use_llm: bool = True,
    embedder=None,
) -> dict[str, Any]:
    """Compact a single markdown file asynchronously."""
    file_path = Path(file_path)
    logger.info(f"[COMPACT] processing {file_path.name}")

    if not file_path.exists():
        logger.error(f"[COMPACT] file not found: {file_path}")
        return _error_result(file_path, "file not found")

    try:
        memories = _compact_and_swap(file_path, use_llm=use_llm, embedder=embedder)
    except Exception as exc:
        logger.exception(f"[COMPACT] {file_path.name} failed")
        return _error_result(file_path, str(exc))

    logger.info(f"[COMPACT] {file_path.name}: {len(memories)} durable memories")
    logger.info(f"[COMPACT] {file_path.name}: wrote {len(memories)} memories back to file")
    return _success_result(file_path, memories)


def should_auto_compact(file_path: str | Path) -> bool:
    """Whether `file_path` has grown into auto-compact territory right now.

    The same two knobs `mem_manager.consolidate.rerank_and_prune` gates its
    own pruning on, read live rather than cached so a config change takes
    effect on the very next write: `config.ENABLE_PRUNING` off means
    auto-compact never fires, and a file under `config.MIN_PRUNE_BUDGET`
    characters is too small a corpus to be worth compacting yet - the same
    threshold PRUNE itself would apply once inside the pipeline.
    """
    if not config.ENABLE_PRUNING:
        return False
    try:
        size = Path(file_path).stat().st_size
    except FileNotFoundError:
        return False
    return size >= config.MIN_PRUNE_BUDGET


def maybe_auto_compact(file_path: str | Path, *, use_llm: bool = True) -> dict[str, Any] | None:
    """Compact `file_path` in place if `should_auto_compact()` says it should.

    The synchronous counterpart to `compact_file_async()`, for callers like
    `artifacts.append_learning()`/`append_user_choices()` that write to a
    shared artifact from ordinary (non-asyncio) code and want compaction to
    happen automatically once the file earns it, rather than waiting for a
    human to run `--compact`. Returns `None` when compaction did not run
    (below `MIN_PRUNE_BUDGET`, or `ENABLE_PRUNING` is off); otherwise the same
    result shape `compact_file_async()` returns.
    """
    file_path = Path(file_path)
    if not should_auto_compact(file_path):
        return None

    logger.info(f"[AUTO-COMPACT] {file_path.name} crossed MIN_PRUNE_BUDGET, compacting")
    try:
        memories = _compact_and_swap(file_path, use_llm=use_llm)
    except Exception as exc:
        logger.exception(f"[AUTO-COMPACT] {file_path.name} failed")
        return _error_result(file_path, str(exc))

    logger.info(f"[AUTO-COMPACT] {file_path.name}: wrote {len(memories)} durable memories")
    return _success_result(file_path, memories)


async def compact_files_parallel(file_paths: list[str | Path], use_llm: bool = True) -> dict[str, Any]:
    """Compact multiple files in parallel."""
    setup_logging(app_name="compact")
    logger.info(f"[COMPACT] starting parallel compaction of {len(file_paths)} file(s)")

    tasks = [compact_file_async(path, use_llm=use_llm) for path in file_paths]
    results = await asyncio.gather(*tasks)

    total_memories = sum(r.get("memory_count", 0) for r in results if r.get("status") == "success")
    failed = sum(1 for r in results if r.get("status") == "error")

    logger.info(f"[COMPACT] completed: {len(results) - failed} succeeded, {failed} failed, {total_memories} total memories")

    return {
        "status": "completed",
        "files_processed": len(results),
        "files_succeeded": len(results) - failed,
        "files_failed": failed,
        "total_memories": total_memories,
        "results": results,
    }


def compact_command(use_llm: bool = True) -> int:
    """Entry point for the /compact command.

    Compacts the episodic memory artifacts (from config.COMPACT_ARTIFACT_FILES)
    in parallel and reports results.
    """
    setup_logging(app_name="compact")

    # Get artifact files from config
    file_paths = config.COMPACT_ARTIFACT_FILES

    # Validate paths
    expanded_paths = []
    for path in file_paths:
        if not path.exists():
            logger.error(f"File not found: {path}")
            print(f"error: {path} not found")
            return 2
        expanded_paths.append(path)

    # Run async compaction
    result = asyncio.run(compact_files_parallel(expanded_paths, use_llm=use_llm))

    # Print summary
    print(f"\nCompaction Summary")
    print("=" * 60)
    print(f"Files processed:  {result['files_processed']}")
    print(f"Files succeeded:  {result['files_succeeded']}")
    print(f"Files failed:     {result['files_failed']}")
    print(f"Total memories:   {result['total_memories']}")
    print()

    # Print per-file results
    for file_result in result["results"]:
        status_str = "✓" if file_result["status"] == "success" else "✗"
        file_name = Path(file_result["file"]).name
        if file_result["status"] == "success":
            print(f"{status_str} {file_name}: {file_result['memory_count']} memories")
        else:
            print(f"{status_str} {file_name}: {file_result.get('error', 'unknown error')}")

    return 0 if result["files_failed"] == 0 else 1
