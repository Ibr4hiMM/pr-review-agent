"""Full agent runs on seeded fixtures (costs API credits). Run with: uv run pytest -m eval -s

Each fixture's `feature` branch is reviewed like a PR. A finding counts as a true positive when it lands
on a seeded bug's file within a few lines; anything else is a false positive. Results are appended to
tests/evals/results.jsonl so prompt/threshold changes can be compared over time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pr_review_agent.config import Settings
from pr_review_agent.review import run_local_review
from pr_review_agent.sandbox import make_sandbox
from tests.fixtures.seeded import FIXTURES

pytestmark = pytest.mark.eval
RESULTS = Path(__file__).with_name("results.jsonl")
SLACK = 3


def score(kept, bugs):
    hits, fps = set(), 0
    for v in kept:
        f = v.finding
        match = next(
            (
                i
                for i, b in enumerate(bugs)
                if b.file == f.file and f.line_start - SLACK <= b.lines[1] and b.lines[0] <= f.line_end + SLACK
            ),
            None,
        )
        if match is None:
            fps += 1
        else:
            hits.add(match)
    return hits, fps


@pytest.mark.parametrize("name", sorted(FIXTURES))
async def test_fixture(name, tmp_path):
    repo = tmp_path / name
    bugs = FIXTURES[name](repo)
    settings = Settings(sandbox="local", cache_dir=tmp_path / "cache")
    out = await run_local_review(
        repo, "main", "feature", settings, make_sandbox("local", settings.cache_dir), progress=lambda m: None
    )
    hits, fps = score(out.kept, bugs)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fixture": name,
        "model": settings.model,
        "seeded": len(bugs),
        "found": len(hits),
        "false_positives": fps,
        "verified": sum(v.tier == "verified" for v in out.kept),
        "dropped": len(out.dropped),
        "cost_usd": round(out.stats.cost_usd, 4),
        "turns": out.stats.turns,
    }
    with RESULTS.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(json.dumps(row))
    assert fps == 0, [v.finding.title for v in out.kept]
    assert len(hits) == len(bugs), f"missed {len(bugs) - len(hits)} seeded bug(s)"
