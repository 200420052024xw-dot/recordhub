"""Submission counts derived only from the roster and source logs."""

from __future__ import annotations

from datetime import date, datetime

from data import FileStateStore
from schema import Organization, TableConfig, WorkLog
from tool.bitable_fields import SHANGHAI, record_fields, scalar
from tool.diagnostics import error_summary
from tool.feishu import BitableService


class SubmissionStatisticsRepository:
    def __init__(self, bitable: BitableService, config: TableConfig,
                 store: FileStateStore) -> None:
        self.bitable = bitable
        self.table = config.tables.get("check_details")
        self.legacy_store = store
        # Keep identities when daily snapshots are cleaned up, and never create
        # an incomplete evaluation workflow just to repair submission counts.
        self.store = FileStateStore(store.root, workflow_type="submission_statistics")

    def publish(self, target_date: date, organization: Organization,
                logs: list[WorkLog]) -> dict:
        table = self.table
        if table is None or not table.table_id:
            return {"target_date": str(target_date), "status": "disabled",
                    "expected": 0, "written": 0, "issues": {}}
        people = organization.person_map()
        if len(people) != len(organization.persons):
            raise ValueError("人员编号重复，不能统计日志人数")
        eligible_ids = {person.person_id for person in people.values()
                        if person.active and person.membership_list()
                        and any(item.role in {"基层学生", "骨干学生"}
                                for item in person.membership_list())}
        submitted = {log.person_id for log in logs
                     if log.submitted_at.astimezone(SHANGHAI).date() == target_date}
        # (key, reporter_id, member ids, title, reporter role, department name)。
        # 部门视图按成员关系重复计，团队视图按 person_id 去重。
        scopes: list[tuple[str, str | None, set[str], str, str, str]] = []
        backbone_departments: dict[str, list[str]] = {}
        for department in sorted(organization.departments, key=lambda item: item.department_id):
            if not department.active:
                continue
            dept_members = organization.members_in_department(
                department.department_id, roles={"基层学生", "骨干学生"})
            for person, membership in dept_members:
                if membership.role == "骨干学生":
                    backbone_departments.setdefault(
                        person.person_id, []).append(department.department_id)
                if membership.role != "骨干学生":
                    continue
                members = {person.person_id} | {
                    other.person_id for other, other_membership in dept_members
                    if other_membership.role == "基层学生"
                    and other_membership.leader_id == person.person_id}
                key = f"{target_date}:MEMBERS:{department.department_id}:{person.person_id}"
                scopes.append((key, person.person_id, members,
                               f"{department.name}·{person.name}小组",
                               membership.role, department.name))
        for department in sorted(organization.departments, key=lambda item: item.department_id):
            if not department.active:
                continue
            dept_ids = {person.person_id
                        for person, _membership in organization.members_in_department(
                            department.department_id, roles={"基层学生", "骨干学生"})}
            scopes.append((f"{target_date}:DEPARTMENT:{department.department_id}",
                           department.minister_id, dept_ids, department.name,
                           "部长", department.name))
        scopes.append((f"{target_date}:TEAM", organization.team_leader_id,
                       set(eligible_ids), "团队", "团队负责人", "团队"))
        rows = {str(row["record_id"]): row for row in self.bitable.list_records(table.table_id)}
        self.store.get_or_create_workflow(target_date)
        has_legacy = self.legacy_store.load_workflow(target_date) is not None
        fields = table.fields
        reported_at = int(datetime.now().timestamp() * 1000)
        issues: dict[str, str] = {}
        field_types: dict[str, int] = {}
        try:
            field_types = {str(item.get("field_name")): int(item.get("type"))
                           for item in self.bitable.list_fields(table.table_id)}
        except Exception:
            field_types = {}
        title_field = fields.get("title", "")
        department_field = fields.get("department", "")
        # 飞书的“文本”列是自动编号（type 1005），不能写；优先写独立“部门”列。
        title_writable = bool(title_field) and field_types.get(title_field) in (1, 15)
        department_writable = bool(department_field) and field_types.get(
            department_field) == 1
        if department_field and not department_writable:
            issues["detail:department:not-writable"] = (
                f"提交详情表字段「{department_field}」不存在或不是文本字段，部门未写入表中")
        elif not department_field and not title_writable:
            issues["detail:title:not-writable"] = (
                f"提交详情表没有可写的部门字段（「{title_field or '未配置'}」不是文本），"
                "部门只记录在本地业务键中")
        assigned_ministers = {department.minister_id for department in organization.departments
                              if department.active}
        for person in people.values():
            if person.active and person.role == "部长" and person.person_id not in assigned_ministers:
                issues[f"detail:minister:{person.person_id}:missing-department"] = (
                    f"「{person.name}」（{person.person_id}）未配置负责的有效部门，无法生成部门统计")
        written = 0
        for key, reporter_id, members, title, reporter_role, department_name in scopes:
            person = people.get(reporter_id)
            if person is None or not person.active:
                issues[f"detail:{key}:missing-reporter"] = f"统计 {key} 缺少有效负责人（{reporter_id or '未配置'}）"
                continue
            if not person.open_id:
                issues[f"detail:{key}:missing-open-id"] = (
                    f"「{person.name}」（{person.person_id}）缺少飞书 OpenID，统计行无法写入")
                continue
            counts = {fields["submitted"]: len(members & submitted),
                      fields["missing"]: len(members - submitted)}
            # The first column is auto-numbered. Identity is persisted locally
            # by business date and scope, never guessed from a reporter or time.
            try:
                mapped = self.store.get_external_record(target_date, key + ":DETAIL")
                if mapped is None and has_legacy:
                    legacy_key = self._legacy_scope_key(
                        target_date, key, people, backbone_departments)
                    if legacy_key:
                        mapped = self.legacy_store.get_external_record(
                            target_date, legacy_key + ":DETAIL")
                        if mapped:
                            self.store.map_external_record(
                                target_date, key + ":DETAIL",
                                mapped["table_name"], mapped["record_id"])
                if mapped is None and has_legacy:
                    mapped = self.legacy_store.get_external_record(target_date, key + ":DETAIL")
                    if mapped:
                        self.store.map_external_record(target_date, key + ":DETAIL",
                                                       mapped["table_name"], mapped["record_id"])
                if mapped:
                    record_id = mapped["record_id"]
                    if mapped["table_name"] != "check_details" or record_id not in rows:
                        raise ValueError("本地映射与飞书不一致，原统计记录不存在")
                    current = record_fields(rows[record_id])
                    changes = {column: value for column, value in counts.items()
                               if scalar(current.get(column)) != str(value)}
                    if title_writable and scalar(current.get(title_field)) != title:
                        changes[title_field] = title
                    if department_writable and scalar(
                            current.get(department_field)) != department_name:
                        changes[department_field] = department_name
                    if changes:
                        self.bitable.update_record(table.table_id, record_id, changes)
                else:
                    values = {**counts,
                              fields["reporter_ref"]: [{"id": person.open_id}],
                              fields["role"]: reporter_role,
                              fields["reported_at"]: reported_at}
                    if title_writable:
                        values[title_field] = title
                    if department_writable:
                        values[department_field] = department_name
                    created = self.bitable.create_record(table.table_id, values)
                    record_id = str(created.get("record_id", ""))
                    if not record_id:
                        raise ValueError("统计行创建后缺少记录 ID")
                    rows[record_id] = {"record_id": record_id, "fields": values}
                    self.store.map_external_record(target_date, key + ":DETAIL",
                                                   "check_details", record_id)
                written += 1
            except Exception as exc:
                issues[f"detail:{key}:write-failed"] = (
                    f"「{person.name}」（{person.person_id}）统计写入失败：{error_summary(exc)}")
        self.store.set_status(target_date, "PARTIAL" if issues else "COMPLETED")
        return {"target_date": str(target_date), "status": "partial" if issues else "complete",
                "expected": len(scopes), "written": written, "issues": issues}

    @staticmethod
    def _legacy_scope_key(target_date: date, key: str,
                          people: dict, backbone_departments: dict[str, list[str]],
                          ) -> str | None:
        """v2 单部门骨干的旧 MEMBERS key，可安全复用其统计行。"""
        prefix = f"{target_date}:MEMBERS:"
        if not key.startswith(prefix):
            return None
        rest = key[len(prefix):]
        if ":" not in rest:
            return None
        department_id, backbone_id = rest.split(":", 1)
        departments = backbone_departments.get(backbone_id, [])
        if len(departments) > 1:
            person = people.get(backbone_id)
            if person is None or person.department_id != department_id:
                return None
        return f"{target_date}:MEMBERS:{backbone_id}"
