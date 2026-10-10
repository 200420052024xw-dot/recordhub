"""Resolve mobile numbers to open_ids under the app currently configured in .env.

open_id is scoped per (user, app): an ou_xxx captured from another app will be
rejected with "open_id cross app". This script asks the *current* app's contact
API for the right open_id, matching data/repositories.py's production flow.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import AppSettings, load_env_file
from tool.feishu import ContactService, FeishuClient
from tool.http import UrllibTransport


def main() -> int:
    load_env_file(ROOT / ".env")
    settings = AppSettings.from_env()
    mobiles = [value.strip() for value in sys.argv[1:] if value.strip()]
    if not mobiles:
        print("用法: python scripts/lookup_open_id_by_mobile.py <手机号> [...]")
        print("      手机号可带 +86 前缀；不带时按国内号码解析。")
        return 1
    print(f"app_id={settings.feishu.app_id}")
    feishu = FeishuClient(settings.feishu,
                          UrllibTransport(settings.http_timeout_seconds))
    contacts = ContactService(feishu)
    found = 0
    for user in contacts.batch_get_ids(mobiles=mobiles, user_id_type="open_id"):
        open_id = str(user.get("user_id") or "").strip()
        if open_id:
            found += 1
            print(f"mobile={user.get('mobile')}  ->  open_id={open_id}")
        else:
            print(f"mobile={user.get('mobile')}  ->  无 user_id（可能不在应用可用范围）: {user}")
    if found < len(mobiles):
        print("提示：查不到的号码请确认已加 +86、且该用户在应用可用范围内。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
