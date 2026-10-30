from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from schema import Department, Membership, Organization, Person, WorkLog
from workflow1.final_evaluation import render_final_evaluation
from workflow1.models import UnitStatus
from workflow1.snapshot import SnapshotBuilder
from workflow1.units import unit_key, units_for_log


def make_org():
    xu = Person(person_id="XU", name="徐婉婷", role="骨干学生",
                department_id="D1", leader_id="M1", memberships=[
                    Membership(department_id="D1", role="骨干学生",
                               leader_id="M1", primary=True),
                    Membership(department_id="D2", role="骨干学生", leader_id="M2"),
                ])
    return Organization(persons=[
        xu,
        Person(person_id="M1", name="李梦成", role="部长", department_id="D1"),
        Person(person_id="M2", name="廖云燕", role="部长", department_id="D2"),
    ], departments=[
        Department(department_id="D1", name="李梦成部门", minister_id="M1"),
        Department(department_id="D2", name="廖云燕部门", minister_id="M2"),
    ])


def make_snapshot():
    org = make_org()
    log = WorkLog(log_id="L1", submitted_at=datetime(2026, 10, 29, 4, tzinfo=timezone.utc),
                  person_id="XU", progress="进展", difficulties="困难", reflection="反思")
    return SnapshotBuilder().build(
        workflow_run_id="R1", target_date=date(2026, 10, 29),
        organization=org, logs=[log], now=datetime.now(timezone.utc))


class SnapshotUnitTests(unittest.TestCase):
    def test_one_log_two_evaluators_one_ai(self):
        snap = make_snapshot()
        self.assertEqual(len(snap.evaluation_units), 2)
        self.assertEqual(len(snap.ai_evaluations), 1)
        self.assertEqual(len(units_for_log(snap, "L1")), 2)

    def test_same_evaluator_across_departments_shares_one_unit(self):
        org = make_org()
        xu = org.persons[0]
        xu.memberships[1].leader_id = "M1"
        m1 = next(item for item in org.persons if item.person_id == "M1")
        m1.memberships = [
            Membership(department_id="D1", role="部长",
                       leader_id=None, primary=True),
            Membership(department_id="D2", role="部长", leader_id=None),
        ]
        log = WorkLog(log_id="L2", submitted_at=datetime(2026, 10, 29, 4, tzinfo=timezone.utc),
                      person_id="XU", progress="p", difficulties="d", reflection="r")
        snap = SnapshotBuilder().build(
            workflow_run_id="R2", target_date=date(2026, 10, 29),
            organization=org, logs=[log], now=datetime.now(timezone.utc))
        self.assertEqual(list(snap.evaluation_units), ["L2:M1"])
        self.assertEqual(snap.evaluation_units["L2:M1"].department_ids, ["D1", "D2"])


class FinalEvaluationTests(unittest.TestCase):
    def test_all_ai_writes_one_marked_segment(self):
        snap = make_snapshot()
        ai = snap.ai_evaluations["L1"]
        ai.positive_ai = "AI肯定"
        ai.improvement_ai = "AI改进"
        for unit in snap.evaluation_units.values():
            unit.status = UnitStatus.WAITING_CONFIRMATION
        text = render_final_evaluation(snap, "L1")
        self.assertEqual(text.count("AI肯定"), 1)
        self.assertEqual(text.count("（AI未确认）"), 1)

    def test_one_human_one_ai(self):
        snap = make_snapshot()
        ai = snap.ai_evaluations["L1"]
        ai.positive_ai = "AI肯定"
        ai.improvement_ai = "AI改进"
        human = snap.evaluation_units[unit_key("L1", "M1")]
        human.source = "HUMAN"
        human.status = UnitStatus.CONFIRMED
        human.positive_final = "人工肯定"
        human.improvement_final = "人工改进"
        text = render_final_evaluation(snap, "L1")
        self.assertIn("人工肯定", text)
        self.assertIn("AI肯定", text)
        self.assertEqual(text.count("（AI未确认）"), 1)

    def test_all_human_has_no_ai(self):
        snap = make_snapshot()
        ai = snap.ai_evaluations["L1"]
        ai.positive_ai = "AI肯定"
        ai.improvement_ai = "AI改进"
        for index, unit in enumerate(snap.evaluation_units.values()):
            unit.source = "HUMAN"
            unit.status = UnitStatus.CONFIRMED
            unit.positive_final = f"人工肯定{index}"
            unit.improvement_final = f"人工改进{index}"
        text = render_final_evaluation(snap, "L1")
        self.assertNotIn("AI未确认", text)
        self.assertNotIn("AI肯定", text)
        self.assertEqual(text.count("人工肯定"), 2)

    def test_ai_failure(self):
        snap = make_snapshot()
        ai = snap.ai_evaluations["L1"]
        ai.fallback = "failed"
        for unit in snap.evaluation_units.values():
            unit.status = UnitStatus.WAITING_CONFIRMATION
        self.assertEqual(render_final_evaluation(snap, "L1"), "评价生成失败")


if __name__ == "__main__":
    unittest.main()
