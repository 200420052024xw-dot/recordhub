from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from data.errors import WorkflowStateError
from schema import WorkflowRun

T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


class FileStateStore:
    """Single-process durable state.

    One atomically replaced JSON file per day and workflow under
    ``<state>/<workflow_type>/<date>.json``; shared caches (the organization
    master) sit at the state root because they belong to no single workflow.
    ``snapshot_model`` is injected so this engine never imports a workflow's
    schema.
    """

    schema_version = 1

    def __init__(
        self,
        root: str | Path,
        *,
        workflow_type: str = "workflow1",
        snapshot_model: type | None = None,
    ) -> None:
        self.root = Path(root)
        self.workflow_type = workflow_type
        self.runs_dir = self.root / workflow_type
        self._snapshot_model = snapshot_model
        self._lock = threading.RLock()
        self._migrate_legacy_layout()

    def _migrate_legacy_layout(self) -> None:
        """One-shot move from the pre-split layout.

        ``workflows/<date>.json`` held the only Workflow1 runs, so those files
        become ``<workflow_type>/<date>.json``; ``cache/<key>.json`` becomes
        ``<key>.json`` at the state root.
        """
        legacy_runs = self.root / "workflows"
        if legacy_runs.is_dir() and self.workflow_type == "workflow1":
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            for path in legacy_runs.glob("*.json"):
                target = self.runs_dir / path.name
                if not target.exists():
                    os.replace(path, target)
            _rmdir_if_empty(legacy_runs)
        legacy_cache = self.root / "cache"
        if legacy_cache.is_dir():
            for path in legacy_cache.glob("*.json"):
                target = self.root / path.name
                if not target.exists():
                    os.replace(path, target)
            _rmdir_if_empty(legacy_cache)

    def _snapshot_class(self) -> type:
        if self._snapshot_model is None:
            raise WorkflowStateError(
                "FileStateStore was created without snapshot_model"
            )
        return self._snapshot_model

    def get_or_create_workflow(self, target_date: date) -> WorkflowRun:
        with self._lock:
            document = self._load_document(target_date, missing_ok=True)
            if document is not None:
                return WorkflowRun.model_validate(document["run"])
            now = utc_now()
            run = WorkflowRun(
                workflow_run_id=str(uuid4()),
                workflow_type=self.workflow_type,
                target_date=target_date,
                status="INITIALIZING",
                current_stage="INITIALIZING",
                started_at=now,
                updated_at=now,
            )
            self._write_document(
                target_date,
                {
                    "schema_version": self.schema_version,
                    "revision": 1,
                    "run": run.model_dump(mode="json"),
                    "snapshot": None,
                    "external_records": {},
                    "notifications": {},
                },
            )
            return run

    def load_workflow(self, target_date: date) -> WorkflowRun | None:
        with self._lock:
            document = self._load_document(target_date, missing_ok=True)
            if document is None:
                return None
            return WorkflowRun.model_validate(document["run"])

    def list_incomplete_workflows(self) -> list[WorkflowRun]:
        with self._lock:
            if not self.runs_dir.exists():
                return []
            runs: list[WorkflowRun] = []
            for path in sorted(self.runs_dir.glob("*.json")):
                document = self._read_json(path)
                run = WorkflowRun.model_validate(document["run"])
                if run.status != "COMPLETED":
                    runs.append(run)
            return runs

    def set_status(
        self,
        target_date: date,
        status: str,
        *,
        error: str | None = None,
    ) -> WorkflowRun:
        def mutate(document: dict[str, Any]) -> WorkflowRun:
            run = WorkflowRun.model_validate(document["run"])
            now = utc_now()
            run.status = str(status)
            run.current_stage = str(status)
            run.updated_at = now
            run.last_error = error
            if error:
                run.retry_count += 1
            if status == "COMPLETED":
                run.completed_at = now
            document["run"] = run.model_dump(mode="json")
            return run

        return self._mutate_document(target_date, mutate)

    def save_snapshot(self, snapshot: Any) -> None:
        def mutate(document: dict[str, Any]) -> None:
            snapshot.updated_at = utc_now()
            document["snapshot"] = snapshot.model_dump(mode="json")

        self._mutate_document(snapshot.target_date, mutate)

    def load_snapshot(self, target_date: date) -> Any:
        with self._lock:
            document = self._load_document(target_date)
            raw = document.get("snapshot")
            return self._snapshot_class().model_validate(raw) if raw else None

    def update_snapshot(
        self, target_date: date, mutator: Callable[[Any], T]
    ) -> T:
        def mutate(document: dict[str, Any]) -> T:
            raw = document.get("snapshot")
            if raw is None:
                raise WorkflowStateError(f"Snapshot is missing for {target_date}")
            snapshot = self._snapshot_class().model_validate(raw)
            result = mutator(snapshot)
            snapshot.updated_at = utc_now()
            document["snapshot"] = snapshot.model_dump(mode="json")
            return result

        return self._mutate_document(target_date, mutate)

    def get_external_record(
        self, target_date: date, business_key: str
    ) -> dict[str, str] | None:
        with self._lock:
            document = self._load_document(target_date)
            item = document.get("external_records", {}).get(business_key)
            return dict(item) if item else None

    def map_external_record(
        self,
        target_date: date,
        business_key: str,
        table_name: str,
        record_id: str,
    ) -> None:
        def mutate(document: dict[str, Any]) -> None:
            records = document.setdefault("external_records", {})
            existing = records.get(business_key)
            if existing and existing["record_id"] != record_id:
                raise WorkflowStateError(
                    f"Business key {business_key} already maps to another record"
                )
            records[business_key] = {
                "table_name": table_name,
                "record_id": record_id,
            }

        self._mutate_document(target_date, mutate)

    def reserve_notification(self, target_date: date, business_key: str) -> bool:
        def mutate(document: dict[str, Any]) -> bool:
            notifications = document.setdefault("notifications", {})
            existing = notifications.get(business_key)
            if existing and existing.get("completed_at"):
                return False
            if existing:
                reserved = datetime.fromisoformat(existing["reserved_at"])
                if (utc_now() - reserved).total_seconds() >= 3600:
                    raise WorkflowStateError(
                        f"通知 {business_key} 发送结果未知且飞书去重窗口已过，需人工核对"
                    )
                return True
            notifications[business_key] = {
                "reserved_at": utc_now().isoformat(),
                "message_id": None,
            }
            return True

        return self._mutate_document(target_date, mutate)

    def notification_sent(self, target_date: date, business_key: str) -> bool:
        with self._lock:
            document = self._load_document(target_date)
            return bool(document.get("notifications", {}).get(business_key, {}).get("completed_at"))

    def release_notification(self, target_date: date, business_key: str) -> None:
        def mutate(document: dict[str, Any]) -> None:
            notifications = document.setdefault("notifications", {})
            item = notifications.get(business_key)
            if item and not item.get("completed_at"):
                notifications.pop(business_key, None)

        self._mutate_document(target_date, mutate)

    def complete_notification(
        self, target_date: date, business_key: str, message_id: str | None
    ) -> None:
        def mutate(document: dict[str, Any]) -> None:
            item = document.setdefault("notifications", {}).get(business_key)
            if item is not None:
                item["message_id"] = message_id
                item["completed_at"] = utc_now().isoformat()

        self._mutate_document(target_date, mutate)

    def save_cache(self, key: str, payload: Any) -> None:
        with self._lock:
            self._atomic_write(
                self.root / f"{key}.json",
                {
                    "schema_version": self.schema_version,
                    "updated_at": utc_now().isoformat(),
                    "payload": payload,
                },
            )

    def load_cache(self, key: str) -> Any | None:
        with self._lock:
            path = self.root / f"{key}.json"
            if not path.exists():
                return None
            return self._read_json(path).get("payload")

    def cleanup_completed(self, retention_days: int, *, today: date | None = None) -> int:
        cutoff = (today or date.today()) - timedelta(days=retention_days)
        removed = 0
        with self._lock:
            if not self.runs_dir.exists():
                return 0
            for path in self.runs_dir.glob("*.json"):
                document = self._read_json(path)
                run = WorkflowRun.model_validate(document["run"])
                if run.status == "COMPLETED" and run.target_date < cutoff:
                    path.unlink()
                    removed += 1
        return removed

    def _mutate_document(
        self, target_date: date, mutator: Callable[[dict[str, Any]], T]
    ) -> T:
        with self._lock:
            document = self._load_document(target_date)
            result = mutator(document)
            document["revision"] = int(document.get("revision", 0)) + 1
            self._write_document(target_date, document)
            return result

    def _load_document(
        self, target_date: date, *, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        path = self._workflow_path(target_date)
        if not path.exists():
            if missing_ok:
                return None
            raise WorkflowStateError(f"Workflow state does not exist for {target_date}")
        document = self._read_json(path)
        if document.get("schema_version") != self.schema_version:
            raise WorkflowStateError(f"Unsupported state schema in {path}")
        return document

    def _write_document(self, target_date: date, document: dict[str, Any]) -> None:
        self._atomic_write(self._workflow_path(target_date), document)

    def _workflow_path(self, target_date: date) -> Path:
        return self.runs_dir / f"{target_date.isoformat()}.json"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowStateError(f"Cannot read state file: {path}") from exc
        if not isinstance(data, dict):
            raise WorkflowStateError(f"State file root must be an object: {path}")
        return data

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
