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
        eligible = {pid: person for pid, person in people.items()
                    if person.active and person.role in {"基层学生", "骨干学生"}}
        submitted = {log.person_id for log in logs
                     if log.submitted_at.astimezone(SHANGHAI).date() == target_date}
        scopes: list[tuple[str, str | None, set[str]]] = []
        for pid, person in sorted(eligible.items()):
            if person.role == "骨干学生":
                members = {pid} | {member_id for member_id, member in eligible.items()
                    if member.leader_id == pid and member.department_id == person.department_id}
                scopes.append((f"{target_date}:MEMBERS:{pid}", pid, members))
        for department in sorted(organization.departments, key=lambda item: item.department_id):
            if department.active:
                scopes.append((f"{target_date}:DEPARTMENT:{department.department_id}",
                    department.minister_id, {pid for pid, person in eligible.items()
                        if person.department_id == department.department_id}))
        scopes.append((f"{target_date}:TEAM", organization.team_leader_id, set(eligible)))
        rows = {str(row["record_id"]): row for row in self.bitable.list_records(table.table_id)}
        self.store.get_or_create_workflow(target_date)
        has_legacy = self.legacy_store.load_workflow(target_date) is not None
        fields = table.fields
        reported_at = int(datetime.now().timestamp() * 1000)
        issues: dict[str, str] = {}
        assigned_ministers = {department.minister_id for department in organization.departments
                              if department.active}
        for person in people.values():
            if person.active and person.role == "部长" and person.person_id not in assigned_ministers:
                issues[f"detail:minister:{person.person_id}:missing-department"] = (
                    f"「{person.name}」（{person.person_id}）未配置负责的有效部门，无法生成部门统计")
        written = 0
        for key, reporter_id, members in scopes:
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
                    if changes:
                        self.bitable.update_record(table.table_id, record_id, changes)
                else:
                    values = {**counts, fields["reporter_ref"]: [{"id": person.open_id}],
                              fields["role"]: person.role, fields["reported_at"]: reported_at}
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
