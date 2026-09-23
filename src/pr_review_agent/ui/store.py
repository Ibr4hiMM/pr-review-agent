"""Small persistent state for the dashboard: triage decisions and remembered repo folders."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

TriageStatus = Literal["open", "fixed", "wont_fix", "false_positive"]
TRIAGE_STATUSES = ("open", "fixed", "wont_fix", "false_positive")
MAX_NOTE = 2000


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    # Triage is keyed by repo + finding fingerprint, so it carries over to later runs of the same repo.
    def triage_for(self, repo: str) -> dict[str, dict[str, Any]]:
        prefix = f"{repo}|"
        with self._lock:
            return {k[len(prefix) :]: v for k, v in self._read().get("triage", {}).items() if k.startswith(prefix)}

    def set_triage(self, repo: str, fingerprint: str, status: str, note: str | None = None) -> dict[str, Any]:
        if status not in TRIAGE_STATUSES:
            raise ValueError(f"status must be one of {', '.join(TRIAGE_STATUSES)}")
        with self._lock:
            data = self._read()
            entries = data.setdefault("triage", {})
            key = f"{repo}|{fingerprint}"
            entry = entries.get(key, {})
            entry["status"] = status
            if note is not None:
                entry["note"] = note[:MAX_NOTE]
            entry["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            entries[key] = entry
            self._write(data)
            return entry

    def remember_repo(self, path: str, slug: str | None = None) -> None:
        with self._lock:
            data = self._read()
            recent = [p for p in data.get("recent_repos", []) if p != path]
            data["recent_repos"] = [path, *recent][:12]
            if slug:
                data.setdefault("repo_paths", {})[slug] = path
            self._write(data)

    def recent_repos(self) -> list[str]:
        with self._lock:
            return list(self._read().get("recent_repos", []))

    def path_for_slug(self, slug: str) -> str | None:
        with self._lock:
            return self._read().get("repo_paths", {}).get(slug)
