from __future__ import annotations

import unittest

from schema import Membership, Organization, Person


def person(pid, name, role, department_id, leader_id, memberships=None):
    return Person(person_id=pid, name=name, role=role,
                  department_id=department_id, leader_id=leader_id,
                  memberships=memberships or [])


class OrganizationMembershipTests(unittest.TestCase):
    def test_legacy_person_synthesizes_primary_membership(self):
        p = person("P1", "张三", "基层学生", "D1", "B1")
        self.assertEqual(p.membership_list(), [
            Membership(department_id="D1", role="基层学生",
                       leader_id="B1", primary=True)])

    def test_memberships_query_across_departments(self):
        xu = person("XU", "徐婉婷", "骨干学生", "D1", "M1", [
            Membership(department_id="D1", role="骨干学生",
                       leader_id="M1", primary=True),
            Membership(department_id="D2", role="骨干学生", leader_id="M2"),
        ])
        org = Organization(persons=[xu], departments=[])
        self.assertEqual(org.departments_of("XU"), ["D1", "D2"])
        self.assertEqual(org.evaluator_for("XU", "D2"), "M2")
        self.assertEqual(len(org.members_in_department("D2")), 1)
        self.assertEqual(org.members_in_department("D2")[0][1].leader_id, "M2")

    def test_subordinates_dedupe_same_person(self):
        xu = person("XU", "徐婉婷", "骨干学生", "D1", "M1", [
            Membership(department_id="D1", role="骨干学生",
                       leader_id="M1", primary=True),
            Membership(department_id="D2", role="骨干学生", leader_id="M1"),
        ])
        org = Organization(persons=[xu], departments=[])
        self.assertEqual([p.person_id for p in org.subordinates()["M1"]], ["XU"])


if __name__ == "__main__":
    unittest.main()
