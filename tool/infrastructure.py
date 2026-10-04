from __future__ import annotations

from dataclasses import dataclass

from llm.client import DeepSeekClient
from config.settings import AppSettings
from tool.feishu import BitableService, ContactService, FeishuClient, MessageService
from tool.http import HttpTransport, UrllibTransport
from tool.notifications import NotificationService
from tool.cloud_docs import CloudDocsService


@dataclass(frozen=True, slots=True)
class Infrastructure:
    bitable: BitableService
    contacts: ContactService
    messages: MessageService
    notifications: NotificationService
    llm: DeepSeekClient
    cloud_docs: CloudDocsService


def build_infrastructure(
    settings: AppSettings, transport: HttpTransport | None = None
) -> Infrastructure:
    settings.validate()
    http = transport or UrllibTransport(settings.http_timeout_seconds)
    feishu = FeishuClient(settings.feishu, http)
    messages = MessageService(
        feishu, recipient_override=settings.message_override_open_id
    )
    return Infrastructure(
        bitable=BitableService(feishu, settings.feishu.bitable_app_token),
        contacts=ContactService(feishu),
        messages=messages,
        notifications=NotificationService(messages),
        llm=DeepSeekClient(settings.deepseek, http),
        cloud_docs=CloudDocsService(feishu),
    )
