from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone

from schema import Department, Membership, Organization, Person, WorkLog
from workflow1.models import DailySnapshot
from workflow1.roster import RosterStore


class RosterArchiveTests(unittest.TestCase):
    def test_round_trip_preserves_memberships(self):
        xu = Person(person_id="XU", name="徐婉婷", role="骨干学生",
                    department_id="D1", leader_id="M1", memberships=[
                        Membership(department_id="D1", role="骨干学生",
                                   leader_id="M1", primary=True),
                        Membership(department_id="D2", role="骨干学生",
                                   leader_id="M2"),
                    ])
        org = Organization(
            persons=[xu],
            departments=[
                Department(department_id="D1", name="D1部门", minister_id="M1"),
                Department(department_id="D2", name="D2部门", minister_id="M2"),
            ],
            team_leader_id=None,
        )
        day = date(2026, 10, 29)
        snapshot = DailySnapshot(
            workflow_run_id="R", target_date=day, organization=org, logs=[],
            submission_status={}, created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc))
        with tempfile.TemporaryDirectory() as root:
            store = RosterStore(root)
            store.save(snapshot)
            loaded = store.load_organization(day)
        self.assertIsNotNone(loaded)
        restored = loaded.person_map()["XU"]
        self.assertEqual([(m.department_id, m.leader_id, m.primary)
                          for m in restored.memberships],
                         [("D1", "M1", True), ("D2", "M2", False)])


if __name__ == "__main__":
    unittest.main()
