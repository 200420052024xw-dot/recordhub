from __future__ import annotations

import unittest

from data.repositories import OrganizationRepository


class OrganizationMobileParsingTests(unittest.TestCase):
    def test_parse_organization_populates_mobile_and_open_id(self):
        person_fields = {
            "person_id": "人员编号",
            "name": "姓名",
            "role": "角色",
            "leader_ref": "直属上级",
            "minister_ref": "本部部长",
            "department_ref": "所属部门",
            "mobile": "手机号",
        }
        department_fields = {
            "department_id": "部门编号",
            "name": "部门名称",
            "minister_ref": "部门部长",
        }
        person_records = [{
            "record_id": "rec1",
            "fields": {
                "人员编号": "B1",
                "姓名": "杨天宇",
                "角色": "骨干学生",
                "所属部门": "杨阳蕊部门",
                "直属上级": [{"id": "ou_minister"}],
                "手机号": "+8615870637343",
            },
        }]
        departments, persons = OrganizationRepository._parse_organization(
            department_fields, [],
            person_fields, person_records,
            mobile_open_ids={"+8615870637343": "ou_b1"},
        )
        self.assertEqual(len(persons), 1)
        self.assertEqual(persons[0].mobile, "+8615870637343")
        self.assertEqual(persons[0].open_id, "ou_b1")
