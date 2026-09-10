"""The live lane: measure what a real model's rewrite costs in recall.

Run directly, never through pytest:

    MEM_QUALITY_LIVE=1 python orchestrator/tests/memory_scenarios/quality_live.py

It has to live outside pytest, and that is deliberate. conftest's autouse
`offline` fixture replaces `dedup_merge._default_llm_calls` with a tripwire
that raises, so the suite structurally cannot reach a paid API however the
environment is set - test_quality_loss_detection.py asserts exactly that. The
consequence is that the one measurement `docs/Research.md` topic 59 says has
never been taken cannot be taken from inside the suite either, so it is taken
from here.

What it produces: for each merging corpus, recall and injection against the
recorded per-sentence keyword ground truth, the tokens the rewrite freed, and
the judge's verdict counts - which answer a question nothing currently can,
namely how often the exact-"FAITHFUL" gate at dedup_merge.py:183 actually
refuses something.

Read the numbers with the report's own caveat in mind: a legitimate paraphrase
is scored as a loss, so live recall is a CONSERVATIVE FLOOR on quality, not an
estimate of it. Compare it against the offline conservation baseline for the
same corpus; the gap is the cost of the rewrite, not a regression.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# mem_manager imports `config` bare, exactly as it does at runtime under
# pytest's pythonpath=orchestrator. Without this the first import fails.
for entry in (str(HERE.parents[1]), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import quality_llms as llms  # noqa: E402
import quality_manifest_io as manifest_io  # noqa: E402
import quality_report as report  # noqa: E402
import quality_runner as qr  # noqa: E402
import quality_tokens as qt  # noqa: E402

# Only corpora that actually form a group of more than one: a rewrite cannot
# lose anything where merge_group returns singletons untouched.
LIVE_SCENARIOS = ["S1", "S2"]
REPEATS = 3


class _CountingPair:
    """Wraps the real provider pair to count verdicts and keep a transcript.

    A live recall figure with no transcript behind it is a number nobody can
    explain later, and the verdict counts are the only way to tell a judge that
    is protecting you from one that is decoration.
    """

    name = "live"

    def __init__(self, merge, judge) -> None:
        self._merge, self._judge = merge, judge
        self.transcript: list[dict] = []
        self.verdicts = {"accepted": 0, "rejected": 0, "unparseable": 0}

    def merge(self, messages):
        answer = self._merge(messages)
        self.transcript.append({"role": "merge", "response": (answer or "")[:800]})
        return answer

    def judge(self, messages):
        answer = self._judge(messages)
        normalized = (answer or "").strip().upper()
        key = (
            "accepted"
            if normalized == "FAITHFUL"
            else "rejected"
            if normalized == "UNFAITHFUL"
            else "unparseable"
        )
        self.verdicts[key] += 1
        self.transcript.append({"role": "judge", "response": (answer or "")[:200]})
        return answer

    def as_pair(self):
        return self.merge, self.judge


def main(argv: list[str]) -> int:
    if not llms.live_enabled():
        print(
            f"refusing to run: set {llms.LIVE_ENV_VAR}=1 to spend real API "
            "credits on this measurement",
            file=sys.stderr,
        )
        return 2
    live = llms.live_merge_calls()
    if live is None:
        print("no live provider pair available", file=sys.stderr)
        return 2

    counter = qt.resolve_token_counter("regex")
    rows = []
    for scenario_id in LIVE_SCENARIOS:
        scenario = manifest_io.by_id(scenario_id)

        baseline = qr.run_scenario(
            scenario, embedder_name="lexical", counter=counter, use_llm=False
        )
        baseline["lane"] = "offline-baseline"
        rows.append(baseline)

        for attempt in range(REPEATS):
            pair = _CountingPair(*live)
            row = qr.run_scenario(
                scenario,
                embedder_name="lexical",
                counter=counter,
                llm_calls=pair.as_pair(),
                use_llm=True,
            )
            row["lane"] = f"live-{attempt + 1}"
            row["judge_verdicts"] = dict(pair.verdicts)
            row["llm_transcript"] = pair.transcript
            rows.append(row)
            print(
                f"{scenario_id} live attempt {attempt + 1}: "
                f"type_recall={row['keywords'].type_recall:.4f} "
                f"rare_recall={row['keywords'].rare_recall} "
                f"injected={list(row['keywords'].injected)} "
                f"tokens_freed={row['tokens'].freed:+d} "
                f"verdicts={pair.verdicts}"
            )

        recalls = [
            row["keywords"].type_recall for row in rows if row["lane"].startswith("live-")
        ]
        print(
            f"{scenario_id} offline baseline {baseline['keywords'].type_recall:.4f} "
            f"vs live min/mean/max "
            f"{min(recalls):.4f}/{sum(recalls) / len(recalls):.4f}/{max(recalls):.4f}"
        )

    directory = report.write(rows)
    print(f"\nrecorded {len(rows)} measurement(s) -> {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
