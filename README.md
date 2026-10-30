# RecordHub

基于飞书多维表格与 DeepSeek 的团队工作日志记录、评价与周期分析服务。

RecordHub 把「学生写日志 → 骨干/部长评价 → 部门汇总 → 团队周期分析」整条链路搬到飞书：读取多维表格里的组织架构和工作日志，调用大模型生成评价与分析，再通过飞书消息、表单和多维表格回写结果。服务同时提供 HTTP 接口、定时调度、飞书长连接事件处理和本地可恢复的工作流状态。

## 主要能力

### Workflow 1：每日日志评价

- 读取前一天的日志，按组织架构确定每条日志的评价人（骨干审核基层、部长审核骨干）。
- 调用 DeepSeek 生成「肯定之处 / 改进之处」，写入 AI 评价表。
- 到点向评价人推送表单或对话入口链接；人工结果通过表单、飞书多维表格或 WorkBuddy 接口收回。
- 截止时间（默认 21:50）自动放行未确认的评价：有人工内容用人工，缺人工用 AI 草稿并标记「（AI未确认）」。
- 把多条评价按日志合并成一条「最终评价」，回写日志表，并生成部门日报/汇总。
- 支持多部门任职：同一个人可以在多个部门拥有不同的角色和直属上级，按部门分别统计与评价。

### Workflow 2：周期分析

| 编号 | 内容 | 周期 |
| --- | --- | --- |
| S04 | 阶段工作进展与困难归纳 | 每 3 个完整自然日 |
| S05 | 降级 / 奖励 / 退出 / 提拔名单建议 | 每月（上一自然月） |
| S06 | idea 清单 | 每月（上一自然月） |
| S07 | 学生例会重点人员 | 每月（上一自然月） |
| S08 | 公共技术建议 | 每周（上一自然周） |
| S09 | 公共培训建议 | 每周（上一自然周） |

分析结果先写入部门分析表，部长通过确认表（或飞书长连接事件）确认/修改后，再推送定稿报告。周期材料优先从当天快照和部门名册重建，快照只保留短期，历史数据仍可从飞书原表恢复。

### WorkBuddy 对话入口

面向学生和老师的对话式接口，与管理员接口隔离：

- **提交**：按姓名识别身份，在每天 12:00–23:50 的窗口内接收工作日志；窗口外返回 `CLOSED`，不允许补交。
- **查询**：按人、某个骨干的小组或某个部门查询一段时间的日志和最终评价。
- **批改**：老师查看当天待确认的评价并提交修改，21:40 后合并进飞书表，21:50 随工作流统一定稿。

### 运维能力

- 飞书长连接接收多维表格记录变更，事件只入队，读写由工作线程处理。
- APScheduler 按 `config/schedules.toml` 安排任务；错过执行会补跑，失败按配置重试。
- JSONL 运行日志按天/大小轮转，按保留天数清理；异常可导出诊断资料。
- 管理员告警：持续故障汇总、AI 辅助分析、过期未完成日期巡检；未完成状态超过期限会告警并最终清理。
- 单进程本地状态：每天一个原子写入的 JSON 状态文件，可断点续跑。

## 架构

```text
飞书开放平台                          DeepSeek
  │  Bitable / 消息 / 长连接事件          │  Chat Completions
  ▼                                      ▼
service/api.py  ── FastAPI :8100 ── service/runtime.py
  │
  ├── service/orchestration.py   APScheduler 排程、重试、恢复
  ├── src/workflow1/             每日日志评价、快照、名册、最终评价
  ├── src/workflow2/             周期材料准备、分析、确认、报告
  ├── service/workbuddy_query/   对话入口查询口径
  ├── service/workbuddy_review/  对话入口批改口径
  ├── llm/                       Prompt 仓库与 DeepSeek 客户端
  ├── data/                      状态存储、仓储、组织缓存
  ├── config/                    .env、表映射、调度配置
  └── tool/                      飞书、云文档、HTTP、通知、可观测性
```

### 目录说明

```text
main.py                  CLI 入口（serve / check-config / 手工补跑等）
config/                  环境变量、表 ID 与字段映射、调度与截止时间
schema/                  Pydantic 领域模型（组织、日志、快照、运行状态）
data/                    状态文件、仓储、组织缓存
src/workflow1/           每日日志评价流程
src/workflow2/           周期分析流程
service/                 FastAPI、调度编排、告警、对话入口
llm/                     Prompt 加载与 DeepSeek 调用
tool/                    飞书 / 云文档 / HTTP / 通知 / 诊断工具
prompts/                 S01、S04–S10 的提示词
tests/                   单元测试（unittest）
```

## 运行要求

- Python **3.11+**
- 一个飞书自建应用，并开通多维表格、通讯录、消息、云文档相关权限
- 一个 DeepSeek API Key
- 可访问飞书开放平台和 DeepSeek 的网络

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

也可以 `pip install -e .` 安装为 `recordhub` 命令。

### 2. 配置环境变量

```bash
cp .env.example .env
```

至少填写：

