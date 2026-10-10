# RecordHub

上游仓库：[200420052024xw-dot/recordhub](https://github.com/200420052024xw-dot/recordhub)。本地唯一项目根目录是 `F:\RecordHub`；IDE、终端和部署均从这里打开／运行。不要再在根目录内克隆一份 `recordhub/`。目录用途与旧副本备份见 [项目目录说明](docs/项目目录说明.md)。

以 `docs/学生工作日志层级管理系统.xlsx` 为当前字段基准。启用周期流程使用 `RECORDHUB_WORKFLOW2_ENABLED`。

RecordHub 的 Workflow1 每天读取前一自然日的飞书工作日志，逐条调用一次 DeepSeek Prompt，收集人工评价后生成三级飞书云文档。消息发送与错误告警见 [调用说明](docs/消息发送与错误告警.md)。

## 代码位置

- `tool/`：飞书鉴权、多维表格、消息、Drive 和云文档接口，以及 HTTP 传输与表字段解析。
- `data/`：组织主数据与工作日志读取（全系统共用的基础服务）、状态存储引擎（run 文件按工作流分目录 `data/state/<workflow>/YYYY-MM-DD.json`，组织缓存共用 `data/state/organization.json`）。
- `llm/`：DeepSeek 客户端和结构化输出校验。
- `config/`：技术底座环境参数（飞书、DeepSeek、路径、服务开关、管理员与运维项）、日程、现有飞书表字段映射。
- `service/`：客户端装配、运行时工作流注册、调度、长连接事件和统一的 HTTP API。
- `src/workflow1/`：Workflow1 专属的每日流程、快照、评价、报告和文档。问卷接口 Token 配在 `.env`，人工评价截止时间 `auto_advance_at` 配在 `config/schedules.toml`。唯一业务 Prompt 是 `prompts/S01.txt`。

## 飞书表格

Workflow1 使用人员表、部门表、工作日志表、AI评价表、人工评价表和报告日志表。基层学生仅填写日志，不加入飞书组织；他们仍需保留在人员表中，姓名须在基层学生中唯一。日志使用「姓名：」，AI 评价使用「被评价人姓名」，人工评价使用「被审核基层：」匹配其人员编号；骨干及以上继续使用人员字段和 OpenID。人员表中的骨干及以上填写飞书账号手机号，程序批量换取 OpenID。人员表和部门表仅在启动或显式刷新时读取，缓存于 `data/state/organization.json`。应用需开通「通过手机号或邮箱获取用户 ID」（`contact:user.id:readonly`）权限。AI 评价表首列为可写文本“业务编号”；Workflow2 各表首列为可写文本“任务编号”。

程序用原日志记录 ID 保存 AI 评价的归属；人工评价表的 `评价编号` 是独立自动编号，按「被审核基层：」或「被审核骨干：」及直属评价填写人匹配当日快照。审核通知按评价人角色分发：部长收到部长审核表，用于审核骨干日志；骨干学生收到骨干审核表，用于审核基层日志。两个表单链接配置在 `config/schedules.toml` 的 `workflow1_daily` 中。**每人每天只保留最后提交的一条日志**。基层姓名重复会触发组织关系异常，须先更正；无法匹配的日志会跳过并记录异常。未交日志的人会在 AI 评价表回写“未填写日志”标注。每日状态保存人员快照、评价进度、文档 Token、通知结果和异常台账。报告日志表的「飞书文档链接」仅记录每日日报；阶段汇报链接写入报告阶段工作表，月度 S05／S06／S07 小类链接写入报告部门分析表；公共技术与公共培训建议周报链接分别写入每月团队分析表的对应列。

在 `config/tables.toml` 中填写实际表 ID。归档父文件夹 Token 配在 `.env` 的 `RECORDHUB_ARCHIVE_PARENT_FOLDER_TOKEN`。云文档按 `YYYY-MM-DD / 部长_部门编号 / 骨干_人员编号` 归档，并在上级文档放置可点击的下级引用。

## 运行

```powershell
Copy-Item .env.example .env
# 手工创建 config/tables.toml：表集合与字段映射以
# tests/test_existing_bitable_layout.py 的 _TABLE_REGISTRY_TOML 为唯一参照
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
.\.venv\Scripts\python.exe main.py check-config
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py serve --host 0.0.0.0 --port 8000
```

运行日志同时输出到终端（stderr，不污染 CLI 的 JSON stdout）和 `logs/recordhub.log`，每天 0 点轮转，按 `RECORDHUB_LOG_RETENTION_DAYS` 保留天数（默认 3 天，含当天），级别可用 `RECORDHUB_LOG_LEVEL` 调整；HTTP 访问日志一并写入文件。

错误日志包含调用堆栈、处理阶段、业务日期与接口错误详情，并通过 `RECORDHUB_ADMIN_OPEN_ID` 即时通知管理员。历史漏发和发送失败的业务消息不再自动补发；管理员可通过 `POST /admin/notifications/send` 选择日期发送未成功的消息。规则、参数和调用示例见 [消息发送与错误告警](docs/消息发送与错误告警.md)。

每日运行状态在 `data/state/workflow1/YYYY-MM-DD.json`。只运行一个服务进程（日志文件轮转不支持多进程共写）。已完成状态按 `RECORDHUB_SNAPSHOT_RETENTION_DAYS` 保留，未完成状态不清理，每天 3:30 执行清理。服务启动时不执行工作流，只重读人员表/部门表（失败时使用持久化的组织缓存）；每日任务随后按日程运行，未完成的人工评价在配置的截止时间采用现有 AI 结果并在文档标注"人工未评价"。飞书长连接用于及时核对问卷结果。飞书消息采用稳定 UUID 去重；发送结果不明且超过飞书去重窗口时会停下并要求人工核对。

当前版本的云文档接口和真实飞书表字段仍需在目标租户中联调。配置检查只验证本地映射，无法替代飞书 API 权限与字段类型检查。


## Workflow2 周期分析

- `src/workflow2/`：周期任务、已完成日志材料准备、Skill 配置、确认、表格回填和云文档生成。
- `src/workflow2/s04.py` 至 `s09.py`：S04—S09 的输入输出约束与来源校验。
- `prompts/S04.txt` 至 `prompts/S09.txt`：各项周期分析的提示词；`prompts/S10.txt` 用于团队汇总，边界规则已写入每个提示词，详见 [Prompt 用途](prompts/README.md)。
- `data/state/checked/workflow1/`：已完成的每日快照，供三天、周度和月度分析使用。

个人 Skill 从飞书 Skill 表读取，字段为“使用人、角色、Skill内容、功能、审核结果、未通过原因”。系统每天 00:55 扫描“审核结果”为空的新记录，用固定案例检查输出 JSON 结构；通过后将该记录设为“通过”并把同一使用人、同一功能的旧版本设为“未使用”，未通过时写明原因并通知使用人。审核通过的 Prompt 缓存在 `data/state/skill_prompt_cache.json`；没有有效个人版本时使用 `prompts/` 中的内置版本。

设置 `RECORDHUB_WORKFLOW2_ENABLED=true` 后启用自动调度。首次启用日期保存在 `data/state/workflow2_activation.toml`，重启不会重新计时；S04 的等待天数、运行时刻和间隔配置在 `config/schedules.toml`。服务启动时不补跑历史周期；S04 从启用后的第一个完整自然日开始收集，满 3 天后首次运行，此后每 3 天运行一次。周报在周一、月报在每月 1 日按原周期运行，有多少已完成且可用的材料就分析多少，并在结果中注明跳过的材料数量。关闭再开启 Workflow2 不会重置首次启用日期。
