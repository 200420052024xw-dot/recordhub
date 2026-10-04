from __future__ import annotations

import unittest

from data.repositories import OrganizationRepository
from schema import TableConfig


class FakeBitable:
    def __init__(self, people):
        self.people = people

    def list_records(self, table_id):
        return self.people if table_id == "persons" else []


class FakeContacts:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = []

    def batch_get_ids(self, *, mobiles, user_id_type):
        self.calls.append((list(mobiles), user_id_type))
        return [
            {"mobile": mobile, "user_id": "ou_" + mobile}
            for mobile in mobiles if mobile not in self.missing
        ]


def config():
    return TableConfig.model_validate({"tables": {
        "departments": {"table_id": "departments", "fields": {
            "department_id": "department_id", "name": "name",
            "minister_ref": "minister_ref"}},
        "persons": {"table_id": "persons", "fields": {
            "person_id": "person_id", "name": "name", "role": "role",
            "leader_ref": "leader_ref", "minister_ref": "minister_ref",
            "department_ref": "department_ref", "mobile": "mobile"}},
    }})


class MobileIdentityTests(unittest.TestCase):
    def test_mobile_is_resolved_before_organization_is_cached(self):
        records = [{"record_id": "r1", "fields": {
            "person_id": "P1", "name": "Alice", "role": "member",
            "mobile": {"texts": [{"text": "13800000001"}]}}}]
        contacts = FakeContacts()
        organization = OrganizationRepository(
            FakeBitable(records), contacts, config()
        ).load()
        self.assertEqual(contacts.calls, [(["13800000001"], "open_id")])
        self.assertEqual(organization.persons[0].open_id, "ou_13800000001")

    def test_unresolved_mobile_is_skipped(self):
        records = [{"record_id": "r1", "fields": {
            "person_id": "P1", "name": "Alice", "role": "member",
            "mobile": "13800000001"}}]
        organization = OrganizationRepository(
            FakeBitable(records), FakeContacts({"13800000001"}), config()
        ).load()
        self.assertEqual(organization.persons, [])

    def test_missing_mobile_is_skipped_before_lookup(self):
        records = [{"record_id": "r1", "fields": {
            "person_id": "P1", "name": "Alice", "role": "member"}}]
        contacts = FakeContacts()
        organization = OrganizationRepository(
            FakeBitable(records), contacts, config()
        ).load()
        self.assertEqual(organization.persons, [])
        self.assertEqual(contacts.calls, [])

    def test_lookup_is_chunked_at_fifty(self):
        records = [{"record_id": str(i), "fields": {
            "person_id": f"P{i}", "name": f"Person {i}", "role": "member",
            "mobile": f"138{i:08d}"}} for i in range(51)]
        contacts = FakeContacts()
        organization = OrganizationRepository(
            FakeBitable(records), contacts, config()
        ).load()
        self.assertEqual([len(mobiles) for mobiles, _ in contacts.calls], [50, 1])
        self.assertEqual(len(organization.persons), 51)


if __name__ == "__main__":
    unittest.main()
