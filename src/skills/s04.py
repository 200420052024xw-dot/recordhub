"""S04: extract stage progress and difficulties from prepared check text."""

from skills.base import TextSkill
from skills.models import ProgressOutput, SkillCode


class S04Skill(TextSkill[ProgressOutput]):
    code = SkillCode.S04
    output_model = ProgressOutput
    requires_confirmed = True
    instructions = """按工作事项汇总本批材料的阶段进展，覆盖培训、工程、论文、专利、竞赛、
本科及研究生毕业论文等实际出现的事项。每项返回工作名称、来源部门编号、参与者编号、
当前进展、主要困难和来源记录编号。不同日期的同一事项可合并，依据按日期理解。
材料未提到困难时 main_difficulties 留空，不将“未提到”表述为“没有困难”。
本次材料范围已由程序确定，不自行限定为三天，不重筛日期。
"""
