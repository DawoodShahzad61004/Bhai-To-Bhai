"""Compact command handler for processing episodic memory artifacts in parallel."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import config
from mem_manager.compact import compact_markdown_file
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
        # Format: ## YYYY-MM-DD HH:MM:SSZ - TAG
        timestamp = m.created_at.strftime("%Y-%m-%d %H:%M:%SZ")
        lines.append(f"## {timestamp} - {m.tag}")
        lines.append("")
        lines.append(m.content)
        lines.append("")

    return "\n".join(lines)


async def compact_file_async(
    file_path: str | Path,
    use_llm: bool = True,
    embedder=None,
    _retry_count: int = 0,
) -> dict[str, Any]:
    """Compact a single markdown file asynchronously."""
    file_path = Path(file_path)
    logger.info(f"[COMPACT] processing {file_path.name}")

    if not file_path.exists():
        logger.error(f"[COMPACT] file not found: {file_path}")
        return {
            "file": str(file_path),
            "status": "error",
            "error": "file not found",
            "memories": [],
        }

    try:
        # Pass embedder if provided, otherwise let compact_markdown_file load it
        kwargs = {"use_llm": use_llm}
        if embedder is not None:
            kwargs["embedder"] = embedder

        memories = compact_markdown_file(file_path, **kwargs)
        logger.info(f"[COMPACT] {file_path.name}: {len(memories)} durable memories")

        # Reconstruct episodic markdown and write back to file
        markdown_content = _memories_to_markdown(memories)
        file_path.write_text(markdown_content, encoding="utf-8")
        logger.info(f"[COMPACT] {file_path.name}: wrote {len(memories)} memories back to file")

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
                    "merged_from_count": len(m.merged_from),
                }
                for m in memories
            ],
        }
    except ModuleNotFoundError as exc:
        error_str = str(exc)
        # If embedding or LLM dependencies are missing, retry with no-op embedder and no LLM
        if _retry_count == 0 and any(
            dep in error_str for dep in ["numpy", "sentence_transformers", "torch", "groq", "openai", "anthropic"]
        ):
            logger.warning(f"[COMPACT] {file_path.name}: missing dependencies, using fallback without LLM")
            return await compact_file_async(
                file_path, use_llm=False, embedder=NoOpEmbedder(), _retry_count=1
            )
        logger.exception(f"[COMPACT] {file_path.name} failed")
        return {
            "file": str(file_path),
            "status": "error",
            "error": error_str,
            "memories": [],
        }
    except Exception as exc:
        logger.exception(f"[COMPACT] {file_path.name} failed")
        return {
            "file": str(file_path),
            "status": "error",
            "error": str(exc),
            "memories": [],
        }


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
