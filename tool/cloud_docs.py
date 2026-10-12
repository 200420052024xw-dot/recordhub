"""Feishu Drive and Docs API primitives; no Workflow1 policy lives here."""

from __future__ import annotations

import re
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
            json_body={"folder_token": parent, "title": title},
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


def divider_block() -> dict[str, Any]:
    """Feishu docx horizontal divider (block_type 22)."""
    return {"block_type": 22, "divider": {}}


# --- 轻量 Markdown → 飞书文档块 ---

def _inline_elements(text: str) -> list[dict[str, Any]]:
    """把 **加粗** 段拆成多个 text_run；不配对的 ** 按原文处理。"""
    parts = text.split("**")
    elements: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if not part:
            continue
        run: dict[str, Any] = {"text_run": {"content": part}}
        if index % 2 == 1:
            run["text_run"]["text_element_style"] = {"bold": True}
        elements.append(run)
    return elements


def paragraph_block(text: str, *, heading: int = 0, bold: bool = False) -> dict[str, Any]:
    """普通文本块；heading=1/2/3 时是标题块，bold=True 时整段加粗（做字段标签）。"""
    if heading:
        return {"block_type": heading + 2,
                f"heading{heading}": {"elements": _inline_elements(text)}}
    if bold:
        return {"block_type": 2, "text": {"elements": [
            {"text_run": {"content": text, "text_element_style": {"bold": True}}}]}}
    return {"block_type": 2, "text": {"elements": _inline_elements(text)}}


def label_value_block(label: str, content: str) -> dict[str, Any]:
    """一行「加粗标签：正文」，用于把短列表/短值并进同一行。"""
    return {"block_type": 2, "text": {"elements": [
        {"text_run": {"content": label, "text_element_style": {"bold": True}}},
        {"text_run": {"content": content}},
    ]}}


def bullet_block(text: str) -> dict[str, Any]:
    """项目符号块（block_type 12）。"""
    return {"block_type": 12, "bullet": {"elements": _inline_elements(text)}}


def ordered_block(text: str) -> dict[str, Any]:
    """有序列表块（block_type 13）。"""
    return {"block_type": 13, "ordered": {"elements": _inline_elements(text)}}


def markdown_blocks(text: str) -> list[dict[str, Any]]:
    """宽松地把带轻量 Markdown 的文本转成飞书文档块。

    支持 #/##/### 标题、- /* 项目符号、1. 有序列表、**加粗**；其余行按普通文本。
    无法识别的行不会抛异常，只原样变成文本块。
    """
    blocks: list[dict[str, Any]] = []
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        content = line.strip()
        if content.startswith("### "):
            blocks.append({"block_type": 5,
                           "heading3": {"elements": _inline_elements(content[4:].strip())}})
        elif content.startswith("## "):
            blocks.append({"block_type": 4,
                           "heading2": {"elements": _inline_elements(content[3:].strip())}})
        elif content.startswith("# "):
            blocks.append({"block_type": 3,
                           "heading1": {"elements": _inline_elements(content[2:].strip())}})
        elif content.startswith("- ") or content.startswith("* "):
            blocks.append(bullet_block(content[2:].strip()))
        elif re.match(r"^\d+\.\s", content):
            blocks.append(ordered_block(re.sub(r"^\d+\.\s", "", content, count=1).strip()))
        else:
            blocks.append(paragraph_block(line.strip()))
    return blocks
