"""Atomic state for periodic cycles and their activation date."""

import os
import tempfile
import tomllib
from datetime import date
from pathlib import Path
from uuid import UUID

from data.store import FileStateStore, utc_now
from workflow2.models import CycleRun


class Workflow2Store:
    def __init__(self, root: str | Path):
        self.root = Path(root) / "workflow2"
        self.activation_path = Path(root) / "workflow2_activation.toml"

    def activate(self, today: date) -> date:
        """Keep the first enablement day across service restarts."""
        if self.activation_path.exists():
            with self.activation_path.open("rb") as stream:
                raw = tomllib.load(stream)
            return date.fromisoformat(raw["activation_date"])
        self.activation_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".workflow2_activation.", suffix=".tmp",
            dir=self.activation_path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f'activation_date = "{today.isoformat()}"\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.activation_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return today

    def path(self, run_id: str) -> Path:
        return self.root / f"{UUID(run_id)}.json"

    def load(self, run_id: str) -> CycleRun | None:
        path = self.path(run_id)
        return CycleRun.model_validate(FileStateStore._read_json(path)) if path.exists() else None

    def save(self, run: CycleRun) -> None:
        run.updated_at = utc_now()
        FileStateStore._atomic_write(self.path(run.run_id), run.model_dump(mode="json"))

    def all_runs(self) -> list[CycleRun]:
        if not self.root.exists():
            return []
        return [run for path in sorted(self.root.glob("*.json"))
                if (run := CycleRun.model_validate(FileStateStore._read_json(path)))]

    def pending(self) -> list[CycleRun]:
        return [run for run in self.all_runs() if run.status != "COMPLETED"]
