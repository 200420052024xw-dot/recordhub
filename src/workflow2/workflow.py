"""Department-first periodic analysis, form confirmation, and team reports."""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from workflow2.materials import MaterialPreparer
from workflow2.periods import due, material_window
from workflow2.configs import SkillConfigRepository
from config.schedules import ScheduleConfig
from data.repositories import OrganizationCache
from data.store import utc_now
from llm import PromptService
from schema import Organization
from workflow2.skills import SkillRunner, TechnologyOutput, TrainingOutput
from tool.bitable_fields import field_datetime, record_fields, references, scalar
from tool.cloud_docs import CloudDocsService, divider_block, text_block
from tool.feishu import MessageService
from workflow1.documents import DailyDocuments
from workflow2.models import (CycleRun, DepartmentAnalysis, DepartmentResult,
                              WeeklySummaryInput)
from workflow2.store import Workflow2Store
from workflow2.tables import Workflow2Tables, date_millis


SCHEDULES = {"stage": "s04_progress", "monthly": "s05_people_suggestions",
             "weekly": "s08_public_technology"}
CODES = {"stage": ("S04",), "monthly": ("S05", "S06", "S07"),
         "weekly": ("S08", "S09")}
NAMES = {"S04": "阶段工作进展", "S05": "人员建议", "S06": "idea清单",
         "S07": "重点谈话", "S08": "公共技术", "S09": "公共培训"}
RESULT_COLUMNS = {"S05": "personnel_s05", "S06": "ideas_s06", "S07": "meeting_s07"}
SUMMARY_MODELS = {"S08": TechnologyOutput, "S09": TrainingOutput}
CONFIRM_COLUMNS = {"S05": ("personnel_decision", "personnel_edited"),
                   "S06": ("ideas_decision", "ideas_edited"),
                   "S07": ("meeting_decision", "meeting_edited")}
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _write_date_millis() -> int:
    """Date shown in Feishu is the local day the record is written."""
    return date_millis(utc_now().astimezone(SHANGHAI).date())


def _partial_notice(omitted: set[str]) -> str:
    return (f"【材料说明】本周期有 {len(omitted)} 项材料缺失或尚未完成；"
            "以下仅分析可用记录。\n") if omitted else ""


def _item_text(item: dict, code: str, department_ids: list[str] | None = None) -> str:
    subject = (item.get("work_item") or item.get("topic") or item.get("technology_name")
               or item.get("idea") or item.get("person_id") or NAMES[code])
    people = item.get("participant_ids") or item.get("contributor_ids") or item.get("target_person_ids")
    people = people or ([item["person_id"]] if item.get("person_id") else [])
    departments = department_ids or item.get("department_ids") or []
    detail = "; ".join(f"{key}: {value}" for key, value in item.items()
                       if key not in {"department_ids", "participant_ids",
                                      "contributor_ids", "target_person_ids"} and value not in ("", [], None))
    return (f"-结果类别：{NAMES[code]}\n-主题或对象：{subject}\n"
            f"-涉及人员及部门：{', '.join(map(str, people)) or '未指定'}；"
            f"{', '.join(map(str, departments)) or '未指定'}\n"
            f"-具体内容：{detail}")


def _result_text(result: dict, code: str, department_ids: list[str] | None = None) -> str:
    content = result.get("content") or {}
    items = content.get("items") or []
    return "\n\n".join(_item_text(item, code, department_ids) for item in items) or "本范围内暂无可确认的分析条目。"


