# 消息发送日志（Message Send Logging）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每次经 `MessageService` 发出飞书消息后，在 `logs/recordhub.log` 记录当天序号、消息类型、收件人 open_id、姓名、手机号与发送结果。

**Architecture:** 在 `MessageService.send` 这一唯一发送入口统一打日志；通过注入的 `recipient_resolver`（`open_id → (姓名, 手机号)`）从组织缓存解析收件人。`Person` 模型补上 `mobile` 字段作为解析数据源；`service/runtime.py` 提供 `make_recipient_resolver` 并在 `build_runtime` 里接线。

**Tech Stack:** Python 3.11+，pydantic v2，标准库 `unittest` / `unittest.mock`。

## Global Constraints

- 日志写到现有滚动日志 `logs/recordhub.log`（`config/logs.py` 已配置），用 `logging.getLogger(__name__)` 模块 logger。
- 序号按天递增，时区固定 `Asia/Shanghai`，跨天自动从 1 重算。
- 收件人姓名/手机号解析不到时记 `-`，不得阻断发送。
- 日志格式与现有代码一致：`logger.info("message_sent seq=%d type=%s ...", ...)`，参数用 `%s` 而非 f-string。
- 失败也必须记录（`logger.warning`）并原样 `raise`。
- 测试用 `.venv/Scripts/python.exe -m unittest tests.test_message_logging -v` 运行。
- 每次提交信息结尾加 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

---

### Task 1: Person 增加 mobile 字段并在解析时填充

**Files:**
- Modify: `schema/domain.py`（`Person` 类，约 13-21 行）
- Modify: `data/repositories.py`（`_parse_organization` 构造 `Person`，约 171-186 行）
- Test: `tests/test_message_logging.py`（新建，仅 `OrganizationMobileParsingTests`）

**Interfaces:**
- Produces: `Person.mobile: str | None`（默认 `None`；解析时填人员表「手机号」字段，空值归一为 `None`）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_message_logging.py`：

```python
from __future__ import annotations

import unittest

from data.repositories import OrganizationRepository


class OrganizationMobileParsingTests(unittest.TestCase):
    def test_parse_organization_populates_mobile_and_open_id(self):
        person_fields = {
            "person_id": "人员编号",
            "name": "姓名",
            "role": "角色",
            "leader_ref": "直属上级",
            "minister_ref": "本部部长",
            "department_ref": "所属部门",
            "mobile": "手机号",
        }
        department_fields = {
            "department_id": "部门编号",
            "name": "部门名称",
            "minister_ref": "部门部长",
        }
        person_records = [{
            "record_id": "rec1",
            "fields": {
                "人员编号": "B1",
                "姓名": "杨天宇",
                "角色": "骨干学生",
                "所属部门": "杨阳蕊部门",
                "直属上级": [{"id": "ou_minister"}],
                "手机号": "+8615870637343",
            },
        }]
        departments, persons = OrganizationRepository._parse_organization(
            department_fields, [],
            person_fields, person_records,
            mobile_open_ids={"+8615870637343": "ou_b1"},
        )
        self.assertEqual(len(persons), 1)
        self.assertEqual(persons[0].mobile, "+8615870637343")
        self.assertEqual(persons[0].open_id, "ou_b1")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_message_logging -v`
Expected: FAIL，`AttributeError`（`Person` 尚无 `mobile` 字段）。

- [ ] **Step 3: 实现**

`schema/domain.py` 的 `Person`，在 `open_id` 之后加一行：

```python
class Person(StrictModel):
    person_id: str
    name: str
    role: str
    department_id: str | None = None
    leader_id: str | None = None
    open_id: str | None = None
    mobile: str | None = None
    source_record_id: str | None = None
    active: bool = True
```

`data/repositories.py` 的 `_parse_organization` 中，`Person(...)` 在 `open_id=person_open_id(item, values) or None,` 之后加一行：

```python
            persons.append(
                Person(
                    person_id=person_id,
                    name=display_name(values.get(person_fields["name"])),
                    role=role,
                    department_id=resolve_reference(
                        values.get(person_fields["department_ref"]),
                        department_aliases,
                    )
                    or None,
                    leader_id=leader_id or None,
                    open_id=person_open_id(item, values) or None,
                    mobile=scalar(values.get(person_fields["mobile"])).strip() or None,
                    source_record_id=str(item.get("record_id", "")) or None,
                    active=field_boolean(values.get(person_fields.get("active"))),
                )
            )
```

（`scalar` 已在该模块顶部导入，无需新增 import。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_message_logging -v`
Expected: PASS（1 个测试）。

- [ ] **Step 5: 提交**

