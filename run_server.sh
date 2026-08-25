#!/usr/bin/env bash
# A股公告看板 - 前台运行脚本（调试/临时运行用）
# 用法：bash run_server.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ -d ".venv" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# HOST/PORT/AUTH_PASSWORD 可环境变量覆盖；默认监听所有网卡
export PYTHONIOENCODING=utf-8
# 如需密码：export AUTH_PASSWORD='你的密码'
cd mainboard_tool
exec python3 server.py
