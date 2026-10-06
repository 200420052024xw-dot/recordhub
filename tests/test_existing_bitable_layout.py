from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from config import load_table_config
from data import FileStateStore
from data.repositories import LogRepository, OrganizationRepository
from schema import (
    Organization,
    Person,
    TableConfig,
)


class StaticOrganizationCache:
    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    def get(self) -> Organization:
        return self.organization


class StaticBitable:
    def __init__(self, records):
        self.records = records

    def list_records(self, table_id, **kwargs):
        return self.records


# 原 config/feishu_tables.example.toml 已删除；此内嵌模板是表注册中心的唯一
# 参照：六张基础服务表 + Workflow1 输出表 + S04-S09 预留表。新建部署时按它
# 手写 config/tables.toml（该文件不入库）。
_TABLE_REGISTRY_TOML = '''
# Person-type fields (提交人/评价人/被评价日志提交人/报告人 etc.) are read and
# written as OpenID. The application resolves each person's 手机号 to OpenID
# through contact/v3/users/batch_get_id when refreshing the organization cache.

[tables.departments]
table_id = ""
[tables.departments.fields]
department_id = "部门编号"
minister_ref = "部门部长"
backbone_refs = "部门骨干"
name = "部门名称"

[tables.persons]
table_id = ""
[tables.persons.fields]
person_id = "人员编号"
name = "姓名"
role = "角色"
leader_ref = "直属上级"
minister_ref = "本部部长"
department_ref = "所属部门"
remark = "备注"
mobile = "手机号"

[tables.logs]
table_id = ""
[tables.logs.fields]
log_id = "自动编号"
submitted_at = "提交时间"
submitter_ref = "提交人"
submitter_name = "姓名："
progress = "工作进展："
difficulties = "工作困难："
reflection = "心得反思："
other = "其他："
full_log = "完整日志"

[tables.evaluations]
table_id = ""
[tables.evaluations.fields]
evaluation_id = "评价编号"
person_ref = "被评价日志提交人"
person_name = "被评价人姓名"
source_log = "工作日志"
evaluated_at = "评价时间"
positive_ai = "肯定之处_AI"
improvement_ai = "改进之处_AI"

[tables.human_evaluations]
table_id = ""
[tables.human_evaluations.fields]
evaluation_id = "评价编号"
basic_name = "被审核基层："
backbone_ref = "被审核骨干："
source_log = "日志原文"
evaluated_at = "评价时间"
positive_final = "肯定之处_人工"
improvement_final = "改进之处_人工"
submitted_by = "填写人"

[tables.check_details]
table_id = ""
[tables.check_details.fields]
title = "文本"
reporter_ref = "报告人"
role = "角色"
reported_at = "报告时间"
submitted = "已交人数"
missing = "未交认数"

[tables.reports]
table_id = ""
[tables.reports.fields]
title = "文本"
reporter_ref = "报告人"
role = "角色"
reported_at = "报告时间"

[tables.prompts]
table_id = ""
[tables.prompts.fields]
config_id = "文本"
user_ref = "使用人"
role = "角色"
template = "分析skill"
modified_at = "修改日期"

[tables.department_analysis]
table_id = ""
[tables.department_analysis.fields]

[tables.team_analysis]
table_id = ""
[tables.team_analysis.fields]
'''


class ExistingBitableLayoutTests(unittest.TestCase):
    def test_table_registry_covers_split_evaluation_tables(self) -> None:
        populated = _TABLE_REGISTRY_TOML.replace('table_id = ""', 'table_id = "tbl_test"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tables.toml"
            path.write_text(populated, encoding="utf-8")
            config = load_table_config(path)

        self.assertEqual(
            set(config.tables),
            {
                "departments",
                "persons",
                "logs",
                "evaluations",
                "human_evaluations",
                "check_details",
                "reports",
                "prompts",
                "department_analysis",
                "team_analysis",
            },
        )

    def test_relation_fields_resolve_to_stable_person_ids(self) -> None:
        department_fields = {
            "department_id": "部门编号",
            "name": "部门名称",
            "minister_ref": "部门部长",
            "backbone_refs": "部门骨干",
        }
        person_fields = {
            "person_id": "人员编号",
            "name": "姓名",
            "role": "角色",
            "leader_ref": "直属上级",
            "minister_ref": "本部部长",
            "department_ref": "所属部门",
            "remark": "备注",
            "mobile": "手机号",
        }
        departments, persons = OrganizationRepository._parse_organization(
            department_fields,
            [
                {
                    "record_id": "rec_department",
                    "fields": {
                        "部门编号": "SW-001",
                        "部门名称": "一部",
                        "部门部长": [{"record_id": "rec_minister"}],
                    },
                }
            ],
            person_fields,
            [
                {
                    "record_id": "rec_leader",
                    "fields": {
                        "人员编号": "P001",
                        "姓名": "负责人",
                        "角色": "团队负责人",
                        "手机号": "13800000001",
                    },
                },
                {
                    "record_id": "rec_minister",
                    "fields": {
                        "人员编号": "P002",
                        "姓名": "部长",
                        "角色": "部长",
                        "直属上级": [{"record_id": "rec_leader"}],
                        "所属部门": [{"record_id": "rec_department"}],
                    },
                },
                {
                    "record_id": "rec_backbone",
                    "fields": {
                        "人员编号": "P003",
                        "姓名": "骨干",
                        "角色": "骨干学生",
                        "本部部长": [{"record_id": "rec_minister"}],
                        "所属部门": "SW-001",
                    },
                },
            ],
            {"13800000001": "ou_leader"},
        )

        by_id = {person.person_id: person for person in persons}
        self.assertEqual(departments[0].minister_id, "P002")
        self.assertEqual(by_id["P002"].leader_id, "P001")
        self.assertEqual(by_id["P003"].leader_id, "P002")
        self.assertEqual(by_id["P003"].department_id, "SW-001")
        self.assertEqual(by_id["P001"].open_id, "ou_leader")

    def test_log_submitter_relation_resolves_to_person_id(self) -> None:
        organization = Organization(
            persons=[
                Person(
                    person_id="P003",
                    name="骨干",
                    role="骨干学生",
                    source_record_id="rec_backbone",
                )
            ],
            departments=[],
        )
        table_config = TableConfig.model_validate(
            {
                "tables": {
                    "logs": {
                        "table_id": "tbl_logs",
                        "fields": {
                            "log_id": "自动编号",
                            "submitted_at": "提交时间",
                            "submitter_ref": "提交人",
                            "progress": "工作进展：",
                            "difficulties": "工作困难：",
                            "reflection": "心得反思：",
                            "other": "其他：",
                            "full_log": "完整日志",
                        },
                    }
                }
            }
        )
        repository = LogRepository(
            StaticBitable(
                [
                    {
                        "record_id": "rec_log",
                        "fields": {
                            "自动编号": "L001",
                            "提交时间": "2026-10-03T12:00:00+08:00",
                            "提交人": [{"record_id": "rec_backbone"}],
                            "工作进展：": "完成测试",
                        },
                    }
                ]
            ),
            table_config,
            StaticOrganizationCache(organization),
        )

        logs, issues = repository.get_logs_by_date(date(2026, 10, 3))
        self.assertEqual(logs[0].person_id, "P003")
        self.assertEqual(issues, {})



if __name__ == "__main__":
    unittest.main()