```bash
git add schema/domain.py data/repositories.py tests/test_message_logging.py
git commit -m "feat: 人员模型增加手机号字段

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: MessageService 发送后记录日志

**Files:**
- Modify: `tool/feishu.py`（imports、`MessageService.__init__`、`send`，约 1-13 行与 330-383 行）
- Test: `tests/test_message_logging.py`（追加 `MessageServiceLoggingTests`）

**Interfaces:**
- Consumes: `recipient_resolver: Callable[[str], tuple[str, str] | None] | None`（由外部注入，本任务不依赖 Task 1）
- Produces: `MessageService(client, *, recipient_override="", recipient_resolver=None)`；`send` 成功后打 `message_sent`（INFO）、失败打 `message_send_failed`（WARNING）并 `raise`；内部 `_next_seq(today: date) -> int` 与 `_recipient_info(open_id) -> tuple[str, str]`

- [ ] **Step 1: 写失败测试**

在 `tests/test_message_logging.py` 顶部（`from data.repositories import OrganizationRepository` 之后）追加 imports：

```python
from datetime import date
from unittest.mock import Mock

from tool.errors import FeishuApiError
from tool.feishu import MessageService
```

在文件末尾追加测试类：

```python
class MessageServiceLoggingTests(unittest.TestCase):
    def _service(self, resolver=None):
        client = Mock()
        return client, MessageService(client, recipient_resolver=resolver)

    def test_send_logs_success_with_recipient_info(self):
        client, messages = self._service(
            resolver=lambda oid: ("杨阳蕊", "+8613837176209"))
        client.request.return_value = {"code": 0, "data": {"message_id": "om_1"}}
        with self.assertLogs("tool.feishu", level="INFO") as captured:
            result = messages.send_text("ou_yyr", "hi")
        self.assertEqual(result, {"message_id": "om_1"})
        self.assertIn(
            "message_sent seq=1 type=text to=ou_yyr name=杨阳蕊 "
            "mobile=+8613837176209 message_id=om_1",
            captured.output[0],
        )

    def test_send_logs_failure_and_reraises(self):
        client, messages = self._service()
        client.request.side_effect = FeishuApiError("boom", code=999)
        with self.assertLogs("tool.feishu", level="WARNING") as captured:
            with self.assertRaises(FeishuApiError):
                messages.send_text("ou_x", "hi")
        self.assertIn(
            "message_send_failed seq=1 type=text to=ou_x name=- mobile=- error=boom",
            captured.output[0],
        )

    def test_sequence_increments_and_resets_per_day(self):
        client, messages = self._service()
        self.assertEqual(messages._next_seq(date(2026, 10, 8)), 1)
        self.assertEqual(messages._next_seq(date(2026, 10, 8)), 2)
        self.assertEqual(messages._next_seq(date(2026, 10, 9)), 1)

    def test_resolver_miss_logs_dash(self):
        client, messages = self._service(resolver=lambda oid: None)
        client.request.return_value = {"code": 0, "data": {"message_id": "om_2"}}
        with self.assertLogs("tool.feishu", level="INFO") as captured:
            messages.send_text("ou_unknown", "hi")
        self.assertIn("name=- mobile=-", captured.output[0])

    def test_resolver_exception_logs_dash(self):
        def boom(oid):
            raise RuntimeError("nope")

        client, messages = self._service(resolver=boom)
        client.request.return_value = {"code": 0, "data": {"message_id": "om_3"}}
        with self.assertLogs("tool.feishu", level="INFO") as captured:
            messages.send_text("ou_bad", "hi")
        self.assertIn("name=- mobile=-", captured.output[0])
```

（`_service` 返回 `(client, messages)`；`client.request` 用 `unittest.mock.Mock` 控制返回/抛错。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_message_logging -v`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'recipient_resolver'`。

- [ ] **Step 3: 实现**

`tool/feishu.py` 顶部 imports 改为：

```python
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable, Iterator, Mapping
from uuid import NAMESPACE_URL, uuid5

from config.settings import FeishuSettings
from tool.bitable_fields import SHANGHAI
from tool.errors import FeishuApiError
from tool.http import HttpResponse, HttpTransport

logger = logging.getLogger(__name__)
```

`MessageService.__init__` 改为：

```python
    def __init__(
        self,
        client: FeishuClient,
        *,
        recipient_override: str = "",
        recipient_resolver: Callable[[str], tuple[str, str] | None] | None = None,
    ) -> None:
        self.client = client
        self.recipient_override = recipient_override.strip()
        self.recipient_resolver = recipient_resolver
        self._seq_date: date | None = None
        self._seq = 0
