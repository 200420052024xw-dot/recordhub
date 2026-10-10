"""Send N test messages through the simulation override to verify routing.

Mirrors service/runtime.py's MessageService construction: the override only
applies when RECORDHUB_SIMULATION_MODE is true. Each send uses a decoy
receive_id to prove the HTTP payload is rewritten to the override open_id.
"""
from __future__ import annotations

import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import AppSettings, load_env_file
from tool.feishu import FeishuClient, MessageService
from tool.http import UrllibTransport


def main() -> int:
    load_env_file(ROOT / ".env")
    settings = AppSettings.from_env()
    print(f"simulation_mode={settings.simulation_mode} "
          f"override={settings.message_override_open_id!r}")
    if not settings.simulation_mode or not settings.message_override_open_id.strip():
        print("未开启模拟模式或 override 为空，消息不会被重定向；终止。")
        return 1
    feishu = FeishuClient(settings.feishu,
                          UrllibTransport(settings.http_timeout_seconds))
    messages = MessageService(
        feishu,
        recipient_override=(settings.message_override_open_id
                            if settings.simulation_mode else ""),
    )
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    ok = 0
    for index in range(1, count + 1):
        try:
            result = messages.send_text(
                "ou_DECOY_should_never_receive",
                f"【模拟测试】第 {index}/{count} 条：验证 override 路由",
            )
            print(f"{index}/{count} 已发送 message_id={result.get('message_id')}")
            ok += 1
        except Exception as exc:
            print(f"{index}/{count} 发送失败: {exc}")
    if ok == count:
        print(f"完成：{ok}/{count} 条发送成功，全部落在 {settings.message_override_open_id}")
    else:
        print(f"完成：{ok}/{count} 条发送成功，{count - ok} 条失败")
    return 0 if ok == count else 2


if __name__ == "__main__":
    raise SystemExit(main())
