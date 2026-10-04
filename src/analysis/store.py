"""Atomic analysis state and frozen inputs; identifiers are UUIDs, never paths."""

import threading
from datetime import date
from pathlib import Path
from uuid import UUID

from data.store import FileStateStore, utc_now
from analysis.models import AnalysisRun


class AnalysisStore:
    def __init__(self, root: str | Path):
        self.root = Path(root) / "analyses"
        self.lock = threading.RLock()

    def path(self, run_id: str) -> Path:
        return self.root / f"{UUID(run_id)}.json"

    def load(self, run_id: str) -> AnalysisRun | None:
        with self.lock:
            path = self.path(run_id)
            if not path.exists():
                return None
            return AnalysisRun.model_validate(FileStateStore._read_json(path))

    def save(self, run: AnalysisRun) -> None:
        with self.lock:
            run.updated_at = utc_now()
            FileStateStore._atomic_write(self.path(run.run_id), run.model_dump(mode="json"))

    def all(self) -> list[AnalysisRun]:
        with self.lock:
            if not self.root.exists():
                return []
            return [AnalysisRun.model_validate(FileStateStore._read_json(path))
                    for path in sorted(self.root.glob("*.json"))]

    def recoverable(self) -> list[AnalysisRun]:
        return [run for run in self.all() if run.status in {"PENDING", "RUNNING", "BLOCKED", "FAILED"}
                or (run.result and not run.notification_message_id
                    and run.status in {"WAITING_CONFIRMATION", "NO_MATERIAL"})]

    def watermarks(self) -> dict[str, date]:
        with self.lock:
            path = self.root / "scheduler" / "checkpoints.json"
            if not path.exists():
                return {}
            return {key: date.fromisoformat(value) for key, value in FileStateStore._read_json(path).items()}

    def checkpoint(self, name: str, day: date) -> None:
        with self.lock:
            current = self.watermarks()
            if name not in current or current[name] < day:
                current[name] = day
            FileStateStore._atomic_write(self.root / "scheduler" / "checkpoints.json",
                                         {key: value.isoformat() for key, value in current.items()})
