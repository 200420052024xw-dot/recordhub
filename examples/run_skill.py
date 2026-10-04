"""Run a Skill against prepared JSON, without reading or writing Feishu tables."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from config import AppSettings, load_env_file
from llm import DeepSeekClient, PromptService
from skills import SkillCode, SkillConfig, SkillInput, SkillRunner
from tool.http import UrllibTransport


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one RecordHub text Skill")
    parser.add_argument("code", choices=[code.value for code in SkillCode])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--department-id", required=True, help="Configuration owner department")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--configs", type=Path, help="Optional JSON array of Skill configs")
    args = parser.parse_args()
    load_env_file(_ROOT / ".env")
    settings = AppSettings.from_env()
    if not settings.deepseek.api_key:
        parser.error("DEEPSEEK_API_KEY is required")
    data = SkillInput.model_validate_json(args.input.read_text(encoding="utf-8-sig"))
    configs = []
    if args.configs:
        raw = json.loads(args.configs.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, list):
            parser.error("--configs must contain a JSON array")
        configs = [SkillConfig.model_validate(item) for item in raw]
    client = DeepSeekClient(settings.deepseek, UrllibTransport(settings.http_timeout_seconds))
    runner = SkillRunner(PromptService(client, max_attempts=settings.llm_max_attempts), configs)
    result = runner.run(args.code, data, department_id=args.department_id, user_id=args.user_id)
    # JSON output is UTF-8 even when launched from a legacy Windows terminal.
    sys.stdout.reconfigure(encoding="utf-8")
    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
