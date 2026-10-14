# 批改接口设计

编写日期：2026-10-14
状态：**已实现**（接口、存储、合并写回、查询侧遗留、SKILL 同步都已完成）
依据：[SKILL.md](SKILL.md)、[api-adaptation-check.md](api-adaptation-check.md)

本文是 `/workbuddy/v1/reviews` 与 `/workbuddy/v1/reviews/pending` 的实现依据。
查询接口 `/workbuddy/v1/queries` 的遗留问题见第九节。

---

## 一、目标

对话入口提供两个功能：

| 功能 | 说明 |
|---|---|
| 查询 | 看过去的日志、评价、阶段产出（已有，见第九节遗留） |
| 批改 | 老师看**今天**待批改的内容，逐条提交，暂存本地，夜间与飞书合并后定稿写回 |

批改的铁律：**入口只收下，说「已收到」；真正定稿是后端夜间统一做的。**

---

## 二、接口清单

| 用途 | 方法 | 接口 | 状态 |
|---|---|---|---|
| 取待批改清单 | POST | `/workbuddy/v1/reviews/pending` | 待实现 |
| 逐条提交批改 | POST | `/workbuddy/v1/reviews` | 待实现 |

---

## 三、时间线（关键）

| 时刻 | 动作 |
|---|---|
| **21:40** | 批改接口**停止接收**，之后提交返回 `CLOSED` |
| 21:40–21:50 | 把本地暂存与飞书表单**合并**，重复的按 `received_at` 覆盖 |
| **21:50** | 统一定稿放行（`auto_advance_at`） |

> 现状：配置里只有 `auto_advance_at = "21:50"`（config/schedules.toml:24）。
> **21:40 需要新增一个配置项**（接口截止）。

学生交日志的窗口是 **12:00–23:50**，和批改截止 21:50 是两回事。

---

## 四、权限（谁批改谁）

| 产出（`kind`） | 批改人 | 依据 |
|---|---|---|
| 日志评价（基层的日志） | 该基层的**骨干** | `snapshot.py:12` `{"基层学生":"骨干学生"}` |
| 日志评价（骨干的日志） | 该骨干的**部长** | `snapshot.py:12` `{"骨干学生":"部长"}` |
| 月报 S05–S07 | **部长**按部门确认 | 写回 `monthly_department_confirmation` |
| 阶段工作总结 S04 | **部长**按部门确认 | 写回 `stage_confirmation`，21:50 截止 |

> 周报 S08–S09 **没有审核**，不进批改队列。

服务端必须校验「这个 `person_id` 有资格批这条」，否则返回 `NOT_YOURS`。

**身份用姓名**：入口带 `person_id`，服务端按组织表核对，不做 token 与人的绑定。

---

## 五、取待批改清单 `POST /workbuddy/v1/reviews/pending`

### 请求

```json
{ "person_id": "p_xxx", "kind": "log" }
```

- `kind`：`log`（默认）/ `monthly` / `stage`。
  - 默认只出**当天的日志 + 日志评价**。
  - 阶段工作总结 / 月报**由老师明确要求时才出**（传对应 `kind`）。

### 返回

```json
{
  "code": "OK",
  "deadlines": [
    { "kind": "log_evaluation", "label": "日志评价", "count": 12,
      "deadline_at": "2026-10-14T21:40:00+08:00" }
  ],
  "missing": [ { "date": "2026-10-13", "name": "张三" } ],
  "items": [
    { "item_id": "…", "kind": "log_evaluation", "date": "2026-10-13",
      "name": "王五",
      "log": { "progress": "…", "difficulties": "…", "reflection": "…", "other": "…" },
      "positive": "…", "improvement": "…", "ai_status": "ok" }
  ]
}
```

- `items` 里**必须带日志原文 `log`**（SKILL:311「只念日志和评价的原文」）。
  > SKILL 第 281 行的示例条目漏写了 `log`，要补。
- `deadline_at` 是**接口截止**（21:40），不是定稿时刻（21:50）。
- `missing` 是**当天没交日志的人**，按**日志日**（昨天）算 —— 学生今天还能交到 23:50，不能按今天算。
  只用于摘要，不进逐条队列。
- **AI 生成失败（FAILED）的条目照样进队列**：`ai_status = "failed"`，`positive` / `improvement` 为空。
  老师现场评价，走 `edit`（没有 AI 原文可 `accept`）。
- 月报 / 阶段工作总结的条目形状：

```json
{ "item_id": "…", "kind": "monthly", "date": "2026-09-30", "name": "研发部",
  "sections": [ { "label": "降级—奖励—退出—提拔名单", "text": "…", "url": "…" } ] }
```

**分页**：`items` 和查询接口一样按页传（每页条数固定 + `next_cursor`）。
单次装不下就**多传几次**，不设单条硬上限。

---

## 六、逐条提交 `POST /workbuddy/v1/reviews`

### 请求

```json
{ "person_id": "p_xxx", "item_id": "…", "decision": "accept" }
{ "person_id": "p_xxx", "item_id": "…", "decision": "edit",
  "edited": { "positive": "…", "improvement": "…" } }
```

