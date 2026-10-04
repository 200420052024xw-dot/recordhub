from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from pydantic import BaseModel

from llm import LLMValidationError, PromptService
from tool.errors import StructuredOutputError


class ExampleInput(BaseModel):
    value: str


class ExampleOutput(BaseModel):
    result: str


class PromptServiceTests(unittest.TestCase):
    def test_executes_a_workflow_independent_prompt(self) -> None:
        client = Mock()
        client.complete_json.side_effect = lambda messages, validator: validator(
            {"result": "完成"}
        )
        result = PromptService(client).execute(
            prompt_code="ANALYZE",
            template="分析输入内容",
            input_data=ExampleInput(value="记录"),
            output_model=ExampleOutput,
        )

        self.assertEqual(result.result, "完成")
        messages = client.complete_json.call_args.args[0]
        self.assertIn("ANALYZE", messages[0]["content"])
        self.assertIn("分析输入内容", messages[0]["content"])
        self.assertIn('"value": "记录"', messages[1]["content"])

    def test_structured_output_failure_retries_and_raises_validation_error(self) -> None:
        client = Mock()
        client.complete_json.side_effect = StructuredOutputError("invalid JSON")
        with patch("llm.service.time.sleep") as sleep:
            with self.assertRaises(LLMValidationError):
                PromptService(client, max_attempts=2).execute(
                    prompt_code="ANALYZE",
                    template="分析输入内容",
                    input_data=ExampleInput(value="记录"),
                    output_model=ExampleOutput,
                )
        self.assertEqual(client.complete_json.call_count, 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
