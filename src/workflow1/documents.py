"""Deterministic Workflow1 document hierarchy and recoverable creation.

Structure (per 老师规定): the team log carries team + per-department submission
stats and references each department doc; a department doc carries per-group
stats (naming non-submitters, the only place names appear) and references each
group doc; a group doc contains only per-person log/evaluation blocks in the
teacher's compact format (no blank lines within one person's block). Dividers
separate departments, groups, and people.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from data.store import FileStateStore
from schema import CloudObject, DailySnapshot, Department, Person, UnitStatus
from tool.cloud_docs import CloudDocsService, divider_block, text_block

SHANGHAI = ZoneInfo("Asia/Shanghai")
DIVIDER_TYPE = 22


def team_doc_title(target_date: date) -> str:
    return f"{target_date:%m-%d} 团队日志"


def department_doc_title(target_date: date, department: Department) -> str:
    return f"{target_date:%m-%d} {department.name}日志"


def group_doc_title(target_date: date, backbone: Person) -> str:
    return f"{target_date:%m-%d} {backbone.name}同学小组日志"


class DailyDocuments:
    def __init__(self, docs: CloudDocsService, store: FileStateStore,
                 parent_folder_token: str) -> None:
        self.docs = docs
        self.store = store
        self.parent = parent_folder_token

    def _folder(self, parent: str, name: str) -> str:
        return self.docs.find_child(parent, name, "folder") or self.docs.create_folder(parent, name)

    def _document(self, target_date: date, key: str, folder: str,
                  title: str, blocks: list[dict],
                  on_issue: Callable[[str, str], None] | None = None) -> CloudObject:
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
            expected = [block for block in blocks if block.get("block_type") != DIVIDER_TYPE]
            conflict = len(existing) > len(expected) or any(
                self._block_signature(actual) != self._block_signature(expected_block)
                for actual, expected_block in zip(existing, expected))
            if conflict:
                # 人工编辑优先：保留人类版本，停止续写并记台账，不再阻断流程。
                reason = (f"云文档「{title}」已有与快照不一致的正文（疑似人工编辑），"
                          "程序已停止续写，以人工版本为准")
                if on_issue is None:
                    def mark_conflict(state: DailySnapshot) -> None:
                        state.cloud_objects[key].content_written = True
                        state.issues.setdefault(f"doc:{key}:manual-edit", reason)
                    self.store.update_snapshot(target_date, mark_conflict)
                else:
                    def mark_written(state: DailySnapshot) -> None:
                        state.cloud_objects[key].content_written = True
                    self.store.update_snapshot(target_date, mark_written)
                    on_issue(f"doc:{key}:manual-edit", reason)
                item.content_written = True
                return item
            tail: list[dict] = []
            seen = 0
            for block in blocks:
                if block.get("block_type") == DIVIDER_TYPE:
                    if seen >= len(existing):
                        tail.append(block)
                else:
                    if seen >= len(existing):
                        tail.append(block)
                    seen += 1
            if tail:
                self.docs.append_blocks(item.token, tail)
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

    # ---- 老师规定格式的内容块 ----

    @staticmethod
    def _backbone_flag(role: str) -> str:
        if role == "骨干学生":
            return "是"
        if role == "基层学生":
            return "否"
        return "不适用"

    @staticmethod
    def _date_line(target_date: date) -> str:
        return f"{target_date.year}年{target_date.month}月{target_date.day}日"

    @staticmethod
    def _datetime_line(value: datetime) -> str:
        local = value.astimezone(SHANGHAI)
        return f"{local.year}年{local.month}月{local.day}日 {local:%H:%M}"

    def _stats_blocks(self, snapshot: DailySnapshot, person_ids: list[str],
                      prefix: str, *, named: bool) -> list[dict]:
        submitted_ids = {pid for pid, status in snapshot.submission_status.items()
                         if status.submitted}
        missing = [pid for pid in person_ids if pid not in submitted_ids]
        people = snapshot.organization.person_map()
        blocks = [
            text_block(prefix),
            text_block(f"-应交人数：{len(person_ids)}"),
            text_block(f"-已交人数：{len(person_ids) - len(missing)}"),
        ]
        if named and missing:
            names = "、".join(sorted(people[pid].name for pid in missing))
            blocks.append(text_block(f"-未交人数：{len(missing)}（{names}未交）"))
        else:
            blocks.append(text_block(f"-未交人数：{len(missing)}"))
        return blocks

    def _evaluation_blocks(self, snapshot: DailySnapshot,
                           state) -> list[dict]:
        if state is None:
            return [text_block("未评价")]
        if state.status == UnitStatus.FAILED:
            return [text_block("-评价：评价生成失败（可重评）")]
        people = snapshot.organization.person_map()
        if state.source == "HUMAN" and state.evaluator_id in people:
            evaluator = people[state.evaluator_id]
            label = f"-评价人：{evaluator.name}，{evaluator.role}"
        elif state.manual_skipped:
            label = "-评价人：系统AI（人工未评价）"
        else:
            label = "-评价人：系统AI"
        blocks = [text_block(label)]
        if state.evaluated_at is not None:
            blocks.append(text_block(f"-评价时间：{self._datetime_line(state.evaluated_at)}"))
        blocks.append(text_block(f"-肯定之处：{state.positive_final or '无'}"))
        blocks.append(text_block(f"-改进之处：{state.improvement_final or '无'}"))
        return blocks

    def _person_blocks(self, snapshot: DailySnapshot, person: Person,
                       departments: dict[str, Department]) -> list[dict]:
        department = departments.get(person.department_id or "")
        dept_line = (f"-所属部门：{department.department_id}·{department.name}"
                     if department else "-所属部门：无")
        blocks = [
            text_block(f"{person.name}的日志", heading=3),
            text_block(dept_line),
            text_block(f"-角色：{person.role}"),
            text_block(f"-是否为骨干：{self._backbone_flag(person.role)}"),
            text_block(f"-时间：{self._date_line(snapshot.target_date)}"),
        ]
        logs = sorted((log for log in snapshot.logs if log.person_id == person.person_id),
                      key=lambda log: (log.submitted_at, log.log_id))
        if not logs:
            blocks.extend([
                text_block("日志内容", heading=4),
                text_block("未填写日志"),
                text_block("评价", heading=4),
                text_block("未评价"),
            ])
            return blocks
        for log in logs:
            blocks.append(text_block("日志内容", heading=4))
            for chunk in log.content().split("\n\n"):
                if chunk.strip():
                    blocks.append(text_block(chunk))
            blocks.append(text_block("评价", heading=4))
            blocks.extend(self._evaluation_blocks(
                snapshot, snapshot.log_evaluations.get(log.log_id)))
        return blocks

    # ---- 三级文档装配 ----

    def advance(self, target_date: date,
                on_issue: Callable[[str, str], None] | None = None,
                ) -> dict[str, CloudObject]:
        if not self.parent:
            raise ValueError("缺少飞书归档父文件夹 Token")
        snapshot = self.store.load_snapshot(target_date)
        assert snapshot is not None
        people = snapshot.organization.person_map()
        departments = snapshot.organization.department_map()
        day_folder = self._folder(self.parent, target_date.isoformat())
        result: dict[str, CloudObject] = {}
        active = [department for department in snapshot.organization.departments
                  if department.active and department.minister_id]

        for department in sorted(active, key=lambda item: item.department_id):
            minister = people[department.minister_id]
            minister_folder = self._folder(day_folder,
                f"{minister.name}_{department.department_id}")
            backbones = sorted((person for person in snapshot.organization.persons
                if person.active and person.role == "骨干学生"
                and person.department_id == department.department_id),
                key=lambda item: item.person_id)
            group_members: dict[str, list[str]] = {}
            for backbone in backbones:
                direct = sorted((person for person in snapshot.organization.persons
                    if person.active and person.leader_id == backbone.person_id
                    and person.role == "基层学生"), key=lambda item: item.person_id)
                progress = snapshot.evaluators.get(backbone.person_id)
                if progress and not progress.closed:
                    continue
                member_ids = [backbone.person_id,
                              *[person.person_id for person in direct]]
                group_members[backbone.person_id] = member_ids
                title = group_doc_title(target_date, backbone)
                blocks = [text_block(title, heading=1)]
                for index, person_id in enumerate(member_ids):
                    if index:
                        blocks.append(divider_block())
                    blocks.extend(self._person_blocks(
                        snapshot, people[person_id], departments))
                key = f"{target_date}:MEMBERS:{backbone.person_id}"
                backbone_folder = self._folder(minister_folder,
                    f"{backbone.name}_{backbone.person_id}")
                result[key] = self._document(target_date, key, backbone_folder,
                    title, blocks, on_issue)
            required = [progress for progress in snapshot.evaluators.values()
                if progress.evaluator_id == department.minister_id
                or people[progress.evaluator_id].department_id == department.department_id]
            if any(not progress.closed for progress in required):
                continue
            if any(f"{target_date}:MEMBERS:{person.person_id}" not in result
                   for person in backbones):
                continue
            title = department_doc_title(target_date, department)
            blocks = [text_block(title, heading=1)]
            for index, backbone in enumerate(backbones):
                if index:
                    blocks.append(divider_block())
                blocks.append(text_block(f"{backbone.name}同学", heading=3))
                member_ids = group_members.get(backbone.person_id,
                                               [backbone.person_id])
                blocks.extend(self._stats_blocks(
                    snapshot, member_ids, "该小组提交情况", named=True))
                child = result[f"{target_date}:MEMBERS:{backbone.person_id}"]
                blocks.append(text_block(
                    f"【{group_doc_title(target_date, backbone)}】", link=child.url))
            key = f"{target_date}:DEPARTMENT:{department.department_id}"
            result[key] = self._document(target_date, key, minister_folder,
                title, blocks, on_issue)

        department_ids = [department.department_id for department in active]
        if all(f"{target_date}:DEPARTMENT:{item}" in result for item in department_ids):
            title = team_doc_title(target_date)
            blocks = [text_block(title, heading=1)]
            blocks.extend(self._stats_blocks(
                snapshot, list(snapshot.submission_status), "提交情况", named=False))
            for department in sorted(active, key=lambda item: item.department_id):
                minister = people[department.minister_id]
                blocks.append(divider_block())
                blocks.append(text_block(f"{minister.name}老师部门：", heading=3))
                dept_ids = [person.person_id
                            for person in snapshot.organization.persons
                            if person.active and person.department_id == department.department_id
                            and person.person_id in snapshot.submission_status]
                blocks.extend(self._stats_blocks(
                    snapshot, dept_ids, "部门提交情况", named=False))
                child = result[f"{target_date}:DEPARTMENT:{department.department_id}"]
                blocks.append(text_block(
                    f"【{department_doc_title(target_date, department)}】", link=child.url))
            key = f"{target_date}:TEAM"
            result[key] = self._document(target_date, key, day_folder,
                title, blocks, on_issue)
        return result
