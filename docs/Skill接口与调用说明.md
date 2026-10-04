# Python Skill 接口与调用说明

本版实现 S01—S09 的独立 Python Skill。AI 输出为经 Pydantic 校验的 JSON 数据，不生成 Markdown 文档。Prompt 写在各 Skill 的 Python 文件内，共用现有 `llm/PromptService` 和 `DeepSeekClient`。现有每日 Workflow1 仍通过 `prompts/S01.txt` 评价日志；它与新增的 S01 日志整理模块是两个明确的调用入口。

## 1. 输入和责任边界

程序先准备完整材料，再构造 `SkillInput`。Skill 不读取飞书、不决定人员权限、不选日期、不计算提交人数、不保存结果、不发送消息，也不自动确认或执行人员变动。

| 输入字段 | 含义 |
| --- | --- |
| `run_id` | 调用方生成的稳定任务编号，重试使用同一编号 |
| `scope` | 实际起止日期、部门编号、材料完整性和缺失来源 |
| `records` | 已筛选且去重的逐日志记录，含原记录编号及版本、人员身份、实际工作日期、表格文字、已有评价和成果出处 |
| `resources` | 本批材料确实包含的课程或技术编号及其来源；没有时传空列表 |

`TextRecord` 的文字字段为 `progress`、`difficulties`、`reflection`、`other`、`full_log`。`work_start/work_end` 是实际工作日期，不能直接把提交时间冒充工作日期。学生日志为单日，部长日志可以覆盖一周。

未交记录设置 `submitted=false`，文字、成果、评价保持为空。它用于调用方保留原清单，并不会发给 AI 生成评价。统计应由程序依据名单和完整读取结果计算。不要把读取失败或搜索未命中转成未交。

`confirmed` 表示来源检查材料已经完成确认，包括按现有每日流程合法收口的情况。S01/S02 接受待确认原日志；S03—S09 只分析已经确认的输入。缺失材料或未确认输入返回 `BLOCKED`，不调用 AI。完整但没有任何已交文字时返回 `NO_MATERIAL`。没有发现有效专项结论时返回 `items=[]`，结果仍等待人工确认。

范围校验只检查调用方的声明与材料是否一致，不代表权限校验。部门权限、教师跨部门权限及组织关系仍由调用方负责。配置归属部门可以是团队管理部门，数据范围则可以包含负责人有权查看的其他部门。

## 2. 每个 Skill 的 content

字段名稳定，所有对象禁止额外字段；程序使用 `result.content` 或 `result.model_dump(mode="json")["content"]` 处理结果。

| Skill / Python 文件 | content 字段 |
| --- | --- |
| S01 `src/skills/s01.py` | `progress`、`difficulties`、`reflection`、`other`；一次整理一条本人原日志，未提供的事实留空 |
| S02 `src/skills/s02.py` | `evaluations`：每条已交日志恰好一项，含 `record_id/positive/improvement`；`summary`：整体工作总结 |
| S03 `src/skills/s03.py` | `summary`：对程序已合并的单日检查材料生成整体工作总结，不重写原日志或评价 |
| S04 `src/skills/s04.py` | `items`：`work_item/department_ids/participant_ids/current_progress/main_difficulties/source_record_ids` |
| S05 `src/skills/s05.py` | `items`：`action/person_id/reason/facts/source_record_ids`；按 `action` 可由代码分组 |
| S06 `src/skills/s06.py` | `items`：`idea/proposer_id/source_record_ids` |
| S07 `src/skills/s07.py` | `items`：`person_id/categories/evidence/source_record_ids`；每人一项，关注类别可多选 |
| S08 `src/skills/s08.py` | `items`：`technology_name/category/problem_solved/department_ids/contributor_ids/existing_achievements/achievement_refs/suitable_scenarios/repository_suggestion/pending_items/source_record_ids` |
| S09 `src/skills/s09.py` | `items`：`topic/target_audience/target_person_ids/common_need/evidence/expected_effect/available_resource_ids/approach/course_suggestion/source_record_ids` |

S05 的 `action` 为“拟降级、拟奖励、拟退出、拟提拔”。前两项限骨干，后两项限基层。S07 的 `categories` 为“思想或态度沟通、持续困难、兴趣、好idea、培养潜力”。S08 的 `category` 为“工程、科研”。

