from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from pydantic import BaseModel, ConfigDict, ValidationError

from llm import LLMError, LLMValidationError, PromptService
from tool.errors import DeepSeekApiError, StructuredOutputError


class ExampleInput(BaseModel):
    value: str


class ExampleOutput(BaseModel):
    # 与生产模型一致：多余字段必须报错，而不是被静默忽略。
    model_config = ConfigDict(extra="forbid")

    result: str


class PromptServiceTests(unittest.TestCase):
    def _client(self, *payloads: str):
        """模拟真实客户端：校验失败时抛出带原始输出的 StructuredOutputError。"""
        calls: list[list[dict]] = []

        def complete(messages, validator):
            calls.append([dict(message) for message in messages])
            payload = payloads[min(len(calls), len(payloads)) - 1]
            try:
                return validator(json.loads(payload))
            except ValidationError as exc:
                raise StructuredOutputError(
                    f"Structured output validation failed: {exc}", raw=payload) from exc

        client = Mock()
        client.complete_json.side_effect = complete
        return client, calls

    def _execute(self, client: Mock, *, max_attempts: int = 3, **overrides):
        with patch("llm.service.time.sleep") as sleep:
            result = PromptService(client, max_attempts=max_attempts).execute(
                prompt_code="ANALYZE",
                template="分析输入内容",
                input_data=ExampleInput(value="记录"),
                output_model=ExampleOutput,
                **overrides,
            )
        return result, sleep

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

    def test_retry_sends_previous_output_and_repair_note(self) -> None:
        invalid = '{"result": "完成", "result_2": ""}'
        client, calls = self._client(invalid, '{"result": "完成"}')

        result, _ = self._execute(client)

        self.assertEqual(result.result, "完成")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[0]), 2)
        self.assertEqual(calls[1][2], {"role": "assistant", "content": invalid})
        note = calls[1][3]
        self.assertEqual(note["role"], "user")
        self.assertIn("result_2", note["content"])
        self.assertIn("只允许出现这些字段：result", note["content"])

    def test_retry_without_feedback_repeats_the_same_request(self) -> None:
        invalid = '{"result": "完成", "result_2": ""}'
        client, calls = self._client(invalid, '{"result": "完成"}')

        result, _ = self._execute(client, max_attempts=2, retry_feedback=False)

        self.assertEqual(result.result, "完成")
        self.assertEqual(calls[0], calls[1])

    def test_api_error_retry_is_not_annotated(self) -> None:
        client = Mock()
        client.complete_json.side_effect = DeepSeekApiError("DeepSeek API request failed")

        with self.assertRaises(LLMError):
            self._execute(client, max_attempts=2)

        first, second = client.complete_json.call_args_list
        self.assertEqual(first.args[0], second.args[0])

    def test_exhausted_retries_recover_nothing_and_stay_loud(self) -> None:
        client, calls = self._client('{"result": "完成", "result_2": ""}')

        with self.assertRaises(LLMValidationError):
            self._execute(client, max_attempts=2)

        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
