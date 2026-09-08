# /compact Command Usage

The `/compact` command applies the mem_manager module to the shared episodic memory artifacts in parallel (user_choices.md and learnings.md).

## Usage

```bash
cd orchestrator
python main.py --compact
```

That's it! The command automatically targets:
- `C:\Users\LOQ\Desktop\Projects\.bhai-artifacts\temp_work_repo-55fce4bb\shared\user_choices.md`
- `C:\Users\LOQ\Desktop\Projects\.bhai-artifacts\temp_work_repo-55fce4bb\shared\learnings.md`

Expected output:

```
Compaction Summary
============================================================
Files processed:  2
Files succeeded:  2
Files failed:     0
Total memories:   5

✓ user_choices.md: 0 memories
✓ learnings.md: 5 memories
```

## How It Works

1. **Parallel Processing**: Both files are processed concurrently using asyncio
2. **Parsing**: Each file is parsed as episodic markdown (headers like `## 2026-08-29 12:48:55Z - tag`)
3. **Scoring**: Records are scored using five factors: recency, frequency, surprise, entity salience, and outcome
4. **Deduplication**: Near-duplicate entries are grouped by embedding similarity (complete-linkage clustering)
5. **Merging**: Similar groups are merged using deterministic union (LLM-assisted merge when available)
6. **Decay**: Passive decay is applied based on age and last access time
7. **Pruning**: The bottom 20% of entries (by importance) are pruned when the corpus is large enough

## Episodic Markdown Format

The parser expects headers in this format:

```markdown
## DATE TIME SEPARATOR TAG

Content here...
```

Where:

- `DATE` is YYYY-MM-DD format (e.g., `2026-08-29`)
- `TIME` is HH:MM:SSZ format (e.g., `12:48:55Z`)
- `SEPARATOR` can be any character (em-dash, hyphen, etc.) — only its position matters
- `TAG` is a label for the record (e.g., `requirements`, `T-001`, `reviewer`)

Example:

```markdown
## 2026-08-29 12:48:55Z - requirements

Some findings or observations here.

## 2026-08-29 12:51:46Z - T-001

Another event with a different tag.
```

## Graceful Degradation

If optional dependencies are missing (`numpy`, `sentence-transformers`, `groq`, `openai`, etc.), the command:

- Uses a no-op embedder (treats each memory as unique)
- Disables LLM-assisted merging
- Still produces valid consolidated memories using deterministic union
- Reports this in the logs with a WARNING message

This allows the compact pipeline to work in any environment, with reduced functionality when ML dependencies aren't installed.

## Output

The command prints:

- Number of files processed, succeeded, and failed
- Total consolidated memories
- Per-file status with memory counts

Each consolidated memory includes:

- **id**: Content hash for deduplication
- **tag**: Category/label of the memory
- **importance**: Composite score [0.0, 1.0] (higher = more important)
- **content**: The consolidated text
- **created_at**: ISO format timestamp of the oldest source record
- **provenance**: "explicit" (user-provided) or "inferred" (agent-observed)
- **merged_from_count**: How many source records were merged into this memory

## Configuration

The compaction pipeline uses these settings from `orchestrator/config.py`:

- `EPISODIC_BLOCK_SPLIT_PATTERN` - Regex for splitting markdown blocks
- `USER_CHOICES_HEADER_PATTERN` - Regex for user_choices.md's run-id-first header shape
- `ENTITY_PATTERN` - Pattern for extracting entities (codes, PascalCase names, filenames)
- `IMPORTANCE_WEIGHTS` - Weights for the five scoring factors
- `DECAY_LAMBDA_PER_HOUR` - Passive decay rate
- `EMBEDDING_MODEL_NAME` - Sentence transformer model for similarity
- `MERGE_SIMILARITY_THRESHOLD` - Threshold for grouping near-duplicates
- `JUDGE_MODEL_NAME` - LLM for validating merges
- `MERGE_LLM_ENABLED` - Enable LLM-assisted merging
- `MERGE_VALIDATION_ENABLED` - Enable judge validation
- `PRUNE_BOTTOM_PERCENT` - Fraction of low-importance entries to prune
- `ENABLE_PRUNING` - Enable pruning (disabled when corpus is small)
- `MIN_PRUNE_BUDGET` - Minimum corpus size to enable pruning

All settings can be overridden via environment variables (see `.env` or `.env.example`).

## Logging

Logs are written to `orchestrator/run_logs/<run-id>.debug.log` with detailed information about:

- **Parsing**: How many episodic records found and their timestamps/tags
- **Scoring**: Importance ranges and factor breakdown
- **Dedup/merge**: Which groups were found and how many memories resulted
- **Consolidation**: Passive decay applied per memory
- **Pruning**: Which entries were retained vs. pruned

## Notes

- **Empty files**: Files with no matching episodic records will show 0 memories (not an error).
- **Large corpora**: Pruning is skipped for corpora under `MIN_PRUNE_BUDGET` (default 2000 characters).
