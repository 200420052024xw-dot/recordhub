from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone

from data.store import FileStateStore
from schema import (Department, Membership, Organization, Person,
                    TableConfig, TableDefinition, WorkLog)
from workflow1.submission_statistics import SubmissionStatisticsRepository


class FakeBitable:
    def __init__(self):
        self.rows = []

    def list_records(self, table_id):
        return list(self.rows)

    def list_fields(self, table_id):
        return [{"field_name": "文本", "type": 1},
                {"field_name": "部门", "type": 1}]

    def create_record(self, table_id, values):
        record_id = f"rec{len(self.rows)}"
        self.rows.append({"record_id": record_id, "fields": dict(values)})
        return {"record_id": record_id}

    def update_record(self, table_id, record_id, values):
        for row in self.rows:
            if row["record_id"] == record_id:
                row["fields"].update(values)
        return {}


def config():
    return TableConfig(tables={"check_details": TableDefinition(
        table_id="tbl", fields={
            "title": "文本", "department": "部门", "reporter_ref": "报告人",
            "role": "角色", "reported_at": "报告时间",
            "submitted": "已交人数", "missing": "未交人数",
        })})


def organization():
    xu = Person(person_id="XU", name="徐婉婷", role="骨干学生",
                department_id="D1", leader_id="M1", open_id="ou_xu",
                memberships=[
                    Membership(department_id="D1", role="骨干学生",
                               leader_id="M1", primary=True),
                    Membership(department_id="D2", role="骨干学生", leader_id="M2"),
                ])
    return Organization(
        persons=[
            xu,
            Person(person_id="M1", name="李梦成", role="部长",
                   department_id="D1", open_id="ou_m1"),
            Person(person_id="M2", name="廖云燕", role="部长",
                   department_id="D2", open_id="ou_m2"),
            Person(person_id="TL", name="黄箐", role="团队负责人", open_id="ou_tl"),
        ],
        departments=[
            Department(department_id="D1", name="李梦成部门", minister_id="M1"),
            Department(department_id="D2", name="廖云燕部门", minister_id="M2"),
        ],
        team_leader_id="TL",
    )


class SubmissionStatisticsTests(unittest.TestCase):
    def test_cross_department_counts(self):
        day = date(2026, 10, 29)
        log = WorkLog(log_id="L1", submitted_at=datetime(2026, 10, 29, 4,
                      tzinfo=timezone.utc), person_id="XU",
                      progress="p", difficulties="d", reflection="r")
        with tempfile.TemporaryDirectory() as root:
            bitable = FakeBitable()
            repo = SubmissionStatisticsRepository(
                bitable, config(), FileStateStore(root))
            result = repo.publish(day, organization(), [log])
        self.assertEqual(result["expected"], 5)  # 2 MEMBERS + 2 DEPARTMENT + TEAM
        self.assertEqual(result["written"], 5)
        titles = sorted(row["fields"]["文本"] for row in bitable.rows)
        self.assertIn("李梦成部门·徐婉婷小组", titles)
        self.assertIn("廖云燕部门·徐婉婷小组", titles)
        self.assertIn("团队", titles)
        # Each department counts Xu once; team counts Xu once.
        department_rows = [row for row in bitable.rows
                           if row["fields"]["文本"] in {"李梦成部门", "廖云燕部门"}]
        self.assertEqual([row["fields"]["已交人数"] for row in department_rows], [1, 1])
        team = next(row for row in bitable.rows if row["fields"]["文本"] == "团队")
        self.assertEqual(team["fields"]["已交人数"], 1)
        self.assertEqual(team["fields"]["未交人数"], 0)
        department_values = sorted({row["fields"]["部门"] for row in bitable.rows})
        self.assertEqual(department_values, ["团队", "廖云燕部门", "李梦成部门"])


if __name__ == "__main__":
    unittest.main()
