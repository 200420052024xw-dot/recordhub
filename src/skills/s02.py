"""S02: suggest evaluations for every supplied submitted log and summarize text."""

from skills.base import TextSkill
from skills.models import CheckOutput, SkillCode, SkillInput


class S02Skill(TextSkill[CheckOutput]):
    code = SkillCode.S02
    output_model = CheckOutput
    instructions = """逐条检查程序提供的已交日志，evaluations 中每条输入记录恰好出现一次。
每项返回原 record_id、positive（肯定之处）、improvement（改进之处）。
肯定具体详实的工作和有新意的想法，建议指出可补充的工作细节、心得或反思。
依据不足时如实说明该日志信息有限，不能捏造优点或进行人格评判。
summary 总结全部已交人员的工作情况，不输出人数，不改变原文或已有评价。
程序负责直属关系、前一日范围、未交状态和统计；本 Skill 只生成文字建议。
"""

    def validate_input(self, data: SkillInput) -> None:
        if data.scope.start_date != data.scope.end_date:
            raise ValueError("S02 输入必须是程序准备好的单日材料")

    def validate_output(self, output: CheckOutput, data: SkillInput) -> None:
        expected = {record.record_id for record in data.records if record.submitted}
        actual = [item.record_id for item in output.evaluations]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("S02 必须对全部已交记录各生成一条评价，不能遗漏、重复或新增")
