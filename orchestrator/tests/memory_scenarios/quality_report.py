"""Writes the recorded measurement out, as JSON and as readable markdown.

The suite asserts; this is what makes the numbers usable afterwards. Every
row carries the pair (type_recall, tokens_freed) rather than either alone,
because doing nothing scores perfect recall and deleting everything scores
perfect savings - a single blended score would hide both.

Written under temp/, which is already gitignored: these are measurements of a
particular run on a particular machine, not source.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import config
import quality_keywords as qk

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT_DIR = REPO_ROOT / "temp" / "mem_quality"

CAVEATS = [
    "Offline recall is a CONSERVATION CHECK, not an accuracy result. "
    "dedupe_and_merge joins content verbatim, refresh_decay never drops a "
    "record, and rerank_and_prune no-ops behind three gates, so 1.0 there is "
    "guaranteed by construction rather than earned.",
    "Recall is a CONSERVATIVE FLOOR on quality, not an estimate of it. A "
    "legitimate paraphrase (failed -> did not succeed) is scored as a loss, "
    "which is why live-lane recall sits below offline recall. That gap is not "
    "a regression.",
    "LexicalEmbedder at MERGE_SIMILARITY_THRESHOLD 0.60 is NOT MiniLM at "
    "0.60. These scenarios measure the pipeline GIVEN an embedder; the groups "
    "that formed are listed per row so the grouping is never left to be "
    "guessed at.",
    "tokens_freed figures are comparable only within one counter. The counter "
    "that produced them is named on every row.",
    "A NEGATIVE tokens_freed is a real result, not an error: on a log with "
    "little to merge, the four or five marker lines _memories_to_markdown "
    "adds per memory cost more than compaction removes.",
]


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _signature(row: dict) -> tuple:
    """Distinct measurements survive, repeated ones collapse. Several tests
    legitimately re-run the same arm; the report should list it once."""
    keywords, tokens = row["keywords"], row["tokens"]
    return (
        row["id"],
        row["embedder"],
        row["denominator"],
        row.get("lane", ""),
        row["records_out"],
        round(keywords.type_recall, 9),
        tokens.freed,
        keywords.injected,
    )


def _row_json(row: dict) -> dict:
    keywords, tokens = row["keywords"], row["tokens"]
    missing = list(keywords.missing_keywords)
    return {
        "id": row["id"],
        "file": row["file"],
        "hypothesis": row["hypothesis"],
        "embedder": row["embedder"],
        "denominator": row["denominator"],
        "lane": row.get("lane", "offline"),
        "records_in": row["records_in"],
        "records_after_merge": row["records_after_merge"],
        "records_out": row["records_out"],
        "groups_formed": row["groups_formed"],
        "pruning_applied": row["pruning_applied"],
        "pruned_source_tags": row["pruned_source_tags"],
        "prune_collateral": row["prune_collateral"],
        "orphaned_keywords": row["orphaned_keywords"],
        "keywords_total": keywords.total,
        "keywords_found": keywords.found,
        "type_recall": keywords.type_recall,
        "rare_total": keywords.rare_total,
        "rare_recall": keywords.rare_recall,
        "macro_recall": keywords.macro_recall,
        "occurrence_retention": keywords.occurrence_retention,
        "sentence_coverage": {str(k): v for k, v in keywords.sentence_coverage.items()},
        "mean_keywords_per_sentence": keywords.mean_keywords_per_sentence,
        "multiword_keyword_count": keywords.multiword_keyword_count,
        "tokens_before": tokens.before,
        "tokens_after": tokens.after,
        "tokens_freed": tokens.freed,
        "tokens_freed_pct": tokens.freed_pct,
        "tokens_after_content_only": tokens.after_content_only,
        "marker_overhead_tokens": tokens.marker_overhead,
        "counter": tokens.counter_name,
        "injected_keywords": list(keywords.injected),
        "missing_keywords": missing[:50],
        "missing_keywords_truncated": max(0, len(missing) - 50),
        "missing_detail": [
            {
                "keyword": entry.keyword,
                "record_tag": entry.record_tag,
                "sentence_index": entry.sentence_index,
            }
            for entry in keywords.missing[:50]
        ],
        "judge_verdicts": row.get("judge_verdicts"),
    }


def build(rows: Sequence[dict]) -> dict:
    unique: dict[tuple, dict] = {}
    for row in rows:
        unique.setdefault(_signature(row), row)
    ordered = sorted(unique.values(), key=lambda r: (r["id"], r["embedder"]))
    counters = {row["tokens"].counter_name for row in ordered}
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "extractor_version": qk.EXTRACTOR_VERSION,
        "counters_used": sorted(counters),
        "config_snapshot": {
            key: getattr(config, key)
            for key in (
                "MERGE_SIMILARITY_THRESHOLD",
                "PRUNE_BOTTOM_PERCENT",
                "MIN_PRUNE_BUDGET",
                "ENABLE_PRUNING",
                "MERGE_LLM_ENABLED",
                "MERGE_VALIDATION_ENABLED",
                "DECAY_LAMBDA_PER_HOUR",
                "RECENCY_HALF_LIFE_HOURS",
            )
        },
        "caveats": CAVEATS,
        "scenarios": [_row_json(row) for row in ordered],
    }


def _fmt(value, spec="{:.4f}", blank="-"):
    return blank if value is None else spec.format(value)


def to_markdown(document: dict) -> str:
    lines = [
        "# mem_manager compaction quality",
        "",
        f"Generated {document['generated_at']} at commit `{document['git_commit']}` ",
        f"with extractor v{document['extractor_version']} and counter(s) "
        f"{', '.join(document['counters_used'])}.",
        "",
        "## How to read this",
        "",
    ]
    lines += [f"- {caveat}" for caveat in document["caveats"]]
    lines += [
        "",
        "## Results",
        "",
        "Recall and savings are always shown together. Neither means anything alone: "
        "doing nothing scores 1.0 recall, and deleting everything scores ~1.0 savings.",
        "",
        "| scenario | arm | lane | denom | records | type recall | rare recall | "
        "full-sentence coverage | tokens before | after | freed | freed % |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in document["scenarios"]:
        lines.append(
            "| {id} | {arm} | {lane} | {denom} | {rin} -> {rout} | {recall} | {rare} | "
            "{cov} | {before} | {after} | {freed:+d} | {pct:+.1f}% |".format(
                id=row["id"],
                arm=row["embedder"],
                lane=row["lane"],
                denom=row["denominator"],
                rin=row["records_in"],
                rout=row["records_out"],
                recall=_fmt(row["type_recall"]),
                rare=_fmt(row["rare_recall"]),
                cov=_fmt(row["sentence_coverage"].get("1.0")),
                before=row["tokens_before"],
                after=row["tokens_after"],
                freed=row["tokens_freed"],
                pct=100 * row["tokens_freed_pct"],
            )
        )

    losses = [
        row
        for row in document["scenarios"]
        if row["missing_keywords"] or row["injected_keywords"]
    ]
    lines += ["", "## Losses", ""]
    if not losses:
        lines.append("No scenario lost or invented a keyword.")
    for row in losses:
        lines += [
            f"### {row['id']} - {row['lane']} lane "
            f"({row['embedder']} embedder, {row['denominator']} denominator)",
            "",
            f"_{row['hypothesis']}_",
            "",
        ]
        if row["pruned_source_tags"]:
            lines += [
                f"Pruned records: {', '.join(row['pruned_source_tags'])} — "
                f"collateral {_fmt(row['prune_collateral'])} "
                "(share of their keywords surviving nowhere else).",
                "",
            ]
        if row["missing_keywords"]:
            extra = row["missing_keywords_truncated"]
            tail = f" (+{extra} more)" if extra else ""
            lines += [
                f"**Missing ({row['keywords_total'] - row['keywords_found']}):** "
                + ", ".join(f"`{k}`" for k in row["missing_keywords"])
                + tail,
                "",
            ]
            for entry in row["missing_detail"][:10]:
                lines.append(
                    f"- `{entry['keyword']}` — from {entry['record_tag']}, "
                    f"sentence {entry['sentence_index']}"
                )
            lines.append("")
        if row["injected_keywords"]:
            lines += [
                "**Invented (present in the output, absent from the source):** "
                + ", ".join(f"`{k}`" for k in row["injected_keywords"]),
                "",
            ]
        if row["judge_verdicts"]:
            lines += [f"Judge verdicts: {row['judge_verdicts']}", ""]
    return "\n".join(lines) + "\n"


def write(rows: Sequence[dict]) -> Path | None:
    if not rows:
        return None
    document = build(rows)
    directory = REPORT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (directory / "report.md").write_text(to_markdown(document), encoding="utf-8")
    return directory
