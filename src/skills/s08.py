"""S08: suggest reusable technical knowledge from supplied evidence."""

from skills.base import TextSkill
from skills.models import SkillCode, SkillInput, TechnologyOutput


class S08Skill(TextSkill[TechnologyOutput]):
    code = SkillCode.S08
    output_model = TechnologyOutput
    requires_confirmed = True
    instructions = """提炼可复用的工程或科研公共技术。每项填写技术名称、类别、解决的问题、
来源部门、贡献者、已有成果、成果出处、适用场景、入库或补充建议、待完善事项、来源记录编号。
材料没有成果或技术详情时对应文字留空、出处列表为空，并在 pending_items 说明待补充内容。
achievement_refs 只能使用来源记录已提供的出处，不编造网址、技术编号或已经入库的事实。
只提出入库建议；不假定已经检索公共技术库，不把普通学习事项夸大为成熟技术成果。
"""

    def validate_output(self, output: TechnologyOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        records = {record.record_id: record for record in data.records}
        for item in output.items:
            refs = {ref for source in item.source_record_ids
                    for ref in records[source].achievement_refs}
            self._validate_ids("achievement_refs", item.achievement_refs, refs)
