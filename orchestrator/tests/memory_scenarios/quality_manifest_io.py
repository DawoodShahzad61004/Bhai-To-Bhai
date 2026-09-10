"""Loading side of quality_manifest.json.

Kept apart from quality_runner so the keyword tests, which have no business
importing the pipeline, can read scenario declarations on their own.
"""
from __future__ import annotations

import json
from pathlib import Path

MANIFEST_PATH = Path(__file__).resolve().parent / "quality_manifest.json"


def load_manifest() -> list[dict]:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return document["scenarios"]


def by_id(scenario_id: str) -> dict:
    for scenario in load_manifest():
        if scenario["id"] == scenario_id:
            return scenario
    raise KeyError(f"no scenario {scenario_id!r} in {MANIFEST_PATH.name}")


def ids(scenario: dict) -> str:
    """pytest id for a parametrized scenario: 'S1-verbatim-duplicates'."""
    return scenario["id"] + "-" + Path(scenario["file"]).stem.split("_", 1)[1].replace(
        "_", "-"
    )
