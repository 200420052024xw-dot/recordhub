"""S09: suggest shared training using only supplied needs and resources."""

from skills.base import TextSkill
from skills.models import SkillCode, SkillInput, TrainingOutput


class S09Skill(TextSkill[TrainingOutput]):
    code = SkillCode.S09
    output_model = TrainingOutput
    requires_confirmed = True
    instructions = """根据共同困难和能力需求提出公共培训建议。每项填写主题、面向人员描述及编号、
共同需求、具体依据、预期学习效果、可用资源编号、开展方式、课程新增或调整建议、来源记录编号。
available_resource_ids 只能使用本次 resources 提供且有相关来源依据的课程或技术编号。
未提供资源时返回空列表，不编造课程和技术编号，不声称已检索公共库。分析只产生建议。
"""

    def validate_output(self, output: TrainingOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        resources = {item.resource_id: item for item in data.resources}
        for item in output.items:
            self._validate_ids("available_resource_ids", item.available_resource_ids,
                               set(resources))
            for resource_id in item.available_resource_ids:
                if not set(resources[resource_id].source_record_ids) & set(item.source_record_ids):
                    raise ValueError("S09 使用的课程或技术缺少本条建议的来源依据")
