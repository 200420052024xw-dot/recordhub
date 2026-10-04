"""Feishu Drive and Docs API primitives; no Workflow1 policy lives here."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from tool.feishu import FeishuClient


class CloudDocsService:
    def __init__(self, client: FeishuClient) -> None:
        self.client = client

    def list_files(self, folder_token: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            payload = self.client.request(
                "GET", "drive/v1/files",
                query={"folder_token": folder_token, "page_size": 200,
                       "page_token": page_token},
            ).get("data", {})
            items.extend(payload.get("files", []))
            if not payload.get("has_more"):
                return items
            next_token = payload.get("next_page_token")
            if not next_token or next_token == page_token:
                raise ValueError("飞书文件夹分页返回无效游标")
            page_token = next_token

    def find_child(self, parent: str, name: str, kind: str) -> str | None:
        matches = [
            item for item in self.list_files(parent)
            if item.get("name") == name and item.get("type") == kind
        ]
        if len(matches) > 1:
            raise ValueError(f"文件夹中存在多个同名 {kind}：{name}")
        return str(matches[0].get("token")) if matches else None

    def create_folder(self, parent: str, name: str) -> str:
        data = self.client.request(
            "POST", "drive/v1/files/create_folder",
            json_body={"name": name, "folder_token": parent},
        ).get("data", {})
        token = data.get("token")
        if not token:
            raise ValueError("飞书创建文件夹未返回 token")
        return str(token)

    def create_document(self, parent: str, title: str) -> str:
        data = self.client.request(
            "POST", "docx/v1/documents",
            query={"folder_token": parent},
            json_body={"title": title},
        ).get("data", {}).get("document", {})
        token = data.get("document_id")
        if not token:
            raise ValueError("飞书创建云文档未返回 document_id")
        return str(token)

    def list_blocks(self, document_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            data = self.client.request(
                "GET", f"docx/v1/documents/{document_id}/blocks/{document_id}/children",
                query={"page_size": 500, "page_token": page_token},
            ).get("data", {})
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            next_token = data.get("page_token")
            if not next_token or next_token == page_token:
                raise ValueError("飞书文档分页返回无效游标")
            page_token = next_token

    def append_blocks(self, document_id: str, blocks: Iterable[dict[str, Any]]) -> None:
        payload = list(blocks)
        for offset in range(0, len(payload), 50):
            self.client.request(
                "POST", f"docx/v1/documents/{document_id}/blocks/{document_id}/children",
                json_body={"children": payload[offset:offset + 50]},
            )


def text_block(text: str, *, link: str | None = None, heading: int = 0) -> dict[str, Any]:
    kind = f"heading{heading}" if heading else "text"
    element: dict[str, Any] = {"text_run": {"content": text}}
    if link:
        element["text_run"]["text_element_style"] = {
            "link": {"url": quote(link, safe="")}}
    return {"block_type": heading + 2 if heading else 2,
            kind: {"elements": [element]}}
