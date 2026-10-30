from __future__ import annotations

import unittest

from data.repositories import OrganizationRepository


def record(record_id, **fields):
    return {"record_id": record_id, "fields": fields}


DEPARTMENTS = {
    "department_id": "部门编号", "name": "部门名称",
    "minister_ref": "部门部长", "backbone_refs": "部门骨干",
}
PERSONS = {
    "person_id": "人员编号", "name": "姓名", "role": "角色",
    "leader_ref": "直属上级", "minister_ref": "本部部长",
    "department_ref": "所属部门", "mobile": "手机号",
}
MEMBERSHIPS = {
    "person_ref": "人员编号", "name": "姓名", "role": "角色",
    "leader_ref": "直属上级", "minister_ref": "本部部长",
    "department_ref": "所属部门", "mobile": "手机号",
}


def build(membership_records):
    departments = [
        record("r_d1", **{"部门编号": "D1", "部门名称": "D1部门",
                          "部门部长": "M1", "部门骨干": ""}),
        record("r_d2", **{"部门编号": "D2", "部门名称": "D2部门",
                          "部门部长": "M2", "部门骨干": ""}),
    ]
    persons = [
        record("r_xu", **{"人员编号": "XU", "姓名": "徐婉婷", "角色": "骨干学生",
                          "直属上级": "M1", "本部部长": "M1", "所属部门": "D1",
                          "手机号": "+861" }),
        record("r_m1", **{"人员编号": "M1", "姓名": "李梦成", "角色": "部长",
                          "直属上级": "", "本部部长": "M1", "所属部门": "D1",
                          "手机号": "+862"}),
        record("r_m2", **{"人员编号": "M2", "姓名": "廖云燕", "角色": "部长",
                          "直属上级": "", "本部部长": "M2", "所属部门": "D2",
                          "手机号": "+863"}),
    ]
    return OrganizationRepository._parse_organization(
        DEPARTMENTS, departments, PERSONS, persons,
        membership_fields=MEMBERSHIPS, membership_records=membership_records,
        use_person_field_ids=True)


class OrganizationLoadingTests(unittest.TestCase):
    def test_extra_department_does_not_duplicate_person(self):
        departments, persons = build([
            record("r_xu2", **{"人员编号": "XU", "姓名": "徐婉婷",
                               "角色": "骨干学生", "直属上级": "M2",
                               "本部部长": "M2", "所属部门": "D2",
                               "手机号": "+861"}),
        ])
        self.assertEqual(len(persons), 3)
        xu = next(item for item in persons if item.person_id == "XU")
        self.assertEqual(len(xu.memberships), 2)
        self.assertEqual(
            [(m.department_id, m.leader_id, m.primary) for m in xu.memberships],
            [("D1", "M1", True), ("D2", "M2", False)])

    def test_single_department_fallback(self):
        departments, persons = build([])
        xu = next(item for item in persons if item.person_id == "XU")
        self.assertEqual(len(xu.memberships), 1)
        self.assertTrue(xu.memberships[0].primary)


if __name__ == "__main__":
    unittest.main()
