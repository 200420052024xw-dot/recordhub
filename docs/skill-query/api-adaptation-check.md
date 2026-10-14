# Skill 接口适配检查

检查日期：2026-10-14  
对照文件：[SKILL.md](SKILL.md)  
范围：当前本地代码，未验证服务器运行版本。

## 接口实现情况

Skill 约定了 4 个接口。目前 2 个已实现，2 个批改接口未实现。已有查询接口仍存在适配问题。

| 用途 | 方法 | 接口 | 状态 |
|---|---|---|---|
| 识别姓名、返回身份 | POST | `/workbuddy/v1/queries/identity` | 已实现 |
| 查看历史日志、评价、阶段报告 | POST | `/workbuddy/v1/queries` | 已实现，但存在适配问题 |
| 获取今天待批改清单 | POST | `/workbuddy/v1/reviews/pending` | 未实现 |
| 每完成一条批改，立即提交保存 | POST | `/workbuddy/v1/reviews` | 未实现 |

## 尚未实现的接口

### 获取待批改清单

```http
POST /workbuddy/v1/reviews/pending
```

```json
{"person_id": "p_xxx"}
```

需要返回：

- `deadlines`：各类待批改材料的数量和入口截止时间。
- `missing`：未交日志人员，只用于摘要，不进入批改队列。
- `items`：逐条批改的日志评价或阶段汇总。

当前没有注册这个接口，WorkBuddy 无法按 Skill 获取完整待批改清单。

### 逐条提交批改结果

```http
POST /workbuddy/v1/reviews
```

接受 AI 原文：

```json
{"person_id": "p_xxx", "item_id": "item_xxx", "decision": "accept"}
```

提交老师修改后的内容：

```json
{
  "person_id": "p_xxx",
  "item_id": "item_xxx",
  "decision": "edit",
  "edited": {
    "positive": "修改后的肯定内容",
    "improvement": "修改后的改进内容"
  }
}
```

阶段汇总按 Skill 使用 `edited.sections` 提交。

接口应保存这一条结果，返回 `code`、`item_id`、`decision`、`received_at`。WorkBuddy 只能回复“已收到”，不能回复“已定稿”。

当前没有注册这个接口，“每完成一名同学的评价，就缓存到云服务器”尚未落地。

## 缺少的批改配套功能

| 功能 | 所需行为 | 当前状态 |
|---|---|---|
| 批改专用凭证 | 接入 `RECORDHUB_REVIEW_TOKEN`，与查询、管理员凭证区分 | 未接入 |
| 逐条持久化暂存 | 每提交一条就保存，暂停对话或重启服务后不丢失 | 未实现 |
| 重复提交 | 截止前再次提交覆盖旧结果，相同请求重试保持幂等 | 未实现 |
| 材料校验 | 检查材料存在、归属和修改内容是否有效 | 未实现 |
| 入口截止 | 接口返回截止时间，过期拒绝写入 | 未实现 |
| 正式写回 | 系统定稿前，将暂存写入本地正式状态及飞书人工评价或确认表 | 未实现 |
| 写回恢复 | 写回失败重试，成功前保留暂存 | 未实现 |
| 业务错误码 | 按约定返回 `CLOSED`、`ITEM_NOT_FOUND`、`NOT_YOURS`、`EMPTY_EDITED`、`REVIEW_FAILED` | 未实现 |

现有 SQLite 保存任务进度，工作日志暂存保存学生提交的日志，均不能视为已经实现了老师批改结果的暂存和写回。

## 已有查询接口的适配问题

### 日期说法没有对齐

Skill 引导部分要求将“昨天”“上周”等说法原样交给接口；API 说明和代码使用 `yesterday`、`last_week` 等固定参数。

本地验证：直接将 `range.shortcut` 设为“昨天”，返回 `RANGE_INVALID`。

需要统一：后端支持中文说法，或 Skill 明确映射成固定参数。具体起止日期仍由后端计算。

### 阶段报告没有按指定部门过滤

负责人查询阶段报告时，代码读取全部部门及团队报告，没有按请求指定的部门筛选。

本地验证：指定“研发部”，同时返回“研发部”“测试部”和“团队”。

需要区分查询人的权限上界与本次请求的范围，按本次指定范围返回结果。

### 查询异常没有统一返回约定错误码

查询服务只将 `QueryError` 转成业务错误码。数据源读取等其他异常继续抛出，由 HTTP 层返回 500，而不是 `QUERY_FAILED`。

本地验证：模拟数据源读取失败，异常直接抛出，没有返回 `QUERY_FAILED`。

需要统一查询失败的返回形式，便于 WorkBuddy 按 Skill 话术处理。

## 旧 review 模式不能替代批改接口

`/workbuddy/v1/queries` 支持的旧 `mode=review` 读取今天提交的日志，没有：

- 待批改 AI 评价内容。
- `deadlines` 及入口截止时间。
- `missing` 人员明细。
- 待批改阶段汇总。
- 逐条保存批改结果的能力。

因此不能用它代替 `/reviews/pending` 和 `/reviews`。

## 核对依据与边界

- [HTTP 接口注册](../../service/api.py)
- [请求与错误码](../../service/workbuddy_query/models.py)
- [查询业务逻辑](../../service/workbuddy_query/service.py)
- [日期解析](../../service/workbuddy_query/ranges.py)
- [环境配置](../../config/settings.py)

已通过路由核对和本地模拟验证确认上述缺口。本次检查未修改业务代码，未部署服务器。

此前讨论的查询凭证与人员身份绑定问题，按用户要求不处理，不计入本次适配修改范围。