- `decision`：`accept`（原样用 AI 的）/ `edit`（用老师给的全文）。
- `edit` 必须带 `edited`，至少一栏非空，否则 `EMPTY_EDITED`。
- 月报 / 阶段工作总结的 `edited` 传 `{"sections": [{"label": "…", "text": "…"}]}`。

### 返回

```json
{ "code": "OK", "item_id": "…", "decision": "edit",
  "received_at": "2026-10-14T20:31:00+08:00" }
```

**是「已收到」，不是「已定稿」。**

### 覆盖规则（统一）

**后面提交的覆盖前面提交的** —— 同一条重复提交，以最后一次为准，更新 `received_at`。

### 错误码

| code | 场景 |
|---|---|
| `OK` | 收下 |
| `CLOSED` | 过了 21:40 接口截止 |
| `ITEM_NOT_FOUND` | `item_id` 找不到 / 不属于今天 |
| `NOT_YOURS` | 这条不归这个人批 |
| `EMPTY_EDITED` | `edit` 但 `edited` 为空 |
| `REVIEW_FAILED` | 其它写入失败 |

---

## 七、存储：按「产出类型 + 提交人」分文件

审批提交暂存**先按产出类型分文件夹，再按提交人（老师）分文件**，和日志暂存的 `workbuddy_inbox/` 并列。

```
state/reviews/
  log/<老师姓名>.json        # 日志评价
  monthly/<老师姓名>.json    # 月报
  stage/<老师姓名>.json      # 阶段工作总结
```

```json
{
  "reviewer_id": "p_xxx",
  "reviewer_name": "张三",
  "target_date": "2026-10-14",
  "kind": "log",
  "items": {
    "<log_id>": { "decision": "edit",
                  "edited": {"positive": "…", "improvement": "…"},
                  "ai_snapshot": {"positive": "…", "improvement": "…"},
                  "received_at": "2026-10-14T20:31:00+08:00" }
  }
}
```

文件夹已经表明 `kind`，所以 `items` 的键就是**原始的 `item_id`**，不用再加前缀。

### 要点（照搬 `WorkBuddyInbox`）

| 点 | 做法 |
|---|---|
| 文件名 | 用**老师姓名**（`_safe()` 清洗，姓名唯一） |
| 日期 | 文件名不带日期，日期看文件里的 `target_date` |
| 覆盖 | 同 `item_id` 重提 → 覆盖，更新 `received_at`（**后覆盖前**） |
| 并发 | 同一老师两个窗口 → **原子写 + 锁**（`FileStateStore._atomic_write`） |
| 审计 | 行内保留 `reviewer_id` / `received_at` / `ai_snapshot` |
| 生命周期 | 写回成功后删文件（或回填 `written_back_at` 留审计） |

**为什么按提交人分、不按学生分**：报告类（月报 / 阶段工作总结）**不属于某个学生**，但它**属于提交它的老师**（部长），所以按提交人分，日志和报告都能装下。

**日志暂存 `workbuddy_inbox/` 不动**（学生各写各的，一人一文件本来就不打架）。

---

## 八、合并与写回

21:40 接口截止后：

1. 遍历 `state/reviews/<kind>/*.json`，收集当天的所有 `item_id`。
2. 读飞书侧的人工评价 / 确认表。
3. 同一个 `item_id` 两边都有 → **按 `received_at` 晚的优先**。
4. 21:50 统一定稿写回本地正式状态及飞书表。
5. 写回成功后清暂存。**写回失败不能丢老师的批改**：参考 `WorkBuddyInbox`，抛错让工作流 FAILED、可 resume，暂存不删。

合并逻辑单独成一个函数，便于测试。

---

## 九、查询接口遗留（`/workbuddy/v1/queries`）

| # | 问题 | 处理 | 状态 |
|---|---|---|---|
| E1 | 中文日期说法与后端不匹配（ranges.py 只认英文） | **后端补快捷词** `this_month` / `last_month`；**SKILL 加「说法 → 固定参数」对照表**，写死「一律先转成固定参数再发」 | ✅ 完成 |
| E2 | 阶段报告没按部门过滤（负责人查全部） | **按部门过滤**：负责人明确指定部门/个人/小组时才收窄本次范围 | ✅ 完成 |
| E3 | 查询失败没统一成业务码（抛 500） | **统一返回 `QUERY_FAILED`**（`query` / `identity` 都包一层） | ✅ 完成 |

### E1 分工原则

- **SKILL 只做「说法 → 固定参数」的映射，不算日期。**
- **后端只做「参数 → 日期」的计算。**
- 中间谁也不越界。

| 用户说 | 传的参数 |
|---|---|
| 今天 | `today` |
| 昨天 | `yesterday` |
| 前天 | `day_before_yesterday` |
| 这周 / 本周 | `this_week` |
| 上周 | `last_week` |
| 最近 7 天 / 这一周 | `last_7_days` |
| 最近 30 天 | `last_30_days` |
| 这个月 | `this_month`（新增） |
| 上个月 | `last_month`（新增） |
| 10 月 7 号到 13 号 | `from` / `to` |

