"""权限：查询人能查谁。只读 Organization，不改动任何既有解析逻辑。"""

from __future__ import annotations

from schema.domain import Organization, Person

from service.workbuddy_query.models import (
    OUT_OF_SCOPE,
    TARGET_NOT_FOUND,
    QueryError,
    Target,
)

ROLE_BASIC = "基层学生"
ROLE_BACKBONE = "骨干学生"
ROLE_MINISTER = "部长"
ROLE_LEADER = "团队负责人"

SCOPE_BY_ROLE = {
    ROLE_BASIC: "self",
    ROLE_BACKBONE: "group",
    ROLE_MINISTER: "department",
    ROLE_LEADER: "all",
}


def scope_of(role: str) -> str:
    return SCOPE_BY_ROLE.get(role, "self")


class ScopeResolver:
    """把「能查谁」和「这次查谁」都算成一组 person_id。"""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization
        self._people = organization.person_map()
        self._subordinates = organization.subordinates()

    def allowed_ids(self, person: Person) -> set[str]:
        role = person.role
        if role == ROLE_LEADER:
            return {item.person_id for item in self.organization.persons if item.active}
        if role == ROLE_MINISTER:
            same_department = {
                item.person_id for item in self.organization.persons
                if item.active and item.department_id == person.department_id}
            return same_department | {person.person_id}
        if role == ROLE_BACKBONE:
            return self.direct_reports(person) | {person.person_id}
        return {person.person_id}

    def direct_reports(self, person: Person) -> set[str]:
        return {item.person_id
                for item in self._subordinates.get(person.person_id, [])}

    def department_ids(self, person_ids: set[str]) -> set[str]:
        return {self._people[pid].department_id
                for pid in person_ids
                if pid in self._people and self._people[pid].department_id}

    def resolve_target(self, target: Target | None, querier: Person) -> set[str]:
        target = target or Target()
        if target.type == "self":
            ids = {querier.person_id}
        elif target.type == "person":
            ids = self._one_person(target.name)
        elif target.type == "group":
            ids = self._group(target.name)
        else:
            ids = self._department(target.name)
        if not ids <= self.allowed_ids(querier):
            raise QueryError(OUT_OF_SCOPE)
        return ids

    def _one_person(self, name: str | None) -> set[str]:
        matches = self.organization.persons_named(name or "")
        if len(matches) != 1:
            raise QueryError(TARGET_NOT_FOUND)
        return {matches[0].person_id}

    def _group(self, name: str | None) -> set[str]:
        matches = [item for item in self.organization.persons_named(name or "")
                   if item.role == ROLE_BACKBONE]
        if len(matches) != 1:
            raise QueryError(TARGET_NOT_FOUND)
        backbone = matches[0]
        return self.direct_reports(backbone) | {backbone.person_id}

    def _department(self, name: str | None) -> set[str]:
        wanted = (name or "").strip()
        matches = [item for item in self.organization.departments
                   if wanted and item.active and item.name.strip() == wanted]
        if len(matches) != 1:
            raise QueryError(TARGET_NOT_FOUND)
        department_id = matches[0].department_id
        return {item.person_id for item in self.organization.persons
                if item.active and item.department_id == department_id}
