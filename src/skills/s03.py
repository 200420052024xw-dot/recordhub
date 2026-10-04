"""S03: summarize already merged, counted and confirmed daily check materials."""

from skills.base import TextSkill
from skills.models import SkillCode, SkillInput, SummaryOutput


class S03Skill(TextSkill[SummaryOutput]):
    code = SkillCode.S03
    output_model = SummaryOutput
    requires_confirmed = True
    instructions = """依据程序已合并的原始日志及已有评价整理整体工作 summary。
总结覆盖全部已交人员，说明主要工作、进展和困难。尊重不同评价来源，不覆盖原评价。
部门和团队范围已由程序确定，不重新合并、排序、去重、计数，也不新增个人评价或评分。
"""

    def validate_input(self, data: SkillInput) -> None:
        if data.scope.start_date != data.scope.end_date:
            raise ValueError("S03 输入必须是程序准备好的单日检查材料")
