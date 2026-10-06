from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace

from data.repositories import LogRepository, OrganizationRepository
from schema import Department, Organization, Person, SubmissionStatus, TableConfig, WorkLog
from workflow1.evaluations import AiEvaluationRepository, HumanEvaluationRepository
from workflow1.models import DailySnapshot, LogEvaluationState, UnitStatus
from workflow2.feishu_material import SnapshotRebuilder


DAY = date(2026, 10, 4)
NOW = datetime(2026, 10, 5, 2, tzinfo=UTC)


class Bitable:
    def __init__(self, rows):
        self.rows = rows
        self.created = []

    def list_records(self, table_id, **kwargs):
        return self.rows.get(table_id, [])

    def batch_create(self, table_id, fields):
        self.created.extend((table_id, row) for row in fields)
        return [{"record_id": f"rec{i}", "fields": row}
                for i, row in enumerate(fields, 1)]


class ExternalStudentFlowTests(unittest.TestCase):
    def setUp(self):
        self.organization = Organization(persons=[
            Person(person_id="B", name="骨干", role="骨干学生", open_id="ou_b",
                   department_id="D", leader_id="T"),
            Person(person_id="S", name="基层", role="基层学生", open_id=None,
                   department_id="D", leader_id="B"),
        ], departments=[Department(department_id="D", name="一部", minister_id="T")])
        self.config = TableConfig.model_validate({"tables": {
            "logs": {"table_id": "logs", "fields": {
                "log_id": "自动编号", "submitted_at": "提交时间",
                "submitter_name": "姓名：", "submitter_ref": "提交人",
                "progress": "工作进展：", "difficulties": "工作困难：",
                "reflection": "心得反思：", "other": "其他：", "full_log": "完整日志"}},
            "evaluations": {"table_id": "ai", "fields": {
                "evaluation_id": "业务编号", "business_key": "业务编号",
                "person_ref": "被评价日志提交人", "person_name": "被评价人姓名",
                "source_log": "工作日志", "evaluated_at": "评价时间",
                "positive_ai": "肯定之处_AI", "improvement_ai": "改进之处_AI"}},
            "human_evaluations": {"table_id": "human", "fields": {
                "evaluation_id": "评价编号", "basic_name": "被审核基层：",
                "backbone_ref": "被审核骨干：", "submitted_by": "填写人",
                "source_log": "日志原文",
                "evaluated_at": "评价时间", "positive_final": "肯定之处_人工",
                "improvement_final": "改进之处_人工",
                "positive_confirmed": "肯定之处_AI是否确认",
                "improvement_confirmed": "需改进之处_AI是否确认："}},
        }})

    def test_basic_student_without_open_id_is_kept(self):
        people = [{"record_id": "s", "fields": {
            "人员编号": "S", "姓名": "基层", "角色": "基层学生", "直属上级": "B"}},
            {"record_id": "b", "fields": {
                "人员编号": "B", "姓名": "骨干", "角色": "骨干学生",
                "手机号": "13800000000"}}]
        tables = TableConfig.model_validate({"tables": {
            "persons": {"table_id": "persons", "fields": {
                "person_id": "人员编号", "name": "姓名", "role": "角色",
                "leader_ref": "直属上级", "minister_ref": "本部部长",
                "department_ref": "所属部门", "mobile": "手机号"}},
            "departments": {"table_id": "departments", "fields": {
                "department_id": "部门编号", "name": "部门名称",
                "minister_ref": "部门部长"}},
        }})
        contacts = SimpleNamespace(batch_get_ids=lambda **kwargs: [
            {"mobile": "13800000000", "user_id": "ou_b"}])
        organization = OrganizationRepository(
            Bitable({"persons": people, "departments": []}), contacts, tables).load()
        self.assertEqual({p.person_id for p in organization.persons}, {"B", "S"})
        self.assertIsNone(organization.person_map()["S"].open_id)

    def test_name_log_and_human_review_without_student_open_id(self):
        rows = {"logs": [{"record_id": "log1", "fields": {
            "自动编号": "L1", "提交时间": "2026-10-04T10:00:00+08:00",
            "姓名：": "基层", "工作进展：": "完成任务"}}], "ai": []}
        bitable = Bitable(rows)
        repository = LogRepository(bitable, self.config,
            SimpleNamespace(get=lambda: self.organization))
        logs, issues = repository.get_logs_by_date(DAY)
        self.assertEqual([log.person_id for log in logs], ["S"])
        self.assertEqual(issues, {})
        state = LogEvaluationState(log_id="L1", person_id="S", evaluator_id="B",
            status=UnitStatus.WAITING_CONFIRMATION, positive_ai="肯定", improvement_ai="建议")
        snapshot = DailySnapshot(workflow_run_id="run", target_date=DAY,
            organization=self.organization, logs=logs,
            submission_status={"S": SubmissionStatus(person_id="S", submitted=True,
                                                       log_id="L1", log_ids=["L1"])},
            log_evaluations={"L1": state}, created_at=NOW, updated_at=NOW)
        mapped, issues = AiEvaluationRepository(
            bitable, self.config, SimpleNamespace()).publish(snapshot)
        self.assertEqual(issues, {})
        self.assertEqual(bitable.created[0][1]["被评价人姓名"], "基层")
        self.assertNotIn("被评价日志提交人", bitable.created[0][1])
        state.evaluation_id = mapped["L1"][1]
        human = {"record_id": "human1", "fields": {
            "评价编号": "PJ-1", "被审核基层：": "基层",
            "填写人": [{"id": "ou_b"}],
            "评价时间": "2026-10-05T09:00:00+08:00",
            "肯定之处_AI是否确认": "确认无误",
            "需改进之处_AI是否确认：": "需修改",
            "改进之处_人工": "具体建议"}}
        parsed = HumanEvaluationRepository(bitable, self.config).parse(snapshot, human)
        self.assertEqual(parsed[1].person_id, "S")
        self.assertEqual(parsed[1].improvement, "具体建议")

    def test_duplicate_basic_name_is_not_guessed(self):
        self.organization.persons.append(Person(
            person_id="S2", name="基层", role="基层学生", leader_id="B"))
        bitable = Bitable({"logs": [{"record_id": "log1", "fields": {
            "自动编号": "L1", "提交时间": "2026-10-04T10:00:00+08:00",
            "姓名：": "基层"}}]})
        logs, issues = LogRepository(bitable, self.config,
            SimpleNamespace(get=lambda: self.organization)).get_logs_by_date(DAY)
        self.assertEqual(logs, [])
        self.assertIn("log:L1:unknown-submitter", issues)
        self.assertTrue(any("姓名" in anomaly and "重复" in anomaly
                            for anomaly in OrganizationRepository._find_anomalies(
                                self.organization.departments, self.organization.persons)))

    def test_rebuilder_matches_numbered_human_row_by_name_and_source_log(self):
        source = "工作进展:完成任务"
        ai = {"record_id": "ai1", "fields": {
            "业务编号": "2026-10-04:EVAL:B:L1", "被评价人姓名": "基层",
            "工作日志": source, "肯定之处_AI": "肯定", "改进之处_AI": "建议"}}
        human = {"record_id": "human1", "fields": {
            "评价编号": "PJ-1", "被审核基层：": "基层",
            "填写人": [{"id": "ou_b"}], "日志原文": source,
            "评价时间": "2026-10-05T09:00:00+08:00",
            "肯定之处_AI是否确认": "确认无误",
            "需改进之处_AI是否确认：": "需修改",
            "改进之处_人工": "具体建议"}}
        bitable = Bitable({"ai": [ai], "human": [human]})
        rebuilder = SnapshotRebuilder(bitable, self.config,
            SimpleNamespace(get=lambda: self.organization),
            SimpleNamespace(), auto_advance_at="12:00")
        ai_rows = rebuilder._read_ai_rows(self.organization)
        human_rows = rebuilder._read_human_rows(self.organization, ai_rows)
        self.assertEqual(ai_rows[(DAY, "L1")]["person_id"], "S")
        self.assertEqual(human_rows[(DAY, "L1")]["improvement"], "具体建议")

    def test_backbone_review_keeps_person_field(self):
        repository = HumanEvaluationRepository(Bitable({}), self.config)
        backbone = self.organization.person_map()["B"]
        self.assertTrue(repository._matches_person(
            {"被审核骨干：": [{"id": "ou_b"}]},
            self.config.tables["human_evaluations"].fields, backbone))
        self.assertFalse(repository._matches_person(
            {"被审核基层：": "骨干"},
            self.config.tables["human_evaluations"].fields, backbone))


if __name__ == "__main__":
    unittest.main()
