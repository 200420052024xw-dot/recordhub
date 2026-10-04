"""S06: identify ideas actually proposed in the supplied records."""

from skills.base import TextSkill
from skills.models import IdeaOutput, SkillCode


class S06Skill(TextSkill[IdeaOutput]):
    code = SkillCode.S06
    output_model = IdeaOutput
    requires_confirmed = True
    instructions = """提取材料中实际提出的有潜力 idea。每项返回 idea 内容、proposer_id
以及来源记录编号。idea 可以归纳表述，但不能替学生发明新想法或将一般困难当作已提出的 idea。
来源部门、姓名由程序根据提出者和来源记录补入，不另行生成。没有有效 idea 返回空列表。
"""
