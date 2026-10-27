#!/bin/bash
# 启动前确认端口没被占用，占用就直接失败退出。
#
# 为什么需要：uvicorn 的 [Errno 98] 是启动流程跑完才抛的，配上 Restart=always
# 会变成无限重启；而且每次重启都完整跑一遍组织刷新和飞书长连接。2026-10-15
# 就是这样空转了 10 小时 1984 次。这里提前一秒失败，并直接告诉你是谁占着。
#
# 用法（systemd）：ExecStartPre=/bin/bash /www/wwwroot/recordhub/scripts/recordhub-port-check.sh 8100
set -euo pipefail

port="${1:-8100}"

if ! [[ "$port" =~ ^[0-9]+$ ]] || (( 10#$port < 1 || 10#$port > 65535 )); then
    echo "无效端口：必须为 1-65535 的整数。" >&2
    exit 2
fi
if ! command -v ss >/dev/null 2>&1; then
    echo "缺少 ss，请先安装 iproute2（或 iproute）。" >&2
    exit 2
fi

# Capture first: a failed ss must not be mistaken for an available port.
listeners="$(ss -Hltn "sport = :${port}")"

if [[ -n "$listeners" ]]; then
    echo "端口 ${port} 已被占用，RecordHub 无法启动。当前占用情况：" >&2
    ss -lptn "sport = :${port}" >&2
    echo "如果是残留的旧进程，先确认它是不是真的在服务，再决定是否 kill。" >&2
    exit 1
fi

exit 0
