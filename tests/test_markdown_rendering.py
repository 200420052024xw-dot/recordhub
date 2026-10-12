"""Tests for the markdown -> Feishu docx blocks rendering and array-root skills."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm import PromptService
from tool.cloud_docs import markdown_blocks
from workflow2.skills import (MaterialScope, SkillInput, SkillRunner,
                              SkillStatus, TextRecord)


class MarkdownBlocksTests(unittest.TestCase):
    def test_heading_bullet_bold_plain(self):
        blocks = markdown_blocks("# 标题\n- **加粗**项\n- 普通项\n尾段")
        self.assertEqual([b["block_type"] for b in blocks], [3, 12, 12, 2])
        runs = blocks[1]["bullet"]["elements"]
        self.assertEqual(runs[0]["text_run"]["content"], "加粗")
        self.assertTrue(runs[0]["text_run"]["text_element_style"]["bold"])
        self.assertEqual(runs[1]["text_run"]["content"], "项")

    def test_ordered_list(self):
        blocks = markdown_blocks("1. 第一\n2. 第二")
        self.assertEqual([b["block_type"] for b in blocks], [13, 13])
        self.assertEqual(blocks[0]["ordered"]["elements"][0]["text_run"]["content"], "第一")

    def test_plain_text_falls_back_to_text_blocks(self):
        blocks = markdown_blocks("只有一行\n第二行")
        self.assertEqual([b["block_type"] for b in blocks], [2, 2])


class ArrayRootSkillTests(unittest.TestCase):
    def test_s05_accepts_top_level_array_and_wraps_into_items(self):
        day = date(2026, 10, 8)
        material = SkillInput(
            run_id="r",
            scope=MaterialScope(start_date=day, end_date=day, department_ids=["dep"]),
            records=[TextRecord(
                record_id="rec1", person_id="M", name="成员", department_id="dep",
                role="骨干学生", work_start=day, work_end=day,
                confirmed=True, progress="完成检查",
                evaluations=[{"evaluator_id": "D", "evaluator_role": "部长",
                              "positive": "好", "improvement": "改进", "source": "AI"}])])
        client = Mock()
        client.complete_json.side_effect = lambda messages, validator: validator(
            [{"name": "成员", "result": "拟奖励", "reason": "表现好"}])
        result = SkillRunner(PromptService(client, max_attempts=1)).run(
            "S05", material, user_id="D")
        self.assertEqual(result.status, SkillStatus.WAITING_CONFIRMATION)
        self.assertEqual(result.content.items[0].name, "成员")
        self.assertEqual(result.content.items[0].result, "拟奖励")


if __name__ == "__main__":
    unittest.main()
