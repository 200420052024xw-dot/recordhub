"""Authorized analysis, frozen materials, JSON publication and explicit confirmation."""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from config.schedules import WorkflowSchedule
from analysis.materials import MaterialPreparer
from analysis.repositories import AnalysisRepository, SkillConfigRepository
from analysis.store import AnalysisStore
from data.repositories import OrganizationCache
from data.store import utc_now
from schema import Organization
from analysis.models import AnalysisRequest, AnalysisRun
from skills import SkillCode, SkillResult, SkillRunner, SkillStatus
from skills.runner import SKILLS
from tool.errors import FeishuApiError
from tool.feishu import MessageService
from analysis.periods import PERIODIC_CODES, due, latest_due, material_window


NAMES = {"S04": "阶段工作进展汇总", "S05": "降级—奖励—退出—提拔建议",
         "S06": "idea 清单", "S07": "重点谈话学生清单", "S08": "公共技术建议", "S09": "公共培训建议"}

logger = logging.getLogger(__name__)


class AnalysisWorkflow:
    def __init__(self, *, store: AnalysisStore, preparer: MaterialPreparer,
                 organization: OrganizationCache, configs: SkillConfigRepository,
                 repository: AnalysisRepository, prompt_service,
                 messages: MessageService, team_department_id: str = "TEAM_MANAGEMENT"):
        self.store, self.preparer = store, preparer
        self.organization, self.configs, self.repository = organization, configs, repository
        self.prompt_service, self.messages = prompt_service, messages
        self.team_department_id = team_department_id
        self.lock = threading.RLock()

    def authorize(self, request: AnalysisRequest, organization: Organization):
        code = request.skill_code
        if code not in {SkillCode.S04, SkillCode.S05, SkillCode.S06, SkillCode.S07, SkillCode.S08, SkillCode.S09}:
            raise ValueError("周期分析仅接受 S04—S09")
        if request.end_date < request.start_date:
            raise ValueError("分析起止日期倒置")
        if request.end_date >= utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("周期分析只使用已结束的完整自然日")
        person = organization.person_map().get(request.user_id)
        if person is None or not person.active:
            raise PermissionError("分析使用人不存在或已停用")
        if person.person_id == organization.team_leader_id and person.role == "团队负责人":
            allowed = sorted(department.department_id for department in organization.departments if department.active)
            scope_type, config_department = "TEAM", self.team_department_id
        elif person.role == "部长":
            if code in {SkillCode.S08, SkillCode.S09}:
                raise PermissionError("公共技术和培训分析仅由团队负责人执行")
            allowed = sorted(department.department_id for department in organization.departments
                             if department.active and department.minister_id == person.person_id)
            scope_type, config_department = "DEPARTMENT", None
        else:
            raise PermissionError("该角色没有周期分析权限")
        selected = request.department_ids if request.department_ids is not None else allowed
        if (not selected or len(selected) != len(set(selected))
                or not set(selected) <= set(allowed)):
            raise PermissionError("分析部门范围为空、重复或超出使用人的管理权限")
        if scope_type == "DEPARTMENT":
            if len(selected) != 1:
                raise ValueError("部长跨部门分析需按部门分别运行，指定一个部门")
            config_department = selected[0]
        return scope_type, config_department, sorted(selected)

    def run(self, request: AnalysisRequest) -> AnalysisRun:
        with self.lock:
            organization = self.organization.get()
            scope_type, department, selected = self.authorize(request, organization)
            request = request.model_copy(update={"department_ids": selected}, deep=True)
            key = json.dumps(request.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
            run_id = str(uuid5(NAMESPACE_URL, "recordhub-analysis:" + key))
            run = self.store.load(run_id)
            if run is None:
                now = utc_now()
                run = AnalysisRun(run_id=run_id, request=request,
                    config_department_id=department, scope_type=scope_type,
                    created_at=now, updated_at=now)
                self.store.save(run)
            return self._execute(run, organization)

    def resume(self, run_id: str) -> AnalysisRun:
        with self.lock:
            run = self.store.load(run_id)
            if run is None:
                raise ValueError("分析任务不存在")
            organization = self.organization.get()
            scope_type, department, selected = self.authorize(run.request, organization)
            if (scope_type != run.scope_type or department != run.config_department_id
                    or selected != run.request.department_ids):
                raise PermissionError("分析任务原管理权限已改变")
            return self._execute(run, organization)

    def recover(self) -> list[AnalysisRun]:
        recovered = []
        for run in self.store.recoverable():
            try:
                recovered.append(self.resume(run.run_id))
            except Exception as exc:
                run.status, run.last_error = "BLOCKED", str(exc)
                self.store.save(run)
                recovered.append(run)
        return recovered

    def _execute(self, run: AnalysisRun, organization: Organization) -> AnalysisRun:
        if run.status == "CONFIRMED":
            return run
        if run.status == "NO_MATERIAL" and run.notification_message_id:
            return run
        try:
            if run.result is None:
                if run.material is None or not run.material.input.scope.complete:
                    run.material = self.preparer.prepare(run_id=run.run_id,
                        start=run.request.start_date, end=run.request.end_date,
                        department_ids=run.request.department_ids)
                    self.store.save(run)
                if not run.material.input.scope.complete:
                    run.status = "BLOCKED"
                    run.last_error = "；".join(run.material.input.scope.missing_sources)
                    self.store.save(run)
                    self._notify(run, organization, blocked=True)
                    return run
                if run.config is None:
                    runner = SkillRunner(self.prompt_service, self.configs.load(organization))
                    run.config = runner.resolve_config(run.request.skill_code,
                        department_id=run.config_department_id, user_id=run.request.user_id)
                    self.store.save(run)
                run.status, run.last_error = "RUNNING", None
                self.store.save(run)
                # Validate the destination before spending a model request.
                self.repository.layout(run)
                result = SKILLS[run.request.skill_code].run(self.prompt_service,
                    run.material.input, run.config, user_id=run.request.user_id)
                if run.material.statistics is not None:
                    result.material_statistics = run.material.statistics.model_dump()
                run.result = result.model_dump(mode="json")
                run.draft_result = json.loads(json.dumps(run.result))
                self.store.save(run)
            confirmed = run.result["status"] == SkillStatus.CONFIRMED.value
            run.external_record_id = self.repository.publish(run, run.result,
                organization=organization, confirmation=confirmed)
            run.status = run.result["status"]
            run.last_error = None
            self.store.save(run)
            if not confirmed:
                self._notify(run, organization)
        except Exception as exc:
            run.status, run.last_error = "FAILED", str(exc)
            self.store.save(run)
        return run

    def _notify(self, run: AnalysisRun, organization: Organization, *, blocked=False) -> None:
        prefix = "blocked_notification" if blocked else "notification"
        if getattr(run, prefix + "_message_id"):
            return
        person = organization.person_map()[run.request.user_id]
        if not person.open_id:
            raise ValueError("分析确认人缺少飞书 OpenID")
        reserved = getattr(run, prefix + "_reserved_at")
        if reserved and (utc_now() - reserved).total_seconds() >= 3600:
            raise ValueError("分析通知发送结果未知且去重窗口已过，请管理员核对后登记消息 ID")
        if not reserved:
            setattr(run, prefix + "_reserved_at", utc_now())
            self.store.save(run)
        title = NAMES[run.request.skill_code.value]
        scope = f"{run.request.start_date} 至 {run.request.end_date}"
        if blocked:
            message = f"【{title}等待材料】{scope}\n{run.last_error}\n任务编号：{run.run_id}"
        else:
            label = "暂无可分析材料" if run.status == "NO_MATERIAL" else "待确认"
            message = f"【{title}：{label}】{scope}\n{self.repository.url(run)}\n任务编号：{run.run_id}"
        try:
            response = self.messages.send_text(person.open_id, message,
                idempotency_key=f"analysis:{run.run_id}:{prefix}")
        except FeishuApiError:
            setattr(run, prefix + "_reserved_at", None)
            self.store.save(run)
            raise
        message_id = response.get("message_id")
        if not message_id:
            raise ValueError("飞书分析通知没有返回消息 ID")
        setattr(run, prefix + "_message_id", str(message_id))
        self.store.save(run)

    def confirm(self, run_id: str, *, user_id: str, content: dict | None = None) -> AnalysisRun:
        with self.lock:
            run = self.store.load(run_id)
            if run is None or not run.result or run.result.get("content") is None:
                raise ValueError("没有可确认的分析结果")
            if user_id != run.request.user_id:
                raise PermissionError("只有本任务指定确认人可以确认")
            organization = self.organization.get()
            self.authorize(run.request, organization)
            if run.status == "CONFIRMED":
                return run
            if run.result["status"] != SkillStatus.CONFIRMED.value:
                remote = self.repository.read(run)
                model = SKILLS[run.request.skill_code].output_model
                # The administrator may supply edited content, or confirm the current table cell.
                edited = model.model_validate(content if content is not None else remote.get("content"))
                SKILLS[run.request.skill_code].validate_output(edited, run.material.input)
                result = SkillResult[model].model_validate(run.result)
                result.content = edited
                result.status = SkillStatus.CONFIRMED
                result.confirmed_by, result.confirmed_at = user_id, utc_now()
                run.result = result.model_dump(mode="json")
                run.status = "RUNNING"
                self.store.save(run)
            return self._execute(run, organization)

    def reconcile_notification(self, run_id: str, *, message_id: str, blocked=False) -> AnalysisRun:
        with self.lock:
            run = self.store.load(run_id)
            if run is None or not message_id.strip():
                raise ValueError("需要有效任务和已核对的消息 ID")
            prefix = "blocked_notification" if blocked else "notification"
            if not getattr(run, prefix + "_reserved_at"):
                raise ValueError("该任务没有待核对的通知")
            setattr(run, prefix + "_message_id", message_id.strip())
            self.store.save(run)
            return run

    def scheduled(self, schedule: WorkflowSchedule, today: date) -> list[AnalysisRun]:
        if not due(schedule, today):
            return []
        code = SkillCode(PERIODIC_CODES[schedule.name])
        start, end = material_window(schedule, today)
        organization = self.organization.get()
        requests = []
        selected = schedule.options.get("department_ids")
        users = schedule.options.get("user_ids")
        active = [department for department in organization.departments if department.active]
        if selected is not None and (not selected or not set(selected) <= {item.department_id for item in active}):
            raise ValueError("调度配置包含空范围或无效部门")
        if users is not None and not set(users) <= set(organization.person_map()):
            raise ValueError("调度配置包含无效使用人")
        if code in {SkillCode.S04, SkillCode.S05, SkillCode.S06, SkillCode.S07}:
            for department in active:
                if (department.minister_id and (selected is None or department.department_id in selected)
                        and (users is None or department.minister_id in users)):
                    requests.append(AnalysisRequest(skill_code=code, start_date=start, end_date=end,
                        user_id=department.minister_id, department_ids=[department.department_id]))
        leader = organization.team_leader_id
        if leader and (users is None or leader in users):
            requests.append(AnalysisRequest(skill_code=code, start_date=start, end_date=end,
                                           user_id=leader, department_ids=selected))
        results = []
        errors = []
        for request in requests:
            try:
                results.append(self.run(request))
            except Exception as exc:
                logger.exception("analysis_owner_dispatch_failed user=%s", request.user_id)
                errors.append(str(exc))
        if errors:
            raise ValueError("；".join(errors))
        return results

    def dispatch(self, schedule: WorkflowSchedule, today: date) -> list[AnalysisRun]:
        results = self.scheduled(schedule, today)
        if due(schedule, today):
            self.store.checkpoint(schedule.name, today)
        return results

    def catch_up(self, schedules, now=None) -> None:
        local = (now or utc_now()).astimezone(ZoneInfo(schedules.timezone))
        for name in PERIODIC_CODES:
            schedule = schedules.workflows.get(name)
            if schedule is None or not schedule.enabled:
                continue
            hour, minute = map(int, schedule.time.split(":"))
            eligible = local.date()
            if (local.hour, local.minute) < (hour, minute):
                eligible -= timedelta(days=1)
            latest = latest_due(schedule, eligible)
            if latest is None:
                continue
            watermark = self.store.watermarks().get(name)
            # First activation catches the latest period, not every period since the anchor.
            day = watermark + timedelta(days=1) if watermark else latest
            while day <= latest:
                if due(schedule, day):
                    self.dispatch(schedule, day)
                day += timedelta(days=1)
