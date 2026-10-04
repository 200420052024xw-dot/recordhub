# RecordHub

RecordHub 的 Workflow1 每天读取前一自然日的飞书工作日志，逐条调用一次 DeepSeek Prompt，收集人工评价后生成三级飞书云文档。目标行为见 [Workflow1 改造目标文档](docs/Workflow1_改造目标文档.md)。

## 代码位置

- `tool/`：飞书鉴权、多维表格、消息、Drive 和云文档接口，以及 HTTP 传输。
- `data/`：组织与日志读取、AI／人工评价表适配、本地快照与外部对象映射。
- `llm/`：DeepSeek 客户端和结构化输出校验。
- `config/`：环境、日程、现有飞书表字段映射。
- `service/`：启动、调度、长连接事件和管理接口。
- `src/workflow1/`：每日流程和文档内容编排。唯一业务 Prompt 是 `prompts/S01.txt`。

## 飞书表格

以 [学生工作日志层级管理系统 .xlsx](docs/学生工作日志层级管理系统%20.xlsx) 的现有列为准。Workflow1 使用人员表、部门表、工作日志表、AI评价表、人工评价表和报告日志表。成员在人员表的 `手机号` 列填写飞书账号手机号，无需填写 OpenID。程序刷新人员缓存时调用飞书「通过手机号或邮箱获取用户 ID」接口，将手机号批量换取 OpenID，用于人员字段写入和消息发送。应用需开通「通过手机号或邮箱获取用户 ID」（`contact:user.id:readonly`）权限，且通讯录可见范围需覆盖相关成员；手机号缺失或查询不到时会使刷新失败并提示人员编号。不要求新增业务键、目标日期、确认状态或文档 Token 列。

程序用原日志记录 ID、评价人及 `评价编号` 对应 AI 与人工评价；本地每日状态保存日期、人员快照、评价进度、文档 Token 和通知结果。人工评价表的 `评价编号` 必须与对应 AI 评价的编号一致，问卷填写人必须是直属评价人。若关联值不一致，流程记录错误，不猜测归属。

在 `config/feishu_tables.toml` 中填写实际表 ID。归档父文件夹 Token 配在 `.env` 的 `RECORDHUB_ARCHIVE_PARENT_FOLDER_TOKEN`。云文档按 `YYYY-MM-DD / 部长_部门编号 / 骨干_人员编号` 归档，并在上级文档放置可点击的下级引用。

## 运行

```powershell
Copy-Item .env.example .env
Copy-Item config/feishu_tables.example.toml config/feishu_tables.toml
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
.\.venv\Scripts\python.exe main.py check-config
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py serve --host 0.0.0.0 --port 8000
```

每日运行状态在 `data/state/workflows/YYYY-MM-DD.json`。只运行一个服务进程。服务重启时从已保存快照继续；未完成的人工评价在配置的截止时间采用现有 AI 结果，已填写的人工意见保留。飞书长连接用于及时触发组织缓存刷新和问卷结果核对。飞书消息采用稳定 UUID 去重；发送结果不明且超过飞书去重窗口时会停下并要求人工核对。

当前版本的云文档接口和真实飞书表字段仍需在目标租户中联调。配置检查只验证本地映射，无法替代飞书 API 权限与字段类型检查。
