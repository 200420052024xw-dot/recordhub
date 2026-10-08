"""Fill 基层学生 (basic students) into the persons table.

基层学生不占飞书组织名额、没有手机号和 OpenID，因此不重灌负责人/部长/骨干，
只在人员表追加（幂等：先删除已存在的基层学生记录再写入）。

每条基层学生记录：
- 人员编号 = S001..S080（基层专用顺序编号，避免与骨干/部长「姓名当编号」冲突）
- 姓名 = 新生姓名
- 角色 = 基层学生
- 所属部门 = 部门名称
- 直属上级 = 分配的骨干（飞书人员字段，写骨干的 open_id）
- 手机号 = 空

分配：固定随机种子打散后按骨干轮转，保证每个骨干分到的人数尽量均匀、结果可复核。

Usage:
    python scripts/fill_basic_students_production.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "scripts")]

from config import AppSettings, load_env_file, load_table_config
from tool.bitable_fields import record_fields, scalar
from tool.feishu import BitableService, ContactService, FeishuClient
from tool.http import UrllibTransport
from fill_organization_production import BACKBONES, MOBILES

# 部门名称 -> 新生（基层）姓名列表。
# 瑶湖 22 人 + 青山湖 1 人 = 23 人 → 罗珍珍部门。
# 数产 59 人，剔除已是骨干的「江鑫鑫」「秦锦龙」后 57 人 → 廖云燕部门。
BASIC_STUDENTS = {
    "罗珍珍部门": [
        "陈玉瑾", "吴枭杰", "钟高畅", "费亦卓", "郑蔼鑫", "杨淑萍", "谢鑫",
        "朱咏欣", "胡可欣", "周子涵", "张善浩", "林水云", "王雅芝", "侯书煌",
        "张维", "廖锦城", "周勤彦", "胡可忻", "高渤林", "张宸硕", "姜禹铭",
        "杨远航", "张静怡",
    ],
    "廖云燕部门": [
        "周宇希", "黄俊能", "温碧珠", "余文卿", "金子鹏", "李雨桐", "李传东",
        "黄书凯", "刘义平", "万英锟", "王奥宸", "霍日阳", "张跃", "涂跃",
        "万佳欣", "全烨泽", "曹淑婷", "黄旭明", "刘杰恺", "唐嘉薇", "周博奥",
        "饶贝妮", "谢熳婕", "曾宇峰", "张凯轩", "徐嘉睿", "张宝根", "黄赞阳",
        "吴嘉骏", "周家喜", "付昕伟", "黎奥聪", "曾雅馨", "曾铭宇", "陈浩诚",
        "叶创意", "邹志博", "罗紫怡", "刘可欣", "林霞", "罗时超", "江申鹏",
        "李俊杰", "刘凌翔", "刘汉民", "曾嘉祥", "谢舒西", "俞胡辉", "毛盛",
        "戴一凡", "杨紫捷", "熊逸欢", "熊晨乐", "刘文萱", "倪炜", "段锦",
        "邓凯文",
    ],
}

# 固定随机种子：打散顺序可复现，重跑结果一致。
SEED = 20261008


def _clear_basic_students(
    bitable: BitableService, app_token: str, persons_table
) -> int:
    records = bitable.list_records(persons_table.table_id)
    ids = [
        str(record.get("record_id", ""))
        for record in records
        if record.get("record_id")
        and scalar(record_fields(record).get(persons_table.fields["role"])).strip()
        == "基层学生"
    ]
    if not ids:
        return 0
    for offset in range(0, len(ids), 500):
        bitable.client.request(
            "POST",
            f"bitable/v1/apps/{app_token}/tables/{persons_table.table_id}"
            "/records/batch_delete",
            json_body={"records": list(ids[offset:offset + 500])},
        )
    return len(ids)


def _resolve_backbone_open_ids(contacts: ContactService) -> dict[str, str]:
    needed = sorted({
        name
        for names in (BACKBONES["罗珍珍部门"], BACKBONES["廖云燕部门"])
        for name in names
    })
    mobiles = [MOBILES[name] for name in needed]
    resolved: dict[str, str] = {}
    for offset in range(0, len(mobiles), 50):
        for user in contacts.batch_get_ids(
            mobiles=mobiles[offset:offset + 50], user_id_type="open_id"
        ):
            mobile = str(user.get("mobile") or "").strip()
            open_id = str(user.get("user_id") or "").strip()
            if mobile and open_id:
                resolved[mobile] = open_id
    missing = [name for name in needed if MOBILES[name] not in resolved]
    if missing:
        raise SystemExit(
            "以下骨干的手机号无法换取 open_id：" + ", ".join(missing)
        )
    return {name: resolved[MOBILES[name]] for name in needed}


def _assign(backbones: list[str], students: list[str]) -> list[tuple[str, str]]:
    rng = random.Random(SEED)
    order = rng.sample(students, len(students))
    return [(student, backbones[index % len(backbones)])
            for index, student in enumerate(order)]


def main() -> int:
    load_env_file()
    app = AppSettings.from_env()
    tables = load_table_config(app.table_config_path).tables
    client = FeishuClient(app.feishu, UrllibTransport(app.http_timeout_seconds))
    bitable = BitableService(client, app.feishu.bitable_app_token)
    contacts = ContactService(client)

    persons_table = tables["persons"]
    fields = persons_table.fields

    open_ids = _resolve_backbone_open_ids(contacts)
    cleared = _clear_basic_students(
        bitable, app.feishu.bitable_app_token, persons_table
    )
    print(f"cleared existing basic students={cleared}", flush=True)

    assignments: dict[str, list[tuple[str, str]]] = {}
    records: list[dict] = []
    seq = 0
    for department, students in BASIC_STUDENTS.items():
        pairs = _assign(BACKBONES[department], students)
        assignments[department] = pairs
        for student, backbone in pairs:
            seq += 1
            records.append({
                fields["person_id"]: f"S{seq:03d}",
                fields["name"]: student,
                fields["role"]: "基层学生",
                fields["department_ref"]: department,
                fields["leader_ref"]: [{"id": open_ids[backbone]}],
            })

    created = 0
    for offset in range(0, len(records), 50):
        chunk = records[offset:offset + 50]
        bitable.batch_create(persons_table.table_id, chunk)
        created += len(chunk)

    summary = {
        "basic_students_created": created,
        "departments": {
            department: {
                "count": len(students),
                "backbones": BACKBONES[department],
                "assignments": [
                    {"student": student, "backbone": backbone}
                    for student, backbone in pairs
                ],
            }
            for department, (students, pairs) in (
                (department, (BASIC_STUDENTS[department], pairs))
                for department, pairs in assignments.items()
            )
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