| 变量 | 说明 |
| --- | --- |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭证 |
| `FEISHU_BITABLE_APP_TOKEN` | 工作日志多维表格的 App Token |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `RECORDHUB_ARCHIVE_PARENT_FOLDER_TOKEN` | 日报/周期报告云文档归档父文件夹 |

`.env` 已被 `.gitignore` 忽略，**不要提交真实密钥**。

### 3. 配置数据表映射

`config/tables.toml` 保存各数据表的 `table_id` 和字段中文名。首次部署可从空白模板复制：

```bash
cp config/tables.example.toml config/tables.toml
```

然后按实际飞书多维表格填写。`table_id` 是多维表链接里的 `tbl...`，不是表单的 `shrcn...`。可选的 `[tables.person_departments]` 用于跨部门任职；删掉或留空 `table_id` 即保持单部门模式。

### 4. 配置调度与提示词

- `config/schedules.toml`：时区、各任务执行时间、通知时间、确认截止时间、审核表单链接、日志提交窗口。
- `prompts/S*.txt`：大模型提示词，编号用途见 [`prompts/README.md`](prompts/README.md)。

### 5. 自检

```bash
python main.py check-config
```

输出 JSON，包含缺失变量、表配置错误、已启用的任务等；退出码非 0 表示配置不完整。

### 6. 启动服务

```bash
python main.py serve --host 0.0.0.0 --port 8100
```

启动后会：

1. 刷新组织架构缓存（失败时回退到本地持久化缓存）；
2. 按配置启动飞书长连接事件流；
3. 启动 APScheduler，恢复未完成的预约任务；
4. 提供 `/health/live` 与 `/health/ready`。

## 常用命令

```bash
# 配置自检
python main.py check-config

# 启动 HTTP 服务
python main.py serve --host 0.0.0.0 --port 8100

# 手工运行某天的 Workflow1（通常是补跑）
python main.py run-workflow --date 2026-10-30
python main.py resume-workflow --date 2026-10-30

# 刷新缺交统计
python main.py refresh-submission-statistics --date 2026-10-30

# 定稿待确认的人工评价（不传日期则处理全部未完成日期）
python main.py finalize-confirmations --date 2026-10-30

# 把对话入口暂存的批改结果合并进飞书表
python main.py flush-workbuddy --date 2026-10-30

# 重新生成并回写「最终评价」
python main.py write-final-evaluations --date 2026-10-30

# 周期分析
python main.py run-workflow2 --kind stage --date 2026-10-30
python main.py run-workflow2 --kind monthly --date 2026-11-01
python main.py run-workflow2 --kind weekly --date 2026-11-02
python main.py resume-workflow2 --run-id <run_id>

# 刷新组织缓存
python main.py refresh-cache organization

# 状态文件迁移
python main.py migrate-state --all

# 按事件 ID / 执行 ID 导出诊断资料（保留期内）
python main.py diagnostics --event-id <event_id>
python main.py diagnostics --execution-id <execution_id>
```

## HTTP 接口

所有业务接口都使用独立的 Bearer Token；某个 Token 未配置时，对应接口返回 `503`，配置后校验失败返回 `401`。

| 路径 | 用途 | Token |
| --- | --- | --- |
| `GET /health/live` / `GET /health/ready` | 存活 / 就绪检查 | 无 |
| `POST /webhooks/feishu/confirmations` | Workflow1 人工确认回调 | `RECORDHUB_CONFIRMATION_WEBHOOK_TOKEN` |
| `POST /admin/workflows/daily/{date}` | 手工启动每日流程 | `RECORDHUB_ADMIN_TOKEN` |
| `POST /admin/workflows/{date}/resume` | 断点续跑 | `RECORDHUB_ADMIN_TOKEN` |
| `GET /admin/workflows/{date}/issues` | 查看当天问题 | `RECORDHUB_ADMIN_TOKEN` |
| `POST /admin/submission-statistics/{date}` | 刷新缺交统计 | `RECORDHUB_ADMIN_TOKEN` |
| `POST /admin/reviews/merge/{date}` | 合并对话入口批改 | `RECORDHUB_ADMIN_TOKEN` |
| `POST /admin/notifications/send` | 手工补发通知 | `RECORDHUB_ADMIN_TOKEN` |
| `GET /admin/notifications/{date}` | 通知投递状态 | `RECORDHUB_ADMIN_TOKEN` |
| `POST/GET/PUT/DELETE /admin/organization/persons...` | 组织人员维护 | `RECORDHUB_ADMIN_TOKEN` |
| `POST /admin/cache/organization/refresh` | 刷新组织缓存 | `RECORDHUB_ADMIN_TOKEN` |
| `POST /admin/workflow2/{kind}/{date}` | 启动周期分析 | `RECORDHUB_ADMIN_TOKEN` |
| `POST /admin/workflow2/retry` | 重试卡住的周期任务 | `RECORDHUB_ADMIN_TOKEN` |
| `POST /workbuddy/v1/work-logs/identity` | 提交入口身份识别 | `RECORDHUB_WORKBUDDY_TOKEN` |
| `POST /workbuddy/v1/work-logs` | 提交工作日志 | `RECORDHUB_WORKBUDDY_TOKEN` |
| `POST /workbuddy/v1/work-logs/previous-evaluation` | 查询上一条评价 | `RECORDHUB_WORKBUDDY_TOKEN` |
| `POST /workbuddy/v1/queries/identity` | 查询入口身份识别 | `RECORDHUB_QUERY_TOKEN` |
| `POST /workbuddy/v1/queries` | 查询日志与最终评价 | `RECORDHUB_QUERY_TOKEN` |
| `POST /workbuddy/v1/reviews/pending` | 查看待批改评价 | `RECORDHUB_REVIEW_TOKEN` |
| `POST /workbuddy/v1/reviews` | 提交批改 | `RECORDHUB_REVIEW_TOKEN` |

