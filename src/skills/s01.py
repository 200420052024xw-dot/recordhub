"""S01: organize one person's supplied log text into table-ready fields."""

from skills.base import TextSkill
from skills.models import LogTextOutput, SkillCode, SkillInput


class S01Skill(TextSkill[LogTextOutput]):
    code = SkillCode.S01
    output_model = LogTextOutput
    instructions = """整理唯一一条本人原始日志，返回 progress（做了什么与具体进展）、
difficulties（困难）、reflection（心得或反思）、other（其他事项）四个表格文字字段。
只改善表达和组织，不评价表现，不替本人编造反思，不生成提交状态或人员信息。
优先以完整日志为原文，结合其他已填写字段。原文没有的信息对应字段留空字符串。
部长周日志按程序提供的起止日期整理；不推算日期，不将其改成学生日记。
"""

    def validate_input(self, data: SkillInput) -> None:
        if len(data.records) > 1:
            raise ValueError("S01 每次只整理一条本人日志")
