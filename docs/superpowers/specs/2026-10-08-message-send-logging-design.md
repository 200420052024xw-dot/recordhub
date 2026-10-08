# 消息发送日志（Message Send Logging）设计

- 日期：2026-10-08
- 状态：已批准

## 目标

所有经 `MessageService` 发出的飞书消息，在应用日志文件 `logs/recordhub.log` 中记录一条，字段包含：

- 当天序号（第几个发出的，按天从 1 递增）
- 消息类型（text / interactive）
- 收件人 open_id
- 收件人姓名、手机号（可解析时）
- 发送结果：成功记 `message_id`，失败记错误原因

## 需求

1. 日志写进现有滚动日志文件 `logs/recordhub.log`（`config/logs.py` 已配置）。
2. 序号按天递增，使用 `Asia/Shanghai` 时区，跨天自动从 1 重算。
3. 收件人姓名/手机号来自组织缓存（飞书人员表）；解析不到时记 `-`，不阻断发送。
4. 失败也必须记录（`warning` 级别），并原样向上抛出异常。

## 改动点

1. `schema/domain.py`：`Person` 增加 `mobile: str | None = None`。
2. `data/repositories.py`：`OrganizationRepository._parse_organization` 在构造 `Person` 时填入 `mobile`。
3. `tool/feishu.py` `MessageService`：
   - 构造函数新增 `recipient_resolver: Callable[[str], tuple[str, str] | None] | None`；
   - 新增按天序号计数器；
   - `send` 成功/失败分别打日志。
4. `service/runtime.py`：构建 `organization_cache` 后，把 resolver 注入 `infrastructure.messages.recipient_resolver`。

## 日志格式

成功：

```
message_sent seq=3 type=text to=ou_xxx name=杨阳蕊 mobile=+8613837176209 message_id=om_xxx
```

失败：

```
message_send_failed seq=4 type=text to=ou_xxx name=刘新瀚 mobile=+8618270337201 error=...
```

## 覆盖范围与非目标

- 覆盖：生产服务与走 `build_runtime` 的 CLI（run-workflow / run-workflow2 / refresh-cache）。
- 非目标：独立 demo 脚本未注入 resolver，姓名/手机号记 `-`。
- 非目标：默认不记录消息正文（避免刷屏与敏感内容入日志）。

## 测试

- 发送成功与失败各打一条日志（含序号递增）。
- 序号跨天重置。
- resolver 命中与未命中（记 `-`）。
