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
from tool.diagnostics import error_summary

logger = logging.getLogger(__name__)


class FeishuTokenProvider:
    def __init__(self, settings: FeishuSettings, transport: HttpTransport) -> None:
        self._settings = settings
        self._transport = transport
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            response = self._transport.request(
                "POST",
                f"{self._settings.base_url}/auth/v3/tenant_access_token/internal",
                json_body={
                    "app_id": self._settings.app_id,
                    "app_secret": self._settings.app_secret,
                },
            )
            data = _require_success(response)
            token = data.get("tenant_access_token")
            if not token:
                raise FeishuApiError("Feishu token response did not contain a token")
            self._token = str(token)
            self._expires_at = time.monotonic() + max(
                int(data.get("expire", 7200)) - 60, 1
            )
            return self._token


class FeishuClient:
    def __init__(
        self,
        settings: FeishuSettings,
        transport: HttpTransport,
        token_provider: FeishuTokenProvider | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.token_provider = token_provider or FeishuTokenProvider(settings, transport)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            response = self.transport.request(
                method,
                f"{self.settings.base_url}/{path.lstrip('/')}",
                headers={"Authorization": f"Bearer {self.token_provider.get_token()}"},
                query=query,
                json_body=json_body,
            )
            result = _require_success(response)
        except Exception as exc:
            exc.method, exc.path = method, path
            if 'response' in locals():
                exc.request_id = next((v for k, v in response.headers.items()
                                       if k.lower() in {"x-tt-logid", "x-request-id"}), None)
            logger.exception("feishu_request_failed method=%s path=%s elapsed_ms=%d error=%s",
                             method, path, (time.monotonic() - started) * 1000,
                             error_summary(exc))
            raise
        logger.info("feishu_request_completed method=%s path=%s status=%s elapsed_ms=%d",
                    method, path, response.status_code, (time.monotonic() - started) * 1000)
        return result


@dataclass(frozen=True, slots=True)
class RecordPage:
    items: list[dict[str, Any]]
    has_more: bool
    page_token: str | None
    total: int | None = None


class BitableService:
    def __init__(self, client: FeishuClient, app_token: str) -> None:
        self.client = client
        self.app_token = app_token

    def _records_path(self, table_id: str) -> str:
        return f"bitable/v1/apps/{self.app_token}/tables/{table_id}/records"

    def list_fields(self, table_id: str) -> list[dict[str, Any]]:
        path = f"bitable/v1/apps/{self.app_token}/tables/{table_id}/fields"
        fields: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            payload = self.client.request(
                "GET", path, query={"page_size": 100, "page_token": page_token}
            )
            data = payload.get("data", {})
            fields.extend(data.get("items", []))
            if not data.get("has_more", False):
                return fields
            next_token = data.get("page_token")
            if not next_token or next_token in seen_tokens:
                raise FeishuApiError("Invalid field pagination state returned by Feishu")
            seen_tokens.add(next_token)
            page_token = next_token

    def list_records_page(
        self,
        table_id: str,
        *,
        page_size: int = 500,
        page_token: str | None = None,
        view_id: str | None = None,
        filter_expression: str | None = None,
        sort: Iterable[str] | None = None,
        field_names: Iterable[str] | None = None,
        user_id_type: str = "open_id",
    ) -> RecordPage:
        payload = self.client.request(
            "GET",
            self._records_path(table_id),
            query={
                "page_size": page_size,
                "page_token": page_token,
                "view_id": view_id,
                "filter": filter_expression,
                "sort": json.dumps(list(sort), ensure_ascii=False) if sort else None,
                "field_names": json.dumps(list(field_names), ensure_ascii=False)
                    if field_names else None,
                "user_id_type": user_id_type,
            },
        )
        return _record_page(payload)

    def iter_records(self, table_id: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            page = self.list_records_page(table_id, page_token=page_token, **kwargs)
            yield from page.items
            if not page.has_more:
                return
            if not page.page_token or page.page_token in seen_tokens:
                raise FeishuApiError("Invalid pagination state returned by Feishu")
            seen_tokens.add(page.page_token)
            page_token = page.page_token

    def list_records(self, table_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.iter_records(table_id, **kwargs))

    def search_records(
        self,
        table_id: str,
        search: Mapping[str, Any],
        *,
        page_size: int = 500,
        page_token: str | None = None,
        user_id_type: str = "open_id",
    ) -> RecordPage:
        payload = self.client.request(
            "POST",
            f"{self._records_path(table_id)}/search",
            query={
                "page_size": page_size,
                "page_token": page_token,
                "user_id_type": user_id_type,
            },
            json_body=dict(search),
        )
        return _record_page(payload)

    def search_all_records(
        self,
        table_id: str,
        search: Mapping[str, Any],
        *,
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            page = self.search_records(
                table_id, search, page_size=page_size, page_token=page_token
            )
            records.extend(page.items)
            if not page.has_more:
                return records
            if not page.page_token or page.page_token in seen_tokens:
                raise FeishuApiError("Invalid search pagination state returned by Feishu")
            seen_tokens.add(page.page_token)
            page_token = page.page_token

    def get_record(
        self, table_id: str, record_id: str, *, user_id_type: str = "open_id"
    ) -> dict[str, Any]:
        payload = self.client.request(
            "GET",
            f"{self._records_path(table_id)}/{record_id}",
            query={"user_id_type": user_id_type},
        )
        return dict(payload.get("data", {}).get("record", {}))

    def create_record(
        self,
        table_id: str,
        fields: Mapping[str, Any],
        *,
        user_id_type: str = "open_id",
    ) -> dict[str, Any]:
        payload = self.client.request(
            "POST",
            self._records_path(table_id),
            query={"user_id_type": user_id_type},
            json_body={"fields": dict(fields)},
        )
        return dict(payload.get("data", {}).get("record", {}))

    def update_record(
        self,
        table_id: str,
        record_id: str,
        fields: Mapping[str, Any],
        *,
        user_id_type: str = "open_id",
    ) -> dict[str, Any]:
        payload = self.client.request(
            "PUT",
            f"{self._records_path(table_id)}/{record_id}",
            query={"user_id_type": user_id_type},
            json_body={"fields": dict(fields)},
        )
        return dict(payload.get("data", {}).get("record", {}))

    def batch_create(
        self,
        table_id: str,
        records: Iterable[Mapping[str, Any]],
        *,
        user_id_type: str = "open_id",
    ) -> list[dict[str, Any]]:
        payload = self.client.request(
            "POST",
            f"{self._records_path(table_id)}/batch_create",
            query={"user_id_type": user_id_type},
            json_body={"records": [{"fields": dict(fields)} for fields in records]},
        )
        return list(payload.get("data", {}).get("records", []))

    def batch_update(
        self,
        table_id: str,
        records: Iterable[Mapping[str, Any]],
        *,
        user_id_type: str = "open_id",
    ) -> list[dict[str, Any]]:
        normalized = [
            {"record_id": record["record_id"], "fields": dict(record["fields"])}
            for record in records
        ]
        payload = self.client.request(
            "POST",
            f"{self._records_path(table_id)}/batch_update",
            query={"user_id_type": user_id_type},
            json_body={"records": normalized},
        )
        return list(payload.get("data", {}).get("records", []))

    def subscribe_document_events(self) -> None:
        """Register this Base for drive record-change event delivery.

        Feishu rejects repeated subscriptions; treat that as success so
        service restarts stay idempotent.
        """
        try:
            self.client.request(
                "POST",
                f"drive/v1/files/{self.app_token}/subscribe",
                query={"file_type": "bitable"},
            )
        except FeishuApiError as exc:
            message = str(exc)
            if "subscribed" in message.lower() or "已订阅" in message:
                return
            raise


class ContactService:
    def __init__(self, client: FeishuClient) -> None:
        self.client = client

    def get_user(
        self,
        user_id: str,
        *,
        user_id_type: str = "open_id",
        department_id_type: str = "open_department_id",
    ) -> dict[str, Any]:
        payload = self.client.request(
            "GET",
            f"contact/v3/users/{user_id}",
            query={
                "user_id_type": user_id_type,
                "department_id_type": department_id_type,
            },
        )
        return dict(payload.get("data", {}).get("user", {}))

    def batch_get_ids(
        self,
        *,
        emails: Iterable[str] = (),
        mobiles: Iterable[str] = (),
        user_id_type: str = "open_id",
    ) -> list[dict[str, Any]]:
        payload = self.client.request(
            "POST",
            "contact/v3/users/batch_get_id",
            query={"user_id_type": user_id_type},
            json_body={"emails": list(emails), "mobiles": list(mobiles)},
        )
        return list(payload.get("data", {}).get("user_list", []))


class MessageService:
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
        self._seq_lock = threading.Lock()

    def _next_seq(self, today: date) -> int:
        with self._seq_lock:
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
            if not info:
                return "-", "-"
            name, mobile = info
        except Exception:
            return "-", "-"
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
        logger.info("message_send_attempt seq=%d type=%s to=%s key=%s content_bytes=%d",
                    seq, msg_type, effective, idempotency_key, len(content.encode("utf-8")))
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
        except Exception as exc:
            logger.exception(
                "message_send_failed seq=%d type=%s to=%s name=%s mobile=%s error=%s key=%s",
                seq, msg_type, effective, name, mobile, str(exc), idempotency_key,
            )
            raise
        message_id = str(payload.get("data", {}).get("message_id", ""))
        logger.info(
            "message_sent seq=%d type=%s to=%s name=%s mobile=%s message_id=%s key=%s",
            seq, msg_type, effective, name, mobile, message_id, idempotency_key,
        )
        return dict(payload.get("data", {}))

    def send_text(
        self, receive_id: str, text: str, *, receive_id_type: str = "open_id",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.send(
            receive_id,
            msg_type="text",
            content=json.dumps({"text": text}, ensure_ascii=False),
            receive_id_type=receive_id_type,
            idempotency_key=idempotency_key,
        )

    def send_card(
        self,
        receive_id: str,
        card: Mapping[str, Any],
        *,
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        return self.send(
            receive_id,
            msg_type="interactive",
            content=json.dumps(card, ensure_ascii=False),
            receive_id_type=receive_id_type,
        )


def _record_page(payload: Mapping[str, Any]) -> RecordPage:
    data = payload.get("data", {})
    return RecordPage(
        items=list(data.get("items") or []),
        has_more=bool(data.get("has_more", False)),
        page_token=data.get("page_token"),
        total=data.get("total"),
    )


def _require_success(response: HttpResponse) -> dict[str, Any]:
    data = response.data if isinstance(response.data, dict) else {}
    code = data.get("code")
    if not 200 <= response.status_code < 300 or code not in (None, 0):
        raise FeishuApiError(
            str(data.get("msg") or data.get("message") or "Feishu API request failed"),
            status_code=response.status_code,
            code=code,
            details=data,
        )
    return data
