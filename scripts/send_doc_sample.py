"""一次性工具：在归档目录下生成「新格式样例」三级云文档，并把链接直发指定飞书账号。

用法：python scripts/send_doc_sample.py [收件人姓名，默认 王金丰]
样例数据全部为演示文本，不读写人员表/日志表，也不产生任何评价记录。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from config import AppSettings, load_env_file
from tool.cloud_docs import divider_block, text_block
from tool.feishu import MessageService
from tool.infrastructure import build_infrastructure

FOLDER_NAME = "样例-云文档新格式"
DATE = "10-04"


def person_blocks(name: str, dept: str, role: str, backbone: str,
                  time_line: str, contents: list[str],
                  evaluation: list[str]) -> list[dict]:
    blocks = [
        text_block(f"{name}的日志", heading=3),
        text_block(dept),
        text_block(role),
        text_block(backbone),
        text_block(time_line),
        text_block("日志内容", heading=4),
    ]
    blocks.extend(text_block(chunk) for chunk in contents)
    blocks.append(text_block("评价", heading=4))
    blocks.extend(text_block(line) for line in evaluation)
    return blocks


def group_blocks(link: str) -> list[dict]:
    blocks = [text_block(f"{DATE} 王金丰同学小组日志（样例）", heading=1)]
    blocks.extend(person_blocks(
        "王金丰", "-所属部门：001·宣传部", "-角色：骨干学生", "-是否为骨干：是",
        "-时间：2026年10月4日",
        ["工作进展：完成新平台原型第二版，重构登录与首页交互\n工作困难：登录页动效调不干净，切换有卡顿\n心得反思：原型先求对再求美，动效问题记入待办\n其他：无"],
        ["-评价人：尚培贤，部长",
         "-评价时间：2026年10月5日 10:20",
         "-肯定之处：迭代节奏快，能主动暴露技术卡点并给出备选方案",
         "-改进之处：动效问题建议周五例会前与刘新瀚结对解决，避免独自耗时"]))
    blocks.append(divider_block())
    blocks.extend(person_blocks(
        "杨黍晨", "-所属部门：001·宣传部", "-角色：基层学生", "-是否为骨干：否",
        "-时间：2026年10月4日",
        ["工作进展：完成社团招新海报初稿三版，收齐两名干事反馈\n工作困难：配色在打印偏色，打样时间不够\n心得反思：先定风格再做变体，返工更少\n其他：无"],
        ["-评价人：王金丰，骨干学生",
         "-评价时间：2026年10月5日 09:40",
         "-肯定之处：初稿即给出三版对比，评审效率高",
         "-改进之处：打样请提前一天预约，避免截止前赶工"]))
    blocks.append(divider_block())
    blocks.extend(person_blocks(
        "曹达荣", "-所属部门：001·宣传部", "-角色：基层学生", "-是否为骨干：否",
        "-时间：2026年10月4日",
        ["未填写日志"],
        ["未评价"]))
    return blocks


def department_blocks(group_title: str, group_link: str) -> list[dict]:
    return [
        text_block(f"{DATE} 宣传部日志（样例）", heading=1),
        text_block("王金丰同学", heading=3),
        text_block("该小组提交情况"),
        text_block("-应交人数：3"),
        text_block("-已交人数：2"),
        text_block("-未交人数：1（曹达荣未交）"),
        text_block(f"【{group_title}】", link=group_link),
    ]


def team_blocks(dept_title: str, dept_link: str) -> list[dict]:
    return [
        text_block(f"{DATE} 团队日志（样例）", heading=1),
        text_block("提交情况"),
        text_block("-应交人数：12"),
        text_block("-已交人数：11"),
        text_block("-未交人数：1"),
        divider_block(),
        text_block("尚培贤老师部门：", heading=3),
        text_block("部门提交情况"),
        text_block("-应交人数：5"),
        text_block("-已交人数：4"),
        text_block("-未交人数：1"),
        text_block(f"【{dept_title}】", link=dept_link),
    ]


def main() -> int:
    recipient_name = sys.argv[1] if len(sys.argv) > 1 else "王金丰"
    load_env_file()
    settings = AppSettings.from_env()
    infra = build_infrastructure(settings)
    docs = infra.cloud_docs
    if not settings.archive_parent_folder_token:
        raise SystemExit("未配置 RECORDHUB_ARCHIVE_PARENT_FOLDER_TOKEN")
    folder = (docs.find_child(settings.archive_parent_folder_token, FOLDER_NAME, "folder")
              or docs.create_folder(settings.archive_parent_folder_token, FOLDER_NAME))
    cache = json.loads((ROOT / "data/state/cache/organization.json").read_text(encoding="utf-8"))
    persons = cache["payload"]["persons"]
    recipient = next((p for p in persons if p["name"] == recipient_name), None)
    if recipient is None or not recipient.get("open_id"):
        raise SystemExit(f"人员缓存中找不到「{recipient_name}」的 OpenID")
    # 直接发真人，绕过 RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID 联调转发。
    messages = MessageService(docs.client)

    group_title = f"{DATE} 王金丰同学小组日志（样例）"
    dept_title = f"{DATE} 宣传部日志（样例）"
    team_title = f"{DATE} 团队日志（样例）"

    group_token = docs.find_child(folder, group_title, "docx") or docs.create_document(folder, group_title)
    docs.append_blocks(group_token, group_blocks("unused"))
    dept_token = docs.find_child(folder, dept_title, "docx") or docs.create_document(folder, dept_title)
    docs.append_blocks(dept_token, department_blocks(group_title, f"https://feishu.cn/docx/{group_token}"))
    team_token = docs.find_child(folder, team_title, "docx") or docs.create_document(folder, team_title)
    docs.append_blocks(team_token, team_blocks(dept_title, f"https://feishu.cn/docx/{dept_token}"))

    links = "\n".join(
        f"https://feishu.cn/docx/{token}" for token in (team_token, dept_token, group_token))
    messages.send_text(
        recipient["open_id"],
        f"【云文档格式样例】新格式已实现，三级样例文档已生成（团队→部门→小组逐级引用）：\n{links}\n"
        "文档在飞书「" + FOLDER_NAME + "」文件夹内，可直接查看排版。",
    )
    print(json.dumps({"folder": folder, "team": team_token, "department": dept_token,
                      "group": group_token, "sent_to": recipient_name},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