人员编号、部门编号和来源记录编号不能由 AI 自由生成。代码校验结论中的人员、部门是否属于本条引用的记录；成果出处限于来源记录中已有的值，S09 资源编号限于本批 `resources` 并保留相应来源。姓名、角色、统计、确认人和表格关联字段应由程序补入。

这些校验能拦截无效结构和未知引用，不能代替人工核实事实、评价质量及建议是否合理。

## 3. 统一结果外层

`SkillResult` 外层全部由 Python 填写：`result_id/run_id/skill_code/config_id/config_department_id/config_version/based_on_version/user_id/status/scope/source_record_ids/generated_at/content/message`，以及可选的 `confirmed_by/confirmed_at/material_statistics`。

`generated_at` 为带时区的 UTC ISO 日期时间，调度和实际材料日期仍由程序按北京时间准备。`result_id` 根据任务、Skill、使用人及配置版本生成，重试保持稳定。外层的 `source_record_ids` 保留整批输入；S04—S09 每项另外保存实际引用的来源。

`WAITING_CONFIRMATION` 表示成功生成的建议，尚待人确认；编排程序完成确认后标记 `CONFIRMED`。`NO_MATERIAL` 和 `BLOCKED` 的 `content` 为 `null`。异常 API 响应、格式错误或引用错误沿用 `PromptService` 重试，耗尽后抛出既有 `LLMError/LLMValidationError`，由调用方记录任务失败。修正材料或配置后可用相同任务编号重试。

## 4. 部门及个人配置

每次调用都传入配置归属 `department_id` 和使用人 `user_id`。优先使用该部门内的个人配置，其次部门配置，最后绑定该部门的内置团队默认模板。配置可通过 `SkillConfig.instructions` 补充评价侧重点、分析方法、领域例子和用语。固定结构及来源校验由代码控制。

`SkillConfig` 字段为 `config_id/skill_code/department_id/user_id/version/based_on_version/instructions/enabled`。部门默认配置的 `user_id=null`。调用方负责选择唯一生效版本；同一优先级有多个生效版本会报错。修改规则时应更新版本号。内置模板当前为 `1.0`，修改版的 `based_on_version` 与当前模板不同时明确报错，要求显式迁移，防止静默升级。

独立 Skill 的配置列表由调用方提供；新增 S04—S09 编排层已经接入飞书配置表、调度和结果写回，见 `docs/周期分析接入与验收说明.md`。S01—S03 继续提供独立 Python 调用接口。

## 5. Python 调用

安装项目后可直接导入；从源代码启动的程序需像现有 `main.py` 一样把 `src` 加入导入路径。

```python
import json
from pathlib import Path
from llm import PromptService
from skills import SkillInput, SkillRunner

# 使用现有 DeepSeekClient 实例。此处 client 由调用方提供。
runner = SkillRunner(PromptService(client, max_attempts=3))
data = SkillInput.model_validate_json(
    Path("examples/skill_input.json").read_text(encoding="utf-8")
)
result = runner.run("S04", data, department_id="d1", user_id="minister-1")
payload = result.model_dump(mode="json")
print(json.dumps(payload, ensure_ascii=False))

# 获取严格的输出 JSON Schema，便于其他代码对接。
schema = SkillRunner.output_schema("S04")
```

可运行示例（会调用 DeepSeek，输入文件中的人员和内容全部是示例）：

```powershell
python examples/run_skill.py S04 --input examples/skill_input.json --department-id d1 --user-id example-minister-1
```

示例只需要 `DEEPSEEK_API_KEY`，其他模型配置沿用 `.env` 和现有默认值；不会读取或写入飞书。可通过 `--configs` 传入 JSON 配置数组。

## 6. 验证

```powershell
python -m unittest discover -s tests -p test_merge_integration.py -v
python -m unittest discover -s tests -v
```

测试使用模拟模型响应，验证全部九个输出、引用限制、空材料、缺失材料、确认边界、配置优先级、版本隔离、稳定结果编号和重试。真实模型生成的文字质量需在业务接入后结合实际材料确认。

此前开发版本曾通过 75 项测试；合并前目录已缺少当时新增的三个测试文件。本次新增 `tests/test_merge_integration.py`，验证新框架与 Skill、周期任务的接入；当前结果见合并说明。回归使用工作目录中的临时依赖，没有修改全局 Python 环境。新增能力的真实 DeepSeek 请求和飞书业务写入尚未在本轮执行。
