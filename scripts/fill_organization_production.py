"""Fill the Feishu persons + departments tables with the real roster.

Source: 江西师范大学 AI创新社-通讯录.xlsx (Feishu 通讯录导出模板).

Rules applied (per 社团负责人):
- 顶层「江西师范大学 AI创新社」中的黄箐是团队负责人（系统里最大的）；
  雷斌、胡海森 不进人员表/部门表；刘新瀚 为罗珍珍部门骨干，王金丰 为程着部门骨干。
- 每个「xxx部门」的 xxx 是部长，其余成员是骨干学生。
- 编号直接用中文名字：人员编号 = 姓名，部门编号 = 部门名称。
- 直属上级：部长 → 黄箐；骨干 → 本部部长。
- 「部门部长」「直属上级」是飞书人员字段，运行时用手机号经
  contact/v3/users/batch_get_id 换成真实 open_id 再写入。

Usage:
    python scripts/fill_organization_production.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file, load_table_config
from tool.feishu import BitableService, ContactService, FeishuClient
from tool.http import UrllibTransport

# 部门名称 -> 部长姓名（按 Excel 顺序）。
DEPARTMENTS = [
    ("廖云燕部门", "廖云燕"),
    ("徐高翔部门", "徐高翔"),
    ("李梦成部门", "李梦成"),
    ("王韶晨部门", "王韶晨"),
    ("程着部门", "程着"),
    ("罗勇部门", "罗勇"),
    ("罗珍珍部门", "罗珍珍"),
    ("胡启敏部门", "胡启敏"),
    ("陈任滔部门", "陈任滔"),
    ("韩林峄部门", "韩林峄"),
    ("黄美春部门", "黄美春"),
    ("杨阳蕊部门", "杨阳蕊"),
]

# 姓名 -> 手机号（全部 41 人；姓名即人员编号，必须唯一）。
MOBILES = {
    "黄箐": "+8613970994105",
    "廖云燕": "+8618970827031",
    "徐高翔": "+8618607912668",
    "李梦成": "+8618210886571",
    "王韶晨": "+8617805851330",
    "程着": "+8618579186989",
    "罗勇": "+8613870089977",
    "罗珍珍": "+8613755627317",
    "胡启敏": "+8613979103118",
    "陈任滔": "+8618370346957",
    "韩林峄": "+8615587120667",
    "黄美春": "+8615717038612",
    "杨阳蕊": "+8613837176209",
    "徐婉婷2": "+8619170076567",
    "欧阳苗": "+8615387960483",
    "黄特来": "+8615970940520",
    "伍毅": "+8619170677781",
    "江鑫鑫": "+8615179205018",
    "陈嘉玲": "+8615580960738",
    "艾康美": "+8619065027044",
    "赵婉莹": "+8617838608542",
    "陈诺": "+8613397940169",
    "樊然": "+8615170073289",
    "刘旭冉": "+8615713389220",
    "汪思梦": "+8618078446313",
    "徐婉婷1": "+8619297914142",
    "万斌华": "+8619880662576",
    "练瀚文": "+8619179322498",
    "邬明筠": "+8613755608799",
    "宋仁浩": "+8618170909952",
    "肖玉麟": "+8618573071275",
    "吴贻顺": "+8613184575014",
    "许圣斌": "+8613766368097",
    "杨天宇": "+8615870637343",
    "周文": "+8618179264655",
    "汪卓": "+8619970684354",
    "王严志": "+8614755771080",
    "王治贤": "+8618270320715",
    "刘新瀚": "+8618270337201",
    "王金丰": "+8618579160637",
    "郑宇浩": "+8615879799667",
}

# 部门名称 -> 骨干姓名列表（Excel 中除部长外的其余成员）。
BACKBONES = {
    "廖云燕部门": ["徐婉婷2", "欧阳苗", "黄特来", "伍毅", "江鑫鑫",
                "陈嘉玲", "艾康美", "赵婉莹"],
    "徐高翔部门": ["陈诺", "樊然", "刘旭冉", "汪思梦"],
    "李梦成部门": ["徐婉婷1"],
    "王韶晨部门": ["万斌华", "练瀚文"],
    "程着部门": ["邬明筠", "宋仁浩", "吴贻顺", "许圣斌", "王金丰"],
    "罗勇部门": [],
    "罗珍珍部门": ["刘新瀚"],
    "胡启敏部门": [],
    "陈任滔部门": ["周文", "汪卓"],
    "韩林峄部门": [],
    "黄美春部门": ["王严志"],
    "杨阳蕊部门": ["杨天宇", "肖玉麟", "王治贤", "郑宇浩"],
}

TEAM_LEADER = "黄箐"


def _clear_table(bitable: BitableService, app_token: str, table_id: str) -> int:
    records = bitable.list_records(table_id)
    ids = [str(record.get("record_id", "")) for record in records
           if record.get("record_id")]
    if not ids:
        return 0
    client = bitable.client
    for offset in range(0, len(ids), 500):
        client.request(
            "POST",
            f"bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
            json_body={"records": list(ids[offset:offset + 500])},
        )
    return len(ids)


def _resolve_open_ids(contacts: ContactService) -> dict[str, str]:
    names = list(MOBILES)
    mobiles = [MOBILES[name] for name in names]
    resolved: dict[str, str] = {}
    for offset in range(0, len(mobiles), 50):
        for user in contacts.batch_get_ids(
            mobiles=mobiles[offset:offset + 50], user_id_type="open_id"
        ):
            mobile = str(user.get("mobile") or "").strip()
            open_id = str(user.get("user_id") or "").strip()
            if mobile and open_id:
                resolved[mobile] = open_id
    missing = [name for name in names if MOBILES[name] not in resolved]
    if missing:
        raise SystemExit(
            "以下成员的手机号无法换取 open_id（可能不在应用可用范围内）："
            + ", ".join(missing)
        )
    return {name: resolved[MOBILES[name]] for name in names}


def main() -> int:
    load_env_file()
    app = AppSettings.from_env()
    tables = load_table_config(app.table_config_path).tables
    client = FeishuClient(app.feishu, UrllibTransport(app.http_timeout_seconds))
    bitable = BitableService(client, app.feishu.bitable_app_token)
    contacts = ContactService(client)

    departments_table = tables["departments"]
    persons_table = tables["persons"]
    dept_fields = departments_table.fields
    person_fields = persons_table.fields

    open_ids = _resolve_open_ids(contacts)

    cleared_depts = _clear_table(bitable, app.feishu.bitable_app_token,
                                 departments_table.table_id)
    cleared_persons = _clear_table(bitable, app.feishu.bitable_app_token,
                                   persons_table.table_id)
    print(f"cleared departments={cleared_depts} persons={cleared_persons}",
          flush=True)

    created_depts = 0
    for dept_name, minister in DEPARTMENTS:
        bitable.create_record(departments_table.table_id, {
            dept_fields["department_id"]: dept_name,
            dept_fields["name"]: dept_name,
            dept_fields["minister_ref"]: [{"id": open_ids[minister]}],
        })
        created_depts += 1

    created_persons = 0
    # 团队负责人：无部门、无直属上级。
    bitable.create_record(persons_table.table_id, {
        person_fields["person_id"]: TEAM_LEADER,
        person_fields["name"]: TEAM_LEADER,
        person_fields["role"]: "团队负责人",
        person_fields["mobile"]: MOBILES[TEAM_LEADER],
    })
    created_persons += 1

    # 部长：直属上级 = 团队负责人。
    for dept_name, minister in DEPARTMENTS:
        bitable.create_record(persons_table.table_id, {
            person_fields["person_id"]: minister,
            person_fields["name"]: minister,
            person_fields["role"]: "部长",
            person_fields["department_ref"]: dept_name,
            person_fields["leader_ref"]: [{"id": open_ids[TEAM_LEADER]}],
            person_fields["mobile"]: MOBILES[minister],
        })
        created_persons += 1

    # 骨干：直属上级 = 本部部长。
    for dept_name, minister in DEPARTMENTS:
        for name in BACKBONES[dept_name]:
            bitable.create_record(persons_table.table_id, {
                person_fields["person_id"]: name,
                person_fields["name"]: name,
                person_fields["role"]: "骨干学生",
                person_fields["department_ref"]: dept_name,
                person_fields["leader_ref"]: [{"id": open_ids[minister]}],
                person_fields["mobile"]: MOBILES[name],
            })
            created_persons += 1

    summary = {
        "departments_created": created_depts,
        "persons_created": created_persons,
        "team_leader": TEAM_LEADER,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
