"""Business appointments; no interval job starts incomplete workflows."""
from datetime import date, datetime, time, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo
import logging

from data.store import utc_now
from service.execution import Appointments, RecordedGeneration
from workflow1.models import UnitStatus
from workflow1.workflow import Workflow1
from workflow2.periods import due, material_window
from workflow2.models import CycleRun
from workflow2.workflow import SCHEDULES, CODES

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, runtime, schedules, scheduler, settings):
        self.runtime, self.schedules, self.settings = runtime, schedules, settings
        self.zone = ZoneInfo(schedules.timezone)
        self.tasks = Appointments(settings.state_dir, scheduler, self.handle,
                                  retry_minutes=settings.llm_retry_interval_minutes,
                                  retry_limit=settings.llm_retry_limit)
        for binding in runtime.workflows.values():
            workflow = binding.workflow
            workflow.batch_limit = settings.llm_concurrency
            workflow.statistics_scheduled = (isinstance(workflow, Workflow1)
                and getattr(workflow, "statistics", None) is not None)
            workflow.prompt_service = RecordedGeneration(getattr(workflow, "prompt_service", None), settings.state_dir,
                                                         self.tasks.has_first_attempt)
        if runtime.workflow2:
            runtime.workflow2.prompt_service = RecordedGeneration(runtime.workflow2.prompt_service,
                settings.state_dir, self.tasks.has_first_attempt)
        # Definite report rejection can schedule a delivery-only retry.
        policies = [getattr(b.workflow, "notifications", None) for b in runtime.workflows.values()]
        policies.append(getattr(runtime.workflow2, "notifications", None))
        for policy in policies:
            if policy is not None:
                policy.retry_callback = self.delivery_failed

    def moment(self, day, clock):
        return datetime.combine(day, time.fromisoformat(clock), self.zone)

    def book(self, action, payload, when, *, lane="control", expires=None, suffix="", retry=False):
        identity = ":".join(str(payload.get(k, "")) for k in ("binding", "kind", "day"))
        return self.tasks.book(f"{action}:{identity}:{suffix}", action, payload, when,
                               lane=lane, expires=expires, retry=retry)

    def plan_day(self, day=None):
        day = day or utc_now().astimezone(self.zone).date()
        review = self.schedules.workflows.get("skill_prompt_review")
        if self.runtime.prompt_repository is not None and review and review.enabled:
            self.book("skill_review", dict(day=str(day)), self.moment(day, review.time),
                      lane="ai", expires=self.moment(day + timedelta(days=1), "00:00"))
        for name, binding in self.runtime.workflows.items():
            schedule = self.schedules.workflows[name]
            if not schedule.enabled:
                continue
            target = day - timedelta(days=int(schedule.options.get("material_days_ago", 1)))
            payload = dict(binding=name, day=str(target))
            cutoff = self.moment(day, binding.auto_advance_at) if binding.auto_advance_at else None
            end = self.moment(day + timedelta(days=1), "00:00")
            if binding.workflow.statistics_scheduled:
                self.book("daily_statistics", payload, self.moment(day, schedule.time), expires=end)
            self.book("daily_generate", payload, self.moment(day, schedule.time),
                      lane="ai", expires=cutoff or end)
            if clock := schedule.options.get("notify_at"):
                self.book("daily_review", payload, self.moment(day, clock), expires=cutoff or end)
            if cutoff:
                self.book("daily_close", payload, cutoff, expires=end)
            # 对话入口的批改结果：接口截止后先合并进飞书表，21:50 定稿时一起读。
            review = self.schedules.workflows.get("workbuddy_review")
            if review is not None and getattr(self.runtime, "review", None) is not None:
                self.book("review_merge", payload, self.moment(day, review.time),
                          expires=cutoff or end)
            if clock := schedule.options.get("report_notify_at"):
                self.book("daily_send", payload, self.moment(day, clock), expires=end)
        periodic = self.runtime.workflow2
        if self.settings.workflow2_enabled and periodic is not None:
            for kind, name in SCHEDULES.items():
                schedule = self.schedules.workflows[name]
                if kind == "stage":
                    activation = periodic.store.activate(day)
                    first = activation + timedelta(days=int(schedule.options["startup_wait_complete_days"]) + 1)
                    eligible = schedule.enabled and day >= first and (day - first).days % int(schedule.options["every_days"]) == 0
                else:
                    eligible = due(schedule, day)
                if eligible:
                    self.plan_cycle(kind, day)

    def plan_cycle(self, kind, day):
        schedule = self.schedules.workflows[SCHEDULES[kind]]
        payload = dict(kind=kind, day=str(day))
        options = schedule.options
        send_day = day + timedelta(days=int(options.get("report_notify_days", 0)))
        send_at = self.moment(send_day, options.get("report_notify_at") or options["notify_at"])
        end = self.moment(send_day + timedelta(days=1), "00:00")
        if end <= utc_now():
            return  # Never reconstruct expired historical appointments.
        cutoff = (self.moment(day + timedelta(days=int(options.get("confirmation_days", 0))),
                              options["confirmation_time"]) if kind != "weekly" else send_at)
        self.book("cycle_generate", payload, self.moment(day, schedule.time), lane="ai", expires=cutoff)
        if kind != "weekly":
            self.book("cycle_review", payload, self.moment(day, options["notify_at"]), expires=cutoff)
            self.book("cycle_close", payload, cutoff, expires=end)
        self.book("cycle_send", payload, send_at, expires=end)

    def restore(self):
        # Recover appointments first, including month-end work spanning two days.
        self.plan_day()
        if self.runtime.workflow2:
            for run in self.runtime.workflow2.store.pending():
                self.plan_cycle(run.kind, run.scheduled_date)
        self.tasks.restore(startup=True)

    def delivery_failed(self, item):
        day = date.fromisoformat(item["send_date"])
        when = utc_now() + timedelta(minutes=10)
        if when.astimezone(self.zone).date() != day:
            return
        payload = {"notification_key": item["key"], "day": item["target_date"]}
        self.book("delivery_retry", payload, when,
                  expires=self.moment(day + timedelta(days=1), "00:00"),
                  suffix=f"{item['key']}:{item['attempts']}", retry=True)

    def cycle(self, payload):
        workflow = self.runtime.workflow2
        kind, day = payload["kind"], date.fromisoformat(payload["day"])
        run_id = str(uuid5(NAMESPACE_URL, f"recordhub-workflow2:{kind}:{day}"))
        run = workflow.store.load(run_id)
        if run is None:
            start, end = material_window(workflow._schedule(kind), day)
            run = CycleRun(run_id=run_id, kind=kind, scheduled_date=day, start_date=start,
                           end_date=end, created_at=utc_now(), updated_at=utc_now())
            workflow.store.save(run)
        return workflow, run

    def handle(self, task):
        action, payload = task["action"], task["payload"]
        day = date.fromisoformat(payload["day"])
        if action == "skill_review":
            self.runtime.prompt_repository.review_pending(self.runtime.organization_cache.get())
            if getattr(self.runtime.prompt_repository, "deferred_count", 0):
                raise RuntimeError("个人 Skill 审核因接口故障未完成")
            return
        if action == "review_merge":
            self.runtime.review.merge(day)
            return
        if action == "delivery_retry":
            policies = [getattr(b.workflow, "notifications", None) for b in self.runtime.workflows.values()]
            policies.append(getattr(self.runtime.workflow2, "notifications", None))
            policy = next(p for p in policies if p is not None)
            policy.retry_report(payload["notification_key"], self.runtime.infrastructure.messages)
            return
        if action.startswith("daily_"):
            workflow = self.runtime.workflows[payload["binding"]].workflow
            if action == "daily_statistics":
                result = workflow.refresh_submission_statistics(day)
                # Read warnings alone do not need another write. Retry failed
                # reads or incomplete writes through this task's own budget.
                if result["status"] == "failed" or result.get("written", 0) < result.get("expected", 0):
                    raise RuntimeError(f"提交人数统计未完成：{result.get('issues', {})}")
            elif action == "daily_generate":
                workflow.prompt_service.context = f"daily:{day}"
                workflow.prompt_service.deadline = datetime.fromisoformat(task["expires"]) if task["expires"] else None
                if payload.get("logs"):
                    def reopen(snapshot):
                        for key in payload["logs"]:
                            state = snapshot.ai_evaluations.get(key)
                            if state is not None and state.status == UnitStatus.FAILED:
                                state.status, state.error, state.retry_after = UnitStatus.PENDING, None, None
                            for unit in snapshot.evaluation_units.values():
                                if unit.log_id == key and unit.status == UnitStatus.FAILED:
                                    unit.status, unit.error = UnitStatus.PENDING, None
                    workflow.store.update_snapshot(day, reopen)
                result = workflow.start(day)
                if result.status == "FAILED":
                    raise ValueError(result.last_error)
                snapshot = workflow.store.load_snapshot(day)
                if snapshot:
                    pending = [s.log_id for s in snapshot.ai_evaluations.values()
                               if s.status in {UnitStatus.PENDING, UnitStatus.RUNNING}]
                    if pending:
                        self.book("daily_generate", dict(payload, logs=pending), utc_now(), lane="ai",
                                  expires=workflow.prompt_service.deadline, suffix="batch:" + ",".join(pending), retry=True)
                    for state in snapshot.ai_evaluations.values():
                        if state.status == UnitStatus.FAILED and state.retry_after:
                            when = state.retry_after
                            self.book("daily_generate", dict(payload, logs=[state.log_id]), when, lane="ai",
                                      expires=workflow.prompt_service.deadline,
                                      suffix=f"log:{state.log_id}:retry:{state.retry_count}", retry=True)
            elif action == "daily_review":
                workflow.notify_evaluators_if_due(day)
            elif action == "daily_close":
                if workflow.store.load_workflow(day) is None:
                    workflow.store.get_or_create_workflow(day)
                result = workflow.finalize_pending_confirmations(day)
                if result.get("errors"):
                    raise ValueError(result["errors"])
            else:
                workflow.notify_documents_if_due(day)
                snapshot = workflow.store.load_snapshot(day)
                if snapshot is None or not any(o.content_written for o in snapshot.cloud_objects.values()):
                    self.send_error(workflow, day, "日报生成错误：暂无可用报告链接")
            return
        workflow, run = self.cycle(payload)
        if action == "cycle_generate":
            workflow.prompt_service.context = run.run_id
            workflow.prompt_service.deadline = datetime.fromisoformat(task["expires"])
            result = workflow.resume(run.run_id)
            if result.status in {"FAILED", "BLOCKED"}:
                raise ValueError(result.last_error)
        elif action == "cycle_close":
            result = workflow.resume(run.run_id)
            if result.status in {"FAILED", "BLOCKED"}:
                raise ValueError(result.last_error)
        elif action == "cycle_review":
            workflow.notify_due(run.kind, day)
        else:
            if run.kind == "weekly" and run.status != "COMPLETED":
                for code in CODES["weekly"]:
                    run.team_results.setdefault(code, {"content": {"items": []}})
                run.issues["materials:weekly"] = "生成错误：部分 AI 分析未完成；" + (run.last_error or "")
                workflow.store.save(run)
                organization = workflow.organization.get()
                workflow._documents(run, organization)
                run.status = "COMPLETED"
                run.completed_at = utc_now()
                workflow.store.save(run)
            workflow.notify_due(run.kind, day)
            run = workflow.store.load(run.run_id)
            if not run.document_urls:
                leader = workflow.organization.get().person_map().get(workflow.organization.get().team_leader_id)
                if leader and leader.open_id:
                    workflow._notify(run, "leader", leader.open_id, "报告生成错误：" + (run.last_error or "无可用链接"))

    def send_error(self, workflow, day, message):
        snapshot = workflow.store.load_snapshot(day)
        organization = snapshot.organization if snapshot else workflow.organization_cache.get()
        ids = {d.minister_id for d in organization.departments if d.active and d.minister_id}
        ids.add(organization.team_leader_id)
        for person_id in ids:
            person = organization.person_map().get(person_id)
            if person and person.open_id:
                workflow._send_business_notification(day, f"{day}:DOC:ERROR:{person_id}", person.open_id, message)
