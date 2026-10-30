"""Per-day organization roster archive.

The Feishu source tables are mutable: a person leaving a department deletes a
membership row.  Historical rebuilds must still use the organization as it was
on that day, so Workflow1 freezes a small roster alongside every snapshot.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from schema import Department, Membership, Organization, Person
from data.store import FileStateStore


class RosterStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir) / "rosters"

    def path(self, day: date) -> Path:
        return self.root / f"{day.isoformat()}.json"

    def exists(self, day: date) -> bool:
        return self.path(day).exists()

    def save(self, snapshot) -> None:
        organization = snapshot.organization
        payload = {
            "schema_version": 1,
            "target_date": snapshot.target_date.isoformat(),
            "departments": [
                {
                    "department_id": item.department_id,
                    "name": item.name,
                    "minister_id": item.minister_id,
                    "active": item.active,
                }
                for item in organization.departments
            ],
            "persons": [
                {
                    "person_id": person.person_id,
                    "name": person.name,
                    "role": person.role,
                    "department_id": person.department_id,
                    "leader_id": person.leader_id,
                    "open_id": person.open_id,
                    "mobile": person.mobile,
                    "source_record_id": person.source_record_id,
                    "memberships": [
                        {
                            "department_id": item.department_id,
                            "role": item.role,
                            "leader_id": item.leader_id,
                            "primary": item.primary,
                        }
                        for item in person.membership_list()
                    ],
                }
                for person in organization.persons
            ],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        FileStateStore._atomic_write(self.path(snapshot.target_date), payload)

    def load_organization(self, day: date) -> Organization | None:
        path = self.path(day)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        departments = [Department.model_validate(item)
                       for item in payload.get("departments", [])]
        persons = []
        for item in payload.get("persons", []):
            data = dict(item)
            data["memberships"] = [Membership.model_validate(one)
                                   for one in data.get("memberships", [])]
            persons.append(Person.model_validate(data))
        return Organization(
            persons=persons, departments=departments,
            team_leader_id=next((person.person_id for person in persons
                                 if person.role == "团队负责人"), None),
        )
