"""Run history: every scan/review is saved as one JSON file that the dashboard reads."""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .models import DroppedFinding, Finding, VerifiedFinding
from .render import Stats

RunKind = Literal["scan", "review", "review-local"]
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[a-z-]+-[0-9a-f]{6}$")


class RunRecord(BaseModel):
    id: str
    kind: RunKind
    repo: str
    target: str  # "me/app#12", "main...feature", or the scanned path/projects
    sha: str
    created_at: str
    model: str
    stats: dict[str, Any] = Field(default_factory=dict)
    findings: list[VerifiedFinding] = Field(default_factory=list)
    dropped: list[DroppedFinding] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    url: str | None = None
    chunks_done: int | None = None
    chunks_total: int | None = None
    repo_path: str | None = None  # local checkout the run came from (scans and branch reviews)
    sources: dict[str, str] = Field(default_factory=dict)  # file -> content at the reviewed commit

    def summary(self) -> dict[str, Any]:
        verified = sum(v.tier == "verified" for v in self.findings)
        return {
            "id": self.id,
            "kind": self.kind,
            "repo": self.repo,
            "target": self.target,
            "sha": self.sha,
            "created_at": self.created_at,
            "model": self.model,
            "cost_usd": self.stats.get("cost_usd", 0.0),
            "verified": verified,
            "possible": len(self.findings) - verified,
            "fixes": sum(1 for v in self.findings if v.fix and v.fix.status == "verified"),
            "dropped": len(self.dropped),
            "severities": {
                s: sum(v.finding.severity == s for v in self.findings) for s in ("critical", "high", "medium", "low")
            },
        }


def new_run(
    kind: RunKind,
    repo: str,
    target: str,
    sha: str,
    model: str,
    stats: Stats,
    findings: list[VerifiedFinding],
    dropped: list[tuple[Finding, str]],
    notes: list[str],
    **extra: Any,
) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        id=f"{now:%Y%m%d-%H%M%S}-{kind}-{uuid.uuid4().hex[:6]}",
        kind=kind,
        repo=repo,
        target=target,
        sha=sha,
        created_at=now.isoformat(timespec="seconds"),
        model=model,
        stats=dataclasses.asdict(stats),
        findings=findings,
        dropped=[DroppedFinding(finding=f, reason=r) for f, r in dropped],
        notes=notes,
        **extra,
    )


def save_run(record: RunRecord, runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{record.id}.json"
    path.write_text(record.model_dump_json(indent=1))
    return path


def load_run(runs_dir: Path, run_id: str) -> RunRecord | None:
    if not RUN_ID_RE.match(run_id):
        return None
    path = runs_dir / f"{run_id}.json"
    try:
        return RunRecord.model_validate_json(path.read_text())
    except (OSError, ValidationError, json.JSONDecodeError):
        return None


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(runs_dir.glob("*.json"), reverse=True):
        rec = load_run(runs_dir, path.stem)
        if rec:
            out.append(rec.summary())
    return out
