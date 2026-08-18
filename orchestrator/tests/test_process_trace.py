from __future__ import annotations

import json

from process_trace import (
    DescendantTracker,
    JsonlWriter,
    ProcessInfo,
    describe_exit_status,
)


def test_tracker_discovers_each_generation_but_ignores_unrelated_processes():
    tracker = DescendantTracker(root_pid=100)

    discovered = tracker.observe(
        [
            ProcessInfo(300, 200, "node.exe"),
            ProcessInfo(100, 50, "python.exe"),
            ProcessInfo(200, 100, "cmd.exe"),
            ProcessInfo(999, 1, "node.exe"),
        ]
    )

    assert [process.pid for process in discovered] == [100, 200, 300]
    assert tracker.known_pids == {100, 200, 300}


def test_tracker_retains_parentage_and_reports_a_live_orphan_candidate():
    tracker = DescendantTracker(root_pid=100)
    tracker.observe(
        [
            ProcessInfo(100, 50, "python.exe"),
            ProcessInfo(200, 100, "cmd.exe"),
            ProcessInfo(300, 200, "node.exe"),
        ]
    )
    tracker.record_exit(200, 0)

    assert tracker.orphan_candidates({100, 300}) == [
        ProcessInfo(300, 200, "node.exe")
    ]


def test_exit_status_labels_do_not_claim_an_unknown_cause():
    assert describe_exit_status(0) == "normal"
    assert describe_exit_status(0xC000013A) == "interrupted"
    assert describe_exit_status(7) == "nonzero_error_or_forced"
    assert describe_exit_status(None) == "unknown"


def test_jsonl_writer_emits_one_parseable_record_per_event(tmp_path):
    output = tmp_path / "trace.jsonl"
    writer = JsonlWriter(output)

    writer.write("process_observed", root_pid=10, pid=11, name="node.exe")
    writer.write("process_exited", root_pid=10, pid=11, exit_code=0)

    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == [
        "process_observed",
        "process_exited",
    ]
    assert all(record["timestamp_utc"].endswith("+00:00") for record in records)
