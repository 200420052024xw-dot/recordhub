from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from schema import Department, Organization, Person, SubmissionStatus, WorkLog
from workflow1.evaluations import ai_key, human_key, parse_evaluation_key
from workflow1.migration import migrate_snapshot
from workflow1.models import (
    DailySnapshot, EvaluatorProgress, LogEvaluationState, UnitStatus,
)


class KeyTests(unittest.TestCase):
    def test_keys_round_trip(self):
        day = date(2026, 10, 29)
        self.assertEqual(ai_key(day, "L1"), "2026-10-29:AI:L1")
        self.assertEqual(human_key(day, "M1", "L1"), "2026-10-29:HUMAN:M1:L1")
        self.assertEqual(parse_evaluation_key("2026-10-29:AI:L1"), (day, None, "L1"))
        self.assertEqual(parse_evaluation_key("2026-10-29:HUMAN:M1:L1"),
                         (day, "M1", "L1"))
        self.assertEqual(parse_evaluation_key("2026-10-29:EVAL:M1:L1"),
                         (day, "M1", "L1"))
        self.assertIsNone(parse_evaluation_key("2026-10-29:MISSING:XU"))
        self.assertIsNone(parse_evaluation_key(""))


class MigrationTests(unittest.TestCase):
    def test_v2_snapshot_migrates_to_units(self):
        org = Organization(
            persons=[
                Person(person_id="A", name="张三", role="基层学生",
                       department_id="D", leader_id="B"),
                Person(person_id="B", name="李四", role="骨干学生",
                       department_id="D", leader_id="C"),
                Person(person_id="C", name="王五", role="部长", department_id="D"),
            ],
            departments=[Department(department_id="D", name="D部门", minister_id="C")],
        )
        log = WorkLog(log_id="L1", submitted_at=datetime.now(timezone.utc),
                      person_id="A")
        snapshot = DailySnapshot(
            schema_version=2, workflow_run_id="R", target_date=date(2026, 10, 29),
            organization=org, logs=[log],
            submission_status={"A": SubmissionStatus(
                person_id="A", submitted=True, log_id="L1", log_ids=["L1"])},
            log_evaluations={"L1": LogEvaluationState(
                log_id="L1", person_id="A", evaluator_id="B",
                status=UnitStatus.CONFIRMED, positive_ai="ai+",
                improvement_ai="ai-", positive_final="h+",
                improvement_final="h-", source="HUMAN")},
            evaluators={"B": EvaluatorProgress(
                evaluator_id="B", log_ids=["L1"], closed=True)},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        migrated = migrate_snapshot(snapshot)
        self.assertEqual(migrated.schema_version, 3)
        self.assertEqual(list(migrated.evaluation_units), ["L1:B"])
        unit = migrated.evaluation_units["L1:B"]
        self.assertEqual(unit.department_ids, ["D"])
        self.assertEqual((unit.source, unit.positive_final), ("HUMAN", "h+"))
        self.assertEqual(migrated.ai_evaluations["L1"].positive_ai, "ai+")
        self.assertEqual(migrated.evaluators["B"].unit_keys, ["L1:B"])


if __name__ == "__main__":
    unittest.main()
