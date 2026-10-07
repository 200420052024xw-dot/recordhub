"""Fill the Feishu persons + departments tables with complete simulated data.

Writes a realistic 3-department organization (1 team leader, 3 ministers,
6 backbones, 24 basic students) so every field in 人员表 / 部门表 is populated,
especially names. Person-type fields (部门部长 / 直属上级) point to the one
real Feishu user available in this app (resolved from their mobile) because
simulated people do not have real Feishu accounts.

Usage:
    python scripts/fill_organization_simulation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file, load_table_config
from tool.feishu import BitableService, FeishuClient
from tool.http import UrllibTransport

# The real user's open_id for THIS app (resolved from their mobile via
# contact/v3/users/batch_get_id). Used for every person-type reference field.
USER_OPEN_ID = "ou_d9f307c37aa2eacad4a202e217257db3"

DEPARTMENTS = [
    {"id": "D01", "name": "综合管理部"},
    {"id": "D02", "name": "技术研发部"},
    {"id": "D03", "name": "宣传外联部"},
]

# (person_id, name, role, department_id, leader_id, mobile)
PERSONS = [
    ("T01", "刘新瀚", "团队负责人", "", "", "18270337201"),
    ("M01", "王敏", "部长", "D01", "T01", "13800138001"),
    ("M02", "李强", "部长", "D02", "T01", "13800138002"),
    ("M03", "赵婷", "部长", "D03", "T01", "13800138003"),
    ("B01", "陈晨", "骨干学生", "D01", "M01", "13900139001"),
    ("B02", "林宇", "骨干学生", "D01", "M01", "13900139002"),
    ("B03", "黄蕾", "骨干学生", "D02", "M02", "13900139003"),
    ("B04", "周杰", "骨干学生", "D02", "M02", "13900139004"),
    ("B05", "吴桐", "骨干学生", "D03", "M03", "13900139005"),
    ("B06", "郑爽", "骨干学生", "D03", "M03", "13900139006"),
    ("S001", "张伟", "基层学生", "D01", "B01", ""),
    ("S002", "王芳", "基层学生", "D01", "B01", ""),
    ("S003", "李娜", "基层学生", "D01", "B01", ""),
    ("S004", "刘洋", "基层学生", "D01", "B01", ""),
    ("S005", "陈静", "基层学生", "D01", "B02", ""),
    ("S006", "杨帆", "基层学生", "D01", "B02", ""),
    ("S007", "赵磊", "基层学生", "D01", "B02", ""),
    ("S008", "黄蓉", "基层学生", "D01", "B02", ""),
    ("S009", "周涛", "基层学生", "D02", "B03", ""),
    ("S010", "吴静", "基层学生", "D02", "B03", ""),
    ("S011", "郑凯", "基层学生", "D02", "B03", ""),
    ("S012", "冯雪", "基层学生", "D02", "B03", ""),
    ("S013", "蒋毅", "基层学生", "D02", "B04", ""),
    ("S014", "韩梅", "基层学生", "D02", "B04", ""),
    ("S015", "曹阳", "基层学生", "D02", "B04", ""),
    ("S016", "邓超", "基层学生", "D02", "B04", ""),
    ("S017", "许晴", "基层学生", "D03", "B05", ""),
    ("S018", "何军", "基层学生", "D03", "B05", ""),
    ("S019", "罗琳", "基层学生", "D03", "B05", ""),
    ("S020", "高翔", "基层学生", "D03", "B05", ""),
    ("S021", "梁婷", "基层学生", "D03", "B06", ""),
    ("S022", "宋佳", "基层学生", "D03", "B06", ""),
    ("S023", "唐磊", "基层学生", "D03", "B06", ""),
    ("S024", "彭飞", "基层学生", "D03", "B06", ""),
]


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
            json_body={"records": [{"record_id": rid}
                                  for rid in ids[offset:offset + 500]]},
        )
    return len(ids)


def main() -> int:
    load_env_file()
    app = AppSettings.from_env()
    tables = load_table_config(app.table_config_path).tables
    client = FeishuClient(app.feishu, UrllibTransport(app.http_timeout_seconds))
    bitable = BitableService(client, app.feishu.bitable_app_token)

    departments_table = tables["departments"]
    persons_table = tables["persons"]
    dept_fields = departments_table.fields
    person_fields = persons_table.fields

    cleared_depts = _clear_table(bitable, app.feishu.bitable_app_token,
                                 departments_table.table_id)
    cleared_persons = _clear_table(bitable, app.feishu.bitable_app_token,
                                   persons_table.table_id)
    print(f"cleared departments={cleared_depts} persons={cleared_persons}",
          flush=True)

    created_depts = 0
    for department in DEPARTMENTS:
        bitable.create_record(departments_table.table_id, {
            dept_fields["department_id"]: department["id"],
            dept_fields["name"]: department["name"],
            dept_fields["minister_ref"]: [{"id": USER_OPEN_ID}],
        })
        created_depts += 1

    created_persons = 0
    for person_id, name, role, dept_id, leader_id, mobile in PERSONS:
        fields: dict = {
            person_fields["person_id"]: person_id,
            person_fields["name"]: name,
            person_fields["role"]: role,
            person_fields["remark"]: "模拟测试",
        }
        if dept_id:
            fields[person_fields["department_ref"]] = dept_id
        if mobile:
            fields[person_fields["mobile"]] = mobile
        if leader_id:
            # Simulated people have no real Feishu account; point the person
            # field at the one real user available in this app.
            fields[person_fields["leader_ref"]] = [{"id": USER_OPEN_ID}]
        bitable.create_record(persons_table.table_id, fields)
        created_persons += 1

    summary = {
        "departments_created": created_depts,
        "persons_created": created_persons,
        "user_open_id": USER_OPEN_ID,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
