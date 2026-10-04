# RecordHub

RecordHub 的 Workflow1 每天读取前一自然日的飞书工作日志，逐条调用一次 DeepSeek Prompt，收集人工评价后生成三级飞书云文档。目标行为见 [Workflow1 改造目标文档](docs/Workflow1_改造目标文档.md)。

## 代码位置

- `tool/`：飞书鉴权、多维表格、消息、Drive 和云文档接口，以及 HTTP 传输与表字段解析。
- `data/`：组织主数据与工作日志读取（全系统共用的基础服务）、状态存储引擎（run 文件按工作流分目录 `data/state/<workflow>/YYYY-MM-DD.json`，组织缓存共用 `data/state/organization.json`）。
- `llm/`：DeepSeek 客户端和结构化输出校验。
- `config/`：技术底座环境参数（飞书、DeepSeek、路径、服务开关、管理员与运维项）、日程、现有飞书表字段映射。
- `service/`：客户端装配、运行时工作流注册、调度、长连接事件和统一的 HTTP API。
- `src/workflow1/`：Workflow1 专属的全部业务——每日流程 `workflow.py`、快照与评价归属 `snapshot.py`、AI/人工评价表适配 `evaluations.py`、报告索引 `reports.py`、三级文档 `documents.py`，以及只归它的参数 `settings.py`（问卷事件 Token `RECORDHUB_CONFIRMATION_WEBHOOK_TOKEN`、自动放行 `RECORDHUB_AUTO_ADVANCE_AT`）。唯一业务 Prompt 是 `prompts/S01.txt`。

## 飞书表格

以 [学生工作日志层级管理系统 .xlsx](docs/学生工作日志层级管理系统%20.xlsx) 的现有列为准。Workflow1 使用人员表、部门表、工作日志表、AI评价表、人工评价表和报告日志表。成员在人员表的 `手机号` 列填写飞书账号手机号，无需填写 OpenID。**人员表和部门表只在服务启动时各读取一次**（批量换取 OpenID 并跳过手机号缺失或查不到 ID 的人员，连同 OpenID 一起持久化到 `data/state/organization.json`），运行期间不再自动重读；人员的新增、修改、删除、查询通过管理接口完成，见 [人员管理接口文档](docs/人员管理接口文档.md)。应用需开通「通过手机号或邮箱获取用户 ID」（`contact:user.id:readonly`）权限。配置 `RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID` 进行联调时，程序直接使用人员表「姓名」人员字段中的 ID，并将所有通知发给该配置的测试账号，不查询手机号。不要求新增业务键、目标日期、确认状态或文档 Token 列。

程序用原日志记录 ID 保存 AI 评价的归属；人工评价表的 `评价编号` 是独立自动编号，程序按被评价人、直属评价填写人和提交时间匹配当日快照。**每人每天只保留最后提交的一条日志**（更早的自动被取代并记入异常台账），问卷因此总能唯一定位日志。未交日志的人会在 AI 评价表回写一行"未填写日志"标注（评价编号 `日期:MISSING:人员编号`）。单点异常（提交人不在人员表、缺提交时间、LLM 失败、缺 OpenID、云文档被人工编辑）不再拖垮整天流程：跳过并标注，统一记入当日异常台账，并在配置 `RECORDHUB_ADMIN_OPEN_ID` 后逐条即时通知管理员；失败日志只能通过重评接口手动重跑，详见 [异常流程与重评接口](docs/异常流程与重评接口.md)。每日状态保存人员快照、评价进度、文档 Token、通知结果和异常台账。现有报告日志表没有文档链接列，文档地址附在「文本」列中。

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

每日运行状态在 `data/state/workflow1/YYYY-MM-DD.json`。只运行一个服务进程。**已完成的状态保留 2 天**（任意时刻本地都有"昨天评价的＋今天评价的"两天数据，供后续 workbuddy 等流程读取；未完成的状态不清理），由 `RECORDHUB_SNAPSHOT_RETENTION_DAYS` 控制，每天 3:30 执行清理。服务重启时从已保存快照继续，并重读一次人员表/部门表（启动重读失败时使用持久化的组织缓存）；未完成的人工评价在配置的截止时间采用现有 AI 结果并在文档标注"人工未评价"，已填写的人工意见保留。飞书长连接用于及时核对问卷结果（人员/部门表变更不再触发组织缓存重读）。飞书消息采用稳定 UUID 去重；发送结果不明且超过飞书去重窗口时会停下并要求人工核对。

当前版本的云文档接口和真实飞书表字段仍需在目标租户中联调。配置检查只验证本地映射，无法替代飞书 API 权限与字段类型检查。
