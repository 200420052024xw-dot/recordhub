"""批改结果暂存：按产出类型分文件夹，再按提交人（老师姓名）分文件。

覆盖式：同一条 ``item_id`` 重提就覆盖旧的，以最后一次为准（后覆盖前）。写回成功后
删除，所以目录里只剩「还没进飞书表」的批改结果。

文件名不带日期——日期以文件里的 ``target_date`` 为准，跨天时整份重置。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from data.store import FileStateStore

SCHEMA_VERSION = 1


def _safe(value: str) -> str:
    """姓名进文件名前的清洗：中文与字母数字保留，其余换下划线。"""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in value)


class ReviewInbox:
    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir) / "reviews"

    def path(self, kind: str, reviewer_name: str) -> Path:
        return self.root / kind / f"{_safe(reviewer_name)}.json"

    def load(self, kind: str, reviewer_name: str) -> dict | None:
        path = self.path(kind, reviewer_name)
        if not path.exists():
            return None
        return self._read(path)

    def save(self, kind: str, reviewer_name: str, *, reviewer_id: str,
             target_date: date, items: dict) -> Path:
        path = self.path(kind, reviewer_name)
        document = {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "reviewer_id": reviewer_id,
            "reviewer_name": reviewer_name,
            "target_date": target_date.isoformat(),
            "items": items,
        }
        FileStateStore._atomic_write(path, document)
        return path

    def record(self, kind: str, reviewer_name: str, *, reviewer_id: str,
               target_date: date, item_id: str, payload: dict) -> Path:
        """写入一条批改结果；同 ``item_id`` 覆盖，跨天整份重置。"""
        today = target_date.isoformat()
        document = self.load(kind, reviewer_name)
        if document is None or document.get("target_date") != today:
            items: dict = {}
        else:
            items = dict(document.get("items") or {})
        items[item_id] = payload
        return self.save(kind, reviewer_name, reviewer_id=reviewer_id,
                         target_date=target_date, items=items)

    def documents(self, kind: str) -> list[dict]:
        """该类型下所有老师的暂存（供合并 / 写回遍历）。"""
        directory = self.root / kind
        if not directory.is_dir():
            return []
        return [self._read(path) for path in sorted(directory.glob("*.json"))]

    def discard(self, kind: str, reviewer_name: str) -> None:
        try:
            self.path(kind, reviewer_name).unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 暂存读不出来不能当作「没人批」：宁可让整轮推迟。
            raise ValueError(f"批改暂存文件损坏：{path}") from exc
        if not isinstance(document, dict):
            raise ValueError(f"批改暂存文件损坏：{path}")
        return document
