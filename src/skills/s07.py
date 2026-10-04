"""S07: prepare evidence-backed discussion topics for student meetings."""

from skills.base import TextSkill
from skills.models import MeetingFocusOutput, SkillCode, SkillInput


class S07Skill(TextSkill[MeetingFocusOutput]):
    code = SkillCode.S07
    output_model = MeetingFocusOutput
    requires_confirmed = True
    instructions = """筛查有具体依据的重点谈话学生，每人一条。categories 可多选：
思想或态度沟通、持续困难、兴趣、好idea、培养潜力；同时填写具体 evidence 和来源记录编号。
思想或态度沟通仅提出需要了解的沟通线索，不作思想、心理诊断；持续困难需要材料支持其持续性。
只处理基层或骨干学生。单次未交、输入缺失不能被推断为态度问题。不决定实际例会时间。
"""

    def validate_output(self, output: MeetingFocusOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        records = {record.record_id: record for record in data.records}
        seen = set()
        for item in output.items:
            if item.person_id in seen or len(item.categories) != len(set(item.categories)):
                raise ValueError("S07 人员或关注类别重复")
            seen.add(item.person_id)
            if any(records[source].role not in {"基层学生", "骨干学生"}
                   for source in item.source_record_ids
                   if records[source].person_id == item.person_id):
                raise ValueError("S07 重点谈话对象必须是学生")
