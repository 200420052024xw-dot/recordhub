from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from data import FileStateStore
from data.repositories import OrganizationCache
from schema import Department, Organization, Person


def sample_organization() -> Organization:
    return Organization(
        persons=[
            Person(person_id="LEADER", name="负责人", role="团队负责人"),
            Person(person_id="M1", name="部长甲", role="部长", department_id="001"),
            Person(person_id="B1", name="骨干甲", role="骨干学生",
                   department_id="001", leader_id="M1", open_id="ou_b1"),
            Person(person_id="S1", name="成员甲", role="基层学生",
                   department_id="001", leader_id="B1", open_id="ou_s1"),
        ],
        departments=[Department(department_id="001", name="部门甲", minister_id="M1")],
        team_leader_id="LEADER",
    )


def offline_repository() -> Mock:
    """A repository that fails loudly if the cache ever hits Feishu again."""
    repository = Mock()
    repository.load.side_effect = AssertionError("人员表不应该被重新读取")
    return repository


class OrganizationCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileStateStore(Path(self.tmp.name) / "state")
        seeding = Mock()
        seeding.load.return_value = sample_organization()
        self.cache = OrganizationCache(self.store, seeding)
        self.cache.get()
        self.assertEqual(seeding.load.call_count, 1)

    def fresh(self) -> OrganizationCache:
        return OrganizationCache(self.store, offline_repository())

    def test_get_reads_persisted_cache_without_touching_feishu(self):
        organization = self.fresh().get()
        self.assertEqual([p.person_id for p in organization.persons],
                         ["LEADER", "M1", "B1", "S1"])
        self.assertEqual(organization.persons[2].open_id, "ou_b1")

    def test_get_keeps_open_ids_that_fail_validation(self):
        cache = self.fresh()
        cache.update_person("S1", {"open_id": "not-an-open-id"})
        organization = self.fresh().get()
        person = next(p for p in organization.persons if p.person_id == "S1")
        self.assertEqual(person.open_id, "not-an-open-id")

    def test_add_persists_and_recomputes_team_leader(self):
        organization = self.fresh().add_person(
            Person(person_id="B2", name="骨干乙", role="骨干学生",
                   department_id="001", leader_id="M1", open_id="ou_b2"))
        self.assertIn("B2", [p.person_id for p in organization.persons])
        self.assertEqual(organization.anomalies, [])
        self.assertEqual(self.fresh().get().team_leader_id, "LEADER")

    def test_add_duplicate_person_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.fresh().add_person(Person(person_id="M1", name="重复", role="骨干学生"))

    def test_update_mutates_fields_and_persists(self):
        self.fresh().update_person("B1", {"role": "部长", "department_id": None})
        person = next(p for p in self.fresh().get().persons if p.person_id == "B1")
        self.assertEqual(person.role, "部长")
        self.assertIsNone(person.department_id)

    def test_update_missing_person_raises_lookup(self):
        with self.assertRaises(LookupError):
            self.fresh().update_person("NOPE", {"role": "部长"})

    def test_delete_removes_person_and_flags_dangling_references(self):
        organization = self.fresh().delete_person("B1")
        self.assertNotIn("B1", [p.person_id for p in organization.persons])
        self.assertTrue(any("成员甲" in item and "S1" in item
                            for item in organization.anomalies))
        self.assertNotIn("B1",
                         [p.person_id for p in self.fresh().get().persons])

    def test_delete_missing_person_raises_lookup(self):
        with self.assertRaises(LookupError):
            self.fresh().delete_person("NOPE")

    def test_second_team_leader_clears_team_leader_id(self):
        organization = self.fresh().add_person(
            Person(person_id="LEADER2", name="负责人乙", role="团队负责人"))
        self.assertIsNone(organization.team_leader_id)
        self.assertIn("必须且只能配置一名团队负责人", organization.anomalies)


if __name__ == "__main__":
    unittest.main()
