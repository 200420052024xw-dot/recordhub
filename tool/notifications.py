from __future__ import annotations

from tool.feishu import MessageService


class NotificationService:
    """Business-neutral notification templates shared by future workflows."""

    def __init__(self, messages: MessageService) -> None:
        self.messages = messages

    def pending_confirmation(
        self, open_id: str, *, title: str, record_url: str
    ) -> dict:
        return self.messages.send_text(
            open_id, f"【待确认】{title}\n请打开待办记录核对并确认：{record_url}"
        )

    def result_ready(self, open_id: str, *, title: str, result_url: str) -> dict:
        return self.messages.send_text(
            open_id, f"【已完成】{title}\n查看结果：{result_url}"
        )

    def processing_failed(self, open_id: str, *, title: str, reason: str) -> dict:
        return self.messages.send_text(
            open_id, f"【处理异常】{title}\n原因：{reason}"
        )