## 调度时间线（默认配置）

| 时间 | 任务 |
| --- | --- |
| 00:00 | 生成当天预约计划 |
| 00:50 | 刷新组织缓存 |
| 00:55 | 检查待审核的个性化 Prompt |
| 01:00 | Workflow1 读取前一日日志并生成 AI 评价 |
| 03:30 | 清理过期状态、巡检未完成日期 |
| 04:00 | S04 阶段分析（每 3 天） |
| 06:00 | S08 / S09 周度分析（每周一） |
| 08:00 | 向评价人发送人工评价通知；S05–S07 月度分析（每月 1 日） |
| 12:00 | 日志提交窗口开放 |
| 21:40 | 对话入口批改截止并合并进飞书表 |
| 21:50 | 人工评价与周期确认截止，自动放行 |
| 22:00 | 推送部门日报 |
| 23:50 | 日志提交窗口关闭 |

以上时间均按 `config/schedules.toml` 中的 `timezone`（默认 `Asia/Shanghai`）解释，修改后重启服务生效。

## 状态、日志与保留策略

- 工作流状态：`data/state/`，按日期和流程分文件，原子替换写入；可断点续跑。
- 组织名册：`data/state/rosters/`，为历史重算保留当天的人员与部门快照。
- 运行日志：`RECORDHUB_LOG_DIR`（默认 `logs/`），JSONL + 可读控制台输出，按大小与天数轮转。
- 默认保留：
  - 已完成快照 `RECORDHUB_SNAPSHOT_RETENTION_DAYS=2` 天；
  - 日志与诊断资料 `RECORDHUB_LOG_RETENTION_DAYS=3` 天；
  - 未完成日期超过 3 天告警，超过 7 天清理状态文件（含飞书记录映射，清理时会告警）。
- 状态清理任务不会删除未完成的日期；未完成日期只能通过续跑、管理员处理或超期清理结束。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试不访问飞书或 DeepSeek，覆盖组织成员关系、快照迁移、名册归档、最终评价合并与缺交统计。

## 部署注意

- **必须单进程运行**：本地状态文件、通知队列和日志轮转都按单进程设计。不要使用多个 uvicorn worker，也不要同时启动两个实例。
- 建议由 systemd / 宝塔等进程管理器托管 `python main.py serve --host 127.0.0.1 --port 8100`，由反向代理负责 HTTPS。
- 飞书长连接需要出网访问飞书开放平台；如果使用 Webhook 回调而不是长连接，需在飞书后台配置可访问的 HTTPS 地址。
- 升级前备份 `data/state/` 与 `.env`；发布时可用 `RECORDHUB_RELEASE` 标记版本，方便日志定位。
- 修改 `config/tables.toml` 或 `config/schedules.toml` 后重启服务。

## 安全与数据

- `.env`（密钥）、`data/state/`、`logs/`、`data/backups/` 不应提交；它们已在 `.gitignore` 中排除。
- `data/backups/` 中的名单导入备份可能包含姓名、手机号等个人信息，不要用 `git add -f` 强行加入。
- `docs/` 下的交付文档可能包含接口 Token，部署分支已在 `.gitignore` 中整体排除。
- `config/tables.toml` 保存真实表 ID 和字段名，本身不是凭证。如果仓库需要对外公开，建议只保留空白的 `config/tables.example.toml`，部署时在本地生成 `config/tables.toml`；注意它一旦进入过 Git 历史，删除当前文件并不能抹掉历史内容。
- 接口 Token 按入口隔离：管理员、确认回调、提交、查询、批改各用各的 Token；泄露一个不会直接拿到 `/admin`。
- 大模型只接收业务所需文本；告警分析提示词明确把资料当数据而非指令。

## Prompt 编号

| 编号 | 用途 |
| --- | --- |
| S01 | 单条工作日志的肯定意见与改进建议 |
| S04 | 部门阶段日志的工作进展与困难归纳 |
| S05 | 部门月度人员调整 / 奖励建议 |
| S06 | 部门月度 idea 清单 |
| S07 | 部门月度重点谈话对象与主题 |
| S08 | 团队周度公共技术建议 |
| S09 | 团队周度公共培训建议 |
| S10 | 各部长确认后的团队摘要 |
