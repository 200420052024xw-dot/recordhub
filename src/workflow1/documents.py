"""Deterministic Workflow1 document hierarchy and recoverable creation."""

from __future__ import annotations

from datetime import date
from urllib.parse import unquote

from data.store import FileStateStore
from schema import CloudObject, DailySnapshot
from tool.cloud_docs import CloudDocsService, text_block


class DailyDocuments:
    def __init__(self, docs: CloudDocsService, store: FileStateStore,
                 parent_folder_token: str) -> None:
        self.docs = docs
        self.store = store
        self.parent = parent_folder_token

    def _folder(self, parent: str, name: str) -> str:
        return self.docs.find_child(parent, name, "folder") or self.docs.create_folder(parent, name)

    def _document(self, target_date: date, key: str, folder: str,
                  title: str, blocks: list[dict]) -> CloudObject:
        snapshot = self.store.load_snapshot(target_date)
        assert snapshot is not None
        item = snapshot.cloud_objects.get(key)
        if item is None:
            token = self.docs.find_child(folder, title, "docx")
            if token is None:
                token = self.docs.create_document(folder, title)
            item = CloudObject(
                business_key=key, token=token,
                url=f"https://feishu.cn/docx/{token}", folder_token=folder,
            )
            self.store.update_snapshot(target_date,
                lambda state: state.cloud_objects.setdefault(key, item))
        if not item.content_written:
            existing = self.docs.list_blocks(item.token)
            existing = [block for block in existing if self._block_content(block).strip()]
            if len(existing) > len(blocks):
                raise ValueError(f"文档 {title} 已有额外正文，无法安全续写")
            for actual, expected in zip(existing, blocks):
                if self._block_signature(actual) != self._block_signature(expected):
                    raise ValueError(f"文档 {title} 已有正文与当日快照不一致")
            if len(existing) < len(blocks):
                self.docs.append_blocks(item.token, blocks[len(existing):])
            self.store.update_snapshot(target_date,
                lambda state: setattr(state.cloud_objects[key], "content_written", True))
            item.content_written = True
        return item

    @staticmethod
    def _block_content(block: dict) -> str:
        kind = "text" if block.get("block_type") == 2 else f"heading{block.get('block_type', 0) - 2}"
        value = block.get(kind, {})
        return "".join(element.get("text_run", {}).get("content", "")
                       for element in value.get("elements", []))

    @classmethod
    def _block_signature(cls, block: dict) -> tuple[int, str, tuple[str, ...]]:
        kind = "text" if block.get("block_type") == 2 else f"heading{block.get('block_type', 0) - 2}"
        links = tuple(unquote(element.get("text_run", {})
                             .get("text_element_style", {})
                             .get("link", {}).get("url", ""))
                      for element in block.get(kind, {}).get("elements", []))
        return int(block.get("block_type", 0)), cls._block_content(block), links

    @staticmethod
    def _log_blocks(snapshot: DailySnapshot, person_id: str) -> list[dict]:
        logs = sorted((log for log in snapshot.logs if log.person_id == person_id),
                      key=lambda log: (log.submitted_at, log.log_id))
        if not logs:
            return [text_block("未交工作日志")]
        blocks: list[dict] = []
        for log in logs:
            blocks.extend([text_block(f"原日志记录 ID：{log.source_record_id or log.log_id}"),
                           text_block(log.content())])
            state = snapshot.log_evaluations.get(log.log_id)
            if state:
                blocks.append(text_block(f"肯定之处（{state.source}）：{state.positive_final}"))
                blocks.append(text_block(f"改进之处（{state.source}）：{state.improvement_final}"))
        return blocks

    def advance(self, target_date: date) -> dict[str, CloudObject]:
        if not self.parent:
            raise ValueError("缺少飞书归档父文件夹 Token")
        snapshot = self.store.load_snapshot(target_date)
        assert snapshot is not None
        people = snapshot.organization.person_map()
        day_folder = self._folder(self.parent, target_date.isoformat())
        result: dict[str, CloudObject] = {}
        for department in sorted(snapshot.organization.departments,
                                 key=lambda item: item.department_id):
            if not department.active or not department.minister_id:
                continue
            backbones = sorted((person for person in snapshot.organization.persons
                if person.active and person.role == "骨干学生"
                and person.department_id == department.department_id),
                key=lambda item: item.person_id)
            minister_folder = self._folder(day_folder,
                f"{people[department.minister_id].name}_{department.department_id}")
            for backbone in backbones:
                direct = sorted((person for person in snapshot.organization.persons
                    if person.active and person.leader_id == backbone.person_id
                    and person.role == "基层学生"), key=lambda item: item.person_id)
                progress = snapshot.evaluators.get(backbone.person_id)
                if progress and not progress.closed:
                    continue
                blocks = [text_block(f"{backbone.name}的成员日志表", heading=1)]
                for member in direct:
                    blocks.append(text_block(member.name, heading=2))
                    blocks.extend(self._log_blocks(snapshot, member.person_id))
                if not direct:
                    blocks.append(text_block("无直属成员"))
                key = f"{target_date}:MEMBERS:{backbone.person_id}"
                backbone_folder = self._folder(minister_folder,
                    f"{backbone.name}_{backbone.person_id}")
                result[key] = self._document(target_date, key, backbone_folder,
                    f"{backbone.name}的成员日志表", blocks)
            required = [progress for progress in snapshot.evaluators.values()
                if progress.evaluator_id == department.minister_id
                or people[progress.evaluator_id].department_id == department.department_id]
            if any(not progress.closed for progress in required):
                continue
            if any(f"{target_date}:MEMBERS:{person.person_id}" not in result
                   for person in backbones):
                continue
            blocks = [text_block(department.name, heading=1)]
            for backbone in backbones:
                blocks.append(text_block(f"{backbone.name}的日志情况", heading=2))
                blocks.extend(self._log_blocks(snapshot, backbone.person_id))
                child = result[f"{target_date}:MEMBERS:{backbone.person_id}"]
                blocks.append(text_block(f"【{backbone.name}的成员日志表】", link=child.url))
            if not backbones:
                blocks.append(text_block("当日无骨干日志"))
            key = f"{target_date}:DEPARTMENT:{department.department_id}"
            result[key] = self._document(target_date, key, minister_folder,
                f"{department.name}部门日志", blocks)
        active = [dep for dep in snapshot.organization.departments
                  if dep.active and dep.minister_id]
        if all(f"{target_date}:DEPARTMENT:{dep.department_id}" in result for dep in active):
            blocks = [text_block(f"{target_date:%m-%d} 团队日志", heading=1)]
            for department in sorted(active, key=lambda item: item.department_id):
                child = result[f"{target_date}:DEPARTMENT:{department.department_id}"]
                blocks.append(text_block(f"【{department.name}】", link=child.url))
            key = f"{target_date}:TEAM"
            result[key] = self._document(target_date, key, day_folder,
                f"{target_date:%m-%d} 团队日志", blocks)
        return result
