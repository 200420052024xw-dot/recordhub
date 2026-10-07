"""Generate 100 synthetic work logs over 5 days for workflow testing.

Generates 20 logs/day for the past 5 days so every Workflow1 day catches a
full set, while Workflow2 S04/S05/S08 still see overlapping slices. Marks
each generated record with a [模拟测试] prefix so the cleanup script can find
them later, and saves the created record_ids to data/state/simulation_manifest.json.

Usage:
    python scripts/generate_simulation_logs.py [--days 5] [--per-day 20]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file, load_table_config
from tool.bitable_fields import references, record_fields, scalar
from tool.feishu import BitableService, FeishuClient
from tool.http import UrllibTransport

SHANGHAI = ZoneInfo("Asia/Shanghai")
MANIFEST_PATH = ROOT / "data" / "state" / "simulation_manifest.json"
PREFIX = "[模拟测试]"

SAMPLE_PROGRESS = [
    "梳理本周剩余工作项并按优先级整理成清单，与组员确认各自拆分的目标。",
    "完成 {topic} 文档初稿，针对三处模糊措辞做了细化并附上代码片段。",
    "梳理 {topic} 流程中五个常见失败模式，更新组内 Wiki 的排查页。",
    "重写 {topic} 接口的边界用例，与 Friday 提交的版本合并后回归通过。",
    "将 {topic} 工具封装为可复用脚本，提交了配套 README 与最小测试。",
    "完成 {topic} 实验数据复盘，补齐缺失的指标定义并标注后续改进方向。",
    "整理 {topic} 培训材料初稿，配合一份现场提问清单用于例会分享。",
    "协助组员 {topic} 工作，跟踪进度并整理了一份风险与依赖清单。",
    "复盘 {topic} 项目阶段产出，更新导出需要重点关注的两个事项。",
    "草拟 {topic} 后续计划，包含三项可执行任务和对应负责人。",
]

SAMPLE_DIFFICULTIES = [
    "对接口返回字段含义不清晰，反复询问后才确认边界条件。",
    "复现某个偶发问题时缺少必要的本地日志，需要重新走查调用链。",
    "同事本周状态紧张，部分沟通被推迟，影响了预期节奏。",
    "工具环境偶发卡顿，需要重启并清理临时文件，影响约一小时。",
    "需求侧未敲定上下文优先级，需要追加一次确认会议。",
    "资料分散在多份飞书文档中，整理花的时间超过预期。",
    "对 {topic} 的旧实现不够熟悉，需要先读历史 PR 才能动。",
    "当日两次短会议间隔太短，没能完成原本要做的进度同步。",
]

SAMPLE_REFLECTION = [
    "应在每天开始前列出最重要两项，否则容易被即时消息打断节奏。",
    "今日发现书面约定容易遗漏，需要把约定落到 Wiki 而非口头。",
    "提前估时的颗粒度可以再细一些，便于周五复盘逐项核实。",
    "对 {topic} 的依赖关系没有先画图，导致中途返工。",
    "应当把阻断性问题单独标记，而不是和长期语音混着排。",
    "提前对齐沟通频率可以减少无效消息，集中处理重要议题。",
]

SAMPLE_OTHER = [
    "已预约明天上午再与 {topic} 负责人同步。",
    "周五例会前会汇总本周共性结论。",
    "下周计划补一篇 {topic} 的工作小结。",
    "明日请假半天，工作已交接给值班同学。",
    "继续观察两天再决定是否需要额外评审。",
]


def _load_organization(bitable: BitableService,
                       tables) -> tuple[list[dict], list[dict]]:
    persons_records = bitable.list_records(tables["persons"].table_id)
    departments_records = bitable.list_records(tables["departments"].table_id)
    return persons_records, departments_records


def _is_active_basic(values: dict) -> bool:
    role = str(values.get("角色", "")).strip()
    return role == "基层学生"


def _is_active_non_basic(values: dict) -> bool:
    role = str(values.get("角色", "")).strip()
    return role in {"骨干学生", "部长", "团队负责人"}


def _person_id_from_record(record: dict, config_fields) -> str:
    values = record_fields(record)
    return scalar(values.get(config_fields["person_id"])).strip() or str(record.get("record_id", ""))


def _name_from_record(record: dict, config_fields) -> str:
    values = record_fields(record)
    return scalar(values.get(config_fields["name"])).strip()


def _submitter_ref(person_record: dict, person_fields: dict) -> dict:
    """Best-effort OpenID of a 骨干/部长 person, falling back to name lookup."""
    values = record_fields(person_record)
    mobile = scalar(values.get(person_fields["mobile"])).strip()
    role = str(values.get(person_fields["role"], "")).strip()
    submitter_raw = values.get("submitter")
    if isinstance(submitter_raw, dict) and submitter_raw.get("id"):
        return {"id": str(submitter_raw["id"])}
    # Feishu person reference in persons table resolves at log-creation
    # time via a PersonReference type field (here we cannot read it without
    # a separate query). The "姓名：" field alone is enough for matching,
    # so return an empty ref and rely on the name.
    return {}


def _build_log_fields(person: dict, person_record: dict, person_fields: dict,
                      log_fields: dict, target_date: date, log_index: int,
                      total_for_day: int, timestamp_ms: int) -> dict:
    name = _name_from_record(person_record, person_fields)
    basic = _is_active_basic(person.get("fields", {}))
    role_label = person.get("fields", {}).get("角色", "")
    topics = ["S01 skill 改造", "事件上报通路", "Wiki 整理", "值班例行", "AI 评价抽样",
              "云文档归档", "Skill 复盘", "日报优化", "新人 Onboarding", "评审材料"]
    topic = random.choice(topics)
    progress = random.choice(SAMPLE_PROGRESS).format(topic=topic)
    difficulties = random.choice(SAMPLE_DIFFICULTIES).format(topic=topic)
    reflection = random.choice(SAMPLE_REFLECTION).format(topic=topic)
    other = random.choice(SAMPLE_OTHER).format(topic=topic)
    full_log = (
        f"工作进展：{PREFIX} {progress}\n"
        f"工作困难：{PREFIX} {difficulties}\n"
        f"心得反思：{PREFIX} {reflection}\n"
        f"其他：{PREFIX} {other}"
    )
    fields = {
        "提交时间": timestamp_ms,
        "工作进展：": PREFIX + " " + progress,
        "工作困难：": PREFIX + " " + difficulties,
        "心得反思：": PREFIX + " " + reflection,
        "其他：": PREFIX + " " + other,
        "完整日志": full_log,
    }
    if basic:
        fields["姓名："] = name
    else:
        ref = _submitter_ref(person_record, person_fields)
        if ref.get("id"):
            fields["提交人"] = ref
        fields["姓名："] = name
    return fields


def _resolve_log_dates(days: int, per_day: int) -> list[date]:
    today = datetime.now(SHANGHAI).date()
    end = today - timedelta(days=1) if today.weekday() >= 5 else today
    # Pick the 5 most recent business days excluding today and weekends.
    chosen: list[date] = []
    cursor = end
    while len(chosen) < days:
        if cursor.weekday() < 5:
            chosen.append(cursor)
        cursor = cursor - timedelta(days=1)
    chosen.reverse()
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--per-day", type=int, default=20)
    parser.add_argument("--start-date", type=date.fromisoformat, default=None,
                        help="可选：覆盖默认日期窗口,使用起始日期倒数 days 天")
    parser.add_argument("--seed", type=int, default=20261008)
    parser.add_argument("--reset", action="store_true",
                        help="删除既有模拟日志后再生成")
    args = parser.parse_args()
    random.seed(args.seed)

    load_env_file()
    app = AppSettings.from_env()
    tables_cfg = load_table_config(app.table_config_path)
    client = FeishuClient(app.feishu, UrllibTransport(app.http_timeout_seconds))
    bitable = BitableService(client, app.feishu.bitable_app_token)

    person_fields = tables_cfg.tables["persons"].fields
    log_fields = tables_cfg.tables["logs"].fields

    persons_records, _ = _load_organization(bitable, tables_cfg.tables)
    basic_records = [r for r in persons_records if _is_active_basic(r.get("fields", {}))]
    other_records = [r for r in persons_records if _is_active_non_basic(r.get("fields", {}))]
    if not basic_records and not other_records:
        print("No active persons found in the Feishu persons table.")
        return 1
    print(f"Available basic={len(basic_records)}, others={len(other_records)}",
          flush=True)

    if args.start_date is not None:
        end_date = args.start_date
        days = [end_date - timedelta(days=offset) for offset in range(args.days - 1, -1, -1)]
    else:
        days = _resolve_log_dates(args.days, args.per_day)

    if args.reset and MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {"records": [], "documents": []}
        existing_ids = [r.get("record_id", "") for r in manifest.get("records", [])]
        existing_ids = [rid for rid in existing_ids if rid]
        if existing_ids:
            print(f"Deleting {len(existing_ids)} previously generated logs...")
            for offset in range(0, len(existing_ids), 500):
                client.request(
                    "POST",
                    f"bitable/v1/apps/{app.feishu.bitable_app_token}"
                    f"/tables/{tables_cfg.tables['logs'].table_id}/records/batch_delete",
                    json_body={"records": [{"record_id": rid}
                                          for rid in existing_ids[offset:offset + 500]]},
                )
        MANIFEST_PATH.unlink(missing_ok=True)
        print("Reset complete.", flush=True)

    total = args.days * args.per_day
    print(f"Will generate {total} logs across {args.days} days:", flush=True)
    for day in days:
        print(f"  {day.isoformat()} ({day.strftime('%A')}): 20 logs", flush=True)

    created_ids: list[str] = []
    manifest_records: list[dict] = []
    for day_index, day in enumerate(days):
        # Rotate through people, but ensure each day uses some new faces.
        pool = (basic_records + other_records) * 4
        random.shuffle(pool)
        chosen = pool[:args.per_day]
        for log_index, person_record in enumerate(chosen):
            submitter = _is_active_basic(person_record.get("fields", {}))
            hour = 18 + (log_index % 5)
            minute = (log_index * 7 + day_index * 13) % 60
            submitted = datetime.combine(day, time(hour, minute), SHANGHAI)
            timestamp_ms = int(submitted.timestamp() * 1000)
            fields = _build_log_fields(
                person=person_record, person_record=person_record,
                person_fields=person_fields, log_fields=log_fields,
                target_date=day, log_index=log_index,
                total_for_day=args.per_day, timestamp_ms=timestamp_ms)
            created = bitable.create_record(
                tables_cfg.tables["logs"].table_id, fields)
            created_ids.append(str(created.get("record_id", "")))
            manifest_records.append({
                "record_id": str(created.get("record_id", "")),
                "date": day.isoformat(),
                "person_id": _person_id_from_record(person_record, person_fields),
                "is_basic": submitter,
            })
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(SHANGHAI).isoformat(),
        "days": [d.isoformat() for d in days],
        "per_day": args.per_day,
        "prefix": PREFIX,
        "records": manifest_records,
        "documents": [],
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"Inserted {len(created_ids)} logs. Manifest: {MANIFEST_PATH}",
          flush=True)
    print("Next: run python scripts/run_workflow_simulation.py", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())