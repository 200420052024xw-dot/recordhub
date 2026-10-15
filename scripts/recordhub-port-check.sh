#!/bin/bash
# 启动前确认端口没被占用，占用就直接失败退出。
#
# 为什么需要：uvicorn 的 [Errno 98] 是启动流程跑完才抛的，配上 Restart=always
# 会变成无限重启；而且每次重启都完整跑一遍组织刷新和飞书长连接。2026-10-15
# 就是这样空转了 10 小时 1984 次。这里提前一秒失败，并直接告诉你是谁占着。
#
# 用法（systemd）：ExecStartPre=/root/recordhub/scripts/recordhub-port-check.sh 8000
set -u

port="${1:-8000}"

if ss -Hlptn "sport = :${port}" | grep -q .; then
    echo "端口 ${port} 已被占用，RecordHub 无法启动。当前占用情况：" >&2
    ss -lptn "sport = :${port}" >&2
    echo "如果是残留的旧进程，先确认它是不是真的在服务，再决定是否 kill。" >&2
    exit 1
fi

exit 0
