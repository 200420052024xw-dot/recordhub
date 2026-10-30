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

_ROLE_ORDER = (ROLE_LEADER, ROLE_MINISTER, ROLE_BACKBONE, ROLE_BASIC)


def scope_of(role: str) -> str:
    return SCOPE_BY_ROLE.get(role, "self")


def member_roles(person: Person) -> set[str]:
    """All roles a person holds across memberships (primary role included)."""
    roles = {item.role for item in person.membership_list()}
    if person.role:
        roles.add(person.role)
    return roles


def widest_scope(person: Person) -> str:
    """Top-level scope: the widest among all membership roles."""
    roles = member_roles(person)
    for role in _ROLE_ORDER:
        if role in roles:
            return SCOPE_BY_ROLE[role]
    return "self"


class ScopeResolver:
    """把「能查谁」和「这次查谁」都算成一组 person_id。跨部门的人按全部成员关系取并集。"""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization
        self._people = organization.person_map()
        self._subordinates = organization.subordinates()

    def allowed_ids(self, person: Person) -> set[str]:
        roles = member_roles(person)
        if ROLE_LEADER in roles:
            return {item.person_id for item in self.organization.persons if item.active}
        ids = {person.person_id}
        if ROLE_MINISTER in roles:
            for department in self.organization.departments:
                if department.minister_id == person.person_id and department.active:
                    ids |= {item.person_id
                            for item, _membership in self.organization.members_in_department(
                                department.department_id)}
            for membership in person.membership_list():
                if membership.role == ROLE_MINISTER:
                    ids |= {item.person_id
                            for item, _membership in self.organization.members_in_department(
                                membership.department_id)}
        if ROLE_BACKBONE in roles:
            ids |= self.direct_reports(person)
        return ids

    def direct_reports(self, person: Person) -> set[str]:
        return {item.person_id
                for item in self._subordinates.get(person.person_id, [])}

    def department_ids(self, person_ids: set[str]) -> set[str]:
        departments: set[str] = set()
        for pid in person_ids:
            departments.update(self.organization.departments_of(pid))
        return departments

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
                   if ROLE_BACKBONE in member_roles(item)]
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
        return {item.person_id
                for item, _membership in self.organization.members_in_department(
                    matches[0].department_id)}