```

`MessageService.send` 改为（同时新增两个私有方法，放在 `send` 之前）：

```python
    def _next_seq(self, today: date) -> int:
        if today != self._seq_date:
            self._seq_date = today
            self._seq = 0
        self._seq += 1
        return self._seq

    def _recipient_info(self, open_id: str) -> tuple[str, str]:
        if self.recipient_resolver is None:
            return "-", "-"
        try:
            info = self.recipient_resolver(open_id)
        except Exception:
            return "-", "-"
        if not info:
            return "-", "-"
        name, mobile = info
        return name or "-", mobile or "-"

    def send(
        self,
        receive_id: str,
        *,
        msg_type: str,
        content: str,
        receive_id_type: str = "open_id",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        effective = self.recipient_override or receive_id
        seq = self._next_seq(datetime.now(SHANGHAI).date())
        name, mobile = self._recipient_info(effective)
        try:
            payload = self.client.request(
                "POST",
                "im/v1/messages",
                query={"receive_id_type": receive_id_type},
                json_body={
                    "receive_id": effective,
                    "msg_type": msg_type,
                    "content": content,
                    **({"uuid": str(uuid5(NAMESPACE_URL, idempotency_key))}
                       if idempotency_key else {}),
                },
            )
        except FeishuApiError as exc:
            logger.warning(
                "message_send_failed seq=%d type=%s to=%s name=%s mobile=%s error=%s",
                seq, msg_type, effective, name, mobile, str(exc),
            )
            raise
        message_id = str(payload.get("data", {}).get("message_id", ""))
        logger.info(
            "message_sent seq=%d type=%s to=%s name=%s mobile=%s message_id=%s",
            seq, msg_type, effective, name, mobile, message_id,
        )
        return dict(payload.get("data", {}))
```

（`send_text` / `send_card` 不变，仍走 `self.send`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_message_logging -v`
Expected: PASS（Task 1 的 1 个 + 本任务 5 个，共 6 个）。

- [ ] **Step 5: 提交**

```bash
git add tool/feishu.py tests/test_message_logging.py
git commit -m "feat: 消息发送后记录日志（序号/收件人/手机号）

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 运行时注入收件人解析器

**Files:**
- Modify: `service/runtime.py`（新增 `make_recipient_resolver`，并在 `build_runtime` 中接线）
- Test: `tests/test_message_logging.py`（追加 `RecipientResolverWiringTests`）

**Interfaces:**
- Consumes: `Person.mobile`（Task 1）、`MessageService.recipient_resolver`（Task 2）
- Produces: `make_recipient_resolver(organization_cache: OrganizationCache) -> Callable[[str], tuple[str, str] | None]`；`build_runtime` 内把它赋给 `infrastructure.messages.recipient_resolver`

- [ ] **Step 1: 写失败测试**

在 `tests/test_message_logging.py` 顶部追加 imports：

```python
from schema import Department, Organization, Person
from service.runtime import make_recipient_resolver
```

在文件末尾追加测试类：

```python
class RecipientResolverWiringTests(unittest.TestCase):
    def test_make_recipient_resolver_maps_open_id_to_name_and_mobile(self):
        cache = Mock()
        cache.get.return_value = Organization(
            persons=[
                Person(person_id="B1", name="杨阳蕊", role="部长",
                       open_id="ou_yyr", mobile="+8613837176209"),
            ],
            departments=[Department(department_id="D1", name="杨阳蕊部门",
                                    minister_id="B1")],
            team_leader_id=None,
        )
        resolve = make_recipient_resolver(cache)
        self.assertEqual(resolve("ou_yyr"), ("杨阳蕊", "+8613837176209"))
        self.assertIsNone(resolve("ou_unknown"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_message_logging -v`
Expected: FAIL，`ImportError: cannot import name 'make_recipient_resolver'`。

- [ ] **Step 3: 实现**

`service/runtime.py` 在 `_build_infrastructure` 函数之后（`@dataclass(slots=True) class Runtime` 之前）新增：

```python
def make_recipient_resolver(
    organization_cache: OrganizationCache,
) -> Callable[[str], tuple[str, str] | None]:
    def resolve(open_id: str) -> tuple[str, str] | None:
        organization = organization_cache.get()
        for person in organization.persons:
            if person.open_id == open_id:
                return person.name, person.mobile or ""
        return None

    return resolve
```

`build_runtime` 中，在 `organization_cache = OrganizationCache(...)` 语句块之后（`ai_evaluations = ...` 之前）新增两行接线：

```python
    infrastructure.messages.recipient_resolver = make_recipient_resolver(
        organization_cache
    )
```

（`Callable` 已在 `service/runtime.py` 顶部 `from collections.abc import Callable` 导入；`OrganizationCache` 也已导入，无需新增 import。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_message_logging -v`
Expected: PASS（Task 1 的 1 个 + Task 2 的 5 个 + 本任务 1 个，共 7 个）。

- [ ] **Step 5: 提交**

```bash
git add service/runtime.py tests/test_message_logging.py
git commit -m "feat: 运行时注入消息收件人解析器

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 全量回归

**Files:** 无代码改动。

- [ ] **Step 1: 跑全部单元测试**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: 全部 PASS（含新增 7 个测试，以及既有 `test_foundation`、`test_organization_cache` 等）。

- [ ] **Step 2: 快速验证真实发送日志**

Run（确认线上一条通知会打日志，且收件人姓名/手机号能解析）：

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "
import sys; sys.path[:0] = ['.', 'src']
from service.runtime import build_runtime
from config import AppSettings, load_env_file, setup_logging
load_env_file()
settings = AppSettings.from_env()
setup_logging(settings)
runtime = build_runtime(settings)
runtime.infrastructure.messages.send_text(
    settings.admin_open_id, '消息日志冒烟测试', idempotency_key='smoke:message-logging')
"
```

Expected: `logs/recordhub.log` 出现一条 `message_sent seq=… type=text to=ou_d9f307… name=刘新瀚 mobile=+8618270337201 message_id=om_…`（若 `message_id` 为空或发送失败，则出现 `message_send_failed`，同样说明日志生效）。
