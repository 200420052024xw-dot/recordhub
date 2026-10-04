"""S05: evidence-backed personnel suggestions, never personnel mutations."""

from skills.base import TextSkill
from skills.models import PersonnelOutput, SkillCode, SkillInput


class S05Skill(TextSkill[PersonnelOutput]):
    code = SkillCode.S05
    output_model = PersonnelOutput
    requires_confirmed = True
    instructions = """依据输入中的具体事实提出人员建议：骨干学生可“拟降级”或“拟奖励”，
基层学生可“拟退出”或“拟提拔”。每项提供 action、person_id、建议理由、具体事实列表、
来源记录编号。不处理部长和团队负责人，不把缺失材料或单次表达简略当成负面事实。
建议应有充分事实依据，不推断思想、人格，不自动执行人员变动。没有充分依据返回空列表。
"""

    def validate_output(self, output: PersonnelOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        records = {record.record_id: record for record in data.records}
        seen = set()
        for item in output.items:
            key = (item.action, item.person_id)
            if key in seen:
                raise ValueError("S05 同一人员同一建议类型重复")
            seen.add(key)
            required = "骨干学生" if item.action in {"拟降级", "拟奖励"} else "基层学生"
            roles = {records[source].role for source in item.source_record_ids
                     if records[source].person_id == item.person_id}
            if roles != {required}:
                raise ValueError("S05 建议类型与来源人员角色不符")
