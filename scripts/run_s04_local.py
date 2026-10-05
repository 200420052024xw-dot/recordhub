"""Run the S04 Skill on a local JSON fixture without Feishu access.

This exercises the real S04 prompt, DeepSeek call, and output validation.
It does not create a Workflow2 cycle, Feishu rows, notifications, or documents.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file
from llm import DeepSeekClient, PromptService
from tool.http import UrllibTransport
from workflow2.skills import SkillInput, SkillRunner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path,
                        default=ROOT / "examples" / "s04_input.json")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "data" / "simulations" / "s04_result.json")
    parser.add_argument("--department-id", default="D001")
    parser.add_argument("--user-id", default="T001")
    parser.add_argument("--validate-only", action="store_true",
                        help="validate the local input without calling DeepSeek")
    args = parser.parse_args()

    source = args.input if args.input.is_absolute() else ROOT / args.input
    target = args.output if args.output.is_absolute() else ROOT / args.output
    data = SkillInput.model_validate_json(source.read_text(encoding="utf-8"))
    if args.department_id not in data.scope.department_ids:
        parser.error("--department-id is outside the input scope")
    print(f"S04 input valid: {len(data.records)} records, "
          f"{data.scope.start_date} to {data.scope.end_date}", flush=True)
    if args.validate_only:
        return 0

    load_env_file(ROOT / ".env")
    settings = AppSettings.from_env()
    if not settings.deepseek.api_key:
        parser.error("DEEPSEEK_API_KEY is missing")
    client = DeepSeekClient(settings.deepseek,
                            UrllibTransport(settings.http_timeout_seconds))
    runner = SkillRunner(PromptService(client,
                                        max_attempts=settings.llm_max_attempts))
    previous_directory = Path.cwd()
    try:
        os.chdir(ROOT)  # SkillRunner resolves prompts/S04.txt from cwd.
        result = runner.run("S04", data, department_id=args.department_id,
                            user_id=args.user_id)
    finally:
        os.chdir(previous_directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps({"status": result.status, "items": len(result.content.items)
                      if result.content else 0, "output": str(target)},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