class Workflow2:
    def __init__(self, *, store: Workflow2Store, preparer: MaterialPreparer,
                 organization: OrganizationCache, configs: SkillConfigRepository,
                 tables: Workflow2Tables, prompt_service: PromptService,
                 messages: MessageService, documents: CloudDocsService,
                 schedules: ScheduleConfig, archive_parent: str):
        self.store, self.preparer, self.organization = store, preparer, organization
        self.configs, self.tables, self.prompt_service = configs, tables, prompt_service
        self.messages, self.documents = messages, documents
        self.schedules, self.archive_parent = schedules, archive_parent
        self.lock = threading.RLock()

    def _schedule(self, kind: str):
        if kind not in SCHEDULES:
            raise ValueError("未知的 Workflow2 周期")
        return self.schedules.workflows[SCHEDULES[kind]]

    def _required_tables(self, kind: str) -> list[str]:
        common = ["reports"]
        if kind == "stage":
            return common + ["stage_analysis", "stage_confirmation", "stage_report"]
        if kind == "monthly":
            return common + ["monthly_department_analysis",
                             "monthly_department_confirmation", "monthly_department_report"]
        return common + ["team_analysis"]

    def _check_ready(self, kind: str) -> None:
        self.tables.require([name for name in self._required_tables(kind) if name != "reports"])
        self.tables.table("reports")
        if kind != "weekly" and not str(self._schedule(kind).options.get("form_url", "")).strip():
            raise ValueError(f"{kind} 确认表单链接尚未配置")
        if not self.archive_parent:
            raise ValueError("缺少云文档归档父文件夹 Token")

    def start(self, kind: str, scheduled_date: date) -> CycleRun:
        with self.lock:
            schedule = self._schedule(kind)
            start, end = material_window(schedule, scheduled_date)
            if end >= utc_now().astimezone(SHANGHAI).date():
                raise ValueError("只能分析已经结束的完整自然日")
            run_id = str(uuid5(NAMESPACE_URL, f"recordhub-workflow2:{kind}:{scheduled_date}"))
            run = self.store.load(run_id)
            if run is None:
                now = utc_now()
                run = CycleRun(run_id=run_id, kind=kind, scheduled_date=scheduled_date,
                               start_date=start, end_date=end, created_at=now, updated_at=now)
                self.store.save(run)
            return self._advance(run)

    def resume(self, run_id: str) -> CycleRun:
        with self.lock:
            run = self.store.load(run_id)
            if run is None:
                raise ValueError("Workflow2 任务不存在")
            return self._advance(run)

    def recover(self) -> list[CycleRun]:
        activation = self.store.activate(utc_now().astimezone(SHANGHAI).date())
        return [self.resume(run.run_id) for run in self.store.pending()
                if run.kind != "stage" or run.start_date > activation]

    def scheduled(self, kind: str, today: date) -> CycleRun | None:
        schedule = self._schedule(kind)
        if kind == "stage":
            activation = self.store.activate(today)
            # The activation day may be partial. Count complete days after it,
            # then use the first reporting day as the interval anchor.
            first = activation + timedelta(
                days=int(schedule.options["startup_wait_complete_days"]) + 1)
            if (not schedule.enabled or today < first or
                    (today - first).days % int(schedule.options["every_days"])):
                return None
        elif not due(schedule, today):
            return None
        return self.start(kind, today)

    def _cutoff(self, run: CycleRun) -> datetime:
        options = self._schedule(run.kind).options
        days = int(options.get("confirmation_days", 1))
        hour, minute = map(int, str(options.get("confirmation_time", "12:00")).split(":"))
        return datetime.combine(run.scheduled_date + timedelta(days=days),
                                time(hour, minute), SHANGHAI)

    def _advance(self, run: CycleRun) -> CycleRun:
        if run.status == "COMPLETED":
            return run
        try:
            self._check_ready(run.kind)
            organization = self.organization.get()
            if organization.anomalies:
                raise ValueError("组织关系异常：" + "；".join(organization.anomalies))
            if run.kind == "weekly":
                self._weekly(run, organization)
            else:
                self._departments(run, organization)
                if not all(set(CODES[run.kind]) <= set(item.final_text)
                           for item in run.departments.values()):
                    run.status = "WAITING_CONFIRMATION"
                    self.store.save(run)
                    return run
            self._documents(run, organization)
            run.status, run.last_error = "COMPLETED", None
            self.store.save(run)
        except ValueError as exc:
            run.status, run.last_error = "BLOCKED", str(exc)
            self.store.save(run)
        except Exception as exc:
            run.status, run.last_error = "FAILED", str(exc)
            self.store.save(run)
        return run

    def _departments(self, run: CycleRun, organization: Organization) -> None:
        people = organization.person_map()
        departments = [item for item in organization.departments if item.active and item.minister_id]
        if not departments:
            raise ValueError("没有有效的部门及部长")
        configs = self.configs.load(organization)
        runner = SkillRunner(self.prompt_service, configs)
        for department in sorted(departments, key=lambda item: item.department_id):
            minister = people[department.minister_id]
            if not minister.active or not minister.open_id:
                raise ValueError(f"部门 {department.department_id} 的部长无有效 OpenID")
            draft = run.departments.get(department.department_id)
            if draft is None:
                draft = DepartmentResult(department_id=department.department_id,
                                         minister_id=minister.person_id)
                run.departments[department.department_id] = draft
                self.store.save(run)
            if set(CODES[run.kind]) <= set(draft.final_text):
                self._publish_department_report(run, draft, minister)
                continue
            allow_rebuild = run.kind != "stage"
            group_materials = [self.preparer.prepare(
                run_id=run.run_id, start=run.start_date, end=run.end_date,
                department_ids=[department.department_id], person_ids=group,
                allow_rebuild=allow_rebuild, allow_partial=allow_rebuild)
                for group in self._analysis_groups(run.kind, department, organization)]
            for group_material in group_materials:
                if not group_material.input.scope.complete:
                    raise ValueError("；".join(group_material.input.scope.missing_sources))
            omitted = {source for material in group_materials
                       for source in material.input.scope.omitted_sources}
            notice = _partial_notice(omitted)
            if notice:
                run.issues[f"materials:{department.department_id}"] = notice.strip()
                self.store.save(run)
            for code in CODES[run.kind]:
                if code not in draft.drafts:
                    if run.kind == "stage":
                        result = runner.run(code, group_materials[0].input,
                            department_id=department.department_id, user_id=minister.person_id)
                        draft.drafts[code] = result.model_dump(mode="json")
                    else:
                        items: list[dict] = []
                        for group_material in group_materials:
                            result = runner.run(code, group_material.input,
                                department_id=department.department_id,
                                user_id=minister.person_id)
                            dump = result.model_dump(mode="json")
                            items.extend((dump.get("content") or {}).get("items", []))
                        draft.drafts[code] = {"content": {"items": items}}
                    draft.draft_text[code] = notice + _result_text(
                        draft.drafts[code], code, [department.department_id])
                    self.store.save(run)
            task_id = f"{run.run_id}:{department.department_id}"
            common = {"minister_ref": [{"id": minister.open_id}],
                      "analysis_date": _write_date_millis()}
            if run.kind == "stage":
                self.tables.upsert("stage_analysis", task_id,
                    {**common, "progress": draft.draft_text["S04"]})
            else:
                self.tables.upsert("monthly_department_analysis", task_id,
                    {**common, **{RESULT_COLUMNS[code]: draft.draft_text[code]
                                  for code in CODES[run.kind]}})
            if utc_now().astimezone(SHANGHAI) < self._cutoff(run):
                self._notify(run, f"minister:{department.department_id}", minister.open_id,
                    f"【{run.start_date} 至 {run.end_date} 周期分析待确认】\n"
                    f"请核对本部门分析并填写确认表：{self._schedule(run.kind).options['form_url']}")
            self._confirmation(run, draft, organization)

    @staticmethod
    def _analysis_groups(kind: str, department,
                         organization: Organization) -> list[list[str] | None]:
        """One LLM material batch per group; None means the whole department.

        Monthly runs analyze per backbone group (the backbone plus the students
        whose leader they are); students without a backbone leader form one
        trailing group under the minister's review.
        """
        if kind == "stage":
            return [None]
        students = [person for person in organization.persons if person.active
                    and person.role in {"基层学生", "骨干学生"}
                    and person.department_id == department.department_id]
        groups: list[list[str]] = []
        assigned: set[str] = set()
        for core in [person for person in students if person.role == "骨干学生"]:
            members = [core.person_id] + [
                person.person_id for person in students
                if person.role == "基层学生" and person.leader_id == core.person_id]
            assigned.update(members)
            groups.append(members)
        orphans = [person.person_id for person in students if person.person_id not in assigned]
        if orphans:
            groups.append(orphans)
        return groups or [None]

    def _confirmation(self, run: CycleRun, draft: DepartmentResult,
                      organization: Organization) -> None:
        codes = CODES[run.kind]
        if set(codes) <= set(draft.final_text):
            return
        table_name = "stage_confirmation" if run.kind == "stage" else "monthly_department_confirmation"
        table = self.tables.table(table_name)
        f = table.fields
        person = organization.person_map()[draft.minister_id]
        task_id = f"{run.run_id}:{draft.department_id}"
        matches = []
        for row in self.tables.bitable.list_records(table.table_id):
            values = record_fields(row)
            supplied_id = scalar(values.get(f["task_id"]))
            if supplied_id and supplied_id != task_id:
                continue
            identities = {draft.minister_id, person.name, person.open_id,
                          person.source_record_id}
            author_key = "submitted_by" if run.kind == "stage" else "minister_ref"
            if not identities.intersection(references(values.get(f[author_key]))):
                continue
            try:
                recorded_day = field_datetime(values.get(f["analysis_date"])).astimezone(SHANGHAI).date()
            except ValueError:
                continue
            # The form date is the day the minister submits it. A confirmation
            # may arrive on the next day before the configured cutoff.
            if (supplied_id == task_id or
                    run.scheduled_date <= recorded_day <= self._cutoff(run).date()):
                matches.append(row)
        if len(matches) > 1:
            raise ValueError(f"部长 {draft.minister_id} 的周期确认记录重复")
        late = utc_now().astimezone(SHANGHAI) >= self._cutoff(run)
        row = matches[0] if matches else None
        row_submitted_late = late
        if row is not None:
            timestamp = row.get("last_modified_time") or row.get("created_time")
            if timestamp:
                row_submitted_late = field_datetime(timestamp).astimezone(SHANGHAI) >= self._cutoff(run)
        if row and row_submitted_late:
            run.issues.setdefault(f"late:{draft.department_id}", "确认截止后提交，保留 AI 草稿")
        elif row:
            values = record_fields(row)
            if not scalar(values.get(f["task_id"])):
                self.tables.bitable.update_record(table.table_id, str(row["record_id"]),
                                                  {f["task_id"]: task_id})
            draft.confirmation_record_id = str(row["record_id"])
            for code in codes:
                if code in draft.final_text:
                    continue
                decision_key, edited_key = (("decision", "edited") if run.kind == "stage"
                                            else CONFIRM_COLUMNS[code])
                decision = scalar(values.get(f[decision_key])).strip()
                if decision == "确认无误":
                    draft.final_text[code] = draft.draft_text[code]
                elif decision == "需修改":
                    edited = scalar(values.get(f[edited_key])).strip()
                    if not edited:
                        raise ValueError(f"{code} 选择需修改但未填写修改内容")
                    draft.final_text[code] = edited
                elif decision:
                    raise ValueError(f"{code} 确认选项无效：{decision}")
        if late:
            for code in codes:
                if code not in draft.final_text:
                    draft.final_text[code] = draft.draft_text[code]
                    if code not in draft.unconfirmed:
                        draft.unconfirmed.append(code)
        self.store.save(run)
        if set(codes) <= set(draft.final_text):
            self._publish_department_report(run, draft, person)

    def _publish_department_report(self, run: CycleRun, draft: DepartmentResult,
                                   minister) -> None:
        def report_text(code: str) -> str:
            prefix = "【人工未确认，采用 AI 草稿】\n" if code in draft.unconfirmed else ""
            text = draft.final_text[code]
            notice = run.issues.get(f"materials:{draft.department_id}", "")
            if notice and notice not in text:
                text = notice + "\n" + text
            return prefix + text
        common = {"minister_ref": [{"id": minister.open_id}],
                  "analysis_date": _write_date_millis()}
        task_id = f"{run.run_id}:{draft.department_id}"
        if run.kind == "stage":
            self.tables.upsert("stage_report", task_id,
                {**common, "progress": report_text("S04")})
        else:
            self.tables.upsert("monthly_department_report", task_id,
                {**common, **{RESULT_COLUMNS[code]: report_text(code)
                              for code in CODES[run.kind]}})

    def _weekly(self, run: CycleRun, organization: Organization) -> None:
        leader_id = organization.team_leader_id
        if not leader_id:
            raise ValueError("团队负责人缺失")
        people = organization.person_map()
        departments = [item for item in organization.departments
                       if item.active and item.minister_id]
        department_ids = sorted(item.department_id for item in departments)
        runner = SkillRunner(self.prompt_service, self.configs.load(organization))

        # ① team reference pass over the whole organization.
        team_material = self.preparer.prepare(run_id=run.run_id, start=run.start_date,
                                             end=run.end_date, department_ids=department_ids,
                                             allow_rebuild=True, allow_partial=True)
        if not team_material.input.scope.complete:
            raise ValueError("；".join(team_material.input.scope.missing_sources))
        weekly_notice = _partial_notice(set(team_material.input.scope.omitted_sources))
        if weekly_notice:
            run.issues["materials:weekly"] = weekly_notice.strip()
            self.store.save(run)
        for code in CODES["weekly"]:
            if code not in run.weekly_reference:
                result = runner.run(code, team_material.input,
                    department_id=self.configs.team_department_id, user_id=leader_id)
                run.weekly_reference[code] = result.model_dump(mode="json")
                self.store.save(run)

        # ② per-department analysis with the department's own skill config.
        for department_id in department_ids:
            bucket = run.department_results.setdefault(department_id, {})
            if set(CODES["weekly"]) <= set(bucket):
                continue
            department_material = self.preparer.prepare(run_id=run.run_id,
                start=run.start_date, end=run.end_date,
                department_ids=[department_id], allow_rebuild=True,
                allow_partial=True)
            if not department_material.input.scope.complete:
                raise ValueError("；".join(department_material.input.scope.missing_sources))
            minister = people[organization.department_map()[department_id].minister_id]
            for code in CODES["weekly"]:
                if code not in bucket:
                    result = runner.run(code, department_material.input,
                        department_id=department_id, user_id=minister.person_id)
                    bucket[code] = result.model_dump(mode="json")
                    self.store.save(run)

        # ③ summary pass merging the reference and department drafts.
        allowed_people = {record.person_id for record in team_material.input.records
                          if record.submitted}
        allowed_departments = {record.department_id for record in team_material.input.records
                               if record.submitted}
        allowed_resources = {resource.resource_id for resource in team_material.input.resources}
        names = organization.department_map()
        for code in CODES["weekly"]:
            if code in run.team_results:
                continue
            sections = [DepartmentAnalysis(
                department_id=department_id, department_name=names[department_id].name,
                items=((run.department_results[department_id].get(code) or {})
                       .get("content") or {}).get("items", []))
                for department_id in department_ids]
            reference_items = ((run.weekly_reference.get(code) or {})
                               .get("content") or {}).get("items", [])
            data = WeeklySummaryInput(skill_code=code, start_date=run.start_date,
                end_date=run.end_date, reference_items=reference_items, departments=sections)
            allowed_refs = {ref for section in [reference_items, *(s.items for s in sections)]
                            for item in section for ref in item.get("achievement_refs", [])}

            def validate(output, *, _people=allowed_people, _departments=allowed_departments,
                         _resources=allowed_resources, _refs=allowed_refs) -> None:
                for item in output.items:
                    for name in ("participant_ids", "contributor_ids", "target_person_ids"):
                        values = getattr(item, name, None)
                        if values is not None and not set(values) <= _people:
                            raise ValueError(f"总结的 {name} 超出本次周度材料人员")
                    values = getattr(item, "department_ids", None)
                    if values is not None and not set(values) <= _departments:
                        raise ValueError("总结的部门编号超出本次周度材料")
                    values = getattr(item, "available_resource_ids", None)
                    if values is not None and not set(values) <= _resources:
                        raise ValueError("总结引用的课程或技术超出本次资源清单")
                    values = getattr(item, "achievement_refs", None)
                    if values is not None and not set(values) <= _refs:
                        raise ValueError("总结的成果出处超出参考版与各部门草稿")
            output = self.prompt_service.execute(prompt_code=f"{code}10",
                template=Path("prompts/S10.txt").read_text(encoding="utf-8").strip(),
                input_data=data, output_model=SUMMARY_MODELS[code], semantic_validator=validate)
            run.team_results[code] = {"content": output.model_dump(mode="json")}
            self.store.save(run)
        self.tables.upsert("team_analysis", run.run_id,
            {"analysis_date": _write_date_millis(),
             "technology_s08": weekly_notice + _result_text(run.team_results["S08"], "S08"),
             "training_s09": weekly_notice + _result_text(run.team_results["S09"], "S09")})

    def _documents(self, run: CycleRun, organization: Organization) -> None:
        leader = organization.person_map().get(organization.team_leader_id or "")
        if leader is None or not leader.open_id:
            raise ValueError("团队负责人缺少 OpenID")
        parent = self.documents.find_child(self.archive_parent, "workflow2", "folder")
        parent = parent or self.documents.create_folder(self.archive_parent, "workflow2")
        folder_name = run.scheduled_date.isoformat()
        folder = self.documents.find_child(parent, folder_name, "folder")
        folder = folder or self.documents.create_folder(parent, folder_name)
        for code in CODES[run.kind]:
            title = f"{run.scheduled_date:%m-%d} 团队{NAMES[code]}"
            token = run.document_tokens.get(code)
            if not token:
                token = self.documents.find_child(folder, title, "docx")
                token = token or self.documents.create_document(folder, title)
                run.document_tokens[code] = token
                self.store.save(run)
            blocks = [text_block(title, heading=1),
                      text_block(f"-分析范围：{run.start_date} 至 {run.end_date}")]
            if run.kind == "weekly" and run.issues.get("materials:weekly"):
                blocks.append(text_block(run.issues["materials:weekly"]))
            if run.kind == "weekly":
                blocks.append(text_block("团队总结", heading=2))
                summary = _result_text(run.team_results[code], code)
                blocks.extend(text_block(line) for line in summary.split("\n") if line)
                names = organization.department_map()
                for department_id, bucket in sorted(run.department_results.items()):
                    department = names[department_id]
                    minister = organization.person_map()[department.minister_id]
                    blocks.append(divider_block())
                    blocks.append(text_block(f"{minister.name}老师部门（{department.name}）", heading=2))
                    department_text = _result_text(bucket.get(code) or {}, code, [department_id])
                    blocks.extend(text_block(line)
                                  for line in department_text.split("\n") if line)
                blocks.append(divider_block())
                blocks.append(text_block("团队参考版", heading=2))
                reference_text = _result_text(run.weekly_reference.get(code) or {}, code)
                blocks.extend(text_block(line) for line in reference_text.split("\n") if line)
            for department_id, draft in sorted(run.departments.items()):
                department = organization.department_map()[department_id]
                minister = organization.person_map()[draft.minister_id]
                blocks.append(divider_block())
                blocks.append(text_block(f"{minister.name}老师部门（{department.name}）", heading=2))
                if code in draft.unconfirmed:
                    blocks.append(text_block("-确认状态：人工未确认，采用 AI 草稿"))
                blocks.extend(text_block(line) for line in draft.final_text[code].split("\n") if line)
            existing = [block for block in self.documents.list_blocks(token)
                        if DailyDocuments._block_content(block).strip()]
            expected = [block for block in blocks if block.get("block_type") != 22]
            if len(existing) > len(expected) or any(
                DailyDocuments._block_signature(actual) != DailyDocuments._block_signature(wanted)
                for actual, wanted in zip(existing, expected)):
                raise ValueError(f"云文档 {title} 已被修改，停止自动覆盖")
            tail = []
            seen = 0
            for block in blocks:
                if block.get("block_type") == 22:
                    if seen >= len(existing):
                        tail.append(block)
                else:
                    if seen >= len(existing):
                        tail.append(block)
                    seen += 1
            if tail:
                self.documents.append_blocks(token, tail)
            url = f"https://feishu.cn/docx/{token}"
            run.document_urls[code] = url
            self.store.save(run)
            self._index_document(run, code, url, leader)
        if run.kind == "stage":
            self.tables.upsert("stage_report", run.run_id + ":TEAM",
                {"minister_ref": [{"id": leader.open_id}],
                 "analysis_date": _write_date_millis(),
                 "progress": run.document_urls["S04"]})
        elif run.kind == "monthly":
            self.tables.upsert("monthly_department_report", run.run_id + ":TEAM",
                {"minister_ref": [{"id": leader.open_id}],
                 "analysis_date": _write_date_millis(),
                 **{RESULT_COLUMNS[code]: run.document_urls[code]
                    for code in CODES["monthly"]}})
        self._notify(run, "leader", leader.open_id,
            "【周期分析报告已生成】\n" + "\n".join(
                f"{NAMES[code]}：{run.document_urls[code]}" for code in CODES[run.kind]))

    def _index_document(self, run: CycleRun, code: str, url: str, leader) -> None:
        table = self.tables.table("reports")
        f = table.fields
        if not f.get("document_url"):
            raise ValueError("报告日志表缺少飞书文档链接映射")
        fields = [item for item in self.tables.bitable.list_fields(table.table_id)
                  if item.get("field_name") == f["document_url"]]
        if len(fields) != 1 or fields[0].get("type") not in (1, 15):
            raise ValueError("报告日志表的文档链接字段必须是文本或超链接")
        rows = [row for row in self.tables.bitable.list_records(table.table_id)
                if scalar(record_fields(row).get(f["document_url"])) == url]
        if len(rows) > 1:
            raise ValueError(f"报告日志表中文档 {url} 重复")
        if not rows:
            self.tables.bitable.create_record(table.table_id, {
                f["reporter_ref"]: [{"id": leader.open_id}], f["role"]: leader.role,
                f["reported_at"]: int(utc_now().timestamp() * 1000),
                f["document_url"]: ({"text": NAMES[code], "link": url}
                                    if fields[0]["type"] == 15 else url)})

    def _notify(self, run: CycleRun, key: str, open_id: str, message: str) -> None:
        if key in run.notification_ids:
            return
        result = self.messages.send_text(open_id, message,
            idempotency_key=f"workflow2:{run.run_id}:{key}")
        message_id = scalar(result.get("message_id"))
        if not message_id:
            raise ValueError("周期通知没有返回 message_id")
        run.notification_ids[key] = message_id
        self.store.save(run)

    def handle_confirmation(self, record_id: str) -> dict:
        # The event merely wakes recovery. Reconciliation checks row identity and cutoff.
        updated = [self.resume(run.run_id).run_id for run in self.store.pending()
                   if run.kind in {"stage", "monthly"}]
        return {"record_id": record_id, "checked_runs": updated}