### 单次传输长度

响应按页传（每页条数固定 + `next_cursor`）。**单次装不下就多传几次**，不设单条硬上限。

---

## 十、凭证

| 用途 | 服务端 token |
|---|---|
| 认人、查看 | `RECORDHUB_QUERY_TOKEN`（已有） |
| 批改（取清单、逐条提交） | **`RECORDHUB_REVIEW_TOKEN`（新增）** |

两把都只能干自己那一件事，拿任一把访问 `/admin/*` 一律 401。

**关于「S04 不等定稿」**：那句指 S04 的材料只汇总日志原文、不带评价，
所以它不用等「日志的人工评价」定稿。S04 **自己是有部长确认的**（`stage_confirmation`，21:50 截止），
所以「阶段工作总结」的批改写回 `stage_confirmation` 即可，和整体时间线不冲突。

---

## 十一、SKILL 同步（已完成）

1. ✅ 删掉「把这个说法原样交给接口」→ 改成「一律转成固定参数再发」。
2. ✅ 加「说法 → 固定参数」对照表（见 E1）。
3. ✅ `/reviews/pending` 条目补 `log` 字段（日志原文）。
4. ✅ 加 `kind` 参数说明（默认 `log`，报告要显式要）。
5. ✅ 两个时刻写清楚：**21:40 接口截止**、**21:50 定稿**。
6. ✅ 错误码：`CLOSED` / `ITEM_NOT_FOUND` / `NOT_YOURS` / `EMPTY_EDITED` / `REVIEW_FAILED`（回话映射表已有）。
7. ✅ FAILED 条目的念法（没有 AI 原文，让老师现场说）。
8. ✅ 查询侧错误码 + 话术（回话映射表已有）。

---

## 十二、实现进度

| # | 事项 | 状态 |
|---|---|---|
| 1 | 配置：`RECORDHUB_REVIEW_TOKEN`、`workbuddy_review.time = "21:40"` | ✅ 完成 |
| 2 | 存储：`reviews/<kind>/<老师姓名>.json`（`ReviewInbox`） | ✅ 完成 |
| 3 | `/reviews/pending` | ✅ 完成（日志评价 + 月报 / 阶段工作总结） |
| 4 | `/reviews` 逐条提交（覆盖、错误码） | ✅ 完成 |
| 5 | 合并 + 写回（21:40–21:50 / 21:50） | ✅ 完成 |
| 6 | 查询侧 E1 / E2 / E3 | ✅ 完成 |
| 7 | SKILL 同步（第十一节） | ✅ 完成 |

落地文件：

- `service/workbuddy_review/models.py`（请求契约、错误码）
- `service/workbuddy_review/settings.py`（接口截止）
- `service/workbuddy_review/store.py`（`ReviewInbox`）
- `service/workbuddy_review/service.py`（`ReviewService`）
- 路由：`service/api.py` `/workbuddy/v1/reviews/pending`、`/workbuddy/v1/reviews`
- 测试：`tests/test_workbuddy_review.py`

**报告类队列**（已实现）：读 `stage_analysis` / `monthly_department_analysis` 草稿表，
按部长引用匹配本部门，`item_id` 取草稿表的任务编号（`{run_id}:{department_id}`），
`sections` 取草稿内容字段。只有部长能用。

- **每个部门只出最近一轮**（按任务编号里的部门分组、取 `analysis_date` 最大那条）；
  最近一轮已确认就整轮不出，免得老师看到攒了几轮的旧草稿。

**合并 + 写回**（已实现）：`ReviewService.merge(log_date)` 由调度器在**接口截止时刻**（21:40）
触发，把 `reviews/*` 按业务键写进飞书表，供既有 21:50 定稿流程读取：

- 日志 → `human_evaluations`（骨干的日志）/ `human_evaluations_backbone`（基层的日志）。
  **只写人工能填的列**：被审核人、填写人、评价时间、肯定/改进_人工、两个「是否确认」。
  > 真实表里 `评价编号` 是**自动编号**、`日志原文` 是**查找引用**，都不可写，交给飞书。
- 报告 → `stage_confirmation` / `monthly_department_confirmation`，业务键 `task_id`（多行文本，可写）。
- **认行键**：日志按「**被审核人姓名 + 评价时间当天**」（加填写人软校验）；
  报告按 `task_id`。
- **合并规则**：只认**接口截止（21:40）之前**提交的飞书行。截止前飞书更晚
  （`record_submission_time` ≥ 本地 `received_at`）就保留飞书，否则覆盖；没有就新建。
  **21:40 之后的飞书行忽略**，不让它压过对话里的批改。写成功的暂存文件清掉，失败则保留、可重跑。

调度：`service/orchestration.py` 的 `plan_day` 在 21:40 排一个 `review_merge` 任务。
手动触发：`POST /admin/reviews/merge/{target_date}`（管理员 token，`target_date` 是日志日）。

**待做项**：

- **SKILL 批改部分同步**（第十一节第 2–7 条）。
