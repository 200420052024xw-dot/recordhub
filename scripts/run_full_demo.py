"""End-to-end demo runner using synthetic organization + 100 synthetic logs.

The Feishu persons table is incomplete, so the demo works around it by:
  - building a valid synthetic Organization in memory;
  - saving it as the OrganizationCache (overrides the live Feishu read);
  - generating 100 logs over 5 days;
  - saving a DailySnapshot per day in a dedicated state directory.

All side-effects are still real:
  - DeepSeek calls (real);
  - evaluations/report rows written to the live evaluations / reports /
    check_details tables;
  - Workflow2 stage/monthly/weekly rows written to the live workflow2 tables;
  - cloud documents created in the archive folder;
  - IM messages sent to RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID.

Usage:
    python scripts/run_full_demo.py --prepare-only
    python scripts/run_full_demo.py --workflow1
    python scripts/run_full_demo.py --stage 2026-10-05
    python scripts/run_full_demo.py --monthly 2026-10-01
    python scripts/run_full_demo.py --weekly 2026-10-05
    python scripts/run_full_demo.py --all
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file, load_schedule_config
from data import FileStateStore
from data.store import utc_now
from schema import (Department, LogResource, Organization, Person, WorkLog,
                    SubmissionStatus, CloudObject, WorkflowRun)
from service.runtime import build_runtime
from workflow1.documents import DailyDocuments
from workflow1.evaluations import AiEvaluationRepository, HumanEvaluationRepository
from workflow1.models import (DailySnapshot, EvaluatorProgress, LogEvaluationState,
                              UnitStatus, WorkflowStatus)
from workflow1.reports import WorkflowReportRepository
from workflow1.settings import W1Settings
from workflow1.snapshot import SnapshotBuilder
from workflow1.workflow import Workflow1
from workflow2.materials import MaterialPreparer
from workflow2.feishu_material import SnapshotRebuilder

SHANGHAI = ZoneInfo("Asia/Shanghai")

DAYS = [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2),
        date(2026, 10, 3), date(2026, 10, 4)]
LOGS_PER_DAY = 20
DEMO_STATE_ROOT = ROOT / "data" / "state" / "sim_demo"
DEMO_DAY = DAYS[-1]


# --------------------------------------------------------------------------- #
# Synthetic organization: 1 team leader, 3 ministers, 6 backbones, 24 basic.  #
# --------------------------------------------------------------------------- #

def _build_organization() -> Organization:
    persons: list[Person] = []
    persons.append(Person(person_id="T01", name="刘老师",
                          role="团队负责人", open_id="ou_f68fbaa465973654a4615c133ac04680"))
    ministers = [
        Person(person_id=f"M0{i+1}", name=f"部长{i+1}",
               role="部长", department_id=f"D0{i+1}",
               leader_id="T01",
               open_id="ou_f68fbaa465973654a4615c133ac04680")
        for i in range(3)
    ]
    persons.extend(ministers)
    backbones: list[Person] = []
    for d_index in range(3):
        for b_index in range(2):
            pid = f"B{d_index+1}{b_index+1}"
            person = Person(person_id=pid, name=f"骨干{d_index}{b_index}",
                            role="骨干学生",
                            department_id=f"D0{d_index+1}",
                            leader_id=f"M0{d_index+1}",
                            open_id="ou_f68fbaa465973654a4615c133ac04680")
            backbones.append(person)
            persons.append(person)
    basic_per_backbone = 4
    for backbone in backbones:
        for offset in range(basic_per_backbone):
            pid = f"{backbone.person_id.lower()}{offset+1:02d}"
            person = Person(person_id=pid, name=f"{backbone.name}-成员{offset+1}",
                            role="基层学生",
                            department_id=backbone.department_id,
                            leader_id=backbone.person_id)
            persons.append(person)
    departments = [
        Department(department_id=f"D0{i+1}", name=f"模拟部门{i+1}",
                    minister_id=f"M0{i+1}")
        for i in range(3)
    ]
    return Organization(persons=persons, departments=departments,
                        team_leader_id="T01", anomalies=[])


# --------------------------------------------------------------------------- #
# Synthetic logs over 5 days                                                  #
# --------------------------------------------------------------------------- #

TOPICS = ["S01 skill 改造", "事件上报通路", "Wiki 整理", "值班例行", "AI 评价抽样",
          "云文档归档", "Skill 复盘", "日报优化", "新人 Onboarding", "评审材料"]
PROGRESS = [
        "梳理本周剩余工作项并按优先级整理成清单，与组员确认各自拆分的目标。",
        "完成 {topic} 文档初稿，针对三处模糊措辞做了细化并附上代码片段。",
        "梳理 {topic} 流程中五个常见失败模式，更新组内 Wiki 的排查页。",
        "将 {topic} 工具封装为可复用脚本，提交了配套 README 与最小测试。",
        "完成 {topic} 实验数据复盘，补齐缺失的指标定义并标注后续改进方向。",
        "整理 {topic} 培训材料初稿，配合一份现场提问清单用于例会分享。",
        "协助组员 {topic} 工作，跟踪进度并整理了一份风险与依赖清单。",
        "复盘 {topic} 项目阶段产出，更新导出需要重点关注的两个事项。",
        "草拟 {topic} 后续计划，包含三项可执行任务和对应负责人。",
    ]
DIFFICULTIES = [
        "对接口返回字段含义不清晰，反复询问后才确认边界条件。",
        "复现某个偶发问题时缺少必要的本地日志，需要重新走查调用链。",
        "同事本周状态紧张，部分沟通被推迟，影响了预期节奏。",
        "工具环境偶发卡顿，需要重启并清理临时文件，影响约一小时。",
        "需求侧未敲定上下文优先级，需要追加一次确认会议。",
    ]
REFLECTIONS = [
        "应在每天开始前列出最重要两项，否则容易被即时消息打断节奏。",
        "今日发现书面约定容易遗漏，需要把约定落到 Wiki 而非口头。",
        "提前估时的颗粒度可以再细一些，便于周五复盘逐项核实。",
        "对 {topic} 的依赖关系没有先画图，导致中途返工。",
    ]
OTHERS = [
        "已预约明天上午再与 {topic} 负责人同步。",
        "周五例会前会汇总本周共性结论。",
        "下周计划补一篇 {topic} 的工作小结。",
        "明日请假半天，工作已交接给值班同学。",
    ]


def _sample(seed: int) -> dict[str, str]:
    rng = random.Random(seed)
    topic = rng.choice(TOPICS)
    return {
        "progress": rng.choice(PROGRESS).format(topic=topic),
        "difficulties": rng.choice(DIFFICULTIES).format(topic=topic),
        "reflection": rng.choice(REFLECTIONS).format(topic=topic),
        "other": rng.choice(OTHERS).format(topic=topic),
        "topic": topic,
    }


def _build_logs_for_day(day: date, organization: Organization) -> list[WorkLog]:
    students = [p for p in organization.persons
                if p.role in {"基层学生", "骨干学生"} and p.active]
    rng = random.Random(int(str(day.toordinal()) + "991"))
    rng.shuffle(students)
    chosen = students[:LOGS_PER_DAY]
    while len(chosen) < LOGS_PER_DAY:
        chosen.append(rng.choice(students))
    logs: list[WorkLog] = []
    for index, person in enumerate(chosen):
        s = _sample(day.toordinal() * 100 + index)
        log_id = f"log-{day.isoformat()}-{index:02d}"
        logs.append(WorkLog(
            log_id=log_id,
            source_record_id=f"src-{day.isoformat()}-{index:02d}",
            person_id=person.person_id,
            submitted_at=datetime.combine(day, time(18 + (index % 4),
                                                    5 + (index * 7 % 50)),
                             SHANGHAI),
            progress="【模拟测试】" + s["progress"],
            difficulties="【模拟测试】" + s["difficulties"],
            reflection="【模拟测试】" + s["reflection"],
            # Unique suffix keeps the composed log text distinct so the AI
            # evaluation table never sees two different people share one log.
            other="【模拟测试】" + s["other"] + f"（模拟日志 {log_id}）",
            full_log="",
        ))
    return logs


# --------------------------------------------------------------------------- #
# State preparation: organization + per-day snapshots                        #
# --------------------------------------------------------------------------- #

class DemoMessages:
    def __init__(self, live_messages) -> None:
        self.live_messages = live_messages
        self.batch_id = str(uuid4())

    def send_text(self, open_id: str, message: str, *, idempotency_key=None):
        return self.live_messages.send_text(
            open_id, "【模拟测试】\n" + message,
            idempotency_key=f"demo:{self.batch_id}:{idempotency_key or uuid4()}")


def _prepare(folder: str) -> dict[str, Any]:
    organization = _build_organization()
    store = FileStateStore(folder, workflow_type="workflow1",
                            snapshot_model=DailySnapshot)
    store.save_cache("organization", organization.model_dump(mode="json"))
    snapshots: list[DailySnapshot] = []
    for day in DAYS:
        run = store.get_or_create_workflow(day)
        logs = _build_logs_for_day(day, organization)
        snapshot = SnapshotBuilder().build(
            workflow_run_id=run.workflow_run_id, target_date=day,
            organization=organization, logs=logs, now=utc_now())
        store.save_snapshot(snapshot)
        snapshots.append(snapshot)
    return {"snapshots": len(snapshots), "persons": len(organization.persons),
            "departments": len(organization.departments),
            "state_dir": folder}


# --------------------------------------------------------------------------- #
# Workflow1 demo                                                                #
# --------------------------------------------------------------------------- #

class BypassLogRepository:
    """Provides logs straight from the cached snapshot."""

    def __init__(self, snapshot_by_date):
        self._by_date = snapshot_by_date

    def get_logs_by_date(self, target_date, organization=None):
        snapshot = self._by_date.get(target_date)
        if snapshot is None:
            return [], {}
        return snapshot.logs, {}

    def backfill_full_logs(self, logs):
        return 0


def _build_workflow1(folder: str, settings: AppSettings,
                     runtime) -> tuple[Workflow1, dict[date, DailySnapshot]]:
    snapshots = {}
    store = FileStateStore(folder, workflow_type="workflow1",
                            snapshot_model=DailySnapshot)
    for day in DAYS:
        snap = store.load_snapshot(day)
        if snap is None:
            raise ValueError(f"快照缺失:{day},先 prepare")
        snapshots[day] = snap
    snapshot_by_date = {snap.target_date: snap for snap in snapshots.values()}

    schedules = load_schedule_config(settings.schedule_config_path)
    w1 = W1Settings.from_schedule(schedules)
    log_repo = BypassLogRepository(snapshot_by_date)
    from config.tables import load_table_config
    table_cfg = load_table_config(settings.table_config_path)
    ai = AiEvaluationRepository(runtime.infrastructure.bitable, table_cfg, store)
    human = HumanEvaluationRepository(runtime.infrastructure.bitable, table_cfg,
                                       cutoff_at=w1.auto_advance_at)
    reports = WorkflowReportRepository(runtime.infrastructure.bitable,
                                        table_cfg, store)
    docs = DailyDocuments(runtime.infrastructure.cloud_docs, store,
                          settings.archive_parent_folder_token)
    messages = DemoMessages(runtime.infrastructure.messages)
    from llm import PromptService
    prompt_service = PromptService(runtime.infrastructure.llm,
                                    max_attempts=settings.llm_max_attempts)
    workflow = Workflow1(
        store=store,
        organization_cache=runtime.organization_cache,
        log_repository=log_repo,
        ai_evaluations=ai,
        human_evaluations=human,
        prompt_service=prompt_service,
        messages=messages,
        documents=docs,
        reports=reports,
        llm_concurrency=settings.llm_concurrency,
        auto_advance_at=w1.auto_advance_at,
        notify_at=w1.notify_at,
        minister_review_form_url=w1.minister_review_form_url,
        backbone_review_form_url=w1.backbone_review_form_url,
        admin_open_id="",
    )
    return workflow, snapshot_by_date


def _ensure_org_cache_loaded(runtime, folder: str):
    from data import FileStateStore
    from schema import Organization
    cache_file = Path(folder) / "organization.json"
    if not cache_file.exists():
        raise ValueError("organization cache missing; run --prepare first")
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    runtime.organization_cache.store.save_cache("organization",
                                                  data["payload"])
    return Organization.model_validate(data["payload"])


def run_workflow1_phase(folder: str, finalize: bool) -> dict:
    load_env_file()
    settings = replace(AppSettings.from_env(), simulation_mode=True)
    runtime = build_runtime(settings)
    _ensure_org_cache_loaded(runtime, folder)
    workflow, snapshots = _build_workflow1(folder, settings, runtime)
    results = []
    for day, snapshot in snapshots.items():
        try:
            run = workflow.start(day)
            results.append({"date": day.isoformat(),
                            "status": run.status,
                            "issues": dict(snapshot.issues or {}),
                            "log_count": len(snapshot.logs)})
        except Exception as exc:
            results.append({"date": day.isoformat(), "status": "FAILED",
                            "error": str(exc)})
    finalize_result = None
    if finalize:
        for day in snapshots:
            try:
                finalize_result = workflow.finalize_pending_confirmations(day)
            except Exception as exc:
                finalize_result = {"date": day.isoformat(),
                                    "error": str(exc)}
    return {"phase": "workflow1", "days": results,
            "finalize": finalize_result}


# --------------------------------------------------------------------------- #
# Workflow2 demo                                                              #
# --------------------------------------------------------------------------- #

def _prepare_workflow2(folder: str) -> dict:
    """Materialize the 5-day daily snapshots as CONFIRMED workflow1 slots
    so workflow2 material preparation accepts them."""
    from data import FileStateStore
    store = FileStateStore(folder, workflow_type="workflow1",
                            snapshot_model=DailySnapshot)
    for day in DAYS:
        snapshot = store.load_snapshot(day)
        if snapshot is None:
            raise ValueError(f"missing snapshot for {day}")
        for state in snapshot.log_evaluations.values():
            state.status = UnitStatus.CONFIRMED
            if not state.positive_ai:
                state.positive_ai = state.positive_final = "演示数据肯定"
                state.improvement_ai = state.improvement_final = "演示数据改进"
            state.ai_evaluated_at = state.evaluated_at = state.confirmed_at = utc_now()
            state.source = state.source or "AI"
        for progress in snapshot.evaluators.values():
            progress.closed = True
        snapshot.evaluations_published = True
        store.save_snapshot(snapshot)
        store.set_status(day, WorkflowStatus.COMPLETED)
    return {"days": [d.isoformat() for d in DAYS]}


def run_workflow2_phase(folder: str, kind: str, scheduled: date) -> dict:
    load_env_file()
    settings = replace(AppSettings.from_env(), simulation_mode=True,
                       state_dir=folder)
    runtime = build_runtime(settings)
    _ensure_org_cache_loaded(runtime, folder)
    _prepare_workflow2(folder)
    workflow2 = runtime.workflow2
    if workflow2 is None:
        return {"phase": f"workflow2.{kind}", "status": "skipped",
                "reason": "Workflow2 未启用"}
    workflow2.messages = DemoMessages(workflow2.messages)
    try:
        run = workflow2.start(kind, scheduled)
        return {"phase": f"workflow2.{kind}",
                "scheduled": scheduled.isoformat(),
                "status": run.status,
                "issues": run.issues,
                "last_error": run.last_error}
    except Exception as exc:
        return {"phase": f"workflow2.{kind}",
                "scheduled": scheduled.isoformat(),
                "status": "FAILED",
                "error": str(exc)}


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--workflow1", action="store_true")
    parser.add_argument("--stage", type=date.fromisoformat)
    parser.add_argument("--monthly", type=date.fromisoformat)
    parser.add_argument("--weekly", type=date.fromisoformat)
    parser.add_argument("--all", action="store_true",
                        help="prepare + workflow1 + stage(2026-10-05) + monthly(2026-10-01) + weekly(2026-10-05)")
    parser.add_argument("--no-finalize", dest="finalize",
                        action="store_false")
    parser.add_argument("--state-dir", default=str(DEMO_STATE_ROOT))
    args = parser.parse_args()
    folder = args.state_dir
    Path(folder).mkdir(parents=True, exist_ok=True)

    summary: dict = {"state_dir": folder}
    if args.prepare_only or args.all or args.workflow1 or args.stage \
            or args.monthly or args.weekly:
        if not (Path(folder) / "organization.json").exists():
            summary["prepare"] = _prepare(folder)
        else:
            summary["prepare"] = {"status": "already-prepared"}
    if args.workflow1 or args.all:
        summary["workflow1"] = run_workflow1_phase(folder, args.finalize)
    if args.all or args.stage:
        summary["stage"] = run_workflow2_phase(
            folder, "stage", args.stage or date(2026, 10, 5))
    if args.all or args.monthly:
        summary["monthly"] = run_workflow2_phase(
            folder, "monthly", args.monthly or date(2026, 10, 1))
    if args.all or args.weekly:
        summary["weekly"] = run_workflow2_phase(
            folder, "weekly", args.weekly or date(2026, 10, 5))
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())